"""Step k6 users through API load; optionally measure stubbed queue jobs with Render suspended."""
import argparse
import json
import os
import shutil
import socket
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

from app.db.database import engine  # noqa: E402
from load_test import pctl, wait_for_health  # noqa: E402

OUT = ROOT / "load_test_results"
PASSWORD = "k6-load-pw-4821"
ENDPOINTS = ("health", "chats", "saved", "credits", "submit", "job_status")


def create_accounts(count: int, tag: str) -> list[dict]:
    """Create only tagged test users and their empty chat sessions in one transaction."""
    from main import create_token, hash_password

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
            users.append({"id": user_id, "chat": chat_id,
                          "token": create_token(user_id, username)})
    return users


def cleanup(users: list[dict]) -> None:
    """Delete only rows owned by this run; never touch catalogue, cache or other users."""
    ids = [u["id"] for u in users]
    if not ids:
        return
    with engine.begin() as db:
        db.execute(text("""
            DELETE FROM job_events WHERE job_id IN
            (SELECT id FROM jobs WHERE user_id = ANY(:ids))
        """), {"ids": ids})
        for table in ("jobs", "chat_messages", "saved_products", "cart_items",
                      "chat_sessions", "user_api_keys"):
            db.execute(text(f"DELETE FROM {table} WHERE user_id = ANY(:ids)"), {"ids": ids})
        db.execute(text("DELETE FROM ecommerce_accounts WHERE id = ANY(:ids)"), {"ids": ids})
    print(f"Cleaned up {len(ids)} test accounts")


def job_stats(ids: list[int], since: datetime) -> dict:
    """Read actual queue and processing times from persisted job timestamps."""
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


def api_stats(summary: dict) -> dict:
    metrics = summary["metrics"]
    endpoints = {}
    for name in ENDPOINTS:
        values = metrics.get(f"api_{name}", {}).get("values", {})
        if values.get("count"):
            errors = metrics.get(f"err_{name}", {}).get("values", {}).get("rate", 0)
            endpoints[name] = {"count": int(values["count"]),
                               **{p: round(values[f"p({n})"], 1)
                                  for p, n in (("p50", 50), ("p95", 95), ("p99", 99))},
                               "error_pct": round(errors * 100, 2)}
    return {"endpoints": endpoints,
            "requests": int(metrics.get("http_reqs", {}).get("values", {}).get("count", 0)),
            "error_pct": round(metrics.get("http_req_failed", {}).get("values", {}).get("rate", 0) * 100, 2),
            "thresholds_failed": [key for key, value in metrics.items()
                                  if any(not t["ok"] for t in value.get("thresholds", {}).values())]}


def find_k6() -> str:
    path = shutil.which("k6") or r"C:\Program Files\k6\k6.exe"
    if not os.path.isfile(path):
        raise SystemExit("k6 is missing; install it or add it to PATH")
    return path


