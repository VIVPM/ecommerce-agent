# CLAUDE.md — working notes for this repo

E-commerce shopping assistant: React + FastAPI + Gemini + Neon Postgres + Pinecone.
Deployed on Render. **This file is orientation + gotchas only** — architecture and
results live in `README.md`, and the full build log / backlog lives in
`upgrade-roadmap.txt` (gitignored, private). Read those before re-deriving anything.

## Commands

Always use the project venv (a bare `python` is a different interpreter):

```bash
# from repo root
./backend/.venv/Scripts/python.exe -m ruff check backend/ tests/   # lint (CI runs this)
./backend/.venv/Scripts/python.exe -m compileall -q backend tests  # syntax (CI runs this)
./backend/.venv/Scripts/python.exe tests/test_logging.py           # unit test (CI runs this)

# from backend/
python load_test.py --ramp --base <url>        # capacity ramp (add --levels 25,50,100)
python load_test.py --calibrate 3 --base <url> # real message latency (costs money)
python test/evaluate_agent_tuned.py            # 200-case LLM-judge eval; RESUMABLE
python grafana/provision.py --dry-run          # Grafana dashboard/alerts

# frontend/
npm run lint && npm run build                  # CI runs both
```

`test/evaluate_agent_tuned.py` writes after every case — if interrupted, just re-run and it
resumes. Delete `evaluation_results.json` to force a fresh run.

## Environment gotchas (these cost real time)

- **Windows console is cp1252.** Emoji/box-chars in script output crash with
  `UnicodeEncodeError`. Fix inside the script: `sys.stdout.reconfigure(encoding='utf-8')`.
  `PYTHONIOENCODING` does **not** fix an already-open stream.
- **The base Python at `E:\python` has an expired CA root.** Raw `urllib` HTTPS fails
  with `CERTIFICATE_VERIFY_FAILED`; `requests`/`httpx` work (they bundle certifi).
  `grafana/provision.py` builds its SSL context from `certifi` for this reason.
- **Pyright uses a different interpreter than the venv**, so "Import could not be
  resolved" for `langfuse`, `opentelemetry`, `app.*` etc. is **noise**, not a real error.
  The `Column[...]` type complaints in `main.py` are pre-existing SQLAlchemy noise too.
- **`load_dotenv()` must run before importing app modules** — `database.py` builds the
  engine at import time and needs `DATABASE_URL`. That's why ruff's **E402 is disabled**
  (`ruff.toml`); don't "tidy" those imports to the top. Notebooks are excluded from lint.
- **Neon's POOLER rejects `-c statement_timeout` in `connect_args`** ("unsupported
  startup parameter in options") and takes the app down at boot. Both engines set it
  with a post-connect `SET` instead — the same mechanism the read-only engine uses.
  Don't "tidy" it into connect_args.
- **`ruff.toml` pins `select = ["E4","E7","E9","F"]`** — ruff's documented default. Newer
  ruff versions widen the *implicit* default (blind-except, isort, bugbear, refurb),
  which turned a routine tool upgrade into 113 CI failures that flagged nothing wrong.
  Don't drop the pin to "use the defaults"; that is the pin.

## Non-obvious architecture facts

- **Two SQLAlchemy engines**: read-write (`engine`) and a **forced read-only** one
  (`readonly_engine`) that runs all LLM-generated SQL — Postgres rejects writes at the
  session level, so injection can't mutate data. Pool is **30 per engine** (10 + 20
  overflow); raising it from 15 cut p95 at 100 concurrent from 19.2s → 7.7s.
- **All Gemini calls are `gemini-2.5-flash`**; Pro is only an error/rate-limit fallback.
  Flash matched and exceeded Pro's performance across the full 200-case evaluation suite.
- **The agent is LangChain `create_agent`** (`app/agent.py`, LangGraph-backed). Three
  `@tool`s, all `return_direct=True` — their output is already shopper-ready markdown,
  so the agent returns it verbatim instead of paraphrasing it through a second model
  call. That is what keeps `_format_top_results`, the rating counts and the
  price-age / unsupported-filter notes intact, and it also makes every run
  single-hop. **The tool docstrings ARE the routing prompt** — the 200-case eval is
  calibrated on their exact wording, so edit them as prompt text and re-run the eval.
- **`user_id` rides in the agent's runtime context (`Ctx`), never as a tool argument.**
  As an argument the model could hallucinate one, or be talked into supplying someone
  else's, and read a stranger's shortlist. Verify with `compare_saved_products.args` —
  it must list `query` only.
