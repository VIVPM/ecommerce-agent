"""
load_test.py — does the API stay responsive while the expensive /message path
is saturated?

This app has no worker queue (the coordinator's load_test.py drained one; there's
no analog here). The one thing worth load testing is the shape it DOES have:
`send_message` is `async def`, streams the LLM answer on the event loop, and
offloads the sync prefix (query rewrite, routing) and the DB save to threads via
asyncio.to_thread. So a burst of concurrent streaming messages could starve the
thread pool / event loop and make ordinary browsing (health, chats, saved) slow.
That's an empirical question:

    idle phase      -> hammer the browse mix with nothing streaming
    saturated phase -> hammer it again with N messages streaming continuously

and compare the RATIO. The LLM boundary is stubbed, so a run costs nothing and
takes seconds — read the idle-vs-saturated ratio, not absolute ms (--msg-seconds
scales the simulated generation time).

    python load_test.py                      # from backend/
    python load_test.py --messages 15 --concurrency 30
    python load_test.py --calibrate 3        # 3 REAL messages, measures true latency/cost (~cents)
    python load_test.py --cleanup            # if a run died mid-way

Login is included but is rate-limited (10/min), so its 429s are counted as
"throttled" (the limiter working), not errors.
"""
import argparse
import asyncio
import json
import math
import os
import subprocess
import sys
import time
from datetime import datetime, timezone

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

from dotenv import load_dotenv
load_dotenv(dotenv_path=os.path.join(BASE_DIR, "app", ".env"))

import httpx

LOAD_USER = "loadtest_user"
LOAD_PASS = "LoadTest-pw-9137"
Q = "cheapest running shoes for men"


def pctl(xs, p):
    """Nearest-rank percentile (small n — interpolation would invent precision)."""
    if not xs:
        return float("nan")
    xs = sorted(xs)
    rank = max(1, math.ceil(p / 100 * len(xs)))
    return xs[min(rank, len(xs)) - 1]


# =============================================================================
# Serve mode — the real FastAPI app with the LLM boundary stubbed
# =============================================================================

def serve_mode(port, msg_seconds):
    """Run the real app, stubbing only the LLM calls in the message path.
    `main` binds these names at import, and the handler calls them by those
    names, so patching them on `main` is what the request path picks up."""
    # Keep synthetic traffic out of Langfuse/Grafana (and skip their startup).
    for k in ("LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY",
              "GRAFANA_OTLP_ENDPOINT", "GRAFANA_OTLP_AUTH"):
        os.environ.pop(k, None)

    import main

    def stub_optimize(query, history=None):
        return query

    def stub_route(_q):
        return ("search_product_database", _q)

    async def stub_stream(*_a, **_k):
        await asyncio.sleep(msg_seconds)      # simulate generation latency
        for tok in ("Here ", "are ", "the ", "results."):
            yield tok

    main.optimize_query = stub_optimize
    main.route_query = stub_route
    main.sql_chain_stream_async = stub_stream
    main.faq_chain_stream_async = stub_stream
    main.compare_saved_stream_async = stub_stream

    import uvicorn
    uvicorn.run(main.app, host="127.0.0.1", port=port, log_level="error")


# =============================================================================
# Clients
# =============================================================================

READ_MIX = (
    ("health", "GET", "/api/health", False),
    ("chats", "GET", "/api/chats", True),
    ("saved", "GET", "/api/saved", True),
    ("login", "POST", "/api/auth/login", None),   # None -> login body, rate-limited
)


async def _hammer(base, token, concurrency, duration):
    """Fire the browse mix and record every latency. 429 on login = throttled."""
    lat = {k[0]: [] for k in READ_MIX}
    errors = {"count": 0, "samples": []}
    throttled = 0
    stop = time.monotonic() + duration
    auth = {"Authorization": f"Bearer {token}"}

    async def one(client):
        nonlocal throttled
        while time.monotonic() < stop:
            for name, method, url, needs_auth in READ_MIX:
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

    async with httpx.AsyncClient(base_url=base) as client:
        await asyncio.gather(*[one(client) for _ in range(concurrency)])
    return {"lat": lat, "errors": errors, "throttled": throttled}


async def _message_load(base, token, chat_id, n_streamers, stop_evt):
    """N clients continuously POST /message and drain the SSE stream."""
    auth = {"Authorization": f"Bearer {token}"}
    sent = 0

    async def loop(client):
        nonlocal sent
        while not stop_evt.is_set():
            try:
                async with client.stream("POST", f"/api/chats/{chat_id}/message",
                                         json={"query": Q, "history": []},
                                         headers=auth, timeout=60) as r:
                    async for _ in r.aiter_lines():
                        pass
                sent += 1
            except Exception:
                pass

    async with httpx.AsyncClient(base_url=base) as client:
        await asyncio.gather(*[loop(client) for _ in range(n_streamers)])
    return sent


