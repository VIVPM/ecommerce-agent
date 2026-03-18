# 🛒 E-Commerce Agent (React + FastAPI)

An intelligent AI-powered e-commerce assistant built with a modern **React** frontend and **FastAPI** backend. Features agentic reasoning, secure authentication, and a premium **Glassmorphism** UI.

---

## 🚀 Key Features

* **Agentic Reasoning**: Gemini-powered Agent with Function Calling intelligently routes queries between a SQL database and an FAQ knowledge base.
* **Intelligent Memory**: Leverages `gemini-2.5-flash` to analyze conversation history and rewrite ambiguous queries into standalone, context-aware prompts.
* **Premium Glassmorphism UI**: High-end, responsive React interface with smooth animations, dark mode aesthetics, and Outfit typography.
* **Secure Authentication**: JWT-based auth with bcrypt password hashing, input validation, and password strength requirements (8+ chars, uppercase, lowercase, digit).
* **Rate Limiting**: Endpoint-level rate limiting (5/min signup, 10/min login, 20/min messages) to prevent abuse.
* **Structured Error Handling**: Consistent JSON error responses across all endpoints with global exception handlers.
* **Input Validation**: Pydantic validators for username (3-30 chars, alphanumeric), password strength, and query length (max 500 chars).
* **Production Logging**: Structured logging via Python's `logging` module across all backend modules — no `print()` statements.
* **Health Check Endpoint**: `GET /api/health` for uptime monitoring and deployment readiness checks.
* **User-Provided API Keys**: Users can enter their own Gemini API keys in the sidebar, persisted locally and sent securely via headers.
* **Persistent Sessions**: Chat selection and session state survive page refreshes via localStorage persistence.
* **Evaluation Suite**: Built-in benchmarking (`evaluate_agent.py`) with LLM-as-a-Judge to track routing accuracy, faithfulness, and relevance across 150 test cases.
* **Cloud-Native Data Layer**:
  * **PostgreSQL (Neon)**: Cloud-hosted product data with read-only engine for LLM-generated SQL (prevents injection attacks).
  * **Pinecone Vector DB**: Scalable FAQ retrieval using semantic search with Gemini embeddings (1024-dim).

---

## 🏗️ Architecture

```mermaid
graph TD
    %% Frontend
    User[👤 User] -->|Interacts| React["⚛️ React Frontend<br>(Vite + React 19)"]

    %% Auth & API Layer
    subgraph API_Layer [API & Auth Layer]
        React -->|HTTP + JWT| FastAPI["⚡ FastAPI Backend<br>(main.py)"]
        FastAPI -->|Auth / Sessions| DB[(PostgreSQL<br>Neon Cloud)]
    end

    %% Logic Layer
    subgraph Backend_Logic [AI Logic & Reasoning]
        FastAPI -->|History + Query| Memory["🧠 Intelligent Memory<br>(memory.py)"]
        Memory -->|Standalone Query| Agent["🤖 Gemini Agent<br>(agent.py)"]

        Agent -->|Tool Call| FAQ["📚 FAQ Chain<br>(faq.py) — gemini-2.5-flash"]
        Agent -->|Tool Call| SQL["📊 SQL Chain<br>(sql.py) — gemini-2.5-pro"]
    end

    %% Data Layer
    subgraph Cloud_Storage [Cloud Data Layer]
        FAQ -->|Semantic Search| Pinecone[(Pinecone Cloud<br>Vector DB)]
        SQL -->|Read-Only Query| Neon[(Neon PostgreSQL<br>Cloud DB)]
    end

    %% Admin & Eval
    subgraph Management [Management & Quality]
        AdminScript["⚙️ Admin Script<br>(admin_ingest_faqs.py)"] -->|Push Vectors| Pinecone
        EvalSuite["⚖️ Eval Suite<br>(evaluate_agent.py)"] -->|Judge| Agent
    end
```

---

## 🔧 Tech Stack