- **Tools stream on LangGraph's custom channel** (`_emit` in `agent.py`), not by
  returning one blob, so the worker forwards one uniform status/token stream whether the
  text came from an LLM or from the deterministic formatter. A tool that returns early
  without going through `_drain` emits nothing and the client renders "I couldn't
  generate a response" — route every exit path through it.
- **The agent runs in a WORKER, not the request.** `POST /message` persists the
  question and a job in one transaction and returns **202 + job_id**; `app/worker.py`
  claims it (`FOR UPDATE SKIP LOCKED`) and appends to `job_events`. Observe with
  `GET /jobs/{id}` (poll) or `GET /jobs/{id}/events?after=<seq>` (SSE, replay-then-tail).
  Don't "simplify" this back into the request — surviving a dropped connection is the
  whole point.
- **One worker PROCESS, N concurrent slots.** In-process via `main.py`'s lifespan
  (Render bills $7/mo for a real background worker and its free web service sleeps);
  `python -m app.worker` is the same code as a separate service. Concurrency scales
  with queue depth between `WORKER_MIN_CONCURRENCY` and `WORKER_MAX_CONCURRENCY` —
  coroutine slots, not machines, which is the right unit when a job is almost
  entirely waiting on a model. The cap is bounded by the SQLAlchemy pool (30 per
  engine × 2), provider RPM and container memory; check all three before raising it.
  A second worker *process* still needs a paid plan and a Neon-ceiling recheck.
- **Jobs are claimed FAIRLY, not FIFO** (`jobs.claim_job`). Each waiting user's oldest
  job competes on when that USER was last served, so one user's burst can't park in
  front of everyone; with one user waiting it degrades to FIFO. **Do not "simplify"
  this to rank-within-the-queue** — that was the first attempt and it silently
  collapses back to FIFO, because once a user's first job is claimed their second
  becomes rank 1 again and wins the tie.
- **Never re-run a job that already emitted output.** `jobs.emitted` gates it: nothing
  streamed → requeue; anything streamed → fail. Both `reap_expired` (crash) and
  `release_job` (shutdown) follow that rule. Re-running bills twice for one answer.
- **Event writes are QUEUED, not awaited inline** (`_Emitter._drain`). Awaiting the
  INSERT in the token path made generation as slow as the database — gpt-oss emits
  slower than the flush window, so the timer fired on nearly every token and one
  comparison took 364s across 324 one-token writes. Queued: 25s, 7 chunks. Don't
  put an `await` back in `token()`.
- **Token spend is metered per job** via one `get_usage_metadata_callback` wrapping
  the whole run, so the rewrite, the routing call and the tool are all counted without
  threading a callback through call sites. **`tokens_used == 0` is a cache-hit signal,
  not a broken counter** — cached sql/faq/route answers make no model call, and
  `optimize_query` short-circuits on empty history.
- **`JOB_HARD_LIMIT_S` must stay below `jobs.LEASE_SECONDS`**, or the reaper fires
  first and a timeout looks like a crash. It's a real `asyncio.timeout`, not a check
  between chunks — a provider that hangs *before* its first chunk would never reach
  an in-loop test.
- **Route caching lives in `@wrap_model_call` middleware**, not in the caller. A hit
  returns a synthetic tool call so the model is never invoked, while the tool still
  executes and streams normally.
- **A circuit breaker fails over between providers** (`llm_provider`): 3 consecutive
  job failures opens it for 60s and the other provider takes over. Pinned per JOB in
  `worker.execute` — never mid-answer, or one reply is spliced from two models. When
  both are open the worker stops CLAIMING rather than burning attempts. `create_agent`
  binds its model once at import, so `_route` middleware re-resolves it per call —
  without that override the tools would fail over but routing would not.
- **Per-step model tiers**: the query rewrite runs on `gemini-2.5-flash-lite`, routing
  and generation on flash. `ROUTING_MODEL` is env-switchable but **stays on flash** —
  the 200-case eval is calibrated on it and routing accuracy is what regresses first.
- **`MAX_SQL_ROWS` caps the result set** by wrapping the generated SQL. Only 10 rows
  are ever shown, but the whole set was being materialised into pandas. Side effect:
  the "showing 10 of N" count saturates at the cap.
