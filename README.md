# 💬 E-Commerce Chatbot 

This is POC of an intelligent chatbot tailored for an e-commerce platform, enabling seamless user interactions by accurately identifying the intent behind user queries. It leverages real-time access to the platform's database, allowing it to provide precise and up-to-date responses.

Folder structure
1. app: All the code for chatbot
2. web-scraping: Code to scrap e-commerce website 

This chatbot currently supports two intents:

- **faq**: Triggered when users ask questions related to the platform's policies or general information. eg. Is online payment available?
- **sql**: Activated when users request product listings or information based on real-time database queries. eg. Show me all nike shoes below Rs. 3000.


## Architecture

```mermaid
graph LR
    %% Data Stage
    subgraph Data_Preparation [1. Data & Storage]
        FAQ_CSV[FAQ CSV] -->|faq.py| Chroma[(ChromaDB<br>Vector Store)]
        Scraped[Flipkart CSV] -->|csv_to_sqlite.py| SQLite[(SQLite<br>db.sqlite)]
    end

    %% Logic Stage
    subgraph AI_Core [2. Core AI Logic]
        Router["Semantic Router<br>(all-MiniLM-L6-v2)"]
        FAQ["FAQ Chain<br>(Groq RAG)"]
        SQL["SQL Chain<br>(Groq Text-to-SQL)"]
        
        Router -->|Intent=FAQ| FAQ
        Router -->|Intent=SQL| SQL
        FAQ_CSV -.-> |ingest| Chroma
        
        Chroma -.->|Retrieve Context| FAQ
        SQLite -.->|Execute Query| SQL
    end

    %% Serving Stage
    subgraph Deployment [3. User Interface]
        UI["🖥️ Streamlit App<br>(app/main.py)"]
        User[👤 User] -->|Chat Query| UI
        UI -->|Ask| Router
        FAQ -->|Return Answer| UI
        SQL -->|Return Answer| UI
    end

    %% Styling
    style Data_Preparation fill:#e1f5fe,stroke:#01579b
    style AI_Core fill:#fff3e0,stroke:#e65100
    style Deployment fill:#e8f5e9,stroke:#1b5e20
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
