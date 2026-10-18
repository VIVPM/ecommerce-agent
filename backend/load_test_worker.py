"""Measure worker queue drain, claim contention and multi-process safety with a stub agent.

    python load_test_worker.py --render-suspended --jobs 40 --workers 2

Never run while Render (or another worker on this Neon DB) is active. No LLM
calls are made; test accounts, jobs and events are removed after the workers stop.
"""
import argparse
import asyncio
import json
import os
import signal
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / "app" / ".env")
sys.path.insert(0, str(ROOT))

from sqlalchemy import text  # noqa: E402

from app.db.database import SessionLocal, engine  # noqa: E402
from app.jobs import create_job  # noqa: E402
from load_test import _no_tracing, preflight, pctl  # noqa: E402
from load_test_k6 import cleanup, create_accounts, job_stats  # noqa: E402

OUT = ROOT / "load_test_results"


def worker_mode(seconds: float) -> None:
    """One real queue worker process with only model, recall and memory calls stubbed."""
    from app import worker

    def rewrite(query, history=None):
        return query

    async def agent(_query, _user_id=None, **_kwargs):
        await asyncio.sleep(seconds)
        yield {"status": "Searching products...", "tool": "search_product_database"}
        yield {"token": "Here are the results."}

    real_claim = worker.jobs.claim_job

    def timed_claim(worker_id):
        started = time.monotonic()
        job = real_claim(worker_id)
        if job:
            print(f"LT_CLAIM {time.time():.3f} {(time.monotonic() - started) * 1000:.1f}", flush=True)
        return job

    worker.optimize_query = rewrite
    worker.memory_recall = lambda *_args: ""
    worker.memory_remember = lambda *_args: None
    worker.is_off_topic = lambda *_args: False
    worker.astream_agent = agent
    worker.jobs.claim_job = timed_claim

    stop = asyncio.Event()
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            signal.signal(sig, lambda *_: loop.call_soon_threadsafe(stop.set))
    try:
        loop.run_until_complete(worker.worker_loop(stop))
    finally:
        loop.close()


def seed_jobs(users: list[dict], count: int) -> list[str]:
    """Queue count jobs among isolated test accounts in one transaction."""
    ids = []
    db = SessionLocal()
    try:
        for i in range(count):
            account = users[i % len(users)]
            ids.append(create_job(account["id"], account["chat"],
                                  "Nike shoes under Rs. 3000", [], db=db))
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
    return ids


def watch(ids: list[str], users: list[dict], timeout: float) -> dict:
    """Observe test jobs only; never count or alter anyone else's queue rows."""
    deadline = time.monotonic() + timeout
    peak = 0
    rows = []
    while time.monotonic() < deadline:
        with engine.connect() as db:
            rows = db.execute(text("""
                SELECT id, status, worker_id, attempts, error
                  FROM jobs WHERE id = ANY(:ids)
            """), {"ids": ids}).mappings().all()
        peak = max(peak, sum(row["status"] == "running" for row in rows))
        if len(rows) == len(ids) and all(row["status"] in ("succeeded", "failed", "cancelled")
                                         for row in rows):
            break
        time.sleep(0.5)
    stats = job_stats([u["id"] for u in users], datetime.fromtimestamp(0, timezone.utc))
    stats.update({"peak_running": peak,
                  "worker_ids": sorted({r["worker_id"] for r in rows if r["worker_id"]}),
                  "retried": sum((r["attempts"] or 0) > 1 for r in rows),
                  "errors": [r["error"] for r in rows if r["status"] == "failed"][:5]})
    return stats