- **LLM provider is swappable** via `LLM_MODEL` — **required, no default** (`GEMINI` or
  `CLOUDFLARE` → `@cf/openai/gpt-oss-20b`; value is upper-cased). The two are
  interchangeable peers, not primary-and-backup: same agent, same tools, same
  streaming, native tool-calling on both. All generation goes through
  `app/llm_provider.py` (`chat` / `complete` / `stream`); `chat()` is what
  `create_agent` binds tools to. Each call site passes its Gemini model, ignored in
  cloudflare mode. **Embeddings ALWAYS run on Gemini** (Pinecone FAQ index is 1024-dim
  gemini-embedding-001), so `GEMINI_API_KEY` is required even in cloudflare mode.
  Caches key on question text, not provider — **purge them when switching providers**
  (`cache_purge('sql'/'faq'/'route')`).
- **The `llm_cache` table caches generated SQL / FAQ answers / routing** — not rows, so
  results can't go stale. **After changing a prompt, purge it**: `cache_purge('sql')`
  after editing `sql_prompt`, `cache_purge('route')` after editing `agent_instruction`
  **or any tool docstring** — the docstrings are what the model routes on.
- **Compare is never cached.** The sql/faq caches key on question text alone, so caching
  "compare my saved" would serve one user's shortlist to another. That's a privacy bug,
  not staleness — leave it uncached.
- **Observability uses no `langfuse` package.** An openinference instrumentor emits LLM
  spans onto one OTel provider exporting to Langfuse's OTLP endpoint *and* Grafana; a
  separate provider sends FastAPI HTTP spans to Grafana only. All fail-open and off
  unless env vars are set.

## Decisions — do not re-litigate

