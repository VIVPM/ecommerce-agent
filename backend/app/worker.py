"""The job worker (v0) — one consumer, no pool.

Runs the agent OUTSIDE the HTTP request that asked for it. The request now only
records a job; this loop executes it and appends events to `job_events`, so the
answer survives a closed tab, a dropped connection or a redeploy.

Deliberately ONE worker. Throughput is one job at a time, and two simultaneous
users means the second waits — that is the v0 trade, and the queue at least makes
the wait visible and survivable instead of hiding it behind a spinner. Worker
COUNT is a later problem (queue-depth autoscaling); do not grow a pool here.

Two entrypoints, same code:
  * in-process  — started from main.py's lifespan; what runs on a free tier that
                  has no separate worker instance
  * standalone  — `python -m app.worker`, for a real background worker service
Moving between them is a deploy change, not a rewrite.
"""
import asyncio
import json
import logging
import os
import signal
import socket
import time

from sqlalchemy import text

from app import jobs
from app.agent import astream_agent
from app.db.database import SessionLocal
from app.db.models import Message, now_ist
from app.memory import optimize_query
from app.observability import (
    trace_message, set_output, flush as trace_flush, record_message,
)

logger = logging.getLogger(__name__)

WORKER_ID = f"{socket.gethostname()}:{os.getpid()}"

POLL_INTERVAL_S = float(os.getenv("WORKER_POLL_INTERVAL_S", "1.0"))
REAP_INTERVAL_S = float(os.getenv("WORKER_REAP_INTERVAL_S", "60"))
# Token coalescing: one row per token would be ~300 inserts for a single
# comparison answer. Flush on whichever comes first — still reads as streaming.
FLUSH_CHARS = int(os.getenv("WORKER_FLUSH_CHARS", "300"))
FLUSH_INTERVAL_S = float(os.getenv("WORKER_FLUSH_INTERVAL_S", "0.4"))
# How often to extend the lease / check the cancel flag while streaming.
HEARTBEAT_INTERVAL_S = float(os.getenv("WORKER_HEARTBEAT_INTERVAL_S", "10"))

_NO_ANSWER = "I'm sorry, I couldn't generate a response."


def _save_assistant_message(job) -> None:
    """Persist the answer. The user's message was already saved at submit time,
    so a failed or cancelled job still leaves the question in the transcript."""
    db = SessionLocal()
    try:
        db.add(Message(chat_id=job["chat_id"], user_id=job["user_id"],
                       role="assistant", content=job["result"]))
        db.execute(text("UPDATE chat_sessions SET updated_at = :now WHERE id = :cid"),
                   {"now": now_ist(), "cid": job["chat_id"]})
        db.commit()
    finally:
        db.close()


class _Emitter:
    """Buffers tokens and appends them to the durable log in coalesced chunks.

    Writes go through an in-memory queue drained by a background task, NOT
    inline. Awaiting the insert in the token path made generation as slow as the
    database: a provider that emits a token every ~0.4s tripped the time-based
    flush on almost every token, and each one then blocked the stream on a Neon
    round-trip — one comparison answer took 364s across 324 one-token writes.
    Queueing decouples the two, so streaming runs at the model's pace.

    Also owns the seq counter. The worker is the only writer for a job, so the
    number is read once at job start and counted in memory from there.
    """

    def __init__(self, job_id: str, seq: int):
        self.job_id = job_id
        self._seq = seq
        self._emitted = False
        self._buf: list[str] = []
        self._chars = 0
        self._last = time.monotonic()
        self.text_parts: list[str] = []
        self._q: asyncio.Queue = asyncio.Queue()
        self._task = asyncio.create_task(self._drain())

    async def _drain(self):
        """Single consumer, so events land in the order they were produced."""
        while True:
            item = await self._q.get()
            if item is None:
                return
            etype, data = item
            self._seq += 1
            try:
                await asyncio.to_thread(jobs.append_event, self.job_id, self._seq, etype, data)
                if etype == "token" and not self._emitted:
                    self._emitted = True      # once per job, not once per chunk
                    await asyncio.to_thread(jobs.mark_emitted, self.job_id)
            except Exception as e:
                logger.error("Could not append %s event for %s: %s", etype, self.job_id, e)

    def _put(self, etype: str, data: str):
        self._q.put_nowait((etype, data))

    def token(self, tok: str):
        self._buf.append(tok)
        self.text_parts.append(tok)
        self._chars += len(tok)
        if self._chars >= FLUSH_CHARS or (time.monotonic() - self._last) >= FLUSH_INTERVAL_S:
            self.flush()

    def flush(self):
        if not self._buf:
            return
        chunk, self._buf, self._chars = "".join(self._buf), [], 0
        self._last = time.monotonic()
        self._put("token", chunk)

    def status(self, text_: str):
        self.flush()        # keep ordering: status never overtakes queued tokens
        self._put("status", text_)

    def terminal(self, etype: str, data: str):
        self.flush()
        self._put(etype, data)

    async def close(self):
        """Wait for every queued event to be written."""
        self._q.put_nowait(None)
        await self._task


