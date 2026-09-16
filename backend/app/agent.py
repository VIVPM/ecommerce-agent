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


def compare_saved_products(action: str, query: str) -> str:
    """
    Use this tool ONLY for the user's SAVED / shortlisted products. Set `action` to:
      - "add":     save product(s) from the results just shown — "save 2", "save the
                   first and third", "add the Nike one to my saved", "shortlist #4".
      - "remove":  remove explicitly named/numbered saved products, or ALL currently
                   saved products when the user explicitly asks to clear/remove them.
      - "compare": compare / rank / choose between what they've saved — "compare my
                   saved shoes", "which of my saved is best value", "what did I save".
    Pass the user's message as `query`. Do NOT use this for searching the catalogue —
    that is search_product_database.
    """
    # Signature + docstring are what the model routes on; execution is dispatched
    # by the caller, which has the user_id (and chat history) this tool needs.
    return ""


def manage_orders(action: str, query: str) -> str:
    """
    Use this tool ONLY for actions on the user's OWN cart / orders. Set `action` to:
      - "add_results_to_cart": add explicitly numbered products from the latest result
                       list ("add items 2, 3 and 4 to my cart"). Do NOT use this if
                       the user says "saved" — use add_to_cart instead.
      - "add_to_cart": add explicitly numbered products from the user's SAVED list to
                       their cart ("add saved items 2 and 3 to my cart"). NEVER use
                       this for vague "add this" requests.
      - "place":  buy / checkout the items in their cart ("place my order", "checkout").
      - "view":   show their order history or status ("show my orders", "track my order").
      - "cancel": cancel an order they placed ("cancel my order", "cancel order 12").
    Pass the user's message as `query`. This does not search or compare products.
    """
    return ""   # user-scoped; dispatched by the caller, which has the user_id


def save_preference(query: str) -> str:
    """
    Use this tool when the user STATES a lasting shopping preference about themselves —
    a favourite brand, a usual budget / price ceiling, a preferred gender or shoe type.
    Examples: "I like Puma and Nike", "I usually buy cheap Adidas", "my budget is 3000",
    "I prefer men's running shoes". Do NOT use it to SEARCH for products, and do NOT use
    it when the user merely ASKS what they like — that's ordinary conversation.
    """
    return ""   # user-scoped; dispatched by the caller


# The tools the model may call. Anything that is NOT one of these — greetings, "is Puma
# any good?", "thanks", "what do I usually buy?" — is handled as plain conversation, no
# tool. Name + docstring are what the Cloudflare routing path picks by.
_ROUTE_TOOLS = [
    ("search_product_database", search_product_database.__doc__),
    ("search_faq_knowledge_base", search_faq_knowledge_base.__doc__),
    ("compare_saved_products", compare_saved_products.__doc__),
    ("manage_orders", manage_orders.__doc__),
    ("save_preference", save_preference.__doc__),
]

_GEMINI_TOOLS = [search_product_database, search_faq_knowledge_base,
                 compare_saved_products, manage_orders, save_preference]


def run_agent(optimized_query: str, user_id: int = None) -> str:
    """Route via the LLM (route_query), then execute the chosen tool. No tool means
    the message is ordinary conversation. Non-streaming path — used by evaluate_agent.py."""
    tool, arg, action = route_query(optimized_query)
    if tool is None:
        return converse(optimized_query)
    if tool == 'search_product_database':
        return sql_chain(arg)
    if tool == 'compare_saved_products':
        if user_id is None:
            # No signed-in user (e.g. the eval harness) — nothing to compare against.
            return "I can only compare saved products for a signed-in user."
        if action == "remove":
            from app.compare import remove_saved_items  # local import avoids a circular import
            return remove_saved_items(user_id, arg)
        if action == "add":
            # Saving resolves "save 2" against the products shown in chat; this
            # history-less path has none to resolve against.
            return "Saving from chat needs the product list you're looking at."
        from app.compare import compare_saved  # local import avoids a circular import
        return compare_saved(arg, user_id)
    if tool == 'manage_orders':
        if user_id is None:
            return "I can only manage orders for a signed-in user."
        from app import orders  # local import avoids a circular import
        return orders.manage_orders(user_id, action, arg)
    if tool == 'save_preference':
        if user_id is None:
            return "I can only save preferences for a signed-in user."
        from app import preferences  # local import avoids a circular import
        return preferences.note_preference(user_id, arg)
    return faq_chain(arg)


_CONVERSE_SENTINEL = "__converse__"

agent_instruction = """
You are an intelligent shopping assistant for a SHOE STORE. For each message, decide:
call ONE tool when the user needs a catalogue lookup, a store-policy answer, to save a
product they were shown, add/remove/compare their saved items, a cart/order action, or
wants to save a preference. Otherwise — greetings,
thanks, opinions ("is Puma any good?"), or a question about what they themselves like /
usually buy — do NOT call a tool; those are ordinary conversation the caller handles.
When you DO call a tool, pass the user's EXACT message as `query`.
"""


