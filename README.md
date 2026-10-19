# 🛒 E-Commerce Agent (React + FastAPI)

An intelligent AI-powered e-commerce assistant built with a modern **React** frontend and **FastAPI** backend. Features agentic reasoning, secure authentication, and a clean **light** UI shared by the landing page and the app.

---

## 🚀 Key Features

- **Agentic reasoning** — a **LangChain agent** (`create_agent`) in which the LLM, not rules, routes each message to four tools: product search (text-to-SQL), FAQ (RAG), the saved-items shortlist, and the cart and orders. Stated preferences are remembered automatically after every message. It may also call **no tool at all** and simply reply — greetings and opinions are conversation, not searches. One message may need several tools (a search and a policy question, a search and "add the first one"); the agent calls them one at a time, and each tool's verified output reaches the shopper verbatim instead of being paraphrased.
- **Swappable LLM provider** — `LLM_MODEL` chooses the app-paid default (Gemini 2.5 Flash or Cloudflare Workers AI `@cf/openai/gpt-oss-20b`); a shopper can select either using their own credentials. The same agent, tools and streaming work on both. Embeddings stay on Gemini (the Pinecone index is 1024-dim `gemini-embedding-001`).
- **Shop by photo** — upload a picture of a shoe and get similar ones from the catalogue. One vision call turns the photo into a search phrase and feeds the existing text-to-SQL search; there is no image index. It checks *"is this even a shoe?"* first, so a random photo is refused rather than answered with invented products, and a brand is read only from a legible logo — never guessed. The same photo always returns the same result (cached on the image bytes).
- **Cart & simulated orders** — add from a chat answer, place an order, cancel it. Placing refuses outright if something has gone out of stock rather than dropping it silently, and each order stores its own titles and prices so the nightly catalogue refresh can never rewrite what you bought.
- **Long-term memory across chats** — say *"remember I prefer Puma under 3000"* and it is stored in **Supermemory**, scoped per user, then recalled in *any* later conversation and folded into the search. Distinct from the 6-message rewrite, which only resolves the immediate follow-up. It also says plainly when it remembers **nothing** about you rather than offering to go and look — silence there is what makes a model invent a capability it doesn't have. Fail-open: with no key, the app behaves exactly as before, minus the memory.
- **It can act, not just answer** — save items from a result list (*"save 2"*), remove them, add to the cart, place an order and cancel one. Destructive actions fail safe: an unnamed *"cancel my order"* asks which, and an unrecognised action falls back to read-only.
- **Honest about stock** — searches only ever recommend what can be bought, but "nothing found" is reserved for things that genuinely aren't there. If matches exist and are merely unavailable it says so, and if you asked for 5 and only 4 are in stock it shows 4 and explains the fifth — rather than padding the list with a repeat to hit the number.
- **Stays on topic** — poems, weather and general knowledge are refused before any tool runs. Obvious shopping messages skip the check entirely, so it costs nothing on normal traffic.
- **Ask for several groups at once** — "4 Nike and 5 Puma shoes", or three brands, or five. Each group becomes its own query branch, so you get the counts you asked for rather than a truncated list.
- **Multi-part questions** — ask two things at once ("what's your return policy *and* show me Nike under 3000") and both get answered. The message is split into its separate intents first, each runs through the agent on its own, and the answers stream back in order. A single request with several filters stays one query.
- **Streaming responses** — answers stream token-by-token over SSE with live progress, so there's no spinner-wait.
- **Fair scheduling under load** — jobs are claimed by which *user* waited longest since their last turn, not first-come-first-served, so one person's burst can't block everyone. Worker concurrency scales with queue depth inside the one process.
- **Survives a provider outage** — a circuit breaker opens after repeated failures and the other provider takes over automatically (pinned per job, never mid-answer). If both are down the worker stops claiming rather than burning attempts.
- **Answers survive the connection** — the agent runs as a queued **job**, not inside the HTTP request. Close the tab, lose signal or redeploy mid-answer and the work continues; reconnecting replays from the last event and tails the rest. Jobs can be polled or cancelled.
- **Spend is bounded by tokens, not messages** — every job records input/output/cached tokens, and a daily token budget is the real cap when using the app's key. Rate limits are keyed on identity (not IP), and every `429` carries `Retry-After`.
- **Per-shopper provider choice** — free messages use the app's configured provider; in Settings a shopper can select Gemini with their own API key or Cloudflare Workers AI with their own Account ID and API token. Keys and tokens are validated and encrypted in Postgres; the Account ID is stored as an identifier. The selected user-paid provider removes daily message/token credits; Gemini still handles FAQ embeddings and image recognition.
- **Context memory** — `gemini-2.5-flash` rewrites follow-ups like *"any cheaper?"* into standalone queries from recent history.
- **Follow-up suggestions** — 2–3 tappable chips under each answer, picked from which tool replied. They surface capabilities a blank input box hides (relative comparisons, compare-saved) and, after a refused search, steer to queries that work. Derived from the routing decision, so they add no LLM call, cost or latency.
- **Save, compare & price alerts** — shortlist products from a chat answer, ask the agent to compare them, and see price drops since you saved (a live join, no scheduler).
- **Live product data** — `refresh_products.py` / `discover_products.py` re-check prices/stock and find new listings from Flipkart's schema.org JSON-LD (no browser); out-of-stock items are filtered out.
- **Honest by construction** — "top rated" uses a confidence-weighted (Bayesian) rank so a 4.7-from-50 can't beat a 4.6-from-500 (a rank, never a `total_ratings` cut-off — a floor would answer "nothing found" to "rated above 4.5" while real matches sat in the catalogue); filters the data can't support (colour, size) are refused, not faked; nothing is invented that isn't in the catalogue.
- **Postgres-backed LLM cache** — caches generated SQL, FAQ answers and routing decisions; survives restarts, shared across instances, fail-open.
- **Optional observability** — OpenTelemetry LLM traces to Langfuse and JSON logs correlated by `request_id`. Tracing is off unless configured and fails open.
- **Secure auth** — JWT + bcrypt with password-strength rules, plus a DB-backed login lockout (5 wrong passwords / 15 min) that holds across instances, on top of rate limits keyed on the authenticated user where there is one and the IP otherwise (5/min signup · 10/min login · 30/min messages).
- **Safe by default** — LLM-generated SQL runs on a read-only engine (injection-proof); Pydantic validates every input; concurrent chat writes take a row-level lock; consistent JSON errors; structured logging, no `print()`s.
- **Cloud-native data** — Neon Postgres (chat history, catalogue, saved products in a dedicated `ecommerce_agent` DB) + Pinecone (FAQ vectors, Gemini 1024-dim embeddings).
- **Polished frontend** — responsive React chat UI, plus a landing page with a live streaming demo, scroll animations, and session state that survives refresh. One design system throughout: the palette lives on `:root` in `index.css` and both the marketing page and the app consume it, so there is a single place to restyle the product.
- **Quality tracking** — a 160-question evaluation (`evals/run_eval_v2.py`, DeepEval GEval) (see [Evaluation Results](#-evaluation-results)).

---

## 🏗️ Architecture

Five layers, read top to bottom. Each arrow is a hand-off between layers; the
shared services (data, external AI) are reached once per layer rather than by
every tool, so the flow stays legible. Caching and observability are cross-cutting.

```mermaid
graph TD
    User(["👤 Shopper"])

    subgraph CLIENT ["1 · Client layer — React / Vite"]
        UI["💬 Chat UI · streamed answers · save · compare"]
        Land["🛬 Landing page"]
    end

    subgraph APP ["2 · Application layer — FastAPI (main.py)"]
        Auth["🔐 Auth · bcrypt · JWT · login lockout · identity rate-limit"]
        REST["🗂️ POST /message → 202 + job_id · GET /jobs/{id}/events (SSE)<br>poll · cancel · saved products"]
        WORK["⚙️ Worker · claims from the Postgres queue<br>lease · reaper · token metering (worker.py)"]
        REST -->|enqueue job| WORK
    end

    subgraph AI ["3 · Reasoning layer — LangChain agent"]
        Mem["🧠 Memory · rewrite query from history (memory.py)"]
        Route["🧭 create_agent · LLM picks tools in turn, or none<br>verbatim tool output · runtime ctx (agent.py)"]
        Mem --> Route
        Route --> SQL["📊 Text-to-SQL · read-only engine (sql.py)"]
        Route --> FAQ["📚 FAQ · RAG (faq.py)"]
        Route --> CMP["⚖️ Compare saved · user-scoped (compare.py)"]
    end

    subgraph DATA ["4 · Data layer — Neon Postgres + Pinecone"]
        PG[("Postgres · product · chat_sessions/messages<br>ecommerce_accounts · saved_products · llm_cache<br>jobs · job_events")]
        Pine[("Pinecone · FAQ vectors")]
    end

    subgraph EXT ["5 · External AI services — app default or shopper choice"]
        Gem["☁️ Google Gemini · 2.5 Flash<br>routing / SQL / FAQ / compare"]
        CF["☁️ Cloudflare Workers AI · gpt-oss-20b<br>routing / SQL / FAQ / compare"]
        Emb["🔢 Gemini embeddings · always, either way"]
    end

    Cache["🗄️ Caching · cross-cutting<br>Postgres llm_cache · SQL / FAQ / routing"]
    OBS["📈 Observability · cross-cutting<br>Langfuse LLM traces + request logs"]

    User --> CLIENT
    CLIENT -->|HTTP + JWT| APP
    APP -->|auth · sessions · saved| DATA
    WORK -->|optimized query| AI
    SQL -->|read-only SQL| PG
    FAQ -->|semantic search| Pine
    CMP -->|saved + live prices| PG
    Route -->|generate · embed| EXT
    AI -.->|hit / miss| Cache
    APP -.->|request logs| OBS
    AI -.->|LLM traces| OBS
```

The reasoning layer is a LangChain agent (`create_agent`) that routes each message
to four tools via the LLM, one at a time, (no rule-based routing — the model chooses,
which is what keeps it an *agent*). The tool docstrings *are* the routing prompt:

| Tool | Module | Routed when |
|---|---|---|
| Product search (text-to-SQL) | `sql.py` | shopper asks about products — price, brand, rating, stock, "cheaper than X" |
| FAQ (RAG) | `faq.py` | shopper asks about store policy — delivery, returns, payment, cancellation |
| Saved items (`add` / `remove` / `compare`) | `compare.py` | anything about their shortlist — *"save 2"*, *"remove the Puma"*, *"which of my saved is best"* |
| Cart & orders (`add_results_to_cart` / `add_to_cart` / `place` / `cancel` / `view`) | `orders.py` | the cart, placing or cancelling an order, or what they have ordered before |
| *(no tool)* | — | greetings, thanks, opinions, questions about themselves — ordinary conversation |

Actions are grouped into one tool each because they are the same kind of action on
the same data. Saved items and orders stay **separate** on purpose: merging them is
what makes a model confuse *"which saved shoe is best?"* with *"buy it"*.

Four design points worth knowing:

- **Tool output is shown verbatim; the model never re-types it.** Product lists,
  rating counts, price-age and unsupported-filter notes stream straight to the
  shopper. The model only decides the next tool, one at a time, and may add one short
  linking sentence, which is dropped if it restates prices, ratings or links. A
  single request stops after its tool with no extra model call. A cart action in the
  same message resolves only against that message's results, and asks when there are
  none.
- **`user_id` travels in the agent's runtime context, never as a tool argument** —
  so the model cannot hallucinate one, or be talked into supplying someone else's,
  and read a stranger's shortlist. The **raw message** and the transcript ride
  there too: *"save 2"* means the second row of the list on screen, and neither
  the number nor the list survives the history-aware rewrite.
- **A tool is optional.** When nothing fits, the model answers directly. Forcing a
  tool on every message is what made greetings get answered out of the returns
  policy; a zero-tool turn is logged instead, because a *missing* capability shows
  up as the model improvising rather than as an error.
- **The routing decision is cached in middleware.** A cache hit returns a synthetic
  tool call, so the model is never invoked, but the tool still executes and streams
  exactly as it otherwise would.

Offline/ops scripts sit alongside the request path, not in it: `refresh_products.py`
/ `discover_products.py` (keep the catalogue live), `admin_ingest_faqs.py` (push FAQ
vectors), `evals/run_eval_v2.py` (quality), and `load_test.py`
(responsiveness) — see Project Structure.

---

## 🔧 Tech Stack

| Layer     | Technology                                                |
| --------- | --------------------------------------------------------- |
| Frontend  | React 19 + Vite, Axios, React Markdown, Lucide Icons      |
| Backend   | FastAPI, Uvicorn, Pydantic, SlowAPI                       |
| Jobs      | Postgres-backed queue (`FOR UPDATE SKIP LOCKED`), fair per-user claiming, depth-scaled concurrency |
| Agent     | LangChain 1.x `create_agent` (LangGraph-backed), 3 `@tool`s |
| AI Models | Gemini 2.5 Flash **or** Cloudflare Workers AI `@cf/openai/gpt-oss-20b`, with `LLM_MODEL` as the app-paid default and a per-user BYOK choice; Gemini 2.5 Pro as SQL fallback |
| Auth      | JWT (python-jose), bcrypt                                 |
| Database  | PostgreSQL (Neon Cloud), SQLAlchemy ORM                   |
| Vector DB | Pinecone (gemini-embedding-001, 1024-dim)                 |
| Logging   | Python `logging` module (structured, leveled)             |

---

## 🛠️ Setup & Execution

### Prerequisites

- **Node.js** (v18+) for frontend
- **Python 3.10+** for backend

### 1. Backend Setup

```bash
cd backend
pip install -r requirements.txt
```

Create `backend/app/.env`:

```env
# Required — which provider runs generation. No default: a forgotten deploy
# setting should fail loudly rather than run on a guessed provider.
LLM_MODEL=GEMINI                 # or CLOUDFLARE
# Required either way — embeddings always run on Gemini, because the Pinecone
# index is 1024-dim gemini-embedding-001 and switching would need a re-index.
GEMINI_API_KEY=your_gemini_api_key
# Required only when LLM_MODEL=CLOUDFLARE
CLOUDFLARE_ACCOUNT_ID=your_account_id
CLOUDFLARE_API_TOKEN=your_workers_ai_token
DATABASE_URL=postgresql://user:pass@host/ecommerce_agent?sslmode=require
PINECONE_API_KEY=your_pinecone_key
PINECONE_INDEX_NAME=your_index_name
PINECONE_HOST=your_index_host_url
JWT_SECRET=your_jwt_secret_key
# Optional — spend and throughput bounds (defaults shown)
DAILY_MESSAGE_CAP=5          # how OFTEN one user may ask
DAILY_TOKEN_CAP=200000       # how MUCH they may spend; 0 disables
MAX_ACTIVE_JOBS=2            # concurrent jobs per user (backpressure)
JOB_HARD_LIMIT_S=240         # must stay below the 300s job lease
RUN_WORKER_IN_PROCESS=1      # 0 = run `python -m app.worker` as its own service
WORKER_MAX_CONCURRENCY=3     # concurrent job slots, scaled from queue depth
BREAKER_THRESHOLD=3          # consecutive failures before failing over
MAX_SQL_ROWS=500             # caps what generated SQL can pull into memory
# Optional — comma-separated CORS allow-list. Defaults to the deployed frontend + localhost:5173
ALLOWED_ORIGINS=https://your-frontend.onrender.com,http://localhost:5173
# Optional — LLM tracing (Langfuse). Leave unset to disable; the app runs identically without it.
LANGFUSE_PUBLIC_KEY=pk-lf-...
LANGFUSE_SECRET_KEY=sk-lf-...
LANGFUSE_HOST=https://cloud.langfuse.com
OTEL_SERVICE_NAME=ecommerce-agent-backend
DEPLOYMENT_ENV=production
```

Langfuse tracing uses native OpenTelemetry OTLP/HTTP; no `langfuse` SDK or CLI
is required. The pinned and locally resolved versions are OTel SDK/exporter
`1.44.0`, OpenInference LangChain `0.1.73` and google-genai `1.2.0`; the backend
has no lockfile. `OTEL_SERVICE_NAME` is optional and labels the service in traces.
For a custom/self-hosted `LANGFUSE_HOST`, confirm support for the required
`x-langfuse-ingestion-version: 4` header before setting
`LANGFUSE_OTLP_HOST_CONFIRMED=1`; otherwise tracing remains disabled.
The app records the question and answer on a root observation and copies
user/session IDs to its children. Queued-job children also carry the job ID;
photo recognition has a separate session-scoped root. Evaluator rules and export
integrations must still be checked in the target Langfuse project before cutover.

The app's Gemini key remains required for FAQ embeddings and vision. Settings lets
a shopper save a Gemini key or a Cloudflare Account ID and Workers AI API token,
then choose which one pays for generation. Without saved credentials, the app's
configured provider and daily credits apply (5 by default). A selected,
validated user-paid provider lifts the message and token caps. Changing or
removing credentials never falls back to the app's key for an already accepted
user-paid job. Keys and tokens are encrypted and never returned by the API;
Cloudflare's Account ID is visible in Settings.
**Before resuming Render, add the missing `jobs.billing_mode` column using the
idempotent v6 `ALTER TABLE` at the end of `backend/migrations.sql`.
`user_providers` already exists in `ecommerce_agent`.**

> **Switching `LLM_MODEL`?** The SQL / FAQ / routing caches key on the question text,
> not the provider, so purge them or the new provider serves the old one's output:
> `python -c "from app.cache import cache_purge; [cache_purge(k) for k in ('sql','faq','route')]"`

Apply the schema helpers (product indexes, `scraped_at`/`availability`/`pid`) once:

```bash
python -c "from sqlalchemy import text; ..."   # or paste backend/migrations.sql into your SQL editor
```

Run the server:

```bash
uvicorn main:app --port 8000
```

### 2. Frontend Setup

```bash
cd frontend
npm install
npm run dev
```

Create `frontend/.env` (the deployed URL is the fallback in `api.js`, so this is only for local dev):

```env
VITE_API_BASE_URL=http://localhost:8000/api
```

Open `http://localhost:5173` in your browser.

### 3. Keeping product data fresh

The catalogue is scraped, so prices/stock drift. Both scripts are plain CLI, need no browser, and are safe to re-run (they process oldest-first):

```bash
python -m app.refresh_products --limit 500   # re-check prices/ratings/stock (2 workers)
python -m app.discover_products --query "running shoes for men" --pages 10   # find new products
```

`.github/workflows/refresh.yml` runs the refresh nightly at 00:00 IST (`--limit 500`), and
can be triggered by hand. It needs one repo secret: `DATABASE_URL`.

---

## 📡 API Endpoints

| Method | Endpoint                  | Auth | Description                          |
| ------ | ------------------------- | ---- | ------------------------------------ |
| `GET`  | `/api/health`             | No   | Health check                         |
| `POST` | `/api/auth/signup`        | No   | Create account                       |
| `POST` | `/api/auth/login`         | No   | Login                                |
| `GET`    | `/api/chats`              | JWT  | Get all user chats                   |
| `POST`   | `/api/chats/new`          | JWT  | Create new chat session              |
| `POST`   | `/api/chats/{id}/message` | JWT  | Submit a message → **`202` + `job_id`**. Accepts an `Idempotency-Key` header (rate limited: 30/min) |
| `GET`    | `/api/jobs/{id}`          | JWT  | Poll one job: status, tool, result, error |
| `GET`    | `/api/jobs/{id}/events`   | JWT  | Stream the answer as SSE. `?after=<seq>` resumes after a dropped connection |
| `POST`   | `/api/jobs/{id}/cancel`   | JWT  | Request cancellation                 |
| `PATCH`  | `/api/chats/{id}`         | JWT  | Rename a chat                        |
| `DELETE` | `/api/chats/{id}`         | JWT  | Delete a chat                        |
| `GET`    | `/api/saved`              | JWT  | Saved products, joined to live prices (incl. change since saved) |
| `POST`   | `/api/saved`              | JWT  | Save a product by `pid` (idempotent) |
| `DELETE` | `/api/saved/{pid}`        | JWT  | Remove a saved product               |
| `GET`    | `/api/cart`               | JWT  | Cart joined to live catalogue data, plus the summed total |
| `POST`   | `/api/cart`               | JWT  | Add a product by `pid` (idempotent, so the chat icon can be a pure toggle) |
| `DELETE` | `/api/cart/{pid}`         | JWT  | Remove from cart                     |
| `GET`    | `/api/orders`             | JWT  | Orders with their line items         |
| `POST`   | `/api/orders`             | JWT  | Turn the cart into an order and empty it, in one transaction. `409` if anything is out of stock |
| `POST`   | `/api/orders/{id}/cancel` | JWT  | Cancel a placed order (scoped to the owner) |
| `GET`    | `/api/account/credits`   | JWT  | Daily credits or `unlimited: true` when the chosen provider is user-paid |
| `GET`    | `/api/account/api-keys`  | JWT  | Selected provider and saved-credential status (no secrets) |
| `PUT`    | `/api/account/provider`  | JWT  | Select `FREE`, `GEMINI` or `CLOUDFLARE`; own credentials required for paid options |
| `PUT`    | `/api/account/api-keys`  | JWT  | Validate and use `{ "gemini_api_key": "..." }` |
| `PUT`    | `/api/account/api-keys/cloudflare` | JWT | Validate and use `{ "account_id": "...", "api_token": "..." }` |
| `DELETE` | `/api/account/api-keys/gemini` | JWT | Remove saved Gemini key |
| `DELETE` | `/api/account/api-keys/cloudflare` | JWT | Remove saved Cloudflare credentials |
| `GET`    | `/api/preferences`        | JWT  | Recall preferences from long-term memory |
| `PUT`    | `/api/preferences`        | JWT  | Store one                                |
| `DELETE` | `/api/preferences`        | JWT  | Clears the panel only — memory is not wiped |

**The message endpoint does not return the answer.** The agent runs in a worker,
so the request only records a job. That is what lets an answer survive a closed
tab, a dropped connection or a redeploy — reconnect to `/events?after=<seq>` and
it picks up exactly where it stopped. Every `429` carries `Retry-After`.

**Sending a photo** goes to the same message endpoint: add `image` (base64, no
`data:` prefix), `image_mime`, and an optional `image_thumb` data URI for display.
`query` may then be empty. Vision runs during the request, so the job carries only
the short phrase it produced, not the image.

---

## 🔒 Security

- **SQL Injection Prevention**: LLM-generated SQL runs on a read-only PostgreSQL engine (`SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY`)
- **Password Security**: bcrypt hashing with auto-migration from legacy SHA-256
- **JWT Auth**: HS256 tokens expire after 12 hours (`JWT_EXPIRY_HOURS`). Login state uses tab-scoped `sessionStorage`: reload keeps the login, but closing the tab normally requires signing in again. Browser session restore can preserve tab storage; closing a tab does not revoke an issued JWT server-side.
- **Login Lockout**: 5 failed logins within 15 minutes locks the username (DB-backed, holds across instances)
- **Concurrency-Safe Writes**: chat updates take a row-level lock so simultaneous messages can't overwrite each other
- **Rate Limiting**: only on sending a chat message (30/min per user), because that is the step that calls the AI and costs money
- **Input Validation**: Pydantic validators reject malformed/oversized inputs before they reach business logic
- **CORS**: Whitelisted origins only

---

## 📈 Evaluation Results

`backend/evals/run_eval_v2.py` runs **160 shopper-style questions** (53 easy, 54 medium,
53 hard) in-process: product search, store policy, saved items, cart and orders,
follow-ups, photos, two requests in one message, and safety cases. DeepEval `GEval`
with a `gemini-3.5-flash-lite` judge scores the answers; code checks PII leakage.
App answers use `gemini-2.5-flash` (rewrite on `gemini-3.1-flash-lite`). The figures
below merge 157 cases from the 2026-10-18 run with three new cases run separately.
They are **not a fresh 160-case run** after the compare-results tool was removed.

| Metric | Cases | Result |
|---|---:|---|
| Latency p50 / p95 / p99 | 160 | 9.5s / 21.4s / 41.2s (mean 10.7s) |
| Time to first token, mean | 160 | 9.6s |
| Cost per query, mean (p50 / p95 / p99) | 160 | $0.0019 ($0.0011 / $0.0058 / $0.0121) |
| Answer completeness | 160 | 91.9% (147/160; mean 0.92) |
| Answer relevancy | 160 | 95.6% (153/160; mean 0.97) |
| Faithfulness (answers with product, FAQ or saved-item evidence) | 97 | 95.9% (93/97; mean 0.97) |
| Toxicity | 8 | 100% non-toxic |
| Scope adherence | 10 | 100% |
| PII leakage | 6 | 100% leak-free |

Latency is in-process (no queue or network). Scores are LLM-judge scores, not
human-calibrated. The merged cases cost $0.30 for the app and $0.19 for the judge.
The Q154 judge marked a response as missing Puma sneakers even though ten appeared
in the answer and the code catalogue check found them; its raw 0.5 judge scores are
retained. See `backend/evals/results_v2/run_2026-10-18_160_merged_sources.json`
for the two source runs and replacement IDs.

**Where one query's time goes** — "Puma running shoes under 3000", third of three
runs in one process, caches off:

| Step | Time |
|---|---:|
| Off-topic check (keyword fast path) | 0.00s |
| Query rewrite (no history, skipped) | 0.00s |
| Memory recall (Supermemory) | 1.27s |
| Agent routing (model picks the tool) | 2.35s |
| SQL generation (model) | 1.85s |
| Postgres query (Neon) | 0.89s |
| Formatting + streaming | 0.94s |
| **Total** | **7.3s** |

The first query in a fresh process took 16.2s: opening the Neon connection and the
model clients. A route or SQL cache hit removes most of the two model steps.

```bash
cd backend
python evals/run_eval_v2.py --all            # all 160 cases
python evals/run_eval_v2.py --ids Q001,Q151  # selected cases
python evals/run_eval_v2.py --all --trace    # one Langfuse trace per case (environment=eval)
```

### Load testing & capacity

Commands (run only with Render suspended for any job mode):

```bash
cd backend
python load_test_k6.py --workers 3 --slots 20 --jobs-per-user 1 --msg-seconds 7.3 --levels 25,50,75,100,150 --render-suspended   # load
python load_test_k6.py --workers 3 --slots 20 --jobs-per-user 1 --msg-seconds 7.3 --levels 200,300,400,500,600 --render-suspended # stress
python load_test_k6.py --workers 3 --slots 20 --jobs-per-user 1 --msg-seconds 7.3 --spike 10,400 --render-suspended               # spike (also 10,150 and 10,200)
python load_test_worker.py --render-suspended --jobs 40 --workers 2                                                              # queue drain
```

**Local k6 tests (2026-10-19, Render suspended).** Real FastAPI, Neon queue and three separate workers × 20 job slots; the AI is replaced by a 7.3s wait, so no model is called. Each virtual user logs in once with the correct password, then browses chats, saved items, cart, orders and credits every 5–10s, submits one message and polls it. Stepped levels ramp for 30s and hold for 180s. k6's default request timeout is 60s. These numbers include laptop-to-Neon latency and were measured with about 0.8 GB of free RAM; they are not production capacity claims.

**Load: 25–150 users.** Latency in seconds (p50 / p95 / p99):

| Users | Requests | Errors | Login | Chats | Submit |
|---:|---:|---:|---|---|---|
| 25 | 2,872 | 0% | 3.5 / 4.1 / 4.2 | 1.6 / 5.1 / 6.6 | 6.8 / 7.8 / 8.1 |
| 50 | 4,801 | 0% | 2.6 / 4.0 / 4.0 | 4.1 / 6.6 / 9.2 | 10.0 / 13.8 / 14.2 |
| 75 | 5,086 | 0% | 3.5 / 4.8 / 5.4 | 8.6 / 10.3 / 10.9 | 30.8 / 36.8 / 37.7 |
| 100 | 4,871 | 0% | 4.1 / 5.6 / 6.3 | 12.2 / 15.4 / 16.2 | 48.6 / 55.7 / 56.2 |
| 150 | 5,120 | 2.13% | 2.8 / 4.1 / 4.2 | 18.4 / 28.0 / 31.8 | 40.1 / 60.0 / 60.0 |

| Users | Jobs created | Succeeded / errored | Clients that got a job ID | Queue wait p50 / p95 / p99 (s) |
|---:|---:|---:|---:|---|
| 25 | 25 | 25 / 0 | 25 / 25 | 3.3 / 18.68 / 18.82 |
| 50 | 50 | 50 / 0 | 50 / 50 | 2.73 / 27.78 / 49.26 |
| 75 | 75 | 75 / 0 | 75 / 75 | 2.81 / 15.43 / 42.42 |
| 100 | 100 | 100 / 0 | 100 / 100 | 2.91 / 16.25 / 19.49 |
| 150 | 150 | 150 / 0 | 150 / 150 | 3.26 / 26.22 / 51.58 |

**Stress: 200–600 users.** Stopped after 400, when request errors exceeded 25%. Latency in seconds (p50 / p95 / p99):

| Users | Requests | Errors | Login | Chats | Submit (errors) |
|---:|---:|---:|---|---|---|
| 200 | 4,100 | 6.1% | 4.9 / 7.4 / 8.4 | 19.4 / 35.4 / 38.3 | 60.0 / 60.0 / 60.0 (62.5%) |
| 300 | 4,446 | 17.43% | 9.9 / 12.0 / 13.0 | 23.1 / 60.0 / 60.0 | 60.0 / 60.0 / 60.0 (99.0%) |
| 400 | 4,482 | 34.94% | 19.2 / 31.0 / 32.1 | 42.4 / 60.0 / 60.1 | 60.0 / 60.0 / 60.0 (100%) |

| Users | Jobs created | Succeeded / errored | Clients that got a job ID | Queue wait p50 / p95 / p99 (s) |
|---:|---:|---:|---:|---|
| 200 | 200 | 200 / 0 | 150 / 200 | 6.28 / 9.62 / 10.39 |
| 300 | 300 | 300 / 0 | 6 / 300 | 6.08 / 57.24 / 67.97 |
| 400 | 0 | 0 / 0 | 0 / 400 | — |

**Spike: 10 users, jump to the peak in 10s, hold 60s, drop back to 10 and watch recovery for 180s.** `GET /chats` and login latency in seconds (p50 / p95 / p99):

| Peak | Phase | Login | Chats | Chats errors | Submit errors |
|---:|:---|---|---|---:|---:|
| 150 | Before | 2.7 / 4.3 / 4.4 | 1.2 / 2.7 / 3.0 | 0% | 0% |
| 150 | Spike | 5.7 / 8.7 / 9.8 | 1.6 / 8.8 / 9.8 | 0% | 74.19% |
| 150 | After | — | 1.2 / 15.6 / 26.7 | 0% | 100% |
| 200 | Before | 2.9 / 4.5 / 4.8 | 1.2 / 2.7 / 2.7 | 0% | 0% |
| 200 | Spike | 6.6 / 11.6 / 12.6 | 1.3 / 12.5 / 13.7 | 0% | 88.89% |
| 200 | After | — | 1.2 / 19.6 / 31.8 | 0% | 100% |
| 400 | Before | 2.6 / 4.2 / 4.3 | 1.2 / 2.6 / 2.8 | 0% | 0% |
| 400 | Spike | 16.6 / 29.9 / 32.1 | 1.7 / 35.9 / 36.8 | 0% | 85.51% |
| 400 | After | — | 1.5 / 19.1 / 41.5 | 0% | 100% |

| Peak users | Requests | All-request errors | Jobs succeeded / errored | Clients that got a job ID | Queue wait p50 / p95 / p99 (s) |
|---:|---:|---:|---:|---:|---|
| 150 | 2,727 | 4.55% | 150 / 0 | 26 / 150 | 7.95 / 17.3 / 18.09 |
| 200 | 2,712 | 6.27% | 200 / 0 | 18 / 200 | 10.82 / 35.39 / 37.36 |
| 400 | 2,854 | 7.36% | 400 / 0 | 20 / 400 | 34.68 / 82.4 / 94.3 |

- **Errors start between 100 and 150 users.** Up to 100 users no request errored. At 150, 2.1% of requests errored, all of them `POST /message` calls that hit the 60s timeout; the users retried and every one of the 150 eventually got its job ID.
- **Login had no errors** in any run, but slows under load: p50 from about 3s to 19s at 400 users.
- **Every created job finished**; none errored. The breaking point is the API answering the submit request in time, not the worker queue: at the 400-user spike all 400 jobs succeeded while only 20 clients received their job ID.
- **Recovery is slow.** After each spike dropped back to 10 users, submit still timed out for the rest of the 3-minute recovery window.

**Before any job mode:** suspend Render's web service and stop all other workers using the shared
Neon queue. `--render-suspended` is an explicit acknowledgment; the harness also refuses an active
queue. API-only ramp and k6 browse runs disable the local worker. Worker drain measures throughput,
queue/run times, claim latency, multi-process claims and optional startup stagger. The k6 harness
seeds tagged chats, saved items, cart, orders and a completed-job probe for every virtual user,
then records endpoint p50/p95/p99 and errors. Stepped queue levels require every configured user
to submit their jobs; an under-submitted level is reported. Spike mode measures before/spike/recovery
phases. Model, recall and memory calls are stubbed in these load modes; calibration is not stubbed.
Reports and logs live under ignored `backend/load_test_results/`. The printed `k6-<tag>` or
`loadtest_<tag>` identifies accounts left by an interrupted run; use `--cleanup-tag` on k6 or
`--cleanup --cleanup-user` on `load_test.py` after stopping workers.

---

## 🐳 CI/CD & Docker

**GitHub Actions** (`.github/workflows/ci.yml`) — four gated jobs on every push/PR:
- **backend**: `ruff` lint + `compileall` syntax check + a no-network unit test
  (`tests/test_logging.py`). No API keys or DB needed, so it's fast and honest.
- **frontend**: `npm ci` + `eslint` + `vite build`.
- **docker**: builds both images (no push) with GitHub Actions cache, so a broken
  `COPY` or dependency breaks here rather than at `compose up`.
- **deploy**: only on push to `main`, only after the other three succeed — triggers
  the Render deploy hooks (`RENDER_DEPLOY_HOOK_*` secrets; skips cleanly if unset).

**Docker** — `docker compose up --build` runs the API (`backend/Dockerfile`, uvicorn)
and the frontend (`frontend/Dockerfile`, Vite build → nginx) locally; Neon and
Pinecone stay remote. Images are non-root, healthchecked against `/api/health`, and
carry no secrets — credentials are injected at runtime via `env_file`.

---

## 📂 Project Structure

```
├── .github/workflows/ci.yml      # CI: backend + frontend + docker build + deploy
├── docker-compose.yml            # Local stack: API + frontend (Neon/Pinecone remote)
├── .dockerignore
├── ruff.toml                     # Lint config (pinned default rule set; E402 for load-dotenv-before-import)
├── tests/
│   └── test_logging.py           # No-network unit test (request_id JSON logging)
│
├── frontend/
│   ├── Dockerfile                # Vite build -> nginx static serve
│   ├── nginx.conf                # SPA routing + asset caching
│   ├── src/
│   │   ├── components/
│   │   │   ├── LandingPage.jsx   # Marketing landing page (entry point)
│   │   │   ├── landing.css       # Landing page styles
│   │   │   ├── Auth.jsx          # Login/Signup with password requirements
│   │   │   ├── Sidebar.jsx       # Chats / Saved tabs, search, price-drop badge
│   │   │   └── ChatArea.jsx      # Chat interface (SSE streaming, save buttons)
│   │   ├── api.js                # Axios config with JWT interceptor
│   │   ├── App.jsx               # Main app with session persistence
│   │   └── index.css             # Design tokens (:root) + app styles
│   └── package.json
│
├── backend/
│   ├── Dockerfile                # python:3.12-slim, uvicorn, non-root, healthcheck
│   ├── main.py                   # FastAPI app, auth, chat + saved endpoints
│   ├── migrations.sql            # Product indexes, scraped_at/availability/pid; drops legacy discount + index cols
│   ├── evals/                    # 160-question eval (DeepEval GEval judge + code checks)
│   ├── load_test.py              # API browse ramp, saturation and real calibration
│   ├── load_test_worker.py       # stubbed queue drain and multi-process claims
│   ├── load_test_k6.py/.js       # stepped API/queue stress and spike/recovery
│   ├── requirements.txt
│   ├── app/
│   │   ├── agent.py              # LangChain create_agent, 4 tools, multi-step routing + route-cache middleware
│   │   ├── llm_provider.py       # Provider switch (LLM_MODEL): Gemini or Cloudflare chat model
│   │   ├── jobs.py               # Postgres job queue: claim, lease, events, usage, reaper
│   │   ├── worker.py             # The worker; runs the agent outside the request, scales with queue depth
│   │   ├── memory.py             # Context-aware query optimization
│   │   ├── sql.py                # Text-to-SQL pipeline (gemini-2.5-flash, Pro fallback)
│   │   ├── compare.py            # Saved items: add from results, remove, compare; reference resolution
│   │   ├── orders.py             # Cart + simulated orders; place/cancel shared with the HTTP endpoints
│   │   ├── order_history.py      # Order summary (one query, totals and spend)
│   │   ├── memory_store.py       # Long-term memory via Supermemory, scoped per user, fail-open
│   │   ├── cache.py              # Postgres-backed LLM response cache
│   │   ├── observability.py      # Optional Langfuse LLM tracing, fail-open
│   │   ├── logging_setup.py      # Structured JSON logs correlated by request_id
│   │   ├── llm_utils.py          # Retry/backoff for transient LLM errors
│   │   ├── refresh_products.py   # Re-check prices/stock via JSON-LD
│   │   ├── discover_products.py  # Find newly listed products
│   │   ├── faq.py                # RAG pipeline with Pinecone (gemini-2.5-flash)
│   │   ├── admin_ingest_faqs.py  # FAQ vector ingestion script
│   │   └── db/
│   │       ├── database.py       # SQLAlchemy engines (read-write + read-only)
│   │       └── models.py         # ORM models (EcommerceAccount, Chat, Message, SavedProduct, LLMCache, LoginFailure)
│   └── app/resources/
│       ├── faq_data.csv          # FAQ knowledge base
│       └── ecommerce_data_final.csv
│
└── web-scrapping/                # Flipkart data collection scripts
```

---

## 📜 License

MIT License — see [LICENSE](LICENSE). Copyright (c) 2026 Vivek P Marakumbi.
