"""Load test: does browsing stay responsive while /message is saturated?"""
import argparse
import asyncio
import json
import math
import os
import re
import socket
import subprocess
import sys
import time
import uuid
from urllib.parse import urlsplit
from datetime import datetime, timezone

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

from dotenv import load_dotenv
load_dotenv(dotenv_path=os.path.join(BASE_DIR, "app", ".env"))

import httpx

LOAD_USER = os.getenv("LOADTEST_USERNAME") or f"loadtest_{uuid.uuid4().hex[:8]}"
LOAD_PASS = "LoadTest-pw-9137"
Q = "cheapest running shoes for men"
_owned_user = False


def preflight() -> None:
    """A stub worker must never claim a real job on the shared Neon queue."""
    from sqlalchemy import text
    from app.db.database import engine
    with engine.connect() as db:
        active = db.execute(text("SELECT count(*) FROM jobs WHERE status IN ('queued', 'running')")).scalar()
    if active:
        raise SystemExit(f"ABORT: {active} job(s) already queued/running; wait for the queue to drain.")


def local_base(base: str) -> str:
    """Refuse remote load targets: this harness uses the live database and test users."""
    url = urlsplit(base)
    if url.scheme != "http" or url.hostname not in ("localhost", "127.0.0.1", "::1"):
        raise SystemExit("--base must be a local HTTP URL; this harness never targets Render.")
    return base.rstrip("/")


def pctl(xs, p):
    """Nearest-rank percentile (small n — interpolation would invent precision)."""
    if not xs:
        return float("nan")
    xs = sorted(xs)
    rank = max(1, math.ceil(p / 100 * len(xs)))
    return xs[min(rank, len(xs)) - 1]


def serve_mode(port, msg_seconds):
    """Run the real API and worker while replacing model calls with a sleep."""
    for k in ("LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY"):
        os.environ.pop(k, None)

    os.environ["DAILY_MESSAGE_CAP"] = str(10**9)
    os.environ["MAX_ACTIVE_JOBS"] = str(10**9)

    import main

    def stub_optimize(query, history=None):
        return query

    async def stub_agent(_query, _user_id=None, **_kwargs):
        """Stands in for routing plus the chosen tool."""
        await asyncio.sleep(msg_seconds)
        yield {"status": "Searching products...", "tool": "search_product_database"}
        for tok in ("Here ", "are ", "the ", "results."):
            yield {"token": tok}

    from app import worker
    worker.optimize_query = stub_optimize
    worker.memory_recall = lambda *_args: ""
    worker.memory_remember = lambda *_args: None
    worker.is_off_topic = lambda *_args: False
    worker.astream_agent = stub_agent

    import uvicorn
    uvicorn.run(main.app, host="127.0.0.1", port=port, log_level="error")


READ_MIX = (
    ("health", "GET", "/api/health", False),
    ("chats", "GET", "/api/chats", True),
    ("saved", "GET", "/api/saved", True),
    ("credits", "GET", "/api/account/credits", True),
    ("login", "POST", "/api/auth/login", None),
)


async def _hammer(base, token, concurrency, duration, mix="read"):
    """Fire the browse mix; 'all' also stresses login, which is IP rate-limited."""
    endpoints = [item for item in READ_MIX if mix == "all" or item[0] != "login"]
    lat = {k[0]: [] for k in endpoints}
    errors = {"count": 0, "samples": []}
    throttled = 0
    stop = time.monotonic() + duration
    auth = {"Authorization": f"Bearer {token}"}

    async def one(client):
        nonlocal throttled
        while time.monotonic() < stop:
            for name, method, url, needs_auth in endpoints:
                kwargs = {}
                if needs_auth is True:
                    kwargs["headers"] = auth
                elif needs_auth is None:
                    kwargs["json"] = {"username": LOAD_USER, "password": LOAD_PASS}
                t = time.monotonic()
                try:
                    r = await client.request(method, url, timeout=30, **kwargs)
                    dt = (time.monotonic() - t) * 1000
                    if r.status_code == 429:
                        throttled += 1
                    elif r.status_code >= 400:
                        errors["count"] += 1
                        if len(errors["samples"]) < 5:
                            errors["samples"].append(f"{name} {r.status_code} {r.text[:60]}")
                    else:
                        lat[name].append(dt)
                except Exception as exc:
                    errors["count"] += 1
                    if len(errors["samples"]) < 5:
                        errors["samples"].append(f"{name} {type(exc).__name__}")
                if time.monotonic() >= stop:
                    return

    limits = httpx.Limits(max_connections=concurrency + 20,
                          max_keepalive_connections=concurrency)
    started = time.monotonic()
    async with httpx.AsyncClient(base_url=base, limits=limits) as client:
        await asyncio.gather(*[one(client) for _ in range(concurrency)])
    return {"lat": lat, "errors": errors, "throttled": throttled,
            "elapsed": time.monotonic() - started}


