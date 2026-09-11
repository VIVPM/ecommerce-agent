import os
import asyncio
import logging
from google import genai
from google.genai import types
import pandas as pd
from dotenv import load_dotenv
from pathlib import Path

logger = logging.getLogger(__name__)

# --- Pinecone Imports ---
from pinecone import Pinecone
from langchain_core.documents import Document

from app.llm_utils import with_retry
from app.cache import cache_get, cache_set
from app.llm_provider import complete, stream as llm_stream

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

# --- Gemini Embedding (gemini-embedding-001, 1024-dim) ---
def get_embedding(text: str) -> list[float] | None:
    """
    Returns a 1024-dimensional embedding vector for the given text using
    Google's gemini-embedding-001 model, or None on failure.
    """
    try:
        client = gemini_client
            
        def _embed():
            return client.models.embed_content(
                model="models/gemini-embedding-001",
                contents=text,
                config=types.EmbedContentConfig(
                    task_type="RETRIEVAL_DOCUMENT",
                    output_dimensionality=1024  # Match existing Pinecone index dimension
                )
            )
        result = with_retry(_embed)
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

def get_relevant_qa(query):
    """Embed the query with gemini-embedding-001 and retrieve top FAQ matches from Pinecone."""
    try:
        query_vector = get_embedding(query)
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


def _faq_prompt(query, context):
    return f'''You are a helpful customer support assistant for an e-commerce store.
    Answer the user's question using ONLY the FAQ context provided below.
    The context contains relevant FAQ answers — use them to form a helpful, natural response.
    If the question is nothing to do with this store — the weather, sport, general
    trivia — do not just say "I don't know", which leaves the shopper nowhere.
    Say briefly that you can't help with that, and name what you CAN do: find
    shoes in the catalogue and answer questions about store policies like
    delivery, returns, payment and cancellation.

    GIVE EVERYTHING RELEVANT THAT IS PRESENT. If the context holds several details
    that bear on the question — an email address, a phone number, a chat channel,
    opening hours — include them ALL. Asked "how do I contact support", the answer
    must lead with the HOW (email address, phone number, chat) and treat hours as
    supporting detail; answering with opening hours alone tells the shopper WHEN
    but not HOW, which doesn't answer the question. Being cautious about what is
    missing is right; withholding what is present is not.

    CRITICAL — do not substitute a near-miss answer. Retrieval returns the CLOSEST
    entries, which is not the same as the RIGHT one. If the context only covers a
    narrower or adjacent case than what was asked, do NOT present it as the general
    answer. Say what the policy does cover, state plainly that the specific case
    asked about isn't covered, and suggest contacting support.
    Worked example — asked "how long does shipping take?" when the only entry is
    about same-day delivery in select metro cities before 11 AM: the honest answer
    is that same-day delivery exists for those cities under that cut-off, and that
    standard delivery times for everywhere else aren't listed. Answering "your
    order arrives the same day" would be wrong for most customers.
    Never invent a number, timeframe, fee or condition that is not in the context.

    FAQ CONTEXT:
    {context}

    CUSTOMER QUESTION: {query}
    '''


def _faq_error_text(e):
    logger.error("Gemini FAQ Error: %s", e)
    if 'API_KEY_INVALID' in str(e):
        return "Error: Invalid Gemini API Key. Please update it in the sidebar."
    return f"Gemini API error occurred: {str(e)[:50]}..."


def generate_answer(query, context):
    try:
        return complete(_faq_prompt(query, context), temperature=0.2, model=GEMINI_MODEL)
    except Exception as e:
        return _faq_error_text(e)


def faq_chain(query):
    docs = get_relevant_qa(query)

    if not docs:
        return "I am unable to answer your question right now because the FAQ data is not processed. Please contact support."

    # Join retrieved FAQ answers with clear separation so the LLM can reason over each one
    context = "\n".join([f"- {d.metadata.get('answer', '')}" for d in docs])

    logger.debug("FAQ Context for LLM:\n%s", context)
    answer = generate_answer(query, context)
    return answer


async def faq_chain_stream_async(query):
    """Async streaming variant. Pinecone retrieval (sync) runs in a thread; the LLM
    answer streams via the async client so the event loop isn't blocked.

    The finished answer is cached — the FAQ corpus is static, so a hit skips
    embedding + Pinecone + generation entirely and lands instantly. Errors are
    never cached."""
    cached = await asyncio.to_thread(cache_get, "faq", query)
    if cached:
        yield cached  # a generator can't be cached, so the assembled text is
        return        # re-emitted as one chunk (instant instead of fake-streamed)

    docs = await asyncio.to_thread(get_relevant_qa, query)
    if not docs:
        yield "I am unable to answer your question right now because the FAQ data is not processed. Please contact support."
        return
    context = "\n".join([f"- {d.metadata.get('answer', '')}" for d in docs])
    parts = []
    try:
        async for tok in llm_stream(_faq_prompt(query, context), temperature=0.2, model=GEMINI_MODEL):
            parts.append(tok)
            yield tok
    except Exception as e:
        yield _faq_error_text(e)
        return  # don't cache a failed answer
    await asyncio.to_thread(cache_set, "faq", query, "".join(parts))


if __name__ == '__main__':
    query = "Do you take cash as a payment option?"
    answer = faq_chain(query)
    logger.info("Answer: %s", answer)