"""Step k6 users through API load; optionally measure stubbed queue jobs with Render suspended."""
import argparse
import json
import os
import re
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
from load_test import _no_tracing, preflight, pctl, wait_for_health  # noqa: E402

OUT = ROOT / "load_test_results"
PASSWORD = "k6-load-pw-4821"
ENDPOINTS = ("login", "health", "chats", "saved", "cart", "orders", "credits", "submit", "job_status")
PHASE_ENDPOINTS = ("login", "chats", "saved", "orders", "credits", "submit", "job_status")


def spike_plan(base: int, peak: int) -> tuple[list[dict], list[dict]]:
    """Settle, spike, then return to base load long enough to measure recovery."""
    stages = [{"seconds": 30, "target": base}, {"seconds": 60, "target": base},
              {"seconds": 10, "target": peak}, {"seconds": 60, "target": peak},
              {"seconds": 10, "target": base}, {"seconds": 180, "target": base}]
    phases = [{"name": "before", "end_s": 90}, {"name": "spike", "end_s": 160},
              {"name": "after", "end_s": 350}]
    return stages, phases


def phase_stats(summary: dict, phases: list[dict]) -> dict:
    """Compare API p50/p95/p99 and errors before, during and after a spike."""
    out = {}
    metrics = summary["metrics"]
    for phase in phases:
        values = {}
        for name in PHASE_ENDPOINTS:
            latency = metrics.get(f"phase_{phase['name']}_{name}", {}).get("values", {})
            if latency.get("count"):
                errors = metrics.get(f"phase_err_{phase['name']}_{name}", {}).get("values", {})
                values[name] = {"count": int(latency["count"]),
                                **{k: round(latency[f"p({n})"], 1)
                                   for k, n in (("p50", 50), ("p95", 95), ("p99", 99))},
                                "error_pct": round(errors.get("rate", 0) * 100, 2)}
        out[phase["name"]] = values
    return out


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
            users.append({"id": user_id, "username": username, "chat": chat_id,
                          "token": create_token(user_id, username)})
    return users


def cleanup(users: list[dict]) -> None:
    """Remove only this run's accounts and every row they own, never catalogue/cache."""
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


def cleanup_by_tag(tag: str) -> int:
    """Recover accounts from an interrupted run using its exact printed tag."""
    if not re.fullmatch(r"k6-[0-9a-f]{8}", tag):
        raise ValueError("Expected an exact k6-<8 hex> run tag")
    with engine.connect() as db:
        rows = db.execute(text("SELECT id, username FROM ecommerce_accounts "
                               "WHERE username LIKE :prefix"),
                          {"prefix": f"{tag}-%"}).mappings().all()
    users = [dict(row) for row in rows]
    cleanup(users)
    return len(users)


def _bulk(db, sql: str, rows: list[dict]) -> None:
    """Insert many rows in ONE round trip: a text() executemany sends one statement
    per row, which took minutes over Neon for 150 users."""
    if rows:
        db.execute(text(sql), {k: [r[k] for r in rows] for k in rows[0]})


def seed_pages(users: list[dict], saved_count: int, message_count: int,
               cart_count: int, order_count: int) -> None:
    """Give browse endpoints real-sized payloads and each user a completed job probe."""
    need = max(saved_count, cart_count, order_count, 1)
    with engine.begin() as db:
        products = db.execute(text("SELECT DISTINCT ON (pid) pid, title, price "
                                   "FROM product WHERE availability = 'InStock' "
                                   "AND pid IS NOT NULL AND price IS NOT NULL "
                                   "ORDER BY pid LIMIT :n"), {"n": need}).mappings().all()
        if len(products) < need:
            raise RuntimeError(f"Need {need} in-stock catalogue products to seed browse data")
        _bulk(db, "INSERT INTO chat_messages (user_id, chat_id, role, content, created_at) "
                  "SELECT u, c, r, t, now() FROM unnest(CAST(:u AS int[]), CAST(:chat AS text[]), "
                  "CAST(:role AS text[]), CAST(:content AS text[])) AS x(u, c, r, t)",
              [{"u": u["id"], "chat": u["chat"], "role": "user" if i % 2 == 0 else "assistant",
                "content": (f"Show me running shoes under Rs. {2000 + i * 100}" if i % 2 == 0
                            else "Here are some in-stock shoes from the catalogue.")}
               for u in users for i in range(message_count)])
        _bulk(db, "INSERT INTO saved_products (user_id, pid, saved_price, created_at) "
                  "SELECT u, p, pr, now() FROM unnest(CAST(:u AS int[]), CAST(:pid AS text[]), "
                  "CAST(:price AS int[])) AS x(u, p, pr)",
              [{"u": u["id"], "pid": p["pid"], "price": p["price"]}
               for u in users for p in products[:saved_count]])
        _bulk(db, "INSERT INTO cart_items (user_id, pid, quantity, created_at) "
                  "SELECT u, p, 1, now() FROM unnest(CAST(:u AS int[]), CAST(:pid AS text[])) AS x(u, p)",
              [{"u": u["id"], "pid": p["pid"]} for u in users for p in products[:cart_count]])
        for product in products[:order_count]:
            orders = db.execute(text("INSERT INTO orders (user_id, status, total, created_at) "
                                     "SELECT uid, 'placed', :price, now() FROM unnest(CAST(:uids AS int[])) uid "
                                     "RETURNING id"),
                                {"uids": [u["id"] for u in users], "price": product["price"]}).scalars().all()
            db.execute(
                text("INSERT INTO order_items (order_id, pid, title, price, quantity) "
                     "SELECT o, :pid, :title, :price, 1 FROM unnest(CAST(:oids AS int[])) o"),
                {"oids": list(orders), "pid": product["pid"], "title": product["title"],
                 "price": product["price"]})
        for user in users:
            user["probe"] = uuid.uuid4().hex
        _bulk(db, "INSERT INTO jobs (id, user_id, chat_id, status, query, history, tool, result, "
                  "created_at, started_at, finished_at, billing_mode) "
                  "SELECT i, u, c, 'succeeded', 'probe', '[]', 'search_product_database', 'ready', "
                  "now(), now(), now(), 'FREE' FROM unnest(CAST(:id AS text[]), CAST(:u AS int[]), "
                  "CAST(:chat AS text[])) AS x(i, u, c)",
              [{"id": u["probe"], "u": u["id"], "chat": u["chat"]} for u in users])


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
            "jobs_submitted": int(metrics.get("jobs_submitted", {}).get("values", {}).get("count", 0)),
            "error_pct": round(metrics.get("http_req_failed", {}).get("values", {}).get("rate", 0) * 100, 2),
            "thresholds_failed": [key for key, value in metrics.items()
                                  if any(not t["ok"] for t in value.get("thresholds", {}).values())]}