# =============================================================================
# Setup / teardown
# =============================================================================

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
    # Retry: a remote free-tier instance can drop the first connection (cold start)
    # or briefly rate-limit signup/login. Don't let a transient blip kill the run.
    last = None
    for _ in range(5):
        try:
            httpx.post(f"{base}/api/auth/signup",
                       json={"username": LOAD_USER, "password": LOAD_PASS}, timeout=30)
            r = httpx.post(f"{base}/api/auth/login",
                           json={"username": LOAD_USER, "password": LOAD_PASS}, timeout=30)
            if r.status_code == 200:
                return r.json()["token"]
            last = f"{r.status_code} {r.text[:120]}"
        except Exception as e:
            last = f"{type(e).__name__}: {e}"
        time.sleep(2)
    sys.exit(f"Could not auth the load-test user after retries: {last}")


def create_chat(base, token):
    r = httpx.post(f"{base}/api/chats/new",
                   headers={"Authorization": f"Bearer {token}"}, timeout=30)
    r.raise_for_status()
    return r.json()["chat_id"]


def cleanup():
    """Delete the load-test user and all its rows straight from the DB."""
    from sqlalchemy import text
    from app.db.database import engine
    with engine.begin() as c:
        uid = c.execute(text("SELECT id FROM ecommerce_accounts WHERE username=:u"),
                        {"u": LOAD_USER}).scalar()
        if uid is None:
            print("nothing to clean up.")
            return
        for tbl, col in (("chat_messages", "user_id"), ("saved_products", "user_id"),
                         ("chat_sessions", "user_id")):
            c.execute(text(f"DELETE FROM {tbl} WHERE {col}=:u"), {"u": uid})
        c.execute(text("DELETE FROM ecommerce_accounts WHERE id=:u"), {"u": uid})
    print(f"cleaned up load-test user {LOAD_USER} (id {uid}) and its rows.")


# =============================================================================
# Report
# =============================================================================

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
    for name, *_ in READ_MIX:
        print(_row(name, idle["lat"][name], sat["lat"][name]))

    n_idle = sum(len(v) for v in idle["lat"].values())
    n_sat = sum(len(v) for v in sat["lat"].values())
    print(f"\n  Browse throughput  {n_idle / args.duration:.0f} -> {n_sat / args.duration:.0f} req/s")
    print(f"  Messages streamed  {sent} during the saturated phase")
    print(f"  Throttled (429)    {idle['throttled']} idle, {sat['throttled']} saturated (login rate-limit)")
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


# =============================================================================
# Calibrate — a few REAL messages, to check the stub's timing model
# =============================================================================

def calibrate(n, base):
    """Send N real messages through the running app (real Gemini + Pinecone) and
    report actual latency. The only part that costs money — a few Flash calls each."""
    token = ensure_user(base)
    chat_id = create_chat(base, token)
    auth = {"Authorization": f"Bearer {token}"}
    took = []
    print(f"Sending {n} real message(s) through {base} ...")
    for i in range(n):
        t = time.monotonic()
        with httpx.stream("POST", f"{base}/api/chats/{chat_id}/message",
                          json={"query": Q, "history": []}, headers=auth, timeout=120) as r:
            for _ in r.iter_lines():
                pass
        dt = time.monotonic() - t
        took.append(dt)
        print(f"  message {i + 1}: {dt:.1f}s")
    print("\n" + "=" * 60)
    print(f"REAL MESSAGE LATENCY  (n={n})")
    print("=" * 60)
    print(f"  p50 {pctl(took, 50):.1f}s   p95 {pctl(took, 95):.1f}s   "
          f"min {min(took):.1f}s   max {max(took):.1f}s")
    print("\n  Feed a value near p50 to --msg-seconds for a realistic (unscaled) run.")
    cleanup()


# =============================================================================
# Ramp — find the browse-capacity knee against a real server (e.g. Render)
# =============================================================================

