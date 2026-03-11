# 🛒 E-Commerce Flipkart Chatbot

An intelligent, production-ready chatbot designed for e-commerce platforms. This bot leverages a modern agentic architecture and cloud-native databases to provide accurate, contextual, and high-performance responses for both product inquiries and general FAQs.

---

## 🚀 Key Features

*   **Agentic Reasoning**: Uses a Gemini-powered Agent with Function Calling to intelligently route queries between the SQL database and FAQ knowledge base.
*   **Contextual Memory**: An interceptor layer rewrites ambiguous follow-up questions (e.g., "Are there any cheaper ones?") by analyzing the last 5 chat messages, ensuring the bot never loses track of the conversation.
*   **Cloud-Native Scale**:
    *   **Database**: Migrated to **Neon PostgreSQL** for persistent, cloud-hosted product data.
    *   **Vector Store**: Uses **Pinecone Cloud** for a scalable, permanent FAQ knowledge base.
*   **High Performance**: Native Python formatting for large SQL result sets to reduce LLM latency for broad catalog searches.
*   **Enhanced UI**: Advanced Streamlit sidebar with chat-history search (titles & content) and 10-item pagination ("Load More").

---

## 🏗️ Architecture

```mermaid
graph TD
    %% User Layer
    User[👤 User] -->|Query| UI["🖥️ Streamlit App<br>(app/main.py)"]
    
    %% Logic Layer
    subgraph Logic_Flow [AI Logic & Reasoning]
        UI -->|Raw Query| Memory["🧠 Memory Optimizer<br>(app/memory.py)"]
        Memory -->|Contextualized Query| Agent["🤖 Gemini Agent<br>(app/agent.py)"]
        
        Agent -->|Tool Call| FAQ["📚 FAQ Chain<br>(app/faq.py)"]
        Agent -->|Tool Call| SQL["📊 SQL Chain<br>(app/sql.py)"]
    end

    %% Data Layer
    subgraph Cloud_Storage [Cloud Data Layer]
        FAQ -->|Retrieval| Pinecone[(Pinecone Cloud<br>Vector DB)]
        SQL -->|Execute Query| Neon[(Neon PostgreSQL<br>Cloud DB)]
    end

    %% Admin Flow
    subgraph Admin_Tools [Management]
        CSV[FAQ CSV Data] -->|ingest| AdminScript["⚙️ Admin Script<br>(admin_ingest_faqs.py)"]
        AdminScript -->|Push Vectors| Pinecone
    end

    %% Styling
    style Logic_Flow fill:#fff3e0,stroke:#e65100,stroke-width:2px
    style Cloud_Storage fill:#e1f5fe,stroke:#01579b,stroke-width:2px
    style User fill:#f5f5f5,stroke:#333
    style UI fill:#e8f5e9,stroke:#1b5e20
```

---

## 🛠️ Set-up & Execution

### 1. Installation
Install the necessary dependencies (SQLAlchemy, Pinecone, Langchain, and Google GenAI):

```bash
pip install -r requirements.txt
```

### 2. Environment Configuration
Create an `app/.env` file with the following variables:

```text
# LLM
GEMINI_API_KEY=your_gemini_api_key

# Database
DATABASE_URL=postgresql://user:pass@host/db?sslmode=require

# Vector Store
PINECONE_API_KEY=your_pinecone_key
PINECONE_INDEX_NAME=your_index_name
PINECONE_HOST=your_index_host_url
```

### 3. Initialize Knowledge Base
Sync your FAQ data from `app/resources/faq_data.csv` to the cloud index:

```bash
python app/admin_ingest_faqs.py
```

### 4. Run the Chatbot
Launch the Streamlit interface:

```bash
streamlit run app/main.py
```

---

## 📂 Project Structure

*   `app/main.py`: Main Streamlit entry point.
*   `app/agent.py`: Agentic reasoning and tool selection.
*   `app/memory.py`: Query optimization interceptor.
*   `app/sql.py`: Text-to-SQL logic and performance optimizations.
*   `app/faq.py`: RAG pipeline using Pinecone.
*   `app/admin_ingest_faqs.py`: CLI tool for administrative data indexing.
*   `app/db/`: Database models and connection management.
*   `app/ui/`: Modular UI components for auth and chat history.
