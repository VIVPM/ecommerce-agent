"""The job worker — one process, N concurrent slots sized from queue depth."""
import asyncio
import json
import logging
import os
import signal
import socket
import time

from sqlalchemy import text

from langchain_core.callbacks import get_usage_metadata_callback

from app import jobs, llm_provider
from app.agent import is_off_topic
from app.memory_store import recall as memory_recall
from app.memory_store import remember as memory_remember
from app.sql import sql_chain_stream_async
from app.agent import astream_agent
from app.db.database import SessionLocal
from app.db.models import Message, now_ist
from app.logging_setup import job_context
from app.memory import optimize_query
from app.observability import (
    trace_message, set_output, set_usage, flush as trace_flush, record_message,
)

logger = logging.getLogger(__name__)

WORKER_ID = f"{socket.gethostname()}:{os.getpid()}"

POLL_INTERVAL_S = float(os.getenv("WORKER_POLL_INTERVAL_S", "1.0"))
REAP_INTERVAL_S = float(os.getenv("WORKER_REAP_INTERVAL_S", "60"))
FLUSH_CHARS = int(os.getenv("WORKER_FLUSH_CHARS", "300"))
FLUSH_INTERVAL_S = float(os.getenv("WORKER_FLUSH_INTERVAL_S", "0.4"))
HEARTBEAT_INTERVAL_S = float(os.getenv("WORKER_HEARTBEAT_INTERVAL_S", "10"))
JOB_SOFT_LIMIT_S = float(os.getenv("JOB_SOFT_LIMIT_S", "90"))
JOB_HARD_LIMIT_S = float(os.getenv("JOB_HARD_LIMIT_S", "240"))
SHUTDOWN_GRACE_S = float(os.getenv("WORKER_SHUTDOWN_GRACE_S", "45"))
MIN_CONCURRENCY = int(os.getenv("WORKER_MIN_CONCURRENCY", "1"))
MAX_CONCURRENCY = int(os.getenv("WORKER_MAX_CONCURRENCY", "3"))

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
    """Buffers tokens and appends them to the durable log in coalesced chunks."""

    def __init__(self, job_id: str, seq: int):
        self.job_id = job_id
        self._seq = seq
        self._emitted = False
        self._buf: list[str] = []
        self._chars = 0
        self._last = time.monotonic()
        self.text_parts: list[str] = []
        self.first_token_at: float | None = None
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
                    self._emitted = True
                    await asyncio.to_thread(jobs.mark_emitted, self.job_id)
            except Exception as e:
                logger.error("Could not append %s event for %s: %s", etype, self.job_id, e)

    def _put(self, etype: str, data: str):
        self._q.put_nowait((etype, data))

    def token(self, tok: str):
        """Buffer one streamed token and schedule a flush when the batch is ready."""
        if self.first_token_at is None:
            self.first_token_at = time.monotonic()
        self._buf.append(tok)
        self.text_parts.append(tok)
        self._chars += len(tok)
        if self._chars >= FLUSH_CHARS or (time.monotonic() - self._last) >= FLUSH_INTERVAL_S:
            self.flush()

    def flush(self):
        """Queue the buffered token batch as one durable job event."""
        if not self._buf:
            return
        chunk, self._buf, self._chars = "".join(self._buf), [], 0
        self._last = time.monotonic()
        self._put("token", chunk)

    def status(self, text_: str):
        self.flush()
        self._put("status", text_)

    def terminal(self, etype: str, data: str):
        self.flush()
        self._put(etype, data)

    async def close(self):
        """Wait for every queued event to be written."""
        self._q.put_nowait(None)
        await self._task


def _totals(usage: dict) -> tuple[int, int, int]:
    """Flatten the per-model usage the callback collected into one triple."""
    inp = out = cached = 0
    for u in (usage or {}).values():
        inp += u.get("input_tokens", 0) or 0
        out += u.get("output_tokens", 0) or 0
        cached += (u.get("input_token_details") or {}).get("cache_read", 0) or 0
    return inp, out, cached


async def _one(text: str):
    """A one-chunk stream, so a canned reply takes the same path as a real one."""
    yield {"token": text}


