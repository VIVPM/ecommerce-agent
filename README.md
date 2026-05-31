# 🛒 E-Commerce Agent (React + FastAPI)

An intelligent AI-powered e-commerce assistant built with a modern **React** frontend and **FastAPI** backend. Features agentic reasoning, secure authentication, and a premium **Glassmorphism** UI.

---

## 🚀 Key Features

- **Agentic Reasoning**: Gemini-powered Agent with Function Calling intelligently routes each query to one of three tools — product search (text-to-SQL), FAQ knowledge base (RAG), or comparing the user's saved products. The LLM chooses; there is no rule-based routing.
- **Streaming Responses**: Answers stream token-by-token over Server-Sent Events (SSE), with live progress status — no waiting for the full response.
- **Intelligent Memory**: Leverages `gemini-2.5-flash` to analyze conversation history and rewrite ambiguous queries into standalone, context-aware prompts.
- **Save, Compare & Price Alerts**: Shortlist products straight from a chat answer, ask the agent to compare them, and see price drops since you saved (the catalogue price is kept current, so a drop is just a join — no scheduler required).
- **Live Product Data**: `refresh_products.py` re-checks prices/ratings/stock from Flipkart's schema.org JSON-LD (no browser needed), and `discover_products.py` finds newly listed products. Out-of-stock and delisted items are flagged and filtered out of recommendations.
- **Honest by construction**: "Top rated" is ranked by a confidence-weighted (Bayesian) score, so a 4.7 from 50 ratings can't outrank a 4.6 from 500. Filters the catalogue can't honour — colour, size, width — are refused with an explanation rather than answered with an unfiltered list. The agent never invents discounts, sizes, materials or specs that aren't in the data, and product results disclose when a match came from the seller's title rather than a verified attribute.
- **LLM Response Caching**: Postgres-backed cache for generated SQL, FAQ answers, and routing decisions — survives restarts and is shared across instances, unlike an in-process cache. Fail-open, so a cache problem can never break a request.
- **Observability (optional, OpenTelemetry → Langfuse + Grafana)**: LLM spans come from OpenTelemetry auto-instrumentation of the `google-genai` SDK on one unified provider that exports **straight to Langfuse's OTLP endpoint and to Grafana Cloud** (no `langfuse` package) — every message is one trace with the routing, SQL-generation and answer calls nested under it, each carrying model, token usage, cost and latency. A **separate** provider sends HTTP-layer spans for every endpoint (auth, chat CRUD, saved products) to Grafana only (RED metrics per route), plus a `chat_messages_total` counter. A committed, reproducible **dashboard + two alert rules + email contact point** (`backend/grafana/`) chart throughput/success-rate/errors; the alert rules are provisioned like the coordinator's but **muted** by default (an always-on mute timing on the route), since the "no messages" rule is noisy for a low-traffic demo — flip `MUTE_ALERTS=False` to actually email. Logs are structured JSON correlated by a per-request `request_id` (also returned as `X-Request-ID`). Each backend is off unless its env vars are set, and all are fail-open like the cache — tracing can never break a request. (Same procedure as the sibling leads-coordinator project.)
- **Premium Glassmorphism UI**: High-end, responsive React interface with smooth animations, dark mode aesthetics, and Outfit typography.
- **Secure Authentication**: JWT-based auth with bcrypt password hashing, input validation, and password strength requirements (8+ chars, uppercase, lowercase, digit).
- **Login Lockout**: DB-backed lockout (5 failed attempts / 15 min per username) that holds across API instances, on top of per-IP rate limiting.
- **Chat Management**: Create, rename, and delete chat sessions; concurrent message writes take a row-level lock so they can't overwrite each other.
- **Rate Limiting**: Endpoint-level rate limiting (5/min signup, 10/min login, 30/min messages) to prevent abuse.
- **Structured Error Handling**: Consistent JSON error responses across all endpoints with global exception handlers.
- **Input Validation**: Pydantic validators for username (3-30 chars, alphanumeric), password strength, and query length (max 500 chars).
- **Production Logging**: Structured logging via Python's `logging` module across all backend modules — no `print()` statements.
- **Health Check Endpoint**: `GET /api/health` for uptime monitoring and deployment readiness checks.
- **Persistent Sessions**: Chat selection and session state survive page refreshes via localStorage persistence.
- **Evaluation Suite**: Built-in benchmarking (`evaluate_agent.py`) with LLM-as-a-Judge to track routing accuracy, faithfulness, and relevance across 150 test cases.
- **Cloud-Native Data Layer**:
  - **PostgreSQL (Neon)**: Chat history, saved products and the product catalogue live in a dedicated `ecommerce_agent` database. LLM-generated SQL runs on a separate read-only engine (prevents injection attacks).
  - **Pinecone Vector DB**: Scalable FAQ retrieval using semantic search with Gemini embeddings (1024-dim).

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
        Auth["🔐 Auth · bcrypt · JWT · login lockout · rate-limit"]
        REST["🗂️ Chat endpoints · POST /message → SSE stream · saved products"]
    end

    subgraph AI ["3 · Reasoning layer — Gemini agent"]
        Mem["🧠 Memory · rewrite query from history (memory.py)"]
        Route["🧭 LLM routing · picks 1 of 3 tools (agent.py)"]
        Mem --> Route
        Route --> SQL["📊 Text-to-SQL · read-only engine (sql.py)"]
        Route --> FAQ["📚 FAQ · RAG (faq.py)"]
        Route --> CMP["⚖️ Compare saved · user-scoped (compare.py)"]
    end

    subgraph DATA ["4 · Data layer — Neon Postgres + Pinecone"]
        PG[("Postgres · product · chat_sessions/messages<br>ecommerce_accounts · saved_products · llm_cache")]
        Pine[("Pinecone · FAQ vectors")]
    end

    subgraph EXT ["5 · External AI services"]
        Gem["☁️ Google Gemini · 2.5 Flash (routing / SQL / FAQ / compare) · embeddings"]
    end

    Cache["🗄️ Caching · cross-cutting<br>Postgres llm_cache · SQL / FAQ / routing"]
    OBS["📈 Observability · cross-cutting<br>Langfuse (LLM) + Grafana (HTTP · metrics · dashboard)"]

    User --> CLIENT
    CLIENT -->|HTTP + JWT| APP
    APP -->|auth · sessions · saved| DATA
    APP -->|optimized query| AI
    SQL -->|read-only SQL| PG
    FAQ -->|semantic search| Pine
    CMP -->|saved + live prices| PG
    Route -->|generate · embed| EXT
    AI -.->|hit / miss| Cache
    APP -.->|HTTP traces · metrics| OBS
    AI -.->|LLM traces| OBS