- **Never add rule-based routing.** The LLM choosing the tool is what makes this an
  agent; the user has explicitly rejected replacing it. (UI-layer suggestions are fine —
  that's presentation, not decision-making.)
- **`AgentExecutor` was considered and rejected.** LangChain 1.0 replaced every chain
  and agent with `create_agent`; `AgentExecutor` survives only in the `langchain-classic`
  compat package and offers nothing `create_agent` doesn't — while costing a deprecated
  dependency and `ToolRuntime` context injection. Don't "restore" it.
- **Don't drop `return_direct` to make the agent multi-step** without re-running the
  eval. It is the only thing stopping a second model call from paraphrasing away the
  verified product formatting. Single-hop is the deliberate trade.
- **Follow-up chips are a static map, on purpose.** `FOLLOW_UPS` in `ChatArea.jsx` is
  keyed by the tool the backend reports on the `done` event. Don't "upgrade" it to an
  LLM call — that adds cost + latency to every message for no gain, and only
  verifiably-supported queries may be suggested.
- **No discount column.** Dropped: the JSON-LD source has no MRP, so a discount can never
  be verified. Don't reference or re-add it.
- **"Top rated" is a Bayesian rank**, not `ORDER BY avg_rating` — a 4.7-from-50 must not
  beat a 4.6-from-500.
- **Unsearchable filters are refused, not faked.** If colour/size/width is the *only*
  filter, the model emits `WHERE 1=0` and the caller explains. Never dump the catalogue.
- **Grafana alerts are deliberately MUTED** (`MUTE_ALERTS=True`) — the "no messages" rule
  spams on a low-traffic demo. The rules exist and evaluate; they just don't email.
- **Sentry is out of scope** (the reference project doesn't use it).
- **Prompt caching is deliberately NOT implemented**, and that's measured rather than
  assumed: `jobs.cached_tokens` reads 0 on every job on both providers, and both
  integrations genuinely do report cache reads. Gemini's implicit minimum is 2,048
  input tokens and most calls here fall below it; explicit caching bills storage per
  token-hour (a loss on sporadic traffic) and helps only one of two providers. Never
  pad a prompt to clear the threshold.
- **Don't add Redis yet.** Postgres `SKIP LOCKED` is the queue. It creaks past ~4
  workers or a high poll rate — that is the trigger, not a hunch.

## Shared resources — be careful

- **Neon**: this app owns the **`ecommerce_agent`** database. The older `neondb` is
  **shared with other apps** (expense tracker etc.) — never drop/alter tables there.
- **Grafana Cloud (`calmcarriage2405`) is shared with the leads-coordinator.** Keep
  changes additive; the notification policy is read-modify-**write** so coordinator
  routes survive. `service.name` separates the two apps' telemetry.
- **`backend/app/.env` holds live secrets** and is gitignored — never commit or echo it.
- Load tests create a `loadtest_user` on the target (including **prod**) and clean it up
  afterwards; `--cleanup` fixes a crashed run.

## Conventions

- **Commits**: no `Co-Authored-By` trailer, and the history is deliberately backdated.
  Match the existing style (`git log`) rather than introducing a new one.
- **`upgrade-roadmap.txt` sorting** (4 parts, by purpose — the file states its own
  rules at the top; follow those): Part 1 = what we actually built, where **completed
  work lands**; Part 2 = product gaps hurting UX; Part 3 = production readiness;
  Part 4 = scaling to 10k+, where every remaining item carries the TRIGGER that
  would justify building it (`[--]` marks ones deliberately unbuilt). No `[DONE]`
  stubs left in Parts 2–4, and if a part has
  nothing outstanding it says so rather than padding — their length is the honest
  size of the backlog.
- **Docs must stay readable.** The user has pushed back on wall-of-text; prefer tight
  bullets and small tables over long paragraphs.
- **Reference project**: `D:\Data science\LLM projects\multi-crew-lead-coordinator` is
  the sibling this repo mirrors for infra decisions (observability, load testing, CI/CD,
  Docker, roadmap style). Check how it did something before inventing an approach.

## Quality signals

**Load test numbers are PER BRANCH — never copy them between branches.** Each branch
measures its own; `load_test.py` stubs the LLM so a run is free. This branch, local,
15 messages @2s + 30 browse clients, two consecutive runs: `/chats` p95 x1.0 both
(the queue working), `/saved` x13.9 and x12.0, `/health` x1.3 and x64.2, throughput
32->12 and 32->11 req/s, 0 errors, all 15 messages completed in both runs.
- **Real calibration (2026-09-03)**: 3 sequential messages, 25.1s cold then 13.7s / 13.1s
  route+SQL cache hits; 2,167 input + 259 output tokens total, estimated $0.001298.
  `--calibrate` waits for each durable job to succeed and saves latency/usage JSON.
  Full completion includes queue/DB/poll overhead; it is not generation-only latency.
  Reproduced 2026-09-04 on a purged cache: 28.0s / 14.8s / 12.9s, same 2,167 in + 260
  out, $0.0013. Two runs a day apart agreeing is the signal; the numbers barely moved.
- **`--calibrate`'s p50 is a CACHE-HIT number, not "real message latency".** Message 1
  warms the sql+route cache, so only it calls the model (per-job `tokens_used`: 2167/260,
  then 0, then 0) and the cold path lands in `max`, not the median. Purging first
  (`cache_purge('sql'/'route')`) resets message 1 only — it does not make all three
  uncached. Quote cold and warm separately or the doc says something false.
- **Capacity ramp (local, `--ramp` on `:8031`, free — no LLM in the browse path)**:
  5 clients 907ms p50 / 1359ms p95 / 5 req/s; 15 -> 921 / 1766 / 20; 30 -> 922 / 1234 /
  41; 50 -> 1484 / 2719 / 41. **0 errors at every level**; the 429s (11/88/187/150) are
  the login rate-limit working and are counted apart from errors. p50 is flat to 30
  clients, then throughput pins at 41 req/s while latency climbs — that is the knee.
  Absolute ms are dominated by laptop->Neon round trip; read the shape.
- **`load_test.py` must stub the WORKER, not `main`.** The agent runs in the worker now,
  so it patches `worker.astream_agent` / `worker.optimize_query` and lifts both
  DAILY_MESSAGE_CAP and MAX_ACTIVE_JOBS. It also drives the job API (202 -> tail
  `/jobs/{id}/events`), not the old streaming POST. Patching `main.*` would silently do
  nothing and a supposedly free run would call real models.
- **DB-backed reads degrade consistently; threadpool starvation is intermittent.**
  `/saved` degraded x12.0-x13.9 in both runs. `/health` does no I/O but ranged from x1.3
  to x64.2: it is a sync `def`, so Starlette runs it in the threadpool that every SSE tail
  hits via `asyncio.to_thread(jobs.read_events)` each `JOB_EVENT_POLL_S`. A favorable run
  can hide the bursty starvation. Fix order: make trivial sync endpoints `async def`,
  reduce or replace event polling, re-run, and only then tune worker concurrency.


One unified suite:
- `test/evaluate_agent_tuned.py` — 200 cases, LLM-as-judge scoring routing, faithfulness, and relevance. 
  Provides hard regression detection. (Current scores: 100% routing, 4.78 faithful, 4.44 relevant).