def claim_stats(logs: list[Path], started: float) -> dict:
    """Boot-to-first-claim and the winning claim call's DB round trip per worker."""
    boot, durations = [], []
    for log in logs:
        claims = []
        for line in log.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.startswith("LT_CLAIM "):
                _, wall, ms = line.split()
                claims.append((float(wall), float(ms)))
        if claims:
            boot.append(round(claims[0][0] - started, 2))
            durations.extend(ms for _, ms in claims)
    return {"worker_boot_s": boot,
            "claim_ms": {k: round(pctl(durations, n), 1) if durations else None
                         for k, n in (("p50", 50), ("p95", 95), ("p99", 99))},
            "claims_won": len(durations)}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--render-suspended", action="store_true")
    parser.add_argument("--worker-mode", action="store_true")
    parser.add_argument("--jobs", type=int, default=40)
    parser.add_argument("--users", type=int, default=5)
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--slots", type=int, default=20, help="max concurrent jobs per worker")
    parser.add_argument("--msg-seconds", type=float, default=2.0)
    parser.add_argument("--timeout", type=float, default=300)
    parser.add_argument("--stagger", type=float, default=0,
                        help="seconds between worker starts, to expose claim/startup races")
    args = parser.parse_args()
    if not args.render_suspended:
        parser.error("worker load requires --render-suspended; the queue is shared with Render")
    if args.worker_mode:
        if os.getenv("LOADTEST_WORKER_CHILD") != "1":
            parser.error("--worker-mode is only started by a guarded load-test harness")
        return worker_mode(args.msg_seconds)
    if not 1 <= args.jobs <= 200 or not 1 <= args.users <= 200 or not 1 <= args.workers <= 3:
        parser.error("jobs/users must be 1–200 and workers 1–3")
    if not 1 <= args.slots <= 20 or not 0 < args.msg_seconds <= 60 or args.stagger < 0:
        parser.error("slots must be 1–20, message seconds 0–60, stagger >= 0")

    preflight()
    OUT.mkdir(exist_ok=True)
    tag = f"k6-{uuid.uuid4().hex[:8]}"
    accounts = create_accounts(args.users, tag)
    procs, logs, handles = [], [], []
    seeded_at = time.time()
    try:
        ids = seed_jobs(accounts, args.jobs)
        env = dict(os.environ, WORKER_MIN_CONCURRENCY="1",
                   WORKER_MAX_CONCURRENCY=str(args.slots), LOADTEST_WORKER_CHILD="1")
        _no_tracing(env)
        for n in range(args.workers):
            path = OUT / f"worker_{tag}_{n}.log"
            handle = path.open("w", encoding="utf-8")
            handles.append(handle)
            logs.append(path)
            procs.append(subprocess.Popen(
                [sys.executable, str(Path(__file__).resolve()), "--worker-mode",
                 "--render-suspended", "--msg-seconds", str(args.msg_seconds)],
                cwd=ROOT, env=env, stdout=handle, stderr=subprocess.STDOUT))
            if args.stagger and n + 1 < args.workers:
                time.sleep(args.stagger)
        started = time.monotonic()
        stats = watch(ids, accounts, args.timeout)
        stats["drain_s"] = round(time.monotonic() - started, 2)
        stats["throughput_jobs_min"] = round(60 * stats["succeeded"] / stats["drain_s"], 2)
        stats["pass"] = (stats["succeeded"] == args.jobs and stats["failed"] == 0
                         and stats["unfinished"] == 0 and stats["retried"] == 0)
    finally:
        for proc in procs:
            proc.terminate()
            try:
                proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
        for handle in handles:
            handle.close()
        claims = claim_stats(logs, seeded_at) if logs else {}
        cleanup(accounts)

    report = {"config": vars(args), "jobs": stats, "claims": claims}
    path = OUT / f"worker_drain_{datetime.now():%Y-%m-%d_%H-%M-%S}.json"
    path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"Worker drain: {stats['succeeded']}/{args.jobs} done, "
          f"{stats['failed']} failed, {stats['unfinished']} unfinished. "
          f"Peak {stats['peak_running']} slots, {stats['throughput_jobs_min']} jobs/min.")
    print(f"Queue wait: {stats['queue_wait_s']} | run: {stats['run_s']}")
    print(f"Claim latency: {claims.get('claim_ms')} | report: {path}")


if __name__ == "__main__":
    main()
