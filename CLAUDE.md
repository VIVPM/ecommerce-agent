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

`test/evaluate_agent_tuned.py` writes after every case — if interrupted, just re-run
and it resumes. Delete `test/evaluation_results_tuned.json` to force a fresh run.

**`--calibrate` must target the DEPLOYMENT (`--base <render-url>`), not localhost.** Run
from a laptop and the number is dominated by your machine→Neon(us-east-1)+Gemini round
trips, not the app — measured ~3.5s p50 against Render vs ~20s+ locally for the same run.
`--calibrate` also spends real Gemini calls and counts against `DAILY_MESSAGE_CAP`, so keep
N small; `--ramp` (browse) is free and unaffected.

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
  which turns a routine tool upgrade into 100+ CI failures that flag nothing wrong.
  Don't drop the pin to "use the defaults"; that is the pin.

## Non-obvious architecture facts

- **Two SQLAlchemy engines**: read-write (`engine`) and a **forced read-only** one
  (`readonly_engine`) that runs all LLM-generated SQL — Postgres rejects writes at the
  session level, so injection can't mutate data. Pool is **30 per engine** (10 + 20
  overflow); raising it from 15 cut p95 at 100 concurrent from 19.2s → 7.7s.
- **All Gemini calls are `gemini-2.5-flash`**; Pro is only an error/rate-limit fallback.
  Flash matched and exceeded Pro's performance across the full 200-case evaluation suite.
- **LLM provider is swappable** via `LLM_MODEL` (`GEMINI` default, or `CLOUDFLARE` →
  `@cf/openai/gpt-oss-20b` over its OpenAI-compatible endpoint; value is upper-cased). All generation goes
  through `app/llm_provider.py` (`complete` / `stream` / `route_cloudflare`); each call
  site passes its Gemini model, ignored in cloudflare mode. **Embeddings ALWAYS run on
  Gemini** (Pinecone FAQ index is 1024-dim gemini-embedding-001), so `GEMINI_API_KEY` is
  required even in cloudflare mode. Caches key on question text, not provider — **purge
  them when switching providers** (`cache_purge('sql'/'faq'/'route')`).
- **The `llm_cache` table caches generated SQL / FAQ answers / routing / decompositions**
  — not rows, so results can't go stale. **After changing a prompt, purge it**:
  `cache_purge('sql')` after editing `sql_prompt`, `cache_purge('route')` after the
  routing instruction, `cache_purge('decompose')` after `_DECOMPOSE_SYS` in `agent.py`.
- **Multi-intent decomposition** (`agent.py: decompose`): a message spanning ≥2
  capabilities (products / policy / saved) is split into standalone sub-questions, each
  routed and streamed as its own labelled section in `main.py`'s event stream. A cheap
  regex pre-check (`_looks_multi_intent`) keeps the LLM split off the ~95% single-intent
  path — those pay nothing. **Don't route the whole compound query to one tool** — that
  was the bug this fixes.
- **The `/message` endpoint is credit-gated** by `DAILY_MESSAGE_CAP` (default 5, set 100
  locally). There is **no credits table**: `remaining = cap − messages sent since IST
  midnight`, counted from `chat_messages`. `GET /api/account/credits` reads it; a 429 is
  returned when it's exhausted. Change the cap in `.env`, not in code.
- **Cart + SIMULATED orders** (`app/orders.py`, tables `cart_items` / `orders` /
  `order_items`). Orders are a demo (COD, no payment/fulfilment) — labelled as such
  everywhere; this is an assistant over a scraped catalogue, not a store. `order_items`
  **snapshots title + price** at placement, so the nightly refresh can't rewrite a past
  order. The agent has **6 tools now** (the 3 original + `place_order` / `view_orders` /
  `cancel_order`); order actions return a **deterministic confirmation, no LLM tokens** —
  only the routing is an LLM call. Adding to cart is **UI-only** (the agent can't know
  which product you mean); there is **no stock-count column**, so cart quantity is fixed
  at 1 and the cart total is derived client-side. Every save/cart click is a
  Postgres-per-click write behind **optimistic UI** (state flips locally first, the write
  is backgrounded) — right at this scale; a Redis/NoSQL cart tier is a Part 4 concern.
- **The catalogue refresh runs nightly on GitHub Actions** (`.github/workflows/refresh.yml`,
  cron `30 18 * * *` = 00:00 IST) — `refresh_products.py --limit 500` oldest-first, so the
  ~3,600-row catalogue rotates ~weekly. It needs only the `DATABASE_URL` repo secret (no
  Gemini/Pinecone). Flipkart tarpits the runner IP, so ~15-20% of rows time out or 529 per
  run — absorbed by a retry loop; transient, not a failure. A NULL `title` is normalised at
  the top of `process()` so one bad row can't abort the whole run.
- **Duplicate seller listings are de-duped at display time, never in the DB** (`sql.py:
  _dedup_rows`, brand-aware `_dedup_key`). The query over-fetches `LIMIT n*2` then keeps the
  first `n` after dedup, so the count stays right. Keys on `brand|normalized_title` so
  different brands with generic titles ("Walking Shoes For Women") don't collapse together.
- **Shop-by-photo (multimodal)** (`app/vision.py`): an uploaded image → one Gemini 2.5
  Flash vision call → `{is_shoe, brand, product_type, gender}` → a phrase fed to the
  EXISTING `sql_chain` (no image embeddings — the catalogue is text). **Colour is
  deliberately dropped** (titles don't carry it) and **brand only if a logo is legible**
  (guessing made the same photo return different brands). A non-shoe → `is_shoe:false` →
  a "couldn't spot a shoe" reply, never a blind catalogue dump. `/message` takes optional
  `image` (base64) + `image_thumb` (small data-URI shown with the stored message after the
  `\n[[SHOEIMG]]` marker, stripped from history/search). An image always means product
  search, so routing/decompose are skipped.
- **Input guardrail** (`agent.py: is_off_topic`): off-topic messages (poems, weather) are
  refused before any tool runs. A keyword fast-path (`_looks_shopping`) lets obvious
  shopping through free; only ambiguous messages pay for a cached `SHOPPING`/`OFFTOPIC`
  classification. **Fails open** — a hiccup never blocks a real shopper.
- **Durable preferences** (`app/preferences.py`, table `user_preferences`, one NL summary
  per user): set / viewed / cleared **in chat** (keyword-gated, merged via one LLM call)
  OR in the sidebar **Preferences panel** (`GET/PUT/DELETE /api/preferences`). The summary
  is folded into product-search queries — the text-to-SQL honours "only Puma, under 3000".
  `main.py` catches preference messages before routing.
- **SQL robustness / compound counts** (`sql.py`): a malformed generated query returns a
  friendly message, never a crash (`run_query` catches, returns None). SQL is cached **only
  after it executes** — a bad query never poisons the cache. `run_query` and `_extract_sql`
  both accept a leading `(`, so **parenthesised UNIONs run** — that's how "4 Nike and 5 Puma"
  (and 3+ groups) work; per-branch `LIMIT`s are respected and neither `_run_sql_for_question`
  nor `_format_top_results` re-caps a compound result at 10 (safety ceiling 50).
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
