# 🛒 E-Commerce Agent (React + FastAPI)

An intelligent AI-powered e-commerce assistant built with a modern **React** frontend and **FastAPI** backend. Features agentic reasoning, secure authentication, and a clean **light** UI shared by the landing page and the app.

---

## 🚀 Key Features

- **Agentic reasoning** — a **LangChain agent** routes each message to four tools: product search (text-to-SQL), FAQ (RAG), saved items, and cart/orders. It can chain several tools in one turn or reply without any.
- **Swappable LLM provider** — Gemini 2.5 Flash or Cloudflare Workers AI via `LLM_MODEL`; shoppers can also bring their own key in Settings (stored encrypted).
- **Shop by photo** — upload a shoe photo and get similar products. Non-shoe photos are refused, not answered with invented results.
- **Cart & simulated orders** — add to cart, place and cancel orders from chat. Out-of-stock items block the order instead of being dropped silently.
- **Long-term memory** — preferences like *"I prefer Puma under 3000"* are stored per user in **Supermemory** and recalled in later chats.
- **Multi-part questions** — "return policy *and* Nike under 3000" or "4 Nike and 5 Puma" are split and each part is answered.
- **Honest answers** — recommends only in-stock items, refuses filters the data can't support, and stays on shopping topics.
- **Streaming & resilient jobs** — answers stream over SSE from a queued job, so closing the tab or redeploying doesn't lose the answer. Users are scheduled fairly under load.
- **Provider failover** — a circuit breaker switches to the other provider after repeated failures.
- **Bounded spend** — daily token budget and per-user rate limits.
- **Live product data** — scripts refresh prices/stock and discover new listings from Flipkart's schema.org data.
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
LLM_MODEL=GEMINI                 # or CLOUDFLARE; no default, fails loudly if unset
GEMINI_API_KEY=your_gemini_api_key   # always required: embeddings + vision run on Gemini
CLOUDFLARE_ACCOUNT_ID=your_account_id      # only when LLM_MODEL=CLOUDFLARE
CLOUDFLARE_API_TOKEN=your_workers_ai_token
DATABASE_URL=postgresql://user:pass@host/ecommerce_agent?sslmode=require
PINECONE_API_KEY=your_pinecone_key
PINECONE_INDEX_NAME=your_index_name
PINECONE_HOST=your_index_host_url
JWT_SECRET=your_jwt_secret_key
SUPERMEMORY_API_KEY=your_key     # optional: long-term memory off without it
LANGFUSE_PUBLIC_KEY=pk-lf-...    # optional: tracing off without them
LANGFUSE_SECRET_KEY=sk-lf-...
```

Shoppers can save their own Gemini key or Cloudflare credentials in Settings (encrypted, never returned by the API); a selected user-paid provider lifts the daily caps.

**Before resuming Render**, run the idempotent v6 `ALTER TABLE` (`jobs.billing_mode`) at the end of `backend/migrations.sql`.

> **Switching `LLM_MODEL`?** Caches key on question text, not provider, so purge them:
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

Key endpoints (all under `/api`, JWT except auth and health; full list in `backend/main.py` or `/docs`):

| Method | Endpoint | Description |
| ------ | -------- | ----------- |
| `POST` | `/auth/signup`, `/auth/login` | Create account / login (5/min, 10/min per username) |
| `POST` | `/chats/{id}/message` | Submit a message → **`202` + `job_id`** (30/min, `Idempotency-Key` supported) |
| `GET`  | `/jobs/{id}/events` | Stream the answer as SSE; `?after=<seq>` resumes |
| `GET`/`POST` | `/saved`, `/cart` | Saved items / cart, joined to live prices |
| `POST` | `/orders` | Place an order from the cart; `409` if anything is out of stock |
| `PUT`  | `/account/provider` | Choose `FREE`, `GEMINI` or `CLOUDFLARE` (own credentials for paid) |

The message endpoint returns a job, not the answer; reconnect to `/events?after=<seq>` to resume. Photos go to the same endpoint as `image` (base64), `image_mime` and optional `image_thumb`. Every `429` carries `Retry-After`.

---

## 🔒 Security

- **SQL Injection Prevention**: LLM-generated SQL runs on a read-only PostgreSQL engine (`SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY`)
- **Password Security**: bcrypt hashing with auto-migration from legacy SHA-256
- **JWT Auth**: HS256 tokens expire after 12 hours (`JWT_EXPIRY_HOURS`). Login state uses tab-scoped `sessionStorage`: reload keeps the login, but closing the tab normally requires signing in again. Browser session restore can preserve tab storage; closing a tab does not revoke an issued JWT server-side.
- **Login Lockout**: 5 failed logins within 15 minutes locks the username (DB-backed, holds across instances)
- **Concurrency-Safe Writes**: chat updates take a row-level lock so simultaneous messages can't overwrite each other
- **Rate Limiting**: signup 5/min and login 10/min per username; chat messages 30/min per user; provider selection and credential validation 10/min per user. Failed-login lockout remains separate.
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

All 160 cases:

<table>
<thead>
<tr><th colspan="4">Latency (s)</th><th rowspan="2">TTFT mean (s)</th><th colspan="4">Cost per query ($)</th></tr>
<tr><th>p50</th><th>p95</th><th>p99</th><th>mean</th><th>p50</th><th>p95</th><th>p99</th><th>mean</th></tr>
</thead>
<tbody>
<tr><td>9.5</td><td>21.4</td><td>41.2</td><td>10.7</td><td>9.6</td><td>0.0011</td><td>0.0058</td><td>0.0121</td><td>0.0019</td></tr>
</tbody>
</table>

<table>
<thead>
<tr><th>Metric</th><th>Cases scored</th><th>Above threshold</th><th>Rate</th><th>Mean score</th></tr>
</thead>
<tbody>
<tr><td>Answer completeness</td><td>160</td><td>147</td><td>91.9%</td><td>0.92</td></tr>
<tr><td>Answer relevancy</td><td>160</td><td>153</td><td>95.6%</td><td>0.97</td></tr>
<tr><td>Faithfulness (answers with product, FAQ or saved-item evidence)</td><td>97</td><td>93</td><td>95.9%</td><td>0.97</td></tr>
<tr><td>Toxicity (non-toxic)</td><td>8</td><td>8</td><td>100%</td><td>—</td></tr>
<tr><td>Scope adherence</td><td>10</td><td>10</td><td>100%</td><td>—</td></tr>
<tr><td>PII leakage (leak-free)</td><td>6</td><td>6</td><td>100%</td><td>—</td></tr>
</tbody>
</table>

Latency is in-process (no queue or network). Scores are LLM-judge, not human-calibrated. Source runs: `backend/evals/results_v2/run_2026-10-18_160_merged_sources.json`.

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

**Load: 25–150 users.**

<table>
<thead>
<tr><th rowspan="2">Users</th><th rowspan="2">Requests</th><th rowspan="2">Errors</th><th colspan="3">Login (s)</th><th colspan="3">Chats (s)</th><th colspan="3">Submit (s)</th></tr>
<tr><th>p50</th><th>p95</th><th>p99</th><th>p50</th><th>p95</th><th>p99</th><th>p50</th><th>p95</th><th>p99</th></tr>
</thead>
<tbody>
<tr><td>25</td><td>2,872</td><td>0%</td><td>3.5</td><td>4.1</td><td>4.2</td><td>1.6</td><td>5.1</td><td>6.6</td><td>6.8</td><td>7.8</td><td>8.1</td></tr>
<tr><td>50</td><td>4,801</td><td>0%</td><td>2.6</td><td>4.0</td><td>4.0</td><td>4.1</td><td>6.6</td><td>9.2</td><td>10.0</td><td>13.8</td><td>14.2</td></tr>
<tr><td>75</td><td>5,086</td><td>0%</td><td>3.5</td><td>4.8</td><td>5.4</td><td>8.6</td><td>10.3</td><td>10.9</td><td>30.8</td><td>36.8</td><td>37.7</td></tr>
<tr><td>100</td><td>4,871</td><td>0%</td><td>4.1</td><td>5.6</td><td>6.3</td><td>12.2</td><td>15.4</td><td>16.2</td><td>48.6</td><td>55.7</td><td>56.2</td></tr>
<tr><td>150</td><td>5,120</td><td>2.13%</td><td>2.8</td><td>4.1</td><td>4.2</td><td>18.4</td><td>28.0</td><td>31.8</td><td>40.1</td><td>60.0</td><td>60.0</td></tr>
</tbody>
</table>

<table>
<thead>
<tr><th rowspan="2">Users</th><th colspan="3">Jobs</th><th colspan="2">Job ID received</th><th colspan="3">Queue wait (s)</th></tr>
<tr><th>Created</th><th>Succeeded</th><th>Errored</th><th>Clients</th><th>Expected</th><th>p50</th><th>p95</th><th>p99</th></tr>
</thead>
<tbody>
<tr><td>25</td><td>25</td><td>25</td><td>0</td><td>25</td><td>25</td><td>3.3</td><td>18.7</td><td>18.8</td></tr>
<tr><td>50</td><td>50</td><td>50</td><td>0</td><td>50</td><td>50</td><td>2.7</td><td>27.8</td><td>49.3</td></tr>
<tr><td>75</td><td>75</td><td>75</td><td>0</td><td>75</td><td>75</td><td>2.8</td><td>15.4</td><td>42.4</td></tr>
<tr><td>100</td><td>100</td><td>100</td><td>0</td><td>100</td><td>100</td><td>2.9</td><td>16.2</td><td>19.5</td></tr>
<tr><td>150</td><td>150</td><td>150</td><td>0</td><td>150</td><td>150</td><td>3.3</td><td>26.2</td><td>51.6</td></tr>
</tbody>
</table>

**Stress (200–400 users):** request errors climbed from 6% at 200 users to 35% at 400; nearly every submit hit the 60s timeout.

**Spike (10 users jumping to 150, 200 or 400):** every job still succeeded, but 74–89% of submits timed out during the spike and all of them during the 3-minute recovery window.

- **Errors start between 100 and 150 users.** Up to 100 users no request errored. At 150, 2.1% of requests errored, all of them `POST /message` calls that hit the 60s timeout; the users retried and every one of the 150 eventually got its job ID.
- **Login had no errors** in any run, but slows under load: p50 from about 3s to 19s at 400 users.
- **Every created job finished**; none errored. The breaking point is the API answering the submit request in time, not the worker queue: at the 400-user spike all 400 jobs succeeded while only 20 clients received their job ID.
- **Recovery is slow.** After each spike dropped back to 10 users, submit still timed out for the rest of the 3-minute recovery window.

**Before any job mode:** suspend Render and stop other workers on the shared Neon queue (`--render-suspended` acknowledges this; the harness also refuses an active queue). Reports go to ignored `backend/load_test_results/`. Clean up an interrupted run with `--cleanup-tag` (k6) or `--cleanup --cleanup-user` (`load_test.py`).

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