async def _message_load(base, token, chat_id, n_streamers, stop_evt):
    """N clients continuously POST /message and drain the SSE stream."""
    auth = {"Authorization": f"Bearer {token}"}
    sent = 0

    async def loop(client):
        nonlocal sent
        while not stop_evt.is_set():
            try:
                r = await client.post(f"/api/chats/{chat_id}/message",
                                      json={"query": Q, "history": []},
                                      headers=auth, timeout=60)
                if r.status_code != 202:
                    await asyncio.sleep(1)
                    continue
                job_id = r.json()["job_id"]
                async with client.stream("GET", f"/api/jobs/{job_id}/events",
                                         headers=auth, timeout=120) as ev:
                    async for _ in ev.aiter_lines():
                        pass
                sent += 1
            except Exception:
                await asyncio.sleep(1)

    async with httpx.AsyncClient(base_url=base) as client:
        await asyncio.gather(*[loop(client) for _ in range(n_streamers)])
    return sent


def wait_for_health(base, timeout=90):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if httpx.get(f"{base}/api/health", timeout=3).status_code == 200:
                return True
        except Exception:
            pass
        time.sleep(1)
    return False


def ensure_user(base):
    """Create a unique test account; a pre-existing username is never reused."""
    global _owned_user
    from sqlalchemy import text
    from app.db.database import engine
    with engine.connect() as db:
        if db.execute(text("SELECT 1 FROM ecommerce_accounts WHERE username=:u"),
                      {"u": LOAD_USER}).scalar():
            raise SystemExit(f"{LOAD_USER} already exists. Confirm ownership and use "
                             f"--cleanup --cleanup-user {LOAD_USER} --render-suspended.")
    print(f"Test account tag: {LOAD_USER}; recover with --cleanup --cleanup-user "
          f"{LOAD_USER} --render-suspended", flush=True)
    last = None
    for _ in range(5):
        try:
            signup = httpx.post(f"{base}/api/auth/signup",
                                json={"username": LOAD_USER, "password": LOAD_PASS}, timeout=30)
            if signup.status_code == 200:
                _owned_user = True
                return signup.json()["token"]
            if signup.status_code == 400:
                raise SystemExit("Account name was taken before signup; refusing to reuse it.")
            last = f"{signup.status_code} {signup.text[:120]}"
        except Exception as e:
            last = f"{type(e).__name__}: {e}"
        time.sleep(2)
    sys.exit(f"Could not auth the load-test user after retries: {last}")


def create_chat(base, token):
    r = httpx.post(f"{base}/api/chats/new",
                   headers={"Authorization": f"Bearer {token}"}, timeout=30)
    r.raise_for_status()
    return r.json()["chat_id"]


def cleanup(username=None):
    """Delete rows only for this run's account or an explicit crashed-run tag."""
    global _owned_user
    target = username or LOAD_USER
    if not re.fullmatch(r"loadtest_[0-9a-f]{8}", target):
        raise ValueError("Only a tagged load-test account may be cleaned up.")
    if username is None and not _owned_user:
        return
    from sqlalchemy import text
    from app.db.database import engine
    with engine.begin() as c:
        uid = c.execute(text("SELECT id FROM ecommerce_accounts WHERE username=:u"),
                        {"u": target}).scalar()
        if uid is None:
            print("nothing to clean up.")
            return
        c.execute(text("DELETE FROM job_events WHERE job_id IN "
                       "(SELECT id FROM jobs WHERE user_id=:u)"), {"u": uid})
        c.execute(text("DELETE FROM order_items WHERE order_id IN "
                       "(SELECT id FROM orders WHERE user_id=:u)"), {"u": uid})
        for table in ("jobs", "orders", "cart_items", "chat_messages", "saved_products",
                      "chat_sessions", "user_api_keys", "user_providers"):
            c.execute(text(f"DELETE FROM {table} WHERE user_id=:u"), {"u": uid})
        c.execute(text("DELETE FROM login_failures WHERE username=:name"), {"name": target})
        c.execute(text("DELETE FROM ecommerce_accounts WHERE id=:u"), {"u": uid})
    if username is None:
        _owned_user = False
    print(f"cleaned up load-test user {target} (id {uid}) and its rows.")


