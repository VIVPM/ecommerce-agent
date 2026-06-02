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

`evaluate_agent.py` writes after every case — if interrupted, just re-run and it
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

## Non-obvious architecture facts

- **Two SQLAlchemy engines**: read-write (`engine`) and a **forced read-only** one
  (`readonly_engine`) that runs all LLM-generated SQL — Postgres rejects writes at the
  session level, so injection can't mutate data. Pool is **30 per engine** (10 + 20
  overflow); raising it from 15 cut p95 at 100 concurrent from 19.2s → 7.7s.
- **All Gemini calls are `gemini-2.5-flash`**; Pro is only an error/rate-limit fallback.
  Flash matched and exceeded Pro's performance across the full 200-case evaluation suite.
- **The `llm_cache` table caches generated SQL / FAQ answers / routing** — not rows, so
  results can't go stale. **After changing a prompt, purge it**: `cache_purge('sql')`
  after editing `sql_prompt`, `cache_purge('route')` after the routing instruction.
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
- **`upgrade-roadmap.txt` sorting**: Part 3 = needs an external service; Part 2 =
  everything else still open; **completed work moves to Part 1** — no `[DONE]` stubs left
  in Parts 2/3, so their length is the honest size of the backlog.
- **Docs must stay readable.** The user has pushed back on wall-of-text; prefer tight
  bullets and small tables over long paragraphs.
- **Reference project**: `D:\Data science\LLM projects\multi-crew-lead-coordinator` is
  the sibling this repo mirrors for infra decisions (observability, load testing, CI/CD,
  Docker, roadmap style). Check how it did something before inventing an approach.

## Quality signals

One unified suite:
- `test/evaluate_agent_tuned.py` — 200 cases, LLM-as-judge scoring routing, faithfulness, and relevance. 
  Provides hard regression detection. (Current scores: 100% routing, 4.78 faithful, 4.44 relevant).
