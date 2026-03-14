# 🛒 E-Commerce Agent (React + FastAPI)

An intelligent agent designed for e-commerce platforms. This project has been refactored into a modern **React** frontend and a **FastAPI** backend, featuring a premium **Glassmorphism** design, agentic AI architecture, and persistent user sessions.

---

## 🚀 Key Features

*   **Premium Glassmorphism UI**: A high-end, responsive React interface with smooth animations, dark mode aesthetics, and Outfit typography.
*   **Agentic Reasoning**: Uses a Gemini-powered Agent with Function Calling to intelligently route queries between a SQL database and an FAQ knowledge base.
*   **Intelligent Memory**: Leverages `gemini-2.5-flash` to analyze conversation history and rewrite user queries into standalone, context-aware prompts.
*   **User Authentication**: Secure signup and login system with persistent chat history stored in a PostgreSQL (Neon) cloud database.
*   **User-Provided API Keys**: Users can enter their own Gemini API keys in the sidebar, which are persisted locally and sent securely via headers.
*   **Evaluation Suite**: Built-in benchmarking tools (`evaluate_agent.py`) and a detailed rubric to track routing accuracy, faithfulness, and relevance.
*   **Cloud-Native Data Layer**:
    *   **PostgreSQL (Neon)**: Cloud-hosted product data for shoes.
    *   **Vector DB (Pinecone)**: Scalable FAQ retrieval using semantic search and Gemini embeddings.
*   **Optimized Performance**: Native Python formatting for large SQL result sets and optimistic UI rendering for a lag-free experience.

---

## 🏗️ Architecture

```mermaid
graph TD
    %% Frontend
    User[👤 User] -->|Interacts| React["⚛️ React Frontend<br>(frontend/)"]
    
    %% Auth & API Layer
    subgraph API_Layer [API & Auth Layer]
        React -->|HTTP / JSON + API Key Header| FastAPI["⚡ FastAPI Backend<br>(backend/main.py)"]
        FastAPI -->|Check/Store| DB[(Database<br>Postgres Cloud)]
    end
    
    %% Logic Layer
    subgraph Backend_Logic [AI Logic & Reasoning]
        FastAPI -->|History + Query| Memory["🧠 Intelligent Memory<br>(backend/app/memory.py)"]
        Memory -->|Standalone Query| Agent["🤖 Gemini Agent<br>(backend/app/agent.py)"]
        
        Agent -->|Tool Call| FAQ["📚 FAQ Chain<br>(backend/app/faq.py)"]
        Agent -->|Tool Call| SQL["📊 SQL Chain<br>(backend/app/sql.py)"]
    end

    %% Data Layer
    subgraph Cloud_Storage [Cloud Data Layer]
        FAQ -->|Retrieval| Pinecone[(Pinecone Cloud<br>Vector DB)]
        SQL -->|Execute Query| Neon[(Neon PostgreSQL<br>Cloud DB)]
    end

    %% Admin & Eval
    subgraph Management [Management & Quality]
        AdminScript["⚙️ Admin Script<br>(backend/app/admin_ingest_faqs.py)"] -->|Push Vectors| Pinecone
        EvalSuite["⚖️ Eval Suite<br>(backend/evaluate_agent.py)"] -->|Judge| Agent
    end
```

---

## 🛠️ Set-up & Execution

### 1. Requirements
Ensure you have **Node.js** (for frontend) and **Python 3.10+** (for backend) installed.

### 2. Backend Setup
1.  Navigate to the backend directory:
    ```bash
    cd backend
    ```
2.  Install dependencies:
    ```bash
    pip install -r requirements.txt
    ```
3.  Configure Environment: Create `backend/app/.env` with:
    ```text
    GEMINI_API_KEY=your_gemini_api_key
    DATABASE_URL=postgresql://user:pass@host/db?sslmode=require
    PINECONE_API_KEY=your_pinecone_key
    PINECONE_INDEX_NAME=your_index_name
    PINECONE_HOST=your_index_host_url
    ```
4.  Run Backend:
    ```bash
    uvicorn main:app --port 8000
    ```

### 3. Frontend Setup
1.  Navigate to the frontend directory:
    ```bash
    cd frontend
    ```
2.  Install dependencies:
    ```bash
    npm install
    ```
3.  Run Dev Server:
    ```bash
    npm run dev
    ```
4.  Open `http://localhost:5173` in your browser.

---

## 📈 Performance & Evaluation

The agent is continuously benchmarked using a dedicated evaluation suite (`backend/evaluate_agent.py`) that leverages an **LLM-as-a-Judge** approach.

### Evaluation Metrics (Latest Run)
*   **Total Test Cases**: 150
*   **Routing Accuracy**: **94%** (Passing queries to the correct tool)
*   **Avg Faithfulness**: **4.51 / 5.0** (Adherence to retrieved data/zero hallucinations)
*   **Avg Relevance**: **4.13 / 5.0** (Helpfulness and completeness of responses)

### Evaluation Rubric
1.  **Routing Accuracy (Pass/Fail)**: Checks if the agent correctly routes to `search_product_database` for products or `search_faq_knowledge_base` for policies.
2.  **Faithfulness (1-5)**: Measures how much of the response is supported by the tool output without any external hallucinations.
3.  **Relevance & Completeness (1-5)**: Evaluates if the response fully answers the user's question in a helpful, concise manner.

---

## 📂 Project Structure

*   **`frontend/`**: Vite + React application.
    *   `src/components/`: UI components (Sidebar, ChatArea, Auth).
    *   `src/api.js`: Axios configuration with auth and API key interceptors.
*   **`backend/`**: FastAPI server.
    *   `main.py`: API entry point, auth routing, and session management.
    *   `evaluate_agent.py`: Agent performance evaluation suite.
    *   `app/agent.py`: Agentic reasoning and tool routing logic.
    *   `app/memory.py`: Intelligent query optimization (History Context).
    *   `app/sql.py`: Text-to-SQL logic for product database.
    *   `app/faq.py`: RAG pipeline for semantic FAQ answering.
    *   `app/db/`: Database schemas, models (SQLAlchemy), and connection logic.
*   **`web-scrapping/`**: Data collection scripts and notebooks.