def _row(label, idle, sat):
    if not idle or not sat:
        return f"  {label:8} {'no data':>50}"
    i50, i95, s50, s95 = pctl(idle, 50), pctl(idle, 95), pctl(sat, 50), pctl(sat, 95)
    ratio = s95 / i95 if i95 else float("nan")
    flag = "  <-- degraded" if ratio >= 2 else ""
    return (f"  {label:8} p50 {i50:6.0f} -> {s50:6.0f}ms   "
            f"p95 {i95:6.0f} -> {s95:6.0f}ms   x{ratio:.1f}{flag}")


def report(idle, sat, args, sent):
    print("\n" + "=" * 76)
    print(f"API UNDER LOAD  ({args.messages} streaming messages @ {args.msg_seconds}s each, "
          f"{args.concurrency} browse clients, {args.duration}s/phase)")
    print("=" * 76)
    print(f"  {'':8} {'idle':>19}   {'saturated':>22}")
    for name in idle["lat"]:
        print(_row(name, idle["lat"][name], sat["lat"][name]))

    n_idle = sum(len(v) for v in idle["lat"].values())
    n_sat = sum(len(v) for v in sat["lat"].values())
    print(f"\n  Browse throughput  {n_idle / idle['elapsed']:.0f} -> "
          f"{n_sat / sat['elapsed']:.0f} req/s")
    print(f"  Messages streamed  {sent} during the saturated phase")
    print(f"  Throttled (429)    {idle['throttled']} idle, {sat['throttled']} saturated")
    print(f"  Errors             {idle['errors']['count']} idle, {sat['errors']['count']} saturated")
    for s in (idle["errors"]["samples"] + sat["errors"]["samples"])[:5]:
        print(f"    {s}")

    out_dir = os.path.join(BASE_DIR, "load_test_results")
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, f"api_{datetime.now().strftime('%Y-%m-%d_%H-%M-%S')}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump({
            "timestamp": datetime.now(timezone.utc).isoformat(), "config": vars(args),
            "messages_streamed": sent,
            "idle": {k: {"p50": pctl(v, 50), "p95": pctl(v, 95), "n": len(v)}
                     for k, v in idle["lat"].items()},
            "saturated": {k: {"p50": pctl(v, 50), "p95": pctl(v, 95), "n": len(v)}
                          for k, v in sat["lat"].items()},
            "throttled": {"idle": idle["throttled"], "saturated": sat["throttled"]},
            "errors": {"idle": idle["errors"]["count"], "saturated": sat["errors"]["count"]},
        }, f, indent=2)
    print(f"\n  Report: {path}")
    print("  Simulated generation latency — compare the ratio, not absolute ms.")


def _calibration_message(base, auth, chat_id):
    """Submit once, then wait on that job without ever resubmitting paid work."""
    t = time.monotonic()
    r = httpx.post(f"{base}/api/chats/{chat_id}/message",
                   json={"query": Q, "history": []}, headers=auth, timeout=30)
    r.raise_for_status()
    job_id = r.json()["job_id"]
    deadline = time.monotonic() + 120
    while True:
        job = httpx.get(f"{base}/api/jobs/{job_id}", headers=auth, timeout=30)
        job.raise_for_status()
        payload = job.json()
        if payload["status"] in ("succeeded", "failed", "cancelled"):
            if payload["status"] != "succeeded":
                raise RuntimeError(payload.get("error") or f"job {job_id} {payload['status']}")
            return job_id, time.monotonic() - t
        if time.monotonic() >= deadline:
            cancelled = httpx.post(f"{base}/api/jobs/{job_id}/cancel",
                                   headers=auth, timeout=30)
            cancelled.raise_for_status()
            raise TimeoutError(f"job {job_id} exceeded 120s; cancellation requested")
        time.sleep(0.25)


