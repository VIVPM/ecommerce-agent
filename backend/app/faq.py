import os
from google import genai
from google.genai import types
import pandas as pd
from dotenv import load_dotenv
from pathlib import Path

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

# --- Gemini Embedding (gemini-embedding-001, 768-dim) ---
def get_embedding(text: str) -> list[float] | None:
    """
    Returns a 768-dimensional embedding vector for the given text using
    Google's gemini-embedding-001 model, or None on failure.
    Same approach as processor_bert.py in the log-classification project.
    """
    try:
        result = gemini_client.models.embed_content(
            model="models/gemini-embedding-001",
            contents=text,
            config=types.EmbedContentConfig(
                task_type="RETRIEVAL_DOCUMENT",
                output_dimensionality=1024  # Match existing Pinecone index dimension
            )
        )
        return list(result.embeddings[0].values)
    except Exception as e:
        print(f"Gemini embedding error: {e}")
        return None

def ingest_faq_data(path_or_file):
    print("Ingesting FAQ data into Pinecone Cloud Vector Store (gemini-embedding-001)...")

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
            print(f"  Skipping row {i} — embedding failed.")
            continue

        vectors.append({
            "id": vector_id,
            "values": embedding,
            "metadata": {"text": question, "answer": answer}
        })
        if (i + 1) % 10 == 0:
            print(f"  {i + 1}/{len(df)} embeddings done...")

    try:
        # Upsert in batches of 50
        batch_size = 50
        for start in range(0, len(vectors), batch_size):
            index.upsert(vectors=vectors[start:start + batch_size], namespace="faq_namespace")
        print(f"FAQ Data successfully ingested into Pinecone namespace: faq_namespace ({len(vectors)} vectors)")
    except Exception as e:
        print(f"Failed to ingest to Pinecone: {e}")

def get_relevant_qa(query):
    """Embed the query with gemini-embedding-001 and retrieve top FAQ matches from Pinecone."""
    try:
        query_vector = get_embedding(query)
        if query_vector is None:
            print("Failed to embed query.")
            return None

        pc = Pinecone(api_key=PINECONE_API_KEY)
        index = pc.Index(PINECONE_INDEX_NAME, host=PINECONE_HOST)

        results = index.query(
            vector=query_vector,
            top_k=2,
            namespace="faq_namespace",
            include_metadata=True
        )

        if not results.matches:
            print("Pinecone returned 0 matches.")
            return None

        docs = []
        for match in results.matches:
            doc = Document(
                page_content=match.metadata.get("text", ""),
                metadata=match.metadata
            )
            docs.append(doc)
            print(f"  Match: ID={match.id}, Score={match.score:.4f}")

        return docs
    except Exception as e:
        print(f"Error accessing Pinecone: {e}")
        import traceback
        traceback.print_exc()
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
        print(f"Gemini FAQ Error: {e}")
        if 'API_KEY_INVALID' in str(e):
            return "Error: Invalid Gemini API Key. Please update it in the sidebar."
        return f"Gemini API error occurred: {str(e)[:50]}..."


def faq_chain(query, api_key=None):
    docs = get_relevant_qa(query)
    
    if not docs:
        return "I am unable to answer your question right now because the FAQ data is not processed. Please contact support."
    
    # Join retrieved FAQ answers with clear separation so the LLM can reason over each one
    context = "\n".join([f"- {d.metadata.get('answer', '')}" for d in docs])
    
    print(f"FAQ Context for LLM:\n{context}")
    answer = generate_answer(query, context, api_key=api_key)
    return answer


if __name__ == '__main__':
    query = "Do you take cash as a payment option?"
    answer = faq_chain(query)
    print("Answer:", answer)