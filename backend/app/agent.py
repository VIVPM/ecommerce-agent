import os
import logging
from google import genai
from google.genai import types
from dotenv import load_dotenv
from pathlib import Path

logger = logging.getLogger(__name__)

from app.sql import sql_chain
from app.faq import faq_chain
from app.llm_utils import with_retry
from app.cache import cache_get, cache_set

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


def compare_saved_products(query: str) -> str:
    """
    Use this tool ONLY when the user asks about the products THEY have SAVED or
    shortlisted — comparing them, ranking them, or choosing between them.
    Examples: "compare my saved shoes", "which of my saved ones is best value",
    "what did I save", "should I buy the saved Campus or the saved Sparx".
    Do NOT use this for searching the catalogue — that is search_product_database.
    """
    # Signature + docstring are what the model routes on; execution is dispatched
    # by the caller, which has the user_id this tool needs.
    return ""


def run_agent(optimized_query: str, user_id: int = None) -> str:
    """Route via the LLM (route_query), then execute the chosen tool.
    Non-streaming path — used by evaluate_agent.py."""
    tool, arg = route_query(optimized_query)
    if tool == 'search_product_database':
        return sql_chain(arg)
    if tool == 'compare_saved_products':
        if user_id is None:
            # No signed-in user (e.g. the eval harness) — nothing to compare against.
            return "I can only compare saved products for a signed-in user."
        from app.compare import compare_saved  # local import avoids a circular import
        return compare_saved(arg, user_id)
    return faq_chain(arg)


def route_query(optimized_query: str):
    """
    Routing-only variant used by the streaming path: asks Gemini which tool to use
    but does NOT execute it, so the caller can stream the tool's answer itself.
    Returns (tool_name, tool_arg). tool_name is None if the model picked no tool.
    Automatic function calling is disabled so response.function_calls is populated
    and the tool functions are not auto-run.
    """
    # Routing is temperature-0, so the choice is cacheable. Only the tool name is
    # stored; the arg is the query itself.
    cached_tool = cache_get("route", optimized_query)
    if cached_tool:
        return cached_tool, optimized_query

    client = gemini_client

    agent_instruction = """
    You are an intelligent e-commerce routing agent. Your ONLY job is to analyze the user's query
    and call the most appropriate tool (`search_product_database` or `search_faq_knowledge_base`).
    You must NOT attempt to answer the user's question directly. Always invoke a tool.
    Pass the user's EXACT query string into the tool you select.
    """

    try:
        response = with_retry(
            client.models.generate_content,
            model=GEMINI_MODEL,
            contents=optimized_query,
            config=types.GenerateContentConfig(
                system_instruction=agent_instruction,
                tools=[search_product_database, search_faq_knowledge_base, compare_saved_products],
                temperature=0.0,
                automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
            )
        )
        if response.function_calls:
            call = response.function_calls[0]
            arg = call.args.get('query', optimized_query)
            logger.info("Agent routed -> `%s` with arg `%s`", call.name, arg)
            cache_set("route", optimized_query, call.name)
            return call.name, arg
    except Exception as e:
        logger.error("Query routing failed: %s", e)

    # Fall back to the FAQ knowledge base for anything we couldn't route.
    return "search_faq_knowledge_base", optimized_query


