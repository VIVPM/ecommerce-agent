"""Embed the store FAQ CSV and upload it to the Pinecone index."""
import sys
from pathlib import Path
from dotenv import load_dotenv

project_root = Path(__file__).resolve().parent.parent
sys.path.append(str(project_root))

load_dotenv(dotenv_path=Path(__file__).resolve().parent / ".env")

from app.faq import ingest_faq_data, faqs_path
from app.cache import cache_purge

if __name__ == '__main__':
    print("--- ADMIN TOOLS: KNOWLEDGE BASE INGESTION ---")
    print(f"Target CSV: {faqs_path}")

    confirm = input("Press Enter to overwrite the Pinecone FAQ Index with this CSV data... ")

    try:
        ingest_faq_data(faqs_path)
        purged = cache_purge('faq')
        print(f"Purged {purged} cached FAQ answers.")
        print("Success! The Agent will now use the updated knowledge base.")
    except Exception as e:
        print(f"Error during ingestion: {e}")