| Layer | Technology |
|-------|-----------|
| Frontend | React 19 + Vite, Axios, React Markdown, Lucide Icons |
| Backend | FastAPI, Uvicorn, Pydantic, SlowAPI |
| AI Models | Gemini 2.5 Pro (SQL), Gemini 2.5 Flash (Agent/FAQ/Memory) |
| Auth | JWT (python-jose), bcrypt |
| Database | PostgreSQL (Neon Cloud), SQLAlchemy ORM |
| Vector DB | Pinecone (gemini-embedding-001, 1024-dim) |
| Logging | Python `logging` module (structured, leveled) |

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
DATABASE_URL=postgresql://user:pass@host/db?sslmode=require
PINECONE_API_KEY=your_pinecone_key
PINECONE_INDEX_NAME=your_index_name
PINECONE_HOST=your_index_host_url
JWT_SECRET=your_jwt_secret_key
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

Open `http://localhost:5173` in your browser.

---

## 📡 API Endpoints

| Method | Endpoint | Auth | Description |
|--------|----------|------|-------------|
| `GET` | `/api/health` | No | Health check |
| `POST` | `/api/auth/signup` | No | Create account (rate limited: 5/min) |
| `POST` | `/api/auth/login` | No | Login (rate limited: 10/min) |
| `GET` | `/api/chats` | JWT | Get all user chats |
| `POST` | `/api/chats/new` | JWT | Create new chat session |
| `POST` | `/api/chats/{id}/message` | JWT | Send message (rate limited: 20/min) |

---

## 🔒 Security

- **SQL Injection Prevention**: LLM-generated SQL runs on a read-only PostgreSQL engine (`SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY`)
- **Password Security**: bcrypt hashing with auto-migration from legacy SHA-256
- **JWT Auth**: HS256 tokens with 1-hour expiry, auto-logout on expiration
- **Rate Limiting**: Per-endpoint limits via SlowAPI decorators
- **Input Validation**: Pydantic validators reject malformed/oversized inputs before they reach business logic
- **CORS**: Whitelisted origins only

---

## 📈 Evaluation Results

Benchmarked using 150 test cases (30 FAQ + 120 SQL) with an **LLM-as-a-Judge** approach:

| Metric | Score |
|--------|-------|
| **Routing Accuracy** | 97.33% |
| **Avg Faithfulness** | 4.6 / 5.0 |
| **Avg Relevance** | 4.27 / 5.0 |
| **Avg Response Time** | ~15.9s |

### Evaluation Criteria

1. **Routing Accuracy (Pass/Fail)**: Correct tool selection — `search_product_database` for products, `search_faq_knowledge_base` for policies.
2. **Faithfulness (1-5)**: Response adherence to retrieved data with zero hallucinations.
3. **Relevance (1-5)**: Helpfulness and completeness of the final response.

---

## 📂 Project Structure

```
├── frontend/
│   ├── src/
│   │   ├── components/
│   │   │   ├── Auth.jsx          # Login/Signup with password requirements
│   │   │   ├── Sidebar.jsx       # Chat history, API key input, search
│   │   │   └── ChatArea.jsx      # Chat interface
│   │   ├── api.js                # Axios config with JWT & API key interceptors
│   │   ├── App.jsx               # Main app with session persistence
│   │   └── index.css             # Glassmorphism design system
│   └── package.json
│
├── backend/
│   ├── main.py                   # FastAPI app, auth, error handling
│   ├── evaluate_agent.py         # LLM-as-a-Judge evaluation suite
│   ├── requirements.txt
│   ├── app/
│   │   ├── agent.py              # Gemini agent with function calling
│   │   ├── memory.py             # Context-aware query optimization
│   │   ├── sql.py                # Text-to-SQL pipeline (gemini-2.5-pro)
│   │   ├── faq.py                # RAG pipeline with Pinecone (gemini-2.5-flash)
│   │   ├── admin_ingest_faqs.py  # FAQ vector ingestion script
│   │   └── db/
│   │       ├── database.py       # SQLAlchemy engines (read-write + read-only)
│   │       └── models.py         # ORM models (EcommerceAccount)
│   └── app/resources/
│       ├── faq_data.csv          # FAQ knowledge base
│       └── ecommerce_data_final.csv
│
└── web-scrapping/                # Flipkart data collection scripts
```
