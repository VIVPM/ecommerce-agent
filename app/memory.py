import os
from google import genai
from dotenv import load_dotenv
from pathlib import Path

env_path = Path(__file__).resolve().parent / ".env"
load_dotenv(dotenv_path=env_path)

GEMINI_MODEL = 'gemini-2.5-flash'
gemini_client = genai.Client(api_key=os.getenv('GEMINI_API_KEY'))

memory_prompt = """You are an AI assistant tasked with optimizing user queries for an e-commerce chatbot based on their conversation history.

The chatbot supports two main functions:
1. SQL Database Queries (searching for shoes with specific filters like price, brand, rating).
2. FAQ Queries (answering general questions about policies, returns, shipping, etc.).

Your objective:
Given the user's LATEST query and the recent conversation HISTORY, your job is to rewrite the LATEST query into a fully standalone, unambiguous sentence that can be understood entirely without the history.

Guidelines:
1. If the latest query contains ambiguous pronouns (it, they, those, this) or relative terms (cheaper, more, other colors), replace them with the actual subjects or context from the HISTORY.
2. IMPORTANT: If the user is asking for "other" options or alternatives, you MUST explicitly include what they are excluding based on the immediate history (e.g., "What payment methods are accepted other than cash on delivery?").
3. If the latest query is ALREADY standalone and clear (e.g., "Show me Puma shoes under 5000"), return the query EXACTLY as it is without changing anything.
4. Keep the rewritten query natural and concise. Do not add conversational filler.
5. Output ONLY the rewritten query string and absolutely nothing else. Neither quotes nor XML tags.

Example 1:
HISTORY: User: "Show me running shoes", Assistant: "Here are some running shoes..."
LATEST QUERY: "Are there any cheaper ones?"
OUTPUT: Are there any running shoes that are cheaper?

Example 2:
HISTORY: User: "whether cash on delivery payment is accepted?", Assistant: "Cash on delivery payment is accepted."
LATEST QUERY: "what other payments are accpeted?"
OUTPUT: What payment methods are accepted other than cash on delivery?

Example 3:
HISTORY: User: "What is your return policy?", Assistant: "You have 30 days to return."
LATEST QUERY: "Does that apply to clearance items?"
OUTPUT: Does the 30 day return policy apply to clearance items?

Example 3:
HISTORY: User: "Find Adidas shoes", Assistant: "Listing Adidas shoes..."
LATEST QUERY: "What about Nike?"
OUTPUT: Find Nike shoes.

Example 4:
HISTORY: User: "Show me top rated shoes", Assistant: "Here are the top rated shoes."
LATEST QUERY: "Do you have formal shoes in size 9?"
OUTPUT: Do you have formal shoes in size 9?
"""

def optimize_query(latest_query: str, history: list) -> str:
    """
    Takes the latest user query and a history of messages format [{'role': 'user/assistant', 'content': '...'}]
    and uses Gemini to rewrite the query so that it is contextually standalone.
    """
    if not history:
        return latest_query
        
    formatted_history = []
    # format the last k elements
    for msg in history:
        role = "User" if msg.get("role") == "user" else "Assistant"
        formatted_history.append(f"{role}: {msg.get('content')}")
        
    history_text = "\n".join(formatted_history)
    
    prompt = f"HISTORY:\n{history_text}\n\nLATEST QUERY: {latest_query}\nOUTPUT:"
    
    try:
        completion = gemini_client.models.generate_content(
            model=GEMINI_MODEL,
            contents=prompt,
            config=genai.types.GenerateContentConfig(
                system_instruction=memory_prompt,
                temperature=0.0, # zero temperature for reproducible deterministic rewrites
            )
        )
        return completion.text.strip()
    except Exception as e:
        print(f"Memory optimization failed: {e}")
        # Fallback to the original raw query if optimization fails to prevent chatbot disruption
        return latest_query