def calibrate(n, base):
    """Measure N real jobs with the configured provider and normal caches enabled.
    Uncached messages cost money; this product-search query uses SQL, not Pinecone.
    """
    token = ensure_user(base)
    chat_id = create_chat(base, token)
    auth = {"Authorization": f"Bearer {token}"}
    took = []
    job_ids = []
    print(f"Sending {n} real message(s) through {base} ...")
    for i in range(n):
        job_id, dt = _calibration_message(base, auth, chat_id)
        job_ids.append(job_id)
        took.append(dt)
        print(f"  message {i + 1}: {dt:.1f}s (succeeded)")

    from sqlalchemy import text
    from app.db.database import engine
    with engine.connect() as c:
        usage = c.execute(text("""
            SELECT id, input_tokens, output_tokens, cached_tokens, provider
              FROM jobs WHERE id = ANY(:ids)
        """), {"ids": job_ids}).mappings().all()
    total_in = sum(row["input_tokens"] or 0 for row in usage)
    total_out = sum(row["output_tokens"] or 0 for row in usage)
    total_cached = sum(row["cached_tokens"] or 0 for row in usage)
    from app.llm_provider import estimate_cost_usd
    total_cost = sum(estimate_cost_usd(row["provider"], row["input_tokens"] or 0,
                                       row["output_tokens"] or 0) for row in usage)
    print("\n" + "=" * 60)
    print(f"REAL MESSAGE LATENCY  (n={n})")
    print("=" * 60)
    print(f"  p50 {pctl(took, 50):.1f}s   p95 {pctl(took, 95):.1f}s   "
          f"min {min(took):.1f}s   max {max(took):.1f}s")
    print(f"  tokens  {total_in:,} input + {total_out:,} output "
          f"({total_cached:,} cached)")
    print(f"  estimated model cost  ${total_cost:.6f}")
    out_dir = os.path.join(BASE_DIR, "load_test_results")
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, f"calibrate_{datetime.now().strftime('%Y-%m-%d_%H-%M-%S')}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump({
            "timestamp": datetime.now(timezone.utc).isoformat(), "base": base,
            "query": Q, "messages": n, "latency_seconds": took,
            "p50_seconds": pctl(took, 50), "p95_seconds": pctl(took, 95),
            "usage": [dict(row) for row in usage], "estimated_cost_usd": total_cost,
        }, f, indent=2)
    print(f"  Report: {path}")
    print("\n  Full completion includes queue/DB/poll time, not just model generation.")
    print("  Do not copy it directly into the generation-only --msg-seconds setting.")


def ramp_mode(base, levels, duration, mix="read", stop_pct=25):
    """Ramp browse clients with the worker off; login is optional because its
    per-IP limiter makes a login-on-every-cycle mix intentionally harsh."""
    print(f"RAMP against {base}  (mix={mix}; health + chats + saved + credits)\n")
    token = ensure_user(base)
    create_chat(base, token)
    print(f"  {'clients':>7} | {'browse p50':>11} {'browse p95':>11} | {'req/s':>6} | {'err':>4} {'429':>5}")
    print("  " + "-" * 62)
    rows = []
    for c in levels:
        res = asyncio.run(_hammer(base, token, c, duration, mix=mix))
        browse = [v for name in ('health', 'chats', 'saved', 'credits')
                  for v in res['lat'][name]]
        p50, p95 = pctl(browse, 50), pctl(browse, 95)
        n = sum(len(v) for v in res['lat'].values())
        rps = n / res['elapsed'] if res['elapsed'] else 0
        print(f"  {c:>7} | {p50:>9.0f}ms {p95:>9.0f}ms | {rps:>6.0f} | "
              f"{res['errors']['count']:>4} {res['throttled']:>5}")
        rows.append({"clients": c, "browse_p50_ms": round(p50) if browse else None,
                     "browse_p95_ms": round(p95) if browse else None,
                     "req_s": round(rps), "errors": res['errors']['count'],
                     "throttled": res['throttled'], "err_samples": res['errors']['samples'][:3]})
        total = n + res['errors']['count'] + res['throttled']
        if total and 100 * res['errors']['count'] / total > stop_pct:
            print(f"Stopping: errors above {stop_pct}%")
            break
    out_dir = os.path.join(BASE_DIR, "load_test_results")
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, f"ramp_{datetime.now().strftime('%Y-%m-%d_%H-%M-%S')}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"base": base, "duration": duration, "levels": rows}, f, indent=2)
    print(f"\n  Report: {path}")