```

The reasoning layer routes each message to **one** of three tools via the LLM
(no rule-based routing — the model chooses, which is what keeps it an *agent*):

| Tool | Module | Routed when |
|---|---|---|
| Product search (text-to-SQL) | `sql.py` | shopper asks about products — price, brand, rating, stock, "cheaper than X" |
| FAQ (RAG) | `faq.py` | shopper asks about store policy — delivery, returns, payment, cancellation |
| Compare saved | `compare.py` | shopper asks to compare or choose among their own saved items |

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
| AI Models | Gemini 2.5 Flash (Agent, SQL, FAQ, Memory, Compare); 2.5 Pro as SQL fallback |
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
GEMINI_API_KEY=your_gemini_api_key
DATABASE_URL=postgresql://user:pass@host/ecommerce_agent?sslmode=require
PINECONE_API_KEY=your_pinecone_key
PINECONE_INDEX_NAME=your_index_name
PINECONE_HOST=your_index_host_url
JWT_SECRET=your_jwt_secret_key
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
| `POST`   | `/api/chats/{id}/message` | JWT  | Send message — streams the answer via SSE (rate limited: 30/min) |
| `PATCH`  | `/api/chats/{id}`         | JWT  | Rename a chat                        |
| `DELETE` | `/api/chats/{id}`         | JWT  | Delete a chat                        |
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

Benchmarked over **150 test cases** (30 FAQ · 90 SQL · 30 adversarial edge cases) with an
**LLM-as-a-Judge** (`evaluate_agent.py`, Gemini Flash judge against `eval_rubric.md`). This
is a **fresh run** against the current agent — Flash routing/SQL, Bayesian ranking, the live
catalogue — not the old snapshot:

| Metric | Score |
| --- | --- |
| **Routing accuracy** | 97.3% (146 / 150) |
| **Avg faithfulness** | 4.66 / 5.0 |
| **Avg relevance** | 4.33 / 5.0 |
| **Avg time / case** | 16.8s\* |

By category:

| Category | n | Routing | Faithfulness | Relevance |
| --- | --- | --- | --- | --- |
| FAQ | 30 | 90% | 4.50 | 4.43 |
| SQL (product) | 90 | **100%** | 4.68 | 4.19 |
| Edge case | 30 | 97% | 4.77 | 4.67 |

The 4 routing misses are all **ambiguous by design** — *"any active deals right now?"*,
*"how do I get your newsletter?"*, *"show me what I've bought before"*, *"translate 'I love
shoes' to French"* — questions with no clean tool (there's no deals/newsletter/order-history
feature), not wrong calls on real queries. Zero judge errors across all 150.

<sub>\* Time is per case **including the judge call**, run cold (cache cleared) from a local box
against remote Neon/Pinecone — not comparable to production agent latency, which is far lower
warm and co-located.</sub>

### Evaluation Criteria

1. **Routing Accuracy (Pass/Fail)**: Correct tool selection — `search_product_database` for products, `search_faq_knowledge_base` for policies.
2. **Faithfulness (1-5)**: Response adherence to retrieved data with zero hallucinations.
3. **Relevance (1-5)**: Helpfulness and completeness of the final response.

> **Caveat, stated honestly:** this is an LLM judging an LLM over synthetic cases — it
> measures whether answers are *well-formed*, not whether a price is truly *correct*. Treat
> it as a regression signal; the hand-graded set below is the stronger check.

### Human evaluation

To get past LLM-judging-LLM, a 26-scenario set (`human_eval.json`) is graded by hand
each iteration — product search, FAQ, compare, relative comparisons, stock, and
adversarial "trap" cases (out-of-catalogue requests, unsearchable attributes,
off-topic questions). Across the graded rounds:

| Metric (1–5)      | Round 1 | Round 2 | Round 3 |
| ----------------- | ------- | ------- | ------- |
| **Correct**       | 4.04    | 4.69    | 4.73    |
| **Useful**        | 3.54    | 3.96    | 4.23    |
| **Hallucinated**  | 12 / 26 | 0 / 26  | 0 / 26  |

Each round surfaced concrete bugs — rating counts that were seller-listing artefacts,
a "top rated" list topped by 5.0-from-3-reviews shoes, a compare tool that leaked one
user's shortlist framing into another's — which were fixed and re-verified. Routing has
held at 26/26 on every cold re-run. Cold-cache latency averages ~11.4s (SQL generation
runs on Flash; warm cache hits are several times faster).

### Load testing

Pointing a load tool at `POST /message` would mostly measure Gemini's latency, and every
call is real money — so the **LLM boundary is stubbed** (`optimize_query` / `route_query` /
the streaming chains are patched) and the part the app actually owns is tested: does
ordinary browsing stay responsive while the streaming `/message` path is saturated? A run
costs nothing and finishes in seconds; read the idle-vs-saturated **ratio**, not absolute ms.

`backend/load_test.py` runs an *idle* phase (browse mix, nothing streaming) then a
*saturated* phase (N messages streaming continuously + the same browse mix) and compares.
From a local box against remote Neon — 10 browse clients, 8 streaming messages, 12s/phase:

| Endpoint | idle p95 | saturated p95 | |
|---|---|---|---|
| `GET /api/health` (no DB) | 31ms | 32ms | ×1.0 |
| `GET /api/chats` | 2609ms | 6109ms | ×2.3 |
| `GET /api/saved` | 1062ms | 4719ms | ×4.4 |
| `POST /api/auth/login` | rate-limited | rate-limited | 429 |

The key result: **`/health` barely moves** (×1.0) — the async streaming design holds, so
concurrent message streaming doesn't starve the event loop. What degrades is **DB I/O**:
the DB-hitting endpoints are dominated by remote-Neon round-trip latency (~1s even idle —
this run was India→us-east-1) and, under load, the message-save queries compete with browse
reads for the 15-connection pool. That's the bottleneck to address at scale (PgBouncer /
bigger pool / co-locating the app near the DB), not the app logic — there were **0 errors**
throughout, and the rate limiter correctly throttled login (429) rather than failing.
Unlike the coordinator's load test (which found a bug that destroyed 100% of in-flight
jobs), this one surfaced no bug; the streaming path stayed correct.

`--calibrate N` sends N *real* messages (the only part that costs money) to measure true
per-message latency and set `--msg-seconds`. Everything above was measured locally; instance
size, proxy timeouts and cold starts are platform questions a local run can't answer, so a
confirmation run against Render is still worth doing.

```bash
cd backend
python load_test.py --messages 5  --concurrency 20   # tiers: 5 -> 10 -> 15 messages
python load_test.py --calibrate 3                     # real latency / cost
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
├── ruff.toml                     # Lint config (E402 for load-dotenv-before-import)
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
│   │   ├── agent.py              # Gemini agent, 3-tool function calling
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
