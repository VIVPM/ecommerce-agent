# 🛒 E-Commerce Agent (React + FastAPI)

An intelligent AI-powered e-commerce assistant built with a modern **React** frontend and **FastAPI** backend. Features agentic reasoning, secure authentication, and a premium **Glassmorphism** UI.

---

## 🚀 Key Features

- **Agentic reasoning** — a **LangChain agent** (`create_agent`) in which the LLM, not rules, routes each message to one of three tools: product search (text-to-SQL), FAQ (RAG), or comparing the user's saved products. Every tool is `return_direct`, so its verified output reaches the shopper verbatim instead of being paraphrased by a second model call.
- **Swappable LLM provider** — one `LLM_MODEL` env var switches every generation call between **Gemini 2.5 Flash** and **Cloudflare Workers AI** (`@cf/openai/gpt-oss-20b`). Interchangeable peers, not primary-and-backup: same agent, same tools, same streaming, native tool-calling on both. Embeddings always run on Gemini (the Pinecone index is 1024-dim `gemini-embedding-001`).
- **Streaming responses** — answers stream token-by-token over SSE with live progress, so there's no spinner-wait.
- **Answers survive the connection** — the agent runs as a queued **job**, not inside the HTTP request. Close the tab, lose signal or redeploy mid-answer and the work continues; reconnecting replays from the last event and tails the rest. Jobs can be polled or cancelled.
- **Spend is bounded by tokens, not messages** — every job records input/output/cached tokens, and a daily token budget is the real cap. Rate limits are keyed on identity (not IP), and every `429` carries `Retry-After`.
- **Context memory** — `gemini-2.5-flash` rewrites follow-ups like *"any cheaper?"* into standalone queries from recent history.
- **Follow-up suggestions** — 2–3 tappable chips under each answer, picked from which tool replied. They surface capabilities a blank input box hides (relative comparisons, compare-saved) and, after a refused search, steer to queries that work. Derived from the routing decision, so they add no LLM call, cost or latency.
- **Save, compare & price alerts** — shortlist products from a chat answer, ask the agent to compare them, and see price drops since you saved (a live join, no scheduler).
- **Live product data** — `refresh_products.py` / `discover_products.py` re-check prices/stock and find new listings from Flipkart's schema.org JSON-LD (no browser); out-of-stock items are filtered out.
- **Honest by construction** — "top rated" uses a confidence-weighted (Bayesian) rank so a 4.7-from-50 can't beat a 4.6-from-500; filters the data can't support (colour, size) are refused, not faked; nothing is invented that isn't in the catalogue.
- **Postgres-backed LLM cache** — caches generated SQL, FAQ answers and routing decisions; survives restarts, shared across instances, fail-open.
- **Optional observability** — OpenTelemetry traces (LLM → Langfuse, HTTP → Grafana), a `chat_messages_total` metric, a committed Grafana dashboard + (muted) alerts, and JSON logs correlated by `request_id`. Off unless configured, fail-open. (Details under [CI/CD & Docker](#-cicd--docker).)
- **Secure auth** — JWT + bcrypt with password-strength rules, plus a DB-backed login lockout (5 fails / 15 min) that holds across instances, on top of rate limits keyed on the authenticated user where there is one and the IP otherwise (5/min signup · 10/min login · 30/min messages).
- **Safe by default** — LLM-generated SQL runs on a read-only engine (injection-proof); Pydantic validates every input; concurrent chat writes take a row-level lock; consistent JSON errors; structured logging, no `print()`s.
- **Cloud-native data** — Neon Postgres (chat history, catalogue, saved products in a dedicated `ecommerce_agent` DB) + Pinecone (FAQ vectors, Gemini 1024-dim embeddings).
- **Polished frontend** — responsive React chat UI, plus a landing page with a live streaming demo, scroll animations, and session state that survives refresh.
- **Quality tracking** — a 200-scenario automated evaluation suite (`evaluate_agent_tuned.py`) (see [Evaluation Results](#-evaluation-results)).

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
        Route["🧭 create_agent · LLM picks 1 of 3 tools<br>return_direct · runtime ctx (agent.py)"]
        Mem --> Route
        Route --> SQL["📊 Text-to-SQL · read-only engine (sql.py)"]
        Route --> FAQ["📚 FAQ · RAG (faq.py)"]
        Route --> CMP["⚖️ Compare saved · user-scoped (compare.py)"]
    end

    subgraph DATA ["4 · Data layer — Neon Postgres + Pinecone"]
        PG[("Postgres · product · chat_sessions/messages<br>ecommerce_accounts · saved_products · llm_cache<br>jobs · job_events")]
        Pine[("Pinecone · FAQ vectors")]
    end

    subgraph EXT ["5 · External AI services — LLM_MODEL picks one"]
        Gem["☁️ Google Gemini · 2.5 Flash<br>routing / SQL / FAQ / compare"]
        CF["☁️ Cloudflare Workers AI · gpt-oss-20b<br>routing / SQL / FAQ / compare"]
        Emb["🔢 Gemini embeddings · always, either way"]
    end

    Cache["🗄️ Caching · cross-cutting<br>Postgres llm_cache · SQL / FAQ / routing"]
    OBS["📈 Observability · cross-cutting<br>Langfuse (LLM) + Grafana (HTTP · metrics · dashboard)"]

    User --> CLIENT
    CLIENT -->|HTTP + JWT| APP
    APP -->|auth · sessions · saved| DATA
    WORK -->|optimized query| AI
    SQL -->|read-only SQL| PG
    FAQ -->|semantic search| Pine
    CMP -->|saved + live prices| PG
    Route -->|generate · embed| EXT
    AI -.->|hit / miss| Cache
    APP -.->|HTTP traces · metrics| OBS
    AI -.->|LLM traces| OBS
```

The reasoning layer is a LangChain agent (`create_agent`) that routes each message
to **one** of three tools via the LLM (no rule-based routing — the model chooses,
which is what keeps it an *agent*). The tool docstrings *are* the routing prompt:

| Tool | Module | Routed when |
|---|---|---|
| Product search (text-to-SQL) | `sql.py` | shopper asks about products — price, brand, rating, stock, "cheaper than X" |
| FAQ (RAG) | `faq.py` | shopper asks about store policy — delivery, returns, payment, cancellation |
| Compare saved | `compare.py` | shopper asks to compare or choose among their own saved items |

Three design points worth knowing:

- **`return_direct=True` on every tool.** Product lists, rating counts, price-age
  and unsupported-filter notes are already shopper-ready, so the agent returns them
  verbatim rather than paraphrasing them through a second model call. This also
  makes each run single-hop.
- **`user_id` travels in the agent's runtime context, never as a tool argument** —
  so the model cannot hallucinate one, or be talked into supplying someone else's,
  and read a stranger's shortlist.
- **The routing decision is cached in middleware.** A cache hit returns a synthetic
  tool call, so the model is never invoked, but the tool still executes and streams
  exactly as it otherwise would.

Offline/ops scripts sit alongside the request path, not in it: `refresh_products.py`
/ `discover_products.py` (keep the catalogue live), `admin_ingest_faqs.py` (push FAQ
vectors), `evaluate_agent.py` + `human_eval.json` (quality), `load_test.py`
(responsiveness), `grafana/provision.py` (dashboard/alerts) — see Project Structure.

---

## 🔧 Tech Stack

| Layer     | Technology                                                |
| --------- | --------------------------------------------------------- |
| Frontend  | React 19 + Vite, Axios, React Markdown, Lucide Icons      |
| Backend   | FastAPI, Uvicorn, Pydantic, SlowAPI                       |
| Jobs      | Postgres-backed queue (`FOR UPDATE SKIP LOCKED`), one async worker |
| Agent     | LangChain 1.x `create_agent` (LangGraph-backed), 3 `@tool`s |
| AI Models | Gemini 2.5 Flash **or** Cloudflare Workers AI `@cf/openai/gpt-oss-20b`, set by `LLM_MODEL` (Agent, SQL, FAQ, Memory, Compare); Gemini 2.5 Pro as SQL fallback |
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
# Optional — comma-separated CORS allow-list. Defaults to the deployed frontend + localhost:5173
ALLOWED_ORIGINS=https://your-frontend.onrender.com,http://localhost:5173
# Optional — LLM tracing (Langfuse). Leave unset to disable; the app runs identically without it.
LANGFUSE_PUBLIC_KEY=pk-lf-...
LANGFUSE_SECRET_KEY=sk-lf-...
LANGFUSE_HOST=https://cloud.langfuse.com
# Optional — HTTP tracing + metrics to Grafana Cloud (OTLP). Leave unset to disable.
GRAFANA_OTLP_ENDPOINT=https://otlp-gateway-<region>.grafana.net/otlp
GRAFANA_OTLP_AUTH=Basic <base64 of instanceID:token>
OTEL_SERVICE_NAME=ecommerce-agent-backend
DEPLOYMENT_ENV=production
```

The API key is the operator's, read once from this file — users never supply their own.

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
python -m app.refresh_products --limit 500 --workers 3   # re-check prices/ratings/stock
python -m app.discover_products --query "running shoes for men" --pages 10   # find new products
```

---

## 📡 API Endpoints

| Method | Endpoint                  | Auth | Description                          |
| ------ | ------------------------- | ---- | ------------------------------------ |
| `GET`  | `/api/health`             | No   | Health check                         |
| `POST` | `/api/auth/signup`        | No   | Create account (rate limited: 5/min) |
| `POST` | `/api/auth/login`         | No   | Login (rate limited: 10/min)         |
| `GET`    | `/api/chats`              | JWT  | Get all user chats                   |
| `POST`   | `/api/chats/new`          | JWT  | Create new chat session              |
| `POST`   | `/api/chats/{id}/message` | JWT  | Submit a message → **`202` + `job_id`**. Accepts an `Idempotency-Key` header (rate limited: 30/min) |
| `GET`    | `/api/jobs/{id}`          | JWT  | Poll one job: status, tool, result, error |
| `GET`    | `/api/jobs/{id}/events`   | JWT  | Stream the answer as SSE. `?after=<seq>` resumes after a dropped connection |
| `POST`   | `/api/jobs/{id}/cancel`   | JWT  | Request cancellation                 |
| `PATCH`  | `/api/chats/{id}`         | JWT  | Rename a chat                        |
| `DELETE` | `/api/chats/{id}`         | JWT  | Delete a chat                        |

**The message endpoint does not return the answer.** The agent runs in a worker,
so the request only records a job. That is what lets an answer survive a closed
tab, a dropped connection or a redeploy — reconnect to `/events?after=<seq>` and
it picks up exactly where it stopped. Every `429` carries `Retry-After`.
| `GET`    | `/api/saved`              | JWT  | Saved products, joined to live prices (incl. change since saved) |
| `POST`   | `/api/saved`              | JWT  | Save a product by `pid` (idempotent) |
| `DELETE` | `/api/saved/{pid}`        | JWT  | Remove a saved product               |

---

## 🔒 Security

- **SQL Injection Prevention**: LLM-generated SQL runs on a read-only PostgreSQL engine (`SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY`)
- **Password Security**: bcrypt hashing with auto-migration from legacy SHA-256
- **JWT Auth**: HS256 tokens with 1-hour expiry, auto-logout on expiration
- **Login Lockout**: 5 failed logins within 15 minutes locks the username (DB-backed, holds across instances)
- **Concurrency-Safe Writes**: chat updates take a row-level lock so simultaneous messages can't overwrite each other
- **Rate Limiting**: Per-endpoint limits via SlowAPI decorators
- **Input Validation**: Pydantic validators reject malformed/oversized inputs before they reach business logic
- **CORS**: Whitelisted origins only

---

## 📈 Evaluation Results

Benchmarked over a unified **200-scenario automated test suite** (`evaluate_agent_tuned.py`, Gemini Flash judge against `eval_rubric.md`). This suite combines complex product searches, multi-item comparisons, FAQ retrieval, and adversarial "trap" cases (out-of-catalogue requests, unsearchable attributes). 

This represents a fresh run against the current agent architecture—featuring Flash routing/SQL, Bayesian ranking, and the live catalogue—compared to the historical Pro-based baseline.

| Metric | Tuned Architecture (Flash) | Baseline (Pro) |
| --- | --- | --- |
| **Routing accuracy** | **100.0%** (200 / 200) | 90.5% |
| **Avg faithfulness** | **4.78 / 5.0** | 4.43 / 5.0 |
| **Avg relevance** | **4.44 / 5.0** | 3.85 / 5.0 |
| **Avg time / case** | **12.5s** | 31.4s |

### Key Improvements:
1. **Massive Latency Drop**: By migrating SQL-generation entirely to `gemini-2.5-flash` and dropping the heavier Pro model, response times were slashed by **60%** (from 31.4s to 12.5s per case).
2. **Elimination of Hallucinations**: Embedding explicit query guardrails (NULL ordering, gender traps, Bayesian ranking) directly into the prompt eliminated previous hallucination issues, pushing faithfulness to 4.78 out of 5.
3. **Perfect Routing**: The updated agent routing instructions achieved a 100% success rate across all 200 cases, successfully filtering out adversarial and edge-case queries.

### Evaluation Criteria

1. **Routing Accuracy (Pass/Fail)**: Correct tool selection — `search_product_database`, `search_faq_knowledge_base`, or `compare_saved_products`.
2. **Faithfulness (1-5)**: Response adherence to retrieved data with zero hallucinations.
3. **Relevance (1-5)**: Helpfulness and completeness of the final response.

### Load testing & capacity

`backend/load_test.py` answers two questions: does the app stay responsive under load, and
how many concurrent users it takes.

**Responsiveness** (local, with the LLM stubbed so a run is free). An *idle* phase versus a
*saturated* phase (streaming messages + a browse mix), compared by ratio. `/health` (no DB)
barely moves — **p95 31 → 32ms (×1.0)** — so message streaming doesn't starve the event loop.
The DB-hitting reads degrade ×2–4 under load, bounded by the 15-connection pool. That's a
scaling limit, not a bug: **0 errors** throughout, and login correctly throttles (429).

**Capacity** (measured against the live Render instance). `--ramp` scales browse concurrency,
`--calibrate` sends real messages on the paid Gemini tier. Current production (DB pool 30):

| Concurrent browsers | p50 | p95 | errors |
|---|---|---|---|
| 25 | 1.0s | 2.4s | 0 |
| 50 | 2.0s | 2.8s | 0 |
| 75 | 3.9s | 5.8s | 0 |
| 100 | 4.5s | 7.7s | 0 |

**Zero errors even at 100 concurrent browsers**, and the DB-pool bump earned its keep: raising
the pool from 15 → 30 (`pool_size=10 + max_overflow=20`) cut p95 at 100 concurrent from **19.2s
to 7.7s** on Render (~2.5×), since requests that used to queue for one of 15 connections now run.
Real messages run **3.3s warm / 7.4s cold**; the `/message` path is rate-limited to 30/min per IP
by design, so system-wide message throughput is bounded by Gemini quota, not the app.

**Bottom line:** one Render instance serves ~100 concurrent browsers with no failures and a ~7.7s
p95 tail (well below that for typical load). To go further the next levers are a dedicated
PgBouncer layer, a read replica, bigger Neon compute, and horizontal scaling (Part 3). Messaging
stays capped per-user by the rate limit and system-wide by Gemini quota.

```bash
cd backend
python load_test.py --ramp --base <url>          # browse capacity
python load_test.py --calibrate 3 --base <url>   # real message latency
```

---

## 🐳 CI/CD & Docker

**GitHub Actions** (`.github/workflows/ci.yml`) — four gated jobs on every push/PR:
- **backend**: `ruff` lint + `compileall` syntax check + a no-network unit test
  (`tests/test_logging.py`). No API keys or DB needed, so it's fast and honest.
- **frontend**: `npm ci` + `eslint` + `vite build`.
- **docker**: builds both images (no push) with GitHub Actions cache, so a broken
  `COPY` or dependency fails here rather than at `compose up`.
- **deploy**: only on push to `main`, only after the other three pass — triggers
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
│   │   └── index.css             # Glassmorphism design system
│   └── package.json
│
├── backend/
│   ├── Dockerfile                # python:3.12-slim, uvicorn, non-root, healthcheck
│   ├── main.py                   # FastAPI app, auth, chat + saved endpoints
│   ├── migrations.sql            # Product indexes, scraped_at/availability/pid; drops legacy discount + index cols
│   ├── evaluate_agent.py         # LLM-as-a-Judge evaluation suite
│   ├── load_test.py              # API responsiveness under message-path saturation (LLM stubbed)
│   ├── grafana/                  # Dashboard JSON + provision.py (dashboard, 2 alerts, email contact point)
│   ├── requirements.txt
│   ├── app/
│   │   ├── agent.py              # LangChain create_agent, 3 return_direct tools, route-cache middleware
│   │   ├── llm_provider.py       # Provider switch (LLM_MODEL): Gemini or Cloudflare chat model
│   │   ├── jobs.py               # Postgres job queue: claim, lease, events, usage, reaper
│   │   ├── worker.py             # The single worker; runs the agent outside the request
│   │   ├── memory.py             # Context-aware query optimization
│   │   ├── sql.py                # Text-to-SQL pipeline (gemini-2.5-flash, Pro fallback)
│   │   ├── compare.py            # Compare the user's saved products
│   │   ├── cache.py              # Postgres-backed LLM response cache
│   │   ├── observability.py      # OTLP tracing -> Langfuse + Grafana, + metric, fail-open (off by default)
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
