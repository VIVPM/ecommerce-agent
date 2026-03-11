import os
from google import genai
import pandas as pd
import streamlit as st
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
@st.cache_resource(show_spinner="Warming up Vector Embeddings... ⏳")
def get_embedding_function():
    # The existing Pinecone index 'langchain-chatbot' expects 1024 dimensions.
    # Switching to BAAI/bge-large-en-v1.5 to match the external project's configuration.
    return HuggingFaceEmbeddings(model_name='BAAI/bge-large-en-v1.5')

@st.cache_resource(show_spinner="Connecting to Pinecone Cloud Database... ⏳")
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
    try:
        vs = get_pinecone_vectorstore()
        # Pinecone similarity search returns a list of Document objects
        docs = vs.similarity_search(query, k=2)
        if not docs:
            return None
        return docs
    except Exception as e:
        print(f"Error accessing Pinecone: {e}")
        return None


def generate_answer(query, context):
    prompt = f'''Given the following context and question, generate answer based on this context only.
    If the answer is not found in the context, kindly state "I don't know". Don't try to make up an answer.
    
    CONTEXT: {context}
    
    QUESTION: {query}
    '''
    completion = gemini_client.models.generate_content(
        model=GEMINI_MODEL,
        contents=prompt
    )
    return completion.text


def faq_chain(query):
    docs = get_relevant_qa(query)
    
    if not docs:
        return "I am unable to answer your question right now because the FAQ data is not processed. Please contact support."
    
    # Langchain similarity_search returns Document objects. Extract the 'answer' from metadata.
    context = "".join([d.metadata.get('answer', '') for d in docs])
    
    print("Pinecone Context Retrieved:", context)
    answer = generate_answer(query, context)
    return answer


if __name__ == '__main__':
    query = "Do you take cash as a payment option?"
    answer = faq_chain(query)
    print("Answer:", answer)