async def _image_stream(job):
    """Shop-by-photo, yielded as the same {status}/{token} chunks the agent
    produces so execute()'s loop — heartbeat, cancellation, shutdown — is
    untouched."""
    img_query = job.get("image_query") or ""
    if not img_query:
        yield {"token": ("I couldn't spot a shoe in that image. Try a clearer photo of "
                         "a single shoe, or just describe what you're looking for.")}
        return
    yield {"status": f"Looking for shoes like {img_query}...", "tool": "image_search"}
    yield {"token": f"Showing shoes similar to your image — **{img_query}**:\n\n"}
    async for tok in sql_chain_stream_async(f"{img_query} {job['query']}".strip()):
        if tok:
            yield {"token": tok}


async def execute(job, stop: asyncio.Event | None = None) -> None:
    """Run one job to completion, streaming into the durable event log."""
    job_id = job["id"]
    emitter = _Emitter(job_id, await asyncio.to_thread(jobs.next_seq, job_id))
    tool_label, status, error = "unknown", "succeeded", None
    last_beat = started = time.monotonic()
    released = False
    provider = llm_provider.active_provider()

    try:
        with get_usage_metadata_callback() as usage_cb, \
                trace_message(job["query"], job["user_id"], job["chat_id"]) as span:
            try:
                async with asyncio.timeout(JOB_HARD_LIMIT_S):
                    if job.get("image_query") is not None:
                        tool_label = "image_search"
                        stream = _image_stream(job)
                    elif await asyncio.to_thread(is_off_topic, job["query"]):
                        tool_label = "off_topic"
                        stream = _one(
                            "I'm a shopping assistant for our shoe store, so I can help you find "
                            "shoes, compare your saved items, answer store-policy questions, or "
                            "manage your cart and orders. Try me with something along those lines!")
                    else:
                        emitter.status("Understanding your query...")
                        optimized = await asyncio.to_thread(
                            optimize_query, job["query"], job["history"])
                        if optimized != job["query"]:
                            logger.info("Original Query: %s -> Optimized Query: %s",
                                        job["query"], optimized)

                        recalled = await asyncio.to_thread(
                            memory_recall, job["user_id"], optimized)
                        memory = (
                            f"What you remember about this shopper, which the "
                            f"current message may override: {recalled}" if recalled else
                            "You remember nothing about this shopper yet. If they ask "
                            "what you know about them, say so plainly -- never offer "
                            "to look it up or promise to fetch anything.")

                        emitter.status("Routing to the right tool...")
                        stream = astream_agent(
                            optimized, job["user_id"],
                            raw_query=job["query"], history=job["history"],
                            memory=memory)

                    async for chunk in stream:
                        if s := chunk.get("status"):
                            tool_label = chunk.get("tool", tool_label)
                            emitter.status(s)
                        if tok := chunk.get("token"):
                            emitter.token(tok)

                        if stop is not None and stop.is_set():
                            logger.info("Shutdown during job %s; releasing it", job_id)
                            released = True
                            break

                        if (time.monotonic() - last_beat) >= HEARTBEAT_INTERVAL_S:
                            last_beat = time.monotonic()
                            if (time.monotonic() - started) >= JOB_SOFT_LIMIT_S:
                                logger.warning("Job %s past the soft limit", job_id)
                            if not await asyncio.to_thread(jobs.heartbeat, job_id):
                                status = "cancelled"
                                break
            except TimeoutError:
                logger.warning("Job %s hit the hard limit of %.0fs", job_id, JOB_HARD_LIMIT_S)
                status, error = "failed", "This took too long and was stopped."

            emitter.flush()
            answer = "".join(emitter.text_parts)
            if status == "cancelled":
                error = "Cancelled."
            elif status == "succeeded":
                answer = answer or _NO_ANSWER
                set_output(span, answer)
            usage = _totals(usage_cb.usage_metadata)
            set_usage(span, provider=provider, tokens_in=usage[0], tokens_out=usage[1],
                      cached=usage[2],
                      cost_usd=llm_provider.estimate_cost_usd(provider, usage[0], usage[1]),
                      ttft_ms=(int((emitter.first_token_at - started) * 1000)
                               if emitter.first_token_at else None),
                      tool=tool_label)
    except Exception as e:
        logger.error("Job %s failed: %s", job_id, e, exc_info=True)
        status, error = "failed", "Something went wrong while processing your request."
        answer = "".join(emitter.text_parts)
        usage = (0, 0, 0)
    finally:
        trace_flush()
        record_message("ok" if status == "succeeded" else "error", tool_label)

    llm_provider.note_result(provider, status != "failed")

    ttft_ms = (int((emitter.first_token_at - started) * 1000)
               if emitter.first_token_at else None)
    cost = llm_provider.estimate_cost_usd(provider, usage[0], usage[1])

    await asyncio.to_thread(jobs.record_usage, job_id, *usage,
                            ttft_ms=ttft_ms, provider=provider)
    logger.info("Job %s provider=%s tokens in=%d out=%d cached=%d cost=$%.6f ttft=%sms tool=%s",
                job_id, provider, usage[0], usage[1], usage[2], cost, ttft_ms, tool_label)

    if released:
        await emitter.close()
        await asyncio.to_thread(jobs.release_job, job_id, bool(emitter.text_parts))
        return

    job["result"] = answer
    await asyncio.to_thread(jobs.finish_job, job_id, status,
                            result=answer if status == "succeeded" else None,
                            tool=tool_label, error=error)

    if status == "succeeded":
        try:
            await asyncio.to_thread(_save_assistant_message, job)
        except Exception as e:
            logger.error("Could not save answer for job %s: %s", job_id, e)

        if tool_label not in ("off_topic", "save_preference"):
            await asyncio.to_thread(
                memory_remember, job["user_id"], f"User asked: {job['query']}")

    emitter.terminal(
        "done" if status == "succeeded" else "error",
        json.dumps({"status": status, "tool": tool_label, "error": error,
                    "no_results": answer.startswith(("I couldn't find any products",
                                                     "I can't search by"))}),
    )
    await emitter.close()