def _spawn_server(port, msg_seconds, run_worker=False):
    """Spawn the real app with model calls stubbed and optional in-process worker."""
    with socket.socket() as probe:
        if probe.connect_ex(("127.0.0.1", port)) == 0:
            raise SystemExit(f"Port {port} is already in use; choose a free port.")
    env = dict(os.environ, RUN_WORKER_IN_PROCESS="1" if run_worker else "0",
               LOADTEST_USERNAME=LOAD_USER)
    for k in ("LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY"):
        env.pop(k, None)
    os.makedirs(os.path.join(BASE_DIR, "load_test_results"), exist_ok=True)
    log = open(os.path.join(BASE_DIR, "load_test_results", "server.log"), "w", encoding="utf-8")
    cmd = [sys.executable, os.path.abspath(__file__), "--serve",
           "--port", str(port), "--msg-seconds", str(msg_seconds)]
    if run_worker:
        cmd.append("--render-suspended")
    proc = subprocess.Popen(cmd, env=env, cwd=BASE_DIR,
                            stdout=log, stderr=subprocess.STDOUT)
    return proc, log


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8021)
    ap.add_argument("--concurrency", type=int, default=20, help="browse clients")
    ap.add_argument("--messages", type=int, default=10, help="concurrent streaming messages")
    ap.add_argument("--duration", type=float, default=15, help="seconds per phase")
    ap.add_argument("--msg-seconds", type=float, default=2.0, help="simulated generation time")
    ap.add_argument("--calibrate", type=int, metavar="N", help="send N REAL messages (costs money)")
    ap.add_argument("--base", default=None, help="target a running server instead of spawning one")
    ap.add_argument("--ramp", action="store_true", help="ramp browse concurrency against --base")
    ap.add_argument("--levels", default="5,15,30,50", help="concurrency levels for --ramp")
    ap.add_argument("--mix", choices=("read", "all"), default="read",
                    help="read is normal browsing; all also logs in every cycle")
    ap.add_argument("--ramp-stop-pct", type=float, default=25,
                    help="stop the ramp when the non-429 error rate exceeds this percentage")
    ap.add_argument("--render-suspended", action="store_true",
                    help="confirm Render and every other worker using this database are stopped")
    ap.add_argument("--serve", action="store_true")
    ap.add_argument("--cleanup", action="store_true")
    ap.add_argument("--cleanup-user", metavar="LOADTEST_TAG",
                    help="exact loadtest_<8 hex> username left by an interrupted run")
    args = ap.parse_args()

    if args.base:
        args.base = local_base(args.base)
        if not args.ramp and not args.calibrate:
            ap.error("--base is only used with --ramp or --calibrate")
    if args.serve:
        if os.getenv("RUN_WORKER_IN_PROCESS", "1") == "1" and not args.render_suspended:
            ap.error("stubbed worker requires --render-suspended")
        return serve_mode(args.port, args.msg_seconds)
    if args.cleanup:
        if not args.render_suspended or not args.cleanup_user:
            ap.error("cleanup requires --render-suspended and --cleanup-user LOADTEST_TAG")
        return cleanup(args.cleanup_user)
    if args.calibrate:
        if not args.render_suspended:
            ap.error("real-message calibration requires --render-suspended")
        preflight()
        try:
            return calibrate(args.calibrate, args.base or "http://127.0.0.1:8000")
        finally:
            cleanup()

    if args.ramp:
        levels = [int(x) for x in args.levels.split(",")]
        if args.base:
            try:
                return ramp_mode(args.base, levels, args.duration, args.mix, args.ramp_stop_pct)
            finally:
                cleanup()
        base = f"http://127.0.0.1:{args.port}"
        proc, log = _spawn_server(args.port, args.msg_seconds, run_worker=False)
        try:
            if not wait_for_health(base):
                sys.exit("app did not come up — see load_test_results/server.log")
            print(f"local app up on {base} (LLM stubbed; worker off)")
            ramp_mode(base, levels, args.duration, args.mix, args.ramp_stop_pct)
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
            log.close()
            cleanup()
        return

    if not args.render_suspended:
        ap.error("message saturation requires --render-suspended")
    preflight()
    base = f"http://127.0.0.1:{args.port}"
    proc, log = _spawn_server(args.port, args.msg_seconds, run_worker=True)
    log_path = os.path.join(BASE_DIR, "load_test_results", "server.log")
    try:
        if not wait_for_health(base):
            sys.exit("app did not come up — see load_test_results/server.log")
        print(f"app up on {base} (LLM stubbed)")
        token = ensure_user(base)
        chat_id = create_chat(base, token)

        print(f"Phase 1/2: idle, {args.concurrency} browse clients for {args.duration}s...")
        idle = asyncio.run(_hammer(base, token, args.concurrency, args.duration, args.mix))

        print(f"Phase 2/2: saturated, {args.messages} messages streaming + browse load...")
        async def saturated():
            stop_evt = asyncio.Event()
            msg_task = asyncio.create_task(_message_load(base, token, chat_id, args.messages, stop_evt))
            await asyncio.sleep(2)
            sat = await _hammer(base, token, args.concurrency, args.duration, args.mix)
            stop_evt.set()
            sent = await msg_task
            return sat, sent
        sat, sent = asyncio.run(saturated())

        report(idle, sat, args, sent)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        log.close()
        print(f"  Server log: {log_path}")
        cleanup()


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        assert pctl([1, 2, 3, 4, 5, 6, 7, 8, 9, 10], 50) == 5
        assert pctl([1, 2, 3, 4, 5, 6, 7, 8, 9, 10], 95) == 10
        assert math.isnan(pctl([], 50))
        print("selftest ok")
    else:
        main()