def ramp_mode(base, levels, duration):
    """Ramp browse concurrency and watch where p95 climbs or errors appear — that's
    the instance's capacity knee. Only the browse mix is ramped: /message is
    rate-limited to 30/min per IP, so message concurrency can't be measured
    honestly from one machine (use --calibrate for real per-message latency)."""
    print(f"RAMP against {base}  (browse mix: health + chats + saved)\n")
    token = ensure_user(base)
    create_chat(base, token)
    print(f"  {'clients':>7} | {'browse p50':>11} {'browse p95':>11} | {'req/s':>6} | {'err':>4} {'429':>5}")
    print("  " + "-" * 62)
    rows = []
    for c in levels:
        res = asyncio.run(_hammer(base, token, c, duration))
        browse = res['lat']['health'] + res['lat']['chats'] + res['lat']['saved']
        p50, p95 = pctl(browse, 50), pctl(browse, 95)
        n = sum(len(v) for v in res['lat'].values())
        print(f"  {c:>7} | {p50:>9.0f}ms {p95:>9.0f}ms | {n / duration:>6.0f} | "
              f"{res['errors']['count']:>4} {res['throttled']:>5}")
        rows.append({"clients": c, "browse_p50_ms": round(p50), "browse_p95_ms": round(p95),
                     "req_s": round(n / duration), "errors": res['errors']['count'],
                     "throttled": res['throttled'], "err_samples": res['errors']['samples'][:3]})
    out_dir = os.path.join(BASE_DIR, "load_test_results")
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, f"ramp_{datetime.now().strftime('%Y-%m-%d_%H-%M-%S')}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"base": base, "duration": duration, "levels": rows}, f, indent=2)
    print(f"\n  Report: {path}")
    cleanup()


# =============================================================================
# Main
# =============================================================================

def _spawn_server(port, msg_seconds):
    """Spawn the real app with the LLM stubbed, in a subprocess. Returns (proc, log)."""
    env = dict(os.environ)
    for k in ("LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY",
              "GRAFANA_OTLP_ENDPOINT", "GRAFANA_OTLP_AUTH"):
        env.pop(k, None)
    os.makedirs(os.path.join(BASE_DIR, "load_test_results"), exist_ok=True)
    log = open(os.path.join(BASE_DIR, "load_test_results", "server.log"), "w", encoding="utf-8")
    proc = subprocess.Popen(
        [sys.executable, os.path.abspath(__file__), "--serve",
         "--port", str(port), "--msg-seconds", str(msg_seconds)],
        env=env, cwd=BASE_DIR, stdout=log, stderr=subprocess.STDOUT)
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
    ap.add_argument("--serve", action="store_true")
    ap.add_argument("--cleanup", action="store_true")
    args = ap.parse_args()

    if args.ramp:
        levels = [int(x) for x in args.levels.split(",")]
        if args.base:                      # ramp a remote server (e.g. Render)
            return ramp_mode(args.base, levels, args.duration)
        base = f"http://127.0.0.1:{args.port}"   # else spawn a local stubbed server
        proc, log = _spawn_server(args.port, args.msg_seconds)
        try:
            if not wait_for_health(base):
                sys.exit("app did not come up — see load_test_results/server.log")
            print(f"local app up on {base} (LLM stubbed)")
            ramp_mode(base, levels, args.duration)
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
            log.close()
        return

    if args.serve:
        return serve_mode(args.port, args.msg_seconds)
    if args.cleanup:
        return cleanup()
    if args.calibrate:
        return calibrate(args.calibrate, args.base or "http://127.0.0.1:8000")

    base = f"http://127.0.0.1:{args.port}"
    env = dict(os.environ)
    for k in ("LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY",
              "GRAFANA_OTLP_ENDPOINT", "GRAFANA_OTLP_AUTH"):
        env.pop(k, None)
    os.makedirs(os.path.join(BASE_DIR, "load_test_results"), exist_ok=True)
    log_path = os.path.join(BASE_DIR, "load_test_results", "server.log")
    log = open(log_path, "w", encoding="utf-8")
    proc = subprocess.Popen(
        [sys.executable, os.path.abspath(__file__), "--serve",
         "--port", str(args.port), "--msg-seconds", str(args.msg_seconds)],
        env=env, cwd=BASE_DIR, stdout=log, stderr=subprocess.STDOUT)
    try:
        if not wait_for_health(base):
            sys.exit("app did not come up — see load_test_results/server.log")
        print(f"app up on {base} (LLM stubbed)")
        token = ensure_user(base)
        chat_id = create_chat(base, token)

        print(f"Phase 1/2: idle, {args.concurrency} browse clients for {args.duration}s...")
        idle = asyncio.run(_hammer(base, token, args.concurrency, args.duration))

        print(f"Phase 2/2: saturated, {args.messages} messages streaming + browse load...")
        async def saturated():
            stop_evt = asyncio.Event()
            msg_task = asyncio.create_task(_message_load(base, token, chat_id, args.messages, stop_evt))
            await asyncio.sleep(2)  # let the message load ramp up
            sat = await _hammer(base, token, args.concurrency, args.duration)
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