def route_query(optimized_query: str):
    """
    Routing-only variant used by the streaming path: asks Gemini which tool to use (if
    any) but does NOT execute it, so the caller can stream the answer itself.
    Returns (tool_name, tool_arg, action). tool_name is None when the message is ordinary
    conversation (no tool). `action` is set only for manage_orders
    (add_results_to_cart/add_to_cart/place/view/cancel) and compare_saved_products (add/remove/compare).
    Automatic function calling is disabled so response.function_calls is populated.
    """
    # Routing is temperature-0, so the decision is cacheable. We store "tool" or
    # "tool|action" (or the converse sentinel); the arg is always the query itself.
    cached = cache_get("route", optimized_query)
    if cached:
        if cached == _CONVERSE_SENTINEL:
            return None, optimized_query, None
        name, _, action = cached.partition("|")
        return name, optimized_query, (action or None)

    # gpt-oss doesn't do Gemini-style function calling, so on Cloudflare the model
    # picks a tool by name via JSON instead — including the order action. The LLM
    # still makes the choice.
    if PROVIDER == "CLOUDFLARE":
        name, arg, action = route_cloudflare(optimized_query, agent_instruction, _ROUTE_TOOLS)
        if name:
            logger.info("Agent routed -> `%s` (action=%s) (cloudflare)", name, action)
            cache_set("route", optimized_query, f"{name}|{action}" if action else name)
            return name, arg, action
        cache_set("route", optimized_query, _CONVERSE_SENTINEL)
        return None, optimized_query, None

    try:
        response = with_retry(
            gemini_client.models.generate_content,
            model=GEMINI_MODEL,
            contents=optimized_query,
            config=types.GenerateContentConfig(
                system_instruction=agent_instruction,
                tools=_GEMINI_TOOLS,
                temperature=0.0,
                automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
            )
        )
        if response.function_calls:
            call = response.function_calls[0]
            arg = call.args.get('query', optimized_query)
            action = call.args.get('action')   # set only by action tools
            logger.info("Agent routed -> `%s` (action=%s) with arg `%s`", call.name, action, arg)
            cache_set("route", optimized_query, f"{call.name}|{action}" if action else call.name)
            return call.name, arg, action
    except Exception as e:
        logger.error("Query routing failed: %s", e)
        return None, optimized_query, None   # fail into conversation, never crash

    # No tool call -> ordinary in-domain conversation.
    cache_set("route", optimized_query, _CONVERSE_SENTINEL)
    return None, optimized_query, None


# --- Conversation (no tool) -------------------------------------------------
# When the router picks no tool, the message is in-domain small talk or a question
# the agent can answer itself (opinions, "what do I usually buy?"). It replies
# naturally, grounded in what we remember about the shopper, and never invents
# catalogue facts — it offers to search when the user actually wants products.

_CONVERSE_SYS = """You are a warm, natural shopping assistant for an online SHOE STORE.
Chat like a friendly human. ONLY discuss shoes and this store (brands, styles, fit, the
shopper's own tastes and history, how the store works).

STYLE — this matters:
- Match the message's size. A greeting or "thanks" gets ONE short, friendly line — do
  NOT list what you can do or recite your capabilities unless the user asks.
- Never say "I'm an AI" or "I don't have feelings" — just answer naturally (e.g. "Doing
  great, thanks — what are you shopping for?").
- At most 1-2 sentences.

MEMORY — the prompt includes "What I remember about this shopper". When they ask about
their own past or tastes ("what was I looking at?", "what do I like?"), answer straight
from it in plain words ("Last time you were after Puma running shoes under 2500.").
If it says "nothing yet", say honestly that you don't have anything remembered yet.
Never make up history.

You CANNOT fetch or look anything up in this reply — never say "just a moment", "let me
fetch", or promise to pull something up. You also have no live catalogue data, so never
invent products, prices, or stock. If they'd want listings, suggest the exact thing to
ask ("Want me to search Puma running shoes under 2500 again?"). Prices are in rupees
(Rs.), never $. If something is clearly outside shoe shopping, gently steer back."""


def _converse_prompt(query: str, recalled: str = "") -> str:
    # Always state what's remembered — even "nothing yet" — so the model never fills
    # the gap by pretending it can go and fetch the shopper's history.
    return (f"What I remember about this shopper (use it if relevant):\n"
            f"{recalled or 'nothing yet'}\n\nShopper: {query}")


def converse(query: str, recalled: str = "") -> str:
    """Non-streaming conversational reply (used by run_agent / the eval)."""
    try:
        return complete(_converse_prompt(query, recalled), system=_CONVERSE_SYS,
                        temperature=0.7) or "How can I help with your shoe shopping today?"
    except Exception as e:
        logger.error("Converse failed: %s", e)
        return "How can I help with your shoe shopping today?"


async def converse_stream_async(query: str, recalled: str = ""):
    """Stream a conversational reply token by token, like the tool generators."""
    from app.llm_provider import stream as llm_stream
    try:
        async for tok in llm_stream(_converse_prompt(query, recalled),
                                    system=_CONVERSE_SYS, temperature=0.7):
            yield tok
    except Exception as e:
        logger.error("Converse stream failed: %s", e)
        yield "How can I help with your shoe shopping today?"


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
payment, cancellation); the user's saved items, cart, orders, or their own shopping
preferences and past ("I like Puma", "what do I usually buy"); or ordinary shopping
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


