import os
from google import genai
from google.genai import types
from dotenv import load_dotenv
from pathlib import Path

from app.sql import sql_chain
from app.faq import faq_chain

env_path = Path(__file__).resolve().parent / ".env"
load_dotenv(dotenv_path=env_path)

GEMINI_MODEL = 'gemini-2.5-flash'
gemini_client = genai.Client(api_key=os.getenv('GEMINI_API_KEY'))

def search_product_database(query: str) -> str:
    """
    Use this tool ONLY when the user is explicitly looking to buy shoes, searching for products,
    filtering by price, brand, rating, or asking about specific inventory (e.g., "Puma shoes under 5000", "cheapest running shoes").
    """
    return sql_chain(query)


def search_faq_knowledge_base(query: str) -> str:
    """
    Use this tool ONLY when the user is asking general questions about store policies, 
    returns, refunds, shipping times, payment methods, or contacting customer support.
    """
    return faq_chain(query)


def run_agent(optimized_query: str, api_key: str = None) -> str:
    """
    Passes the user's optimized query to Gemini with access to the SQL and FAQ tools.
    Gemini reasons about the intent and executes the most appropriate tool.
    """
    client = gemini_client
    if api_key:
        client = genai.Client(api_key=api_key)
        
    agent_instruction = """
    You are an intelligent e-commerce routing agent. Your ONLY job is to analyze the user's query 
    and call the most appropriate tool (`search_product_database` or `search_faq_knowledge_base`).
    You must NOT attempt to answer the user's question directly. Always invoke a tool.
    Pass the user's EXACT query string into the tool you select.
    """

    try:
        response = client.models.generate_content(
            model=GEMINI_MODEL,
            contents=optimized_query,
            config=types.GenerateContentConfig(
                system_instruction=agent_instruction,
                tools=[search_product_database, search_faq_knowledge_base],
                temperature=0.0, # Deterministic tool selection
            )
        )
        
        # Check if the model decided to call a function
        if response.function_calls:
            call = response.function_calls[0]
            function_name = call.name
            args = call.args
            
            query_arg = args.get('query', optimized_query)
            
            print(f"🕵️ Agent Reasoned -> Calling Tool: `{function_name}` with Args: `{query_arg}`")
            
            if function_name == 'search_product_database':
                return sql_chain(query_arg, api_key=api_key)
            elif function_name == 'search_faq_knowledge_base':
                return faq_chain(query_arg, api_key=api_key)
                
        return response.text if response.text else "I'm sorry, I encountered an issue routing your request."
        
    except Exception as e:
        print(f"Agent execution failed: {e}")
        return "I'm sorry, my reasoning engine encountered a technical error."
