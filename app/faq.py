import os
import chromadb
from chromadb.utils import embedding_functions
from google import genai
import pandas as pd
import streamlit as st
from dotenv import load_dotenv
from pathlib import Path

env_path = Path(__file__).resolve().parent / ".env"
load_dotenv(dotenv_path=env_path)

GEMINI_MODEL = 'gemini-2.5-flash'
gemini_client = genai.Client(api_key=os.getenv('GEMINI_API_KEY'))
collection_name_faq = 'faqs'

faqs_path = Path(__file__).parent / "resources/faq_data.csv"

@st.cache_resource(show_spinner="Initializing Database Engine... ⏳")
def get_chroma_client():
    db_path = str(Path(__file__).parent / "chroma_db")
    return chromadb.PersistentClient(path=db_path)

@st.cache_resource(show_spinner="Warming up AI models for the first time... ⏳")
def get_embedding_function():
    return embedding_functions.SentenceTransformerEmbeddingFunction(
        model_name='sentence-transformers/all-MiniLM-L6-v2'
    )

def ingest_faq_data(path_or_file):
    client = get_chroma_client()
    try:
        client.delete_collection(collection_name_faq)
        print(f"Deleted existing collection: {collection_name_faq} for fresh upload")
    except Exception:
        pass

    print("Ingesting FAQ data into Chromadb...")
    ef = get_embedding_function()
    client = get_chroma_client()
    collection = client.create_collection(
        name=collection_name_faq,
        embedding_function=ef
    )
    df = pd.read_csv(path_or_file)
    docs = df['question'].to_list()
    metadata = [{'answer': ans} for ans in df['answer'].to_list()]
    ids = [f"id_{i}" for i in range(len(docs))]
    collection.add(
        documents=docs,
        metadatas=metadata,
        ids=ids
    )
    print(f"FAQ Data successfully ingested into Chroma collection: {collection_name_faq}")


def get_relevant_qa(query):
    ef = get_embedding_function()
    client = get_chroma_client()
    try:
        collection = client.get_collection(
            name=collection_name_faq,
            embedding_function=ef
        )
        if collection.count() == 0:
            return None
    except Exception as e:
        print(f"Collection not found or error accessing it: {e}")
        return None
    result = collection.query(
        query_texts=[query],
        n_results=2
    )
    return result


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
    result = get_relevant_qa(query)
    if not result or not result['metadatas'] or not result['metadatas'][0]:
        return "I am unable to answer your question right now because the FAQ data is not processed. Please contact support."
    
    context = "".join([r.get('answer') for r in result['metadatas'][0]])
    print("Context:", context)
    answer = generate_answer(query, context)
    return answer


if __name__ == '__main__':
    ingest_faq_data(faqs_path)
    # query = "what's your policy on defective products?"
    query = "Do you take cash as a payment option?"
    # result = get_relevant_qa(query)
    answer = faq_chain(query)
    print("Answer:",answer)