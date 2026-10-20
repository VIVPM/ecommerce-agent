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
from main import create_token, hash_password  # noqa: E402

OUT = ROOT / "load_test_results"
PASSWORD = "k6-load-pw-4821"


def create_accounts(count: int, tag: str) -> list[dict]:
    password_hash = hash_password(PASSWORD)
    users = []
    with engine.begin() as db:
        for n in range(count):
            username = f"{tag}-{n}"
            user_id = db.execute(text("""
                INSERT INTO ecommerce_accounts (username, hashed_password)
                VALUES (:username, :password) RETURNING id
            """), {"username": username, "password": password_hash}).scalar_one()
            chat_id = uuid.uuid4().hex
            db.execute(text("""
                INSERT INTO chat_sessions (id, user_id, title, created_at, updated_at)
                VALUES (:chat, :uid, 'New Chat', now(), now())
            """), {"chat": chat_id, "uid": user_id})
            users.append({"id": user_id, "username": username, "chat": chat_id,
                          "token": create_token(user_id, username)})
    return users


def cleanup(users: list[dict]) -> None:
    ids = [u["id"] for u in users]
    if not ids:
        return
    with engine.begin() as db:
        db.execute(text("DELETE FROM job_events WHERE job_id IN "
                        "(SELECT id FROM jobs WHERE user_id = ANY(:ids))"), {"ids": ids})
        db.execute(text("DELETE FROM order_items WHERE order_id IN "
                        "(SELECT id FROM orders WHERE user_id = ANY(:ids))"), {"ids": ids})
        for table in ("jobs", "orders", "cart_items", "chat_messages", "saved_products",
                      "chat_sessions", "user_api_keys", "user_providers"):
            db.execute(text(f"DELETE FROM {table} WHERE user_id = ANY(:ids)"), {"ids": ids})
        db.execute(text("DELETE FROM login_failures WHERE username = ANY(:names)"),
                   {"names": [u["username"] for u in users]})
        db.execute(text("DELETE FROM ecommerce_accounts WHERE id = ANY(:ids)"), {"ids": ids})
    print(f"Cleaned up {len(ids)} test accounts")


def job_stats(ids: list[int], since: datetime) -> dict:
    with engine.connect() as db:
        rows = db.execute(text("""
            SELECT status, created_at, started_at, finished_at
              FROM jobs WHERE user_id = ANY(:ids) AND created_at >= :since
        """), {"ids": ids, "since": since}).mappings().all()
    periods = {
        "queue_wait_s": [(r["started_at"] - r["created_at"]).total_seconds()
                         for r in rows if r["started_at"]],
        "run_s": [(r["finished_at"] - r["started_at"]).total_seconds()
                  for r in rows if r["finished_at"] and r["started_at"]],
        "total_s": [(r["finished_at"] - r["created_at"]).total_seconds()
                    for r in rows if r["finished_at"]],
    }
    result: dict[str, object] = {"submitted": len(rows),
              "succeeded": sum(r["status"] == "succeeded" for r in rows),
              "failed": sum(r["status"] == "failed" for r in rows),
              "unfinished": sum(r["status"] in ("queued", "running") for r in rows)}
    for name, times in periods.items():
        result[name] = {key: round(pctl(times, n), 2) if times else None
                        for key, n in (("p50", 50), ("p95", 95), ("p99", 99))}
    return result


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
