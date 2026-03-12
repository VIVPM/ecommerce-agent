import os
from functools import lru_cache
from google import genai
import pandas as pd
from dotenv import load_dotenv
from pathlib import Path

# --- Langchain Pinecone Imports ---
from langchain_huggingface import HuggingFaceEmbeddings
from langchain_pinecone import PineconeVectorStore
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

# Ensure required Pinecone ENV vars are explicitly set for langchains underlying api client
os.environ["PINECONE_API_KEY"] = PINECONE_API_KEY or ""
os.environ["PINECONE_INDEX_NAME"] = PINECONE_INDEX_NAME or ""

# --- Cloud Vector Store Initialization ---
@lru_cache(maxsize=1)
def get_embedding_function():
    # The existing Pinecone index 'langchain-chatbot' expects 1024 dimensions.
    # Switching to BAAI/bge-large-en-v1.5 to match the external project's configuration.
    return HuggingFaceEmbeddings(model_name='BAAI/bge-large-en-v1.5')

@lru_cache(maxsize=1)
def get_pinecone_vectorstore():
    embeddings = get_embedding_function()
    try:
        return PineconeVectorStore(
            index_name=PINECONE_INDEX_NAME,
            pinecone_api_key=PINECONE_API_KEY,
            embedding=embeddings,
            namespace="faq_namespace" # Use a dedicated namespace so it doesn't collide with the other project
        )
    except Exception as e:
        raise RuntimeError(f"Failed to connect to Pinecone: {str(e)[:100]}")

def ingest_faq_data(path_or_file):
    print("Ingesting FAQ data into Pinecone Cloud Vector Store...")
    vs = get_pinecone_vectorstore()
    
    # Read CSV
    df = pd.read_csv(path_or_file)
    
    # Convert to Langchain Documents
    docs = []
    for i, row in df.iterrows():
        question = str(row.get('question', ''))
        answer = str(row.get('answer', ''))
        
        # We index the question as the page_content for similarity matching,
        # and store the target answer in the metadata to extract later.
        doc = Document(
            page_content=question, 
            metadata={"answer": answer, "id": f"faq_id_{i}"}
        )
        docs.append(doc)

    # Note: Depending on existing Pinecone namespaces, deleting old docs requires 
    # vector IDs. For simplicity in re-ingestion, we just overwrite using identical IDs.
    ids = [d.metadata["id"] for d in docs]
    try:
        vs.add_documents(docs, ids=ids)
        print(f"FAQ Data successfully ingested into Pinecone namespace: faq_namespace")
    except Exception as e:
        print(f"Failed to ingest to Pinecone: {e}")

def get_relevant_qa(query):
    """Query Pinecone directly using the raw client to avoid langchain filtering issues."""
    try:
        from pinecone import Pinecone
        
        embeddings = get_embedding_function()
        query_vector = embeddings.embed_query(query)
        
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
        
        # Convert Pinecone matches to Langchain-style Document objects for compatibility
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