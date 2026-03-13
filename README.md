# 🛒 E-Commerce Agent (React + FastAPI)

An intelligent agent designed for e-commerce platforms. This project has been refactored into a modern **React** frontend and a **FastAPI** backend, featuring a premium **Glassmorphism** design and an agentic AI architecture.

---

## 🚀 Key Features

*   **Premium Glassmorphism UI**: A high-end, responsive React interface with smooth animations, dark mode aesthetics, and Outfit typography.
*   **Agentic Reasoning**: Uses a Gemini-powered Agent with Function Calling to intelligently route queries between a SQL database and an FAQ knowledge base.
*   **User-Provided API Keys**: Users can enter their own Gemini API keys in the sidebar, which are persisted locally and sent securely via headers.
*   **Intelligent Memory**: Analyzes conversation history to contextualize follow-up questions, ensuring the bot maintains state effectively.
*   **Cloud-Native Data Layer**:
    *   **PostgreSQL (Neon)**: Cloud-hosted product data for shoes.
    *   **Vector DB (Pinecone)**: Scalable FAQ retrieval using semantic search.
*   **Optimized Performance**: Native Python formatting for large SQL result sets and optimistic UI rendering for a lag-free experience.

---

## 🏗️ Architecture

```mermaid
graph TD
    %% Frontend
    User[👤 User] -->|Interacts| React["⚛️ React Frontend<br>(frontend/)"]
    
    %% API Layer
    React -->|HTTP / JSON + API Key Header| FastAPI["⚡ FastAPI Backend<br>(backend/main.py)"]
    
    %% Logic Layer
    subgraph Backend_Logic [AI Logic & Reasoning]
        FastAPI -->|Query| Agent["🤖 Gemini Agent<br>(backend/app/agent.py)"]
        
        Agent -->|Tool Call| FAQ["📚 FAQ Chain<br>(backend/app/faq.py)"]
        Agent -->|Tool Call| SQL["📊 SQL Chain<br>(backend/app/sql.py)"]
    end

    %% Data Layer
    subgraph Cloud_Storage [Cloud Data Layer]
        FAQ -->|Retrieval| Pinecone[(Pinecone Cloud<br>Vector DB)]
        SQL -->|Execute Query| Neon[(Neon PostgreSQL<br>Cloud DB)]
    end

    %% Admin Flow
    subgraph Admin_Tools [Management]
        CSV[FAQ CSV Data] -->|ingest| AdminScript["⚙️ Admin Script<br>(backend/app/admin_ingest_faqs.py)"]
        AdminScript -->|Push Vectors| Pinecone
    end
```

---

## 🛠️ Set-up & Execution

### 1. Requirements
Ensure you have **Node.js** (for frontend) and **Python 3.9+** (for backend) installed.

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

## 📂 Project Structure

*   **`frontend/`**: Vite + React application.
    *   `src/components/`: Glassmorphism UI components (Sidebar, ChatArea, Auth).
    *   `src/api.js`: Axios configuration with API key interceptors.
*   **`backend/`**: FastAPI server.
    *   `main.py`: API entry point and routing.
    *   `app/agent.py`: Agentic reasoning logic.
    *   `app/sql.py`: Text-to-SQL logic.
    *   `app/faq.py`: RAG pipeline and semantic FAQ answering.
    *   `app/db/`: Database models and connection management (SQLAlchemy).
*   **`web-scrapping/`**: Scripts used for initial data collection and preparation.