def find_k6() -> str:
    path = shutil.which("k6") or r"C:\Program Files\k6\k6.exe"
    if not os.path.isfile(path):
        raise SystemExit("k6 is missing; install it or add it to PATH")
    return path


def run_level(k6: str, args, accounts: list[dict], accounts_path: Path,
              level: int, stages=None, phases=None) -> dict:
    """One k6 level: request errors, job outcomes and latency. `passed` marks a level
    with under 1% request errors and every submitted job finished; latency is reported,
    not used in that mark, and k6 exit 99 (a crossed p95 threshold) is ignored."""
    summary_path = OUT / f"k6_summary_{level}.json"
    summary_path.unlink(missing_ok=True)
    since = datetime.now(timezone.utc)
    env = dict(os.environ, BASE=f"http://127.0.0.1:{args.port}", VUS=str(level),
               RAMP_S=str(args.ramp_up), HOLD_S=str(args.hold),
               JOBS_PER_USER=str(args.jobs_per_user), START_MS=str(int(time.time() * 1000)),
               STAGES=json.dumps(stages) if stages else "", PHASES=json.dumps(phases or []),
               ACCOUNTS=str(accounts_path), SUMMARY=str(summary_path), PASSWORD=PASSWORD)
    result = subprocess.run([k6, "run", "--quiet", str(ROOT / "load_test_k6.js")],
                            env=env, check=False)
    if not summary_path.exists():
        raise RuntimeError(f"k6 exited {result.returncode} without writing a summary")
    try:
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
        api = api_stats(summary)
        phase = phase_stats(summary, phases) if phases else None
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
    expected = level * args.jobs_per_user if args.jobs_per_user else 0
    passed = api["error_pct"] < 1 and result.returncode in (0, 99)
    if jobs is not None:
        passed = (passed and api["jobs_submitted"] >= expected
                  and jobs["submitted"] == api["jobs_submitted"]
                  and jobs["failed"] == 0 and jobs["unfinished"] == 0)
    if phase is not None:
        passed = passed and all(phase.get(name) for name in ("before", "spike", "after"))
    return {"users": level, "passed": passed, "expected_jobs": expected,
            "api": api, "jobs": jobs, "phases": phase}


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
    parser.add_argument("--workers", type=int, default=0,
                        help="separate stub workers; 0 uses the in-process worker")
    parser.add_argument("--spike", metavar="BASE,PEAK",
                        help="settle, spike, and recover (350 seconds total)")
    parser.add_argument("--stop-error-pct", type=float, default=25)
    parser.add_argument("--cleanup-tag", metavar="K6_TAG",
                        help="remove accounts left by an interrupted run")
    parser.add_argument("--slots", type=int, default=20, help="concurrent jobs per separate worker")
    parser.add_argument("--port", type=int, default=8021)
    args = parser.parse_args()
    if args.cleanup_tag:
        if not args.render_suspended:
            parser.error("cleanup requires --render-suspended")
        print(f"Cleaned {cleanup_by_tag(args.cleanup_tag)} accounts for {args.cleanup_tag}")
        return
    try:
        levels = [int(value) for value in args.levels.split(",")]
        pair = [int(value) for value in args.spike.split(",")] if args.spike else []
    except ValueError:
        parser.error("--levels and --spike require comma-separated integers")
    if args.spike and (len(pair) != 2 or not 1 <= pair[0] < pair[1] <= 600):
        parser.error("--spike requires BASE,PEAK with 1 <= BASE < PEAK <= 600")
    if not levels or min(levels) < 1 or max(levels) > 600 or args.jobs_per_user < 0 or args.hold < 1 or args.ramp_up < 1:
        parser.error("levels must be 1–600; hold and ramp-up positive; jobs-per-user nonnegative")
    if not 0 <= args.workers <= 3 or (args.workers and not args.jobs_per_user):
        parser.error("--workers requires queue mode and a value from 1 to 3")
    if args.jobs_per_user and not args.render_suspended:
        parser.error("queue load requires --render-suspended: Render can claim stub jobs")
    if args.msg_seconds <= 0 or args.drain_timeout <= 0 or not 0 <= args.stop_error_pct <= 100:
        parser.error("message seconds and drain timeout must be positive; stop error percent 0–100")
    if args.spike:
        levels = [pair[1]]
    _no_tracing(os.environ)
    k6 = find_k6()
    if args.jobs_per_user:
        preflight()
    with socket.socket() as check:
        if check.connect_ex(("127.0.0.1", args.port)) == 0:
            parser.error(f"port {args.port} is in use; choose another port")
    OUT.mkdir(exist_ok=True)
    tag = f"k6-{uuid.uuid4().hex[:8]}"
    print(f"Run tag: {tag}; recover with --cleanup-tag {tag} --render-suspended", flush=True)
    log = (OUT / f"{tag}_server.log").open("w", encoding="utf-8")
    env = dict(os.environ, RUN_WORKER_IN_PROCESS="1" if args.jobs_per_user and not args.workers else "0")
    cmd = [sys.executable, str(ROOT / "load_test.py"), "--serve",
           "--port", str(args.port), "--msg-seconds", str(args.msg_seconds)]
    if args.jobs_per_user and not args.workers:
        cmd.append("--render-suspended")
    proc = subprocess.Popen(cmd, env=env, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT)
    accounts = []
    workers, worker_logs = [], []
    results = []
    accounts_path = OUT / f"{tag}_accounts.json"
    try:
        if not wait_for_health(f"http://127.0.0.1:{args.port}") or proc.poll() is not None:
            raise RuntimeError(f"stub API did not start; inspect {OUT / f'{tag}_server.log'}")
        accounts = create_accounts(max(levels), tag)
        seed_pages(accounts, saved_count=5, message_count=20, cart_count=2, order_count=2)
        accounts_path.write_text(json.dumps(accounts), encoding="utf-8")
        if args.workers:
            worker_env = dict(os.environ, WORKER_MIN_CONCURRENCY="1",
                              WORKER_MAX_CONCURRENCY=str(args.slots), LOADTEST_WORKER_CHILD="1")
            _no_tracing(worker_env)
            for n in range(args.workers):
                handle = (OUT / f"{tag}_worker_{n}.log").open("w", encoding="utf-8")
                worker_logs.append(handle)
                workers.append(subprocess.Popen(
                    [sys.executable, str(ROOT / "load_test_worker.py"), "--worker-mode",
                     "--render-suspended", "--msg-seconds", str(args.msg_seconds)],
                    cwd=ROOT, env=worker_env, stdout=handle, stderr=subprocess.STDOUT))
        for level in levels:
            stages, phases = spike_plan(*pair) if args.spike else (None, None)
            print(f"\n{level} users ({'spike/recovery' if args.spike else f'{args.ramp_up}s ramp, {args.hold}s hold'})", flush=True)
            row = run_level(k6, args, accounts, accounts_path, level, stages, phases)
            results.append(row)
            print(f"  {row['api']['requests']} requests; {row['api']['error_pct']}% errors")
            for name, stat in row["api"]["endpoints"].items():
                print(f"  {name}: p50 {stat['p50']}ms / p95 {stat['p95']}ms / p99 {stat['p99']}ms"
                      f" ({stat['error_pct']}% errors)")
            if row["jobs"] is not None:
                print(f"  jobs: {row['jobs']}; submitted {row['api']['jobs_submitted']}/{row['expected_jobs']}")
            if phases:
                print(f"  before/spike/after: {row['phases']}")
            if row["api"]["error_pct"] >= args.stop_error_pct:
                print(f"Stopping: API errors reached {args.stop_error_pct}%")
                break
        report = OUT / f"{tag}_stress.json"
        report.write_text(json.dumps({"config": vars(args), "levels": results,
                                      "highest_passing_users": max(
                                          (r["users"] for r in results if r["passed"]), default=None)}, indent=2),
                          encoding="utf-8")
        print(f"Report: {report}")
    finally:
        for child in [*workers, proc]:
            if child.poll() is None:
                child.terminate()
            try:
                child.wait(timeout=30)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()
        for handle in [*worker_logs, log]:
            handle.close()
        accounts_path.unlink(missing_ok=True)
        cleanup(accounts)


if __name__ == "__main__":
    main()
