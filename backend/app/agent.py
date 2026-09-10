import os
import re
import json
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
from app.llm_provider import PROVIDER, route_cloudflare, complete

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


def place_order(query: str) -> str:
    """
    Use this tool ONLY when the user wants to BUY / place an order / checkout the
    items currently in THEIR CART. Examples: "place my order", "checkout",
    "buy what's in my cart", "order these". This does not search or compare —
    it turns the existing cart into an order.
    """
    return ""   # user-scoped; dispatched by the caller, which has the user_id


def view_orders(query: str) -> str:
    """
    Use this tool ONLY when the user asks about THEIR existing orders — order
    history or status. Examples: "show my orders", "what did I order",
    "my order status", "track my order".
    """
    return ""   # user-scoped; dispatched by the caller


def cancel_order(query: str) -> str:
    """
    Use this tool ONLY when the user wants to CANCEL an order they placed.
    Examples: "cancel my order", "cancel order 12", "cancel my last order".
    """
    return ""   # user-scoped; dispatched by the caller


# Name + description for each tool, used by the Cloudflare routing path (which asks
# the model to pick one by name instead of Gemini's native function-calling).
_ROUTE_TOOLS = [
    ("search_product_database", search_product_database.__doc__),
    ("search_faq_knowledge_base", search_faq_knowledge_base.__doc__),
    ("compare_saved_products", compare_saved_products.__doc__),
    ("place_order", place_order.__doc__),
    ("view_orders", view_orders.__doc__),
    ("cancel_order", cancel_order.__doc__),
]


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
    if tool in ('place_order', 'view_orders', 'cancel_order'):
        if user_id is None:
            return "I can only manage orders for a signed-in user."
        from app import orders  # local import avoids a circular import
        return getattr(orders, tool)(user_id, arg)
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

    # gpt-oss doesn't do Gemini-style function calling, so on Cloudflare the model
    # picks a tool by name via JSON instead. The LLM still makes the choice.
    if PROVIDER == "CLOUDFLARE":
        name, arg = route_cloudflare(optimized_query, agent_instruction, _ROUTE_TOOLS)
        if name:
            logger.info("Agent routed -> `%s` with arg `%s` (cloudflare)", name, arg)
            cache_set("route", optimized_query, name)
            return name, arg
        return "search_faq_knowledge_base", optimized_query

    try:
        response = with_retry(
            client.models.generate_content,
            model=GEMINI_MODEL,
            contents=optimized_query,
            config=types.GenerateContentConfig(
                system_instruction=agent_instruction,
                tools=[search_product_database, search_faq_knowledge_base, compare_saved_products,
                       place_order, view_orders, cancel_order],
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


# --- Multi-intent query decomposition ---------------------------------------
# Split a message into sub-questions ONLY when it spans more than one capability
# (products / store policy / saved-item compare), so "return policy AND nike shoes
# AND compare my saved" gets each part answered instead of only the one tool the
# router picks. Single-intent queries pay nothing — a keyword pre-check skips the
# LLM call for them, and results are cached like routing.

_POLICY_RE = re.compile(
    r"\b(return|refund|deliver|shipping|ship|payment|cod|cash on delivery|cancel|"
    r"warranty|policy|support|contact|track|exchange|hours)\b", re.I)
_SAVED_RE = re.compile(r"\b(saved|shortlist|wishlist)\b", re.I)
_PRODUCT_RE = re.compile(
    r"\b(shoe|sneaker|boot|sandal|slipper|under\s*\d|below\s*\d|cheap|rating|rated|"
    r"brand|nike|puma|adidas|campus|sparx|skechers|reebok|bata)\b", re.I)


def _looks_multi_intent(q: str) -> bool:
    """Cheap pre-check: does the query touch >=2 distinct capabilities? Keeps the
    LLM decompose call off the ~95% of single-intent messages."""
    hits = sum(bool(rx.search(q or "")) for rx in (_POLICY_RE, _SAVED_RE, _PRODUCT_RE))
    return hits >= 2


_DECOMPOSE_SYS = """You split a shopper's message into independent sub-questions, but
ONLY when it genuinely asks about more than one of these SEPARATE capabilities:
  - PRODUCTS: searching/filtering shoes (price, brand, rating, stock, "cheaper than X")
  - POLICY:   store FAQ (returns, delivery, payment, cancellation, contacting support)
  - SAVED:    comparing/choosing among the shopper's OWN saved / shortlisted items
Rules:
  - If the message is a SINGLE request (even a long one), return it UNCHANGED as one item.
  - Split only on a true change of intent - NOT on every "and".
  - Each sub-question must be standalone and self-contained (resolve pronouns).
Output ONLY a JSON array of strings, nothing else."""


def decompose(query: str) -> list:
    """Sub-questions to answer; one item means single-intent (no split). The keyword
    pre-check keeps simple queries free; multi-part results are cached like routing."""
    if not _looks_multi_intent(query):
        return [query]
    cached = cache_get("decompose", query)
    if cached:
        try:
            parts = json.loads(cached)
            if isinstance(parts, list) and parts:
                return parts
        except json.JSONDecodeError:
            pass
    try:
        out = complete(query, system=_DECOMPOSE_SYS, temperature=0.0) or ""
        m = re.search(r"\[.*\]", out, re.DOTALL)
        if m:
            parts = json.loads(m.group(0))
            if isinstance(parts, list) and parts and all(isinstance(p, str) for p in parts):
                parts = parts[:4]   # safety cap
                if len(parts) > 1:
                    cache_set("decompose", query, json.dumps(parts))
                return parts
    except Exception as e:
        logger.error("Decompose failed: %s", e)
    return [query]


# --- Input guardrail --------------------------------------------------------
# Reject clearly off-topic messages (poems, weather, general knowledge) before
# spending any routing/tool calls. A keyword pre-check lets obvious shopping
# messages straight through; only the ambiguous ones pay for a cached LLM check.

_ORDER_RE = re.compile(r"\b(order|orders|cart|checkout|buy|bought|purchase)\b", re.I)


def _looks_shopping(q: str) -> bool:
    """Obvious shopping/store message? Then skip the guardrail LLM entirely."""
    return any(rx.search(q or "") for rx in (_POLICY_RE, _SAVED_RE, _PRODUCT_RE, _ORDER_RE))


_GUARDRAIL_SYS = """You are the input filter for a SHOE STORE shopping assistant.
Decide whether the user's message is something this assistant should handle: searching
or buying shoes; prices, brands, ratings, stock; store policies (delivery, returns,
payment, cancellation); the user's saved items, cart, or orders; or ordinary shopping
chit-chat (greetings, thanks, "show more", "any cheaper"). ANYTHING unrelated — writing
poems or code, general knowledge, weather, math, jokes, other stores — is off topic.
Reply with EXACTLY one word: SHOPPING or OFFTOPIC."""


def is_off_topic(query: str) -> bool:
    """True if the message isn't shopping/store related, so the caller can refuse it
    without running any tools. Fails OPEN (returns False) so a hiccup never blocks a
    real shopper."""
    if _looks_shopping(query):
        return False
    cached = cache_get("guardrail", query)
    if cached is not None:
        return cached == "OFFTOPIC"
    try:
        out = (complete(query, system=_GUARDRAIL_SYS, temperature=0.0) or "").strip().upper()
        verdict = "OFFTOPIC" if "OFFTOPIC" in out else "SHOPPING"
        cache_set("guardrail", query, verdict)
        return verdict == "OFFTOPIC"
    except Exception as e:
        logger.error("Guardrail failed: %s", e)
        return False


