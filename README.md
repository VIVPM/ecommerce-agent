# 💬 e-commerce chatbot (Gen AI RAG project using LLama3.3 and GROQ)

This is POC of an intelligent chatbot tailored for an e-commerce platform, enabling seamless user interactions by accurately identifying the intent behind user queries. It leverages real-time access to the platform's database, allowing it to provide precise and up-to-date responses.

Folder structure
1. app: All the code for chatbot
2. web-scraping: Code to scrap e-commerce website 

This chatbot currently supports two intents:

- **faq**: Triggered when users ask questions related to the platform's policies or general information. eg. Is online payment available?
- **sql**: Activated when users request product listings or information based on real-time database queries. eg. Show me all nike shoes below Rs. 3000.


## Architecture

```mermaid
graph TD
    %% User Interaction
    User[👤 User] -->|Query| UI["🖥️ Streamlit UI<br>(app/main.py)"]
    
    %% Routing
    UI -->|Query| Router["🔀 Semantic Router<br>(app/router.py)"]
    Router -->|Encode| Encoder["🧬 HuggingFace Encoder<br>(all-MiniLM-L6-v2)"]
    Encoder -.-> Router
    
    Router -->|If Intent == FAQ| FAQ["❓ FAQ Chain<br>(app/faq.py)"]
    Router -->|If Intent == SQL| SQL["🛒 SQL Chain<br>(app/sql.py)"]
    
    %% FAQ Pipeline
    subgraph FAQ_Pipeline [FAQ Pipeline]
        FAQ -->|Embed Query| Embed1["🧬 SentenceTransformer<br>(all-MiniLM-L6-v2)"]
        Embed1 -->|Retrieve Context| Chroma[(🗄️ ChromaDB<br>Vector Store)]
        Chroma -->|Top Results| LLM1["🧠 Groq LLM<br>(Answer Generation)"]
        LLM1 -->|Answer| Ans1["💡 FAQ Answer"]
    end
    
    %% SQL Pipeline
    subgraph SQL_Pipeline [SQL Pipeline]
        SQL -->|Prompt Schema + Query| LLM2["🧠 Groq LLM<br>(Text-to-SQL)"]
        LLM2 -->|SQL Query| DB[(🛢️ SQLite<br>db.sqlite)]
        DB -->|Query Results Context| LLM3["🧠 Groq LLM<br>(Data Comprehension)"]
        LLM3 -->|Formatted Output| Ans2["💡 Product Information"]
    end
    
    Ans1 -->|Return to UI| UI
    Ans2 -->|Return to UI| UI

    style UI fill:#e1f5fe,stroke:#01579b
    style Router fill:#fff3e0,stroke:#e65100
    style FAQ_Pipeline fill:#f3e5f5,stroke:#4a148c
    style SQL_Pipeline fill:#e8f5e9,stroke:#1b5e20
```


### Set-up & Execution

1. Run the following command to install all dependencies. 

    ```bash
    pip install -r app/requirements.txt
    ```

1. Inside app folder, create a .env file with your GROQ credentials as follows:
    ```text
    GROQ_MODEL=<Add the model name, e.g. llama-3.3-70b-versatile>
    GROQ_API_KEY=<Add your groq api key here>
    ```

1. Run the streamlit app by running the following command.

    ```bash
    streamlit run app/main.py
    ```