async def _run_one(job, stop: asyncio.Event) -> None:
    with job_context(job["id"]):
        logger.info("Claimed job %s (attempt %s)", job["id"], job["attempts"])
        await execute(job, stop)


async def worker_loop(stop: asyncio.Event) -> None:
    """One worker PROCESS, N concurrent job slots, sized from queue depth."""
    logger.info("Worker %s starting (concurrency %d-%d)", WORKER_ID, MIN_CONCURRENCY, MAX_CONCURRENCY)
    running: set[asyncio.Task] = set()
    next_reap, last_target = 0.0, 0

    while not stop.is_set():
        try:
            for task in {t for t in running if t.done()}:
                running.discard(task)
                if (exc := task.exception()) is not None:
                    logger.error("Job task crashed: %s", exc, exc_info=exc)

            if time.monotonic() >= next_reap:
                next_reap = time.monotonic() + REAP_INTERVAL_S
                await asyncio.to_thread(jobs.reap_expired)

            if llm_provider.all_providers_open():
                logger.warning("All providers tripped; not claiming work")
                await asyncio.wait_for(stop.wait(), timeout=POLL_INTERVAL_S * 5)
                continue

            depth = await asyncio.to_thread(jobs.queue_depth)
            target = max(MIN_CONCURRENCY, min(MAX_CONCURRENCY, depth))
            if target != last_target and depth:
                logger.info("Queue depth %d -> %d concurrent slot(s)", depth, target)
                last_target = target

            claimed = 0
            while len(running) < target:
                job = await asyncio.to_thread(jobs.claim_job, WORKER_ID)
                if job is None:
                    break
                running.add(asyncio.create_task(_run_one(job, stop)))
                claimed += 1

            if not claimed and not running:
                await asyncio.wait_for(stop.wait(), timeout=POLL_INTERVAL_S)
            else:
                await asyncio.sleep(POLL_INTERVAL_S)
        except asyncio.TimeoutError:
            continue
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error("Worker loop error: %s", e, exc_info=True)
            await asyncio.sleep(POLL_INTERVAL_S)

    if running:
        logger.info("Draining %d in-flight job(s)", len(running))
        try:
            await asyncio.wait_for(asyncio.gather(*running, return_exceptions=True),
                                   timeout=SHUTDOWN_GRACE_S)
        except asyncio.TimeoutError:
            logger.warning("Drain exceeded %.0fs; the reaper will reclaim the rest",
                           SHUTDOWN_GRACE_S)
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
            signal.signal(sig, lambda *_: stop.set())
    loop.run_until_complete(worker_loop(stop))


if __name__ == "__main__":
    _main()
