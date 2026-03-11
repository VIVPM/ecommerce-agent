import sys
from pathlib import Path

# Add the project root to sys.path so 'app.xyz' imports work
project_root = Path(__file__).resolve().parent.parent
sys.path.append(str(project_root))

from app.faq import ingest_faq_data, faqs_path

if __name__ == '__main__':
    print("--- ADMIN TOOLS: KNOWLEDGE BASE INGESTION ---")
    print(f"Target CSV: {faqs_path}")
    
    confirm = input("Press Enter to overwrite the Pinecone FAQ Index with this CSV data... ")
    
    try:
        ingest_faq_data(faqs_path)
        print("Success! The Chatbot will now use the updated knowledge base.")
    except Exception as e:
        print(f"Error during ingestion: {e}")
