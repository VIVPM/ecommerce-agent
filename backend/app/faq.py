import os
import logging
from google import genai
from google.genai import types
import pandas as pd
from dotenv import load_dotenv
from pathlib import Path

logger = logging.getLogger(__name__)

# --- Pinecone Imports ---
from pinecone import Pinecone
from langchain.docstore.document import Document

env_path = Path(__file__).resolve().parent / ".env"
load_dotenv(dotenv_path=env_path)

GEMINI_MODEL = 'gemini-2.5-flash'
gemini_client = genai.Client(api_key=os.getenv('GEMINI_API_KEY'))
collection_name_faq = 'faqs'

faqs_path = Path(__file__).parent / "resources/faq_data.csv"

PINECONE_API_KEY = os.getenv("PINECONE_API_KEY")
PINECONE_INDEX_NAME = os.getenv("PINECONE_INDEX_NAME")
PINECONE_HOST = os.getenv("PINECONE_HOST")

if not all([PINECONE_API_KEY, PINECONE_INDEX_NAME, PINECONE_HOST]):
    raise ValueError("PINECONE_API_KEY, PINECONE_INDEX_NAME, and PINECONE_HOST must be set in .env. Cloud vector store is required.")

# --- Gemini Embedding (gemini-embedding-001, 768-dim) ---
def get_embedding(text: str, api_key: str = None) -> list[float] | None:
    """
    Returns a 1024-dimensional embedding vector for the given text using
    Google's gemini-embedding-001 model, or None on failure.
    Same approach as processor_bert.py in the log-classification project.
    """
    try:
        client = gemini_client
        if api_key:
            client = genai.Client(api_key=api_key)
            
        result = client.models.embed_content(
            model="models/gemini-embedding-001",
            contents=text,
            config=types.EmbedContentConfig(
                task_type="RETRIEVAL_DOCUMENT",
                output_dimensionality=1024  # Match existing Pinecone index dimension
            )
        )
        return list(result.embeddings[0].values)
    except Exception as e:
        logger.error("Gemini embedding error: %s", e)
        return None

def ingest_faq_data(path_or_file):
    logger.info("Ingesting FAQ data into Pinecone Cloud Vector Store (gemini-embedding-001)...")

    df = pd.read_csv(path_or_file)

    pc = Pinecone(api_key=PINECONE_API_KEY)
    index = pc.Index(PINECONE_INDEX_NAME, host=PINECONE_HOST)

    vectors = []
    for i, row in df.iterrows():
        question = str(row.get('question', ''))
        answer = str(row.get('answer', ''))
        vector_id = f"faq_id_{i}"

        embedding = get_embedding(question)
        if embedding is None:
            logger.warning("Skipping row %d — embedding failed.", i)
            continue

        vectors.append({
            "id": vector_id,
            "values": embedding,
            "metadata": {"text": question, "answer": answer}
        })
        if (i + 1) % 10 == 0:
            logger.info("%d/%d embeddings done...", i + 1, len(df))

    try:
        # Upsert in batches of 50
        batch_size = 50
        for start in range(0, len(vectors), batch_size):
            index.upsert(vectors=vectors[start:start + batch_size], namespace="faq_namespace")
        logger.info("FAQ Data successfully ingested into Pinecone namespace: faq_namespace (%d vectors)", len(vectors))
    except Exception as e:
        logger.error("Failed to ingest to Pinecone: %s", e)

def get_relevant_qa(query, api_key=None):
    """Embed the query with gemini-embedding-001 and retrieve top FAQ matches from Pinecone."""
    try:
        query_vector = get_embedding(query, api_key=api_key)
        if query_vector is None:
            logger.error("Failed to embed query.")
            return None

        pc = Pinecone(api_key=PINECONE_API_KEY)
        index = pc.Index(PINECONE_INDEX_NAME, host=PINECONE_HOST)

        results = index.query(
            vector=query_vector,
            top_k=4,
            namespace="faq_namespace",
            include_metadata=True
        )

        if not results.matches:
            logger.info("Pinecone returned 0 matches.")
            return None

        docs = []
        for match in results.matches:
            doc = Document(
                page_content=match.metadata.get("text", ""),
                metadata=match.metadata
            )
            docs.append(doc)
            logger.debug("Match: ID=%s, Score=%.4f", match.id, match.score)

        return docs
    except Exception as e:
        logger.error("Error accessing Pinecone: %s", e, exc_info=True)
        return None


def generate_answer(query, context, api_key=None):
    try:
        client = gemini_client
        if api_key:
            client = genai.Client(api_key=api_key)
            
        prompt = f'''You are a helpful customer support assistant for an e-commerce store.
        Answer the user's question using ONLY the FAQ context provided below.
        The context contains relevant FAQ answers — use them to form a helpful, natural response.
        Only say "I don't know" if the context is completely unrelated to the question.
        
        FAQ CONTEXT:
        {context}
        
        CUSTOMER QUESTION: {query}
        '''
        completion = client.models.generate_content(
            model=GEMINI_MODEL,
            contents=prompt,
            config=genai.types.GenerateContentConfig(
                temperature=0.2,
            )
        )
        return completion.text
    except Exception as e:
        logger.error("Gemini FAQ Error: %s", e)
        if 'API_KEY_INVALID' in str(e):
            return "Error: Invalid Gemini API Key. Please update it in the sidebar."
        return f"Gemini API error occurred: {str(e)[:50]}..."


def faq_chain(query, api_key=None):
    docs = get_relevant_qa(query, api_key=api_key)

    if not docs:
        return "I am unable to answer your question right now because the FAQ data is not processed. Please contact support."

    # Join retrieved FAQ answers with clear separation so the LLM can reason over each one
    context = "\n".join([f"- {d.metadata.get('answer', '')}" for d in docs])

    logger.debug("FAQ Context for LLM:\n%s", context)
    answer = generate_answer(query, context, api_key=api_key)
    return answer


if __name__ == '__main__':
    query = "Do you take cash as a payment option?"
    answer = faq_chain(query)
    logger.info("Answer: %s", answer)