def run_level(k6: str, args, accounts: list[dict], accounts_path: Path, level: int) -> dict:
    summary_path = OUT / f"k6_summary_{level}.json"
    summary_path.unlink(missing_ok=True)
    since = datetime.now(timezone.utc)
    env = dict(os.environ, BASE=f"http://127.0.0.1:{args.port}", VUS=str(level),
               RAMP_S=str(args.ramp_up), HOLD_S=str(args.hold),
               JOBS_PER_USER=str(args.jobs_per_user),
               ACCOUNTS=str(accounts_path), SUMMARY=str(summary_path))
    result = subprocess.run([k6, "run", "--quiet", str(ROOT / "load_test_k6.js")],
                            env=env, check=False)
    if not summary_path.exists():
        raise RuntimeError(f"k6 exited {result.returncode} without writing a summary")
    try:
        api = api_stats(json.loads(summary_path.read_text(encoding="utf-8")))
    finally:
        summary_path.unlink()
    jobs = None
    if args.jobs_per_user:
        deadline = time.monotonic() + args.drain_timeout
        ids = [u["id"] for u in accounts]
        while True:
            jobs = job_stats(ids, since)
            if not jobs["unfinished"] or time.monotonic() >= deadline:
                break
            time.sleep(2)
    passed = (api["error_pct"] < 1 and not api["thresholds_failed"]
              and result.returncode in (0, 99))
    if jobs is not None:
        passed = (passed and jobs["submitted"] > 0 and jobs["failed"] == 0
                  and jobs["unfinished"] == 0 and jobs["queue_wait_s"]["p95"] <= 30)
    return {"users": level, "passed": passed, "api": api, "jobs": jobs}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--levels", default="10,25,50,75,100,150")
    parser.add_argument("--ramp-up", type=int, default=30)
    parser.add_argument("--hold", type=int, default=180)
    parser.add_argument("--jobs-per-user", type=int, default=0,
                        help="enable queue load (requires --render-suspended)")
    parser.add_argument("--render-suspended", action="store_true",
                        help="confirm Render's web service and any other worker are stopped")
    parser.add_argument("--msg-seconds", type=float, default=2,
                        help="stubbed per-job duration; no model is called")
    parser.add_argument("--drain-timeout", type=int, default=180)
    parser.add_argument("--port", type=int, default=8021)
    args = parser.parse_args()
    levels = [int(value) for value in args.levels.split(",")]
    if not levels or min(levels) < 1 or args.jobs_per_user < 0 or args.hold < 1 or args.ramp_up < 1:
        parser.error("levels, hold and ramp-up must be positive; jobs-per-user cannot be negative")
    if args.jobs_per_user and not args.render_suspended:
        parser.error("queue load requires --render-suspended: Render can claim stub jobs")
    k6 = find_k6()
    with socket.socket() as check:
        if check.connect_ex(("127.0.0.1", args.port)) == 0:
            parser.error(f"port {args.port} is in use; choose another port")
    OUT.mkdir(exist_ok=True)
    tag = f"k6-{uuid.uuid4().hex[:8]}"
    log = (OUT / "k6_server.log").open("w", encoding="utf-8")
    env = dict(os.environ, RUN_WORKER_IN_PROCESS="1" if args.jobs_per_user else "0")
    proc = subprocess.Popen([sys.executable, str(ROOT / "load_test.py"), "--serve",
                             "--port", str(args.port), "--msg-seconds", str(args.msg_seconds)],
                            env=env, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT)
    accounts = []
    results = []
    accounts_path = OUT / f"{tag}_accounts.json"
    try:
        if not wait_for_health(f"http://127.0.0.1:{args.port}") or proc.poll() is not None:
            raise RuntimeError(f"stub API did not start; inspect {OUT / 'k6_server.log'}")
        accounts = create_accounts(max(levels), tag)
        accounts_path.write_text(json.dumps(accounts), encoding="utf-8")
        for level in levels:
            print(f"\n{level} users ({args.ramp_up}s ramp, {args.hold}s hold)", flush=True)
            row = run_level(k6, args, accounts, accounts_path, level)
            results.append(row)
            print(f"  {row['api']['requests']} requests; {row['api']['error_pct']}% errors")
            for name, stat in row["api"]["endpoints"].items():
                print(f"  {name}: p50 {stat['p50']}ms / p95 {stat['p95']}ms / p99 {stat['p99']}ms"
                      f" ({stat['error_pct']}% errors)")
            if row["jobs"] is not None:
                print(f"  jobs: {row['jobs']}")
            print("  PASS" if row["passed"] else "  FAIL", flush=True)
            if row["api"]["error_pct"] >= 25:
                print("Stopping: API errors reached 25%")
                break
        report = OUT / "k6_stress.json"
        report.write_text(json.dumps({"config": vars(args), "levels": results,
                                      "highest_passing_users": max(
                                          (r["users"] for r in results if r["passed"]), default=None)}, indent=2),
                          encoding="utf-8")
        print(f"Report: {report}")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        log.close()
        accounts_path.unlink(missing_ok=True)
        cleanup(accounts)


if __name__ == "__main__":
    main()