async def execute(job) -> None:
    """Run one job to completion, streaming into the durable event log."""
    job_id = job["id"]
    emitter = _Emitter(job_id, await asyncio.to_thread(jobs.next_seq, job_id))
    tool_label, status, error = "unknown", "succeeded", None
    last_beat = time.monotonic()

    try:
        with trace_message(job["query"], job["user_id"], job["chat_id"]) as span:
            emitter.status("Understanding your query...")
            optimized = await asyncio.to_thread(optimize_query, job["query"], job["history"])
            if optimized != job["query"]:
                logger.info("Original Query: %s -> Optimized Query: %s", job["query"], optimized)

            emitter.status("Routing to the right tool...")

            async for chunk in astream_agent(optimized, job["user_id"]):
                if s := chunk.get("status"):
                    tool_label = chunk.get("tool", tool_label)
                    emitter.status(s)
                if tok := chunk.get("token"):
                    emitter.token(tok)

                # Cooperative cancellation: leaving the loop closes the agent's
                # async generator, which aborts the in-flight model call.
                if (time.monotonic() - last_beat) >= HEARTBEAT_INTERVAL_S:
                    last_beat = time.monotonic()
                    if not await asyncio.to_thread(jobs.heartbeat, job_id):
                        status = "cancelled"
                        break

            emitter.flush()
            answer = "".join(emitter.text_parts)
            if status == "cancelled":
                error = "Cancelled."
            else:
                answer = answer or _NO_ANSWER
                set_output(span, answer)
    except Exception as e:
        logger.error("Job %s failed: %s", job_id, e, exc_info=True)
        status, error = "failed", "Something went wrong while processing your request."
        answer = "".join(emitter.text_parts)
    finally:
        trace_flush()
        record_message("ok" if status == "succeeded" else "error", tool_label)

    job["result"] = answer
    await asyncio.to_thread(jobs.finish_job, job_id, status,
                            result=answer if status == "succeeded" else None,
                            tool=tool_label, error=error)

    if status == "succeeded":
        try:
            await asyncio.to_thread(_save_assistant_message, job)
        except Exception as e:
            logger.error("Could not save answer for job %s: %s", job_id, e)

    # The terminal event is what tells a tailing client to stop. `no_results`
    # keeps the client's follow-up chips working exactly as before.
    emitter.terminal(
        "done" if status == "succeeded" else "error",
        json.dumps({"status": status, "tool": tool_label, "error": error,
                    "no_results": answer.startswith(("I couldn't find any products",
                                                     "I can't search by"))}),
    )
    # Block until the queued writes have landed. A crash before this loses only
    # event rows, never the answer: `jobs.result` was already committed above.
    await emitter.close()


async def worker_loop(stop: asyncio.Event) -> None:
    logger.info("Worker %s starting", WORKER_ID)
    next_reap = 0.0
    while not stop.is_set():
        try:
            if time.monotonic() >= next_reap:
                next_reap = time.monotonic() + REAP_INTERVAL_S
                await asyncio.to_thread(jobs.reap_expired)

            job = await asyncio.to_thread(jobs.claim_job, WORKER_ID)
            if job is None:
                await asyncio.wait_for(stop.wait(), timeout=POLL_INTERVAL_S)
                continue
            logger.info("Claimed job %s (attempt %s)", job["id"], job["attempts"])
            await execute(job)
        except asyncio.TimeoutError:
            continue        # idle poll expired, loop again
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error("Worker loop error: %s", e, exc_info=True)
            await asyncio.sleep(POLL_INTERVAL_S)
    logger.info("Worker %s stopped", WORKER_ID)


def _main() -> None:
    from dotenv import load_dotenv
    from pathlib import Path
    load_dotenv(dotenv_path=Path(__file__).resolve().parent / ".env")

    stop = asyncio.Event()
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            signal.signal(sig, lambda *_: stop.set())   # Windows
    loop.run_until_complete(worker_loop(stop))


if __name__ == "__main__":
    _main()
