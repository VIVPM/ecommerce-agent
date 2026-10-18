"""The shopping agent: one LangChain agent, four tools, two interchangeable providers.

_MULTI_HINT gates a second step: without a hint of a second request the run ends
after its tool with no extra model call. _FACTS_RE drops a closing line that
restates catalogue data, because the tool output above it holds the facts.

A product search replaces ctx.history with its own output, so "add the first one"
in the same message resolves only against those results and, with none listed,
asks instead of reaching back to an older list. A search that lists nothing also
returns a model-only note: without it the dead-end answer read as finished and a
cart or save request in the same message was dropped (3/3 runs).
"""
import asyncio
import logging
import re
from dataclasses import dataclass

from langchain.agents import create_agent
from langchain.agents.middleware import wrap_model_call
from langchain.tools import ToolRuntime, tool
from langchain_core.messages import AIMessage, ToolMessage
from langgraph.config import get_stream_writer

from app.cache import cache_get, cache_set
from app.compare import (compare_saved_stream_async,
                         remove_saved_items_stream_async,
                         save_from_results_stream_async)
from app.faq import faq_chain_stream_async
from app.orders import manage_orders_stream_async
from app.llm_provider import ROUTING_MODEL, chat, complete
from app.sql import sql_chain_stream_async

logger = logging.getLogger(__name__)

FAQ_TOOL = "search_faq_knowledge_base"


@dataclass
class Ctx:
    """Per-request data the tools need but the model must never see."""
    user_id: int | None = None
    raw_query: str = ""
    history: list | None = None
    memory: str = ""
    shown: int = 0


_MULTI_HINT = re.compile(r"\b(?:and|also|plus|then|as well as)\b|\?\s*\S|;|,\s*\S|\.\s+[A-Za-z]",
                         re.IGNORECASE)
_FACTS_RE = re.compile(r"Rs\.?\s*\d|₹|\[View Product\]|https?://|\d\.\d\s*(?:★|stars?|rating)", re.I)


def _emit(payload: dict):
    """Push one chunk onto the agent's custom stream. No-ops when a tool is called
    outside the agent (the eval harness, any sync caller)."""
    try:
        get_stream_writer()(payload)
    except RuntimeError:
        pass


async def _drain(agen, status: str, tool_name: str, ctx: Ctx | None = None) -> str:
    """Stream a tool's verified output to the shopper as-is and return it to the
    model, which may call another tool but never re-types this text."""
    _emit({"status": status, "tool": tool_name})
    if ctx is not None:
        if ctx.shown:
            _emit({"token": "\n\n---\n\n"})
        ctx.shown += 1
    parts = []
    async for token in agen:
        if token:
            parts.append(token)
            _emit({"token": token})
    return "".join(parts)


@tool
async def search_product_database(query: str, runtime: ToolRuntime[Ctx]) -> str:
    """
    Use this tool ONLY when the user is explicitly looking to buy shoes, searching for products,
    filtering by price, brand, rating, or asking about specific inventory (e.g., "Puma shoes under 5000", "cheapest running shoes").
    """
    ctx = runtime.context
    text = await _drain(sql_chain_stream_async(query),
                        "Searching products...", "search_product_database", ctx)
    if ctx is not None:
        ctx.history = [{"role": "assistant", "content": text}]
    if "[View Product]" not in text:
        return (text + "\n\n[Not shown to the shopper: this search listed NO products. "
                "If the message also asks to add or save one, call that tool now; it "
                "will tell the shopper there is nothing to add.]")
    return text


@tool
async def search_faq_knowledge_base(query: str, runtime: ToolRuntime[Ctx]) -> str:
    """
    Use this tool ONLY when the user is asking general questions about store policies,
    returns, refunds, shipping times, payment methods, or contacting customer support.
    """
    return await _drain(faq_chain_stream_async(query),
                        "Searching the knowledge base...", FAQ_TOOL, runtime.context)


async def _signed_out():
    yield "I can only compare saved products for a signed-in user."


_SAVED_STATUS = {"add": "Saving that for you...",
                 "remove": "Updating your saved list...",
                 "compare": "Reviewing your saved products..."}


@tool
async def manage_saved(action: str, query: str, runtime: ToolRuntime[Ctx]) -> str:
    """
    Use this tool for anything to do with the user's SAVED items (their shortlist
    or wishlist). Set `action` to one of:

    - "add" - save a product they were just shown. "save 2", "save the first and
      third", "save the Nike one", "add that to my list".
    - "remove" - take items off the saved list. "remove saved item 2", "delete the
      Puma from my saved", "clear my saved items".
    - "compare" - compare, rank or choose between what they have saved. "compare my
      saved shoes", "which of my saved is best value", "what did I save".

    Pass the user's EXACT message as `query`.
    Do NOT use this to search the catalogue - that is search_product_database. Do
    NOT use it for items already ORDERED - that is order_history.
    """
    ctx = runtime.context
    user_id = ctx.user_id if ctx else None
    if user_id is None:
        return await _drain(_signed_out(), _SAVED_STATUS["compare"], "manage_saved", ctx)

    raw = (ctx.raw_query or query) if ctx else query

    action = (action or "").strip().lower()
    if action == "add":
        agen = save_from_results_stream_async(raw, user_id, ctx.history if ctx else None)
    elif action == "remove":
        agen = remove_saved_items_stream_async(raw, user_id)
    else:
        if action != "compare":
            logger.warning("manage_saved: unknown action %r - treating as compare.", action)
        agen = compare_saved_stream_async(query, user_id)
    return await _drain(agen, _SAVED_STATUS.get(action, _SAVED_STATUS["compare"]),
                        "manage_saved", ctx)


async def _signed_out_orders():
    yield "I can only handle carts and orders for a signed-in user."


_ORDER_STATUS = {"add_to_cart": "Adding that to your cart...",
                 "add_results_to_cart": "Adding that to your cart...",
                 "place": "Placing your order...",
                 "cancel": "Cancelling that order...",
                 "view": "Looking up your orders..."}


@tool
async def manage_orders(action: str, query: str, runtime: ToolRuntime[Ctx]) -> str:
    """
    Use this tool for the user's CART and ORDERS. Set `action` to one of:

    - "add_results_to_cart" - add products from the list you just showed them.
      "add items 2 and 3 to my cart", "put the first one in my cart".
    - "add_to_cart" - add from their SAVED list, when they say so explicitly.
      "add saved items 1 and 2 to my cart", "add my saved Puma to the cart".
    - "place" - buy what is in the cart. "place my order", "checkout", "buy it".
    - "cancel" - cancel an order they already placed. "cancel order 12".
    - "view" - what they have ordered before: history, spend, an order's status.
      "what have I ordered", "show my orders", "how much have I spent".

    Pass the user's EXACT message as `query`.
    Do NOT use this to SAVE or compare shortlisted items - that is manage_saved.
    Do NOT use it to search the catalogue - that is search_product_database.
    """
    ctx = runtime.context
    user_id = ctx.user_id if ctx else None
    if user_id is None:
        return await _drain(_signed_out_orders(), _ORDER_STATUS["view"], "manage_orders", ctx)

    raw = (ctx.raw_query or query) if ctx else query
    action = (action or "").strip().lower()
    agen = manage_orders_stream_async(action, raw, user_id, ctx.history if ctx else None)
    return await _drain(agen, _ORDER_STATUS.get(action, _ORDER_STATUS["view"]),
                        "manage_orders", ctx)


TOOLS = [search_product_database, search_faq_knowledge_base, manage_saved, manage_orders]
_TOOL_NAMES = {t.name for t in TOOLS}

agent_instruction = """You are a warm, natural shopping assistant for an online SHOE STORE.

WHEN A TOOL FITS, CALL IT. The tool descriptions say what each is for; read them and
pick the best match, passing the user's EXACT query string as the argument.

SEVERAL REQUESTS IN ONE MESSAGE. If the message asks for two or more different things
(a search and a store-policy question, a search and a cart action), call ONE tool per step, in the order asked, passing each tool only the part
of the message it handles. A single request with several filters ("Nike under 3000
rated above 4") is ONE search. A cart or save action that refers to "the first one"
or "those" comes AFTER the search that shows them, and is still called when that
search found nothing: the action tool tells the shopper there is nothing to add.

AFTER A TOOL. The shopper already sees every tool's output exactly as returned. When
nothing else was asked, end with an empty reply. Never repeat products, prices,
ratings, links or policy text. At most one short linking sentence.

WHEN NO TOOL FITS, JUST REPLY. Greetings, thanks, opinions ("is Puma any good?"),
questions about the shopper's own tastes or history — these are ordinary conversation,
not searches. Do NOT force them into a tool.

STYLE — this matters:
- Match the message's size. A greeting or "thanks" gets ONE short, friendly line. Do
  NOT list what you can do or recite your capabilities unless you are asked.
- At most 1-2 sentences.
- Never say "I'm an AI" or "I don't have feelings" — just answer naturally
  ("Doing great, thanks — what are you shopping for?").
- Prices are in rupees (Rs.), never dollars.

WHAT YOU CANNOT DO IN A PLAIN REPLY. You have no live catalogue data here, so never
invent products, prices or stock, and never promise to go and look: no "just a moment",
no "let me fetch that". If the shopper wants listings, say the exact thing they could
ask ("Want me to search Puma running shoes under 2500?").

A STATED PREFERENCE ("I like Puma", "my budget is under 3000", "I wear size 9") is
remembered automatically after every message. Acknowledge it in one short line that
names it, and if the same message also asks for products, search for them too.

WHAT YOU REMEMBER is the ABOUT THIS SHOPPER block at the top of these instructions.
When the shopper asks about their own tastes, brands or budget, answer from that
block. Only when that block says you remember nothing yet, say so plainly, without
implying you will retrieve it.

PERSONAL DETAILS. Never repeat back a phone number, email address, card number or
address the shopper types, and never claim you will store or use it: you cannot save
contact or payment details. Say you can't store those and that the shopper should not
share them here.

Only discuss shoes and this store. Steer anything else gently back."""


def _tool_call(name: str, query: str, call_id: str, action: str = "") -> AIMessage:
    args = {"query": query}
    if action:
        args["action"] = action
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": call_id}])


@wrap_model_call
async def _route(request, handler):
    """Routing is temperature-0, so the tool choice is cacheable. Only the tool
    NAME is stored; the argument is the query itself.
    """
    messages = request.state["messages"]
    query = next(m.content for m in messages if getattr(m, "type", "") == "human")
    if any(isinstance(m, ToolMessage) for m in messages):
        return await _next_step(request, handler, query)

    cached = await asyncio.to_thread(cache_get, "route", query)
    if cached:
        name, _, action = cached.partition(":")
        if name in _TOOL_NAMES:
            return _tool_call(name, query, "cached-route", action)
        logger.warning("Stale cached route %r is not a live tool - re-routing.", cached)

    memory = getattr(getattr(request.runtime, "context", None), "memory", "") or ""
    overrides = {"model": chat(temperature=0.0, model=ROUTING_MODEL)}
    if memory:
        # Memory goes BEFORE the instruction: appended last, the "remember nothing"
        # line made flash return an empty reply (no tool, 0 tokens) for brand+price
        # searches like "Nike shoes under 5000", 3/3 runs.
        overrides["system_prompt"] = f"ABOUT THIS SHOPPER:\n{memory}\n\n{agent_instruction}"
    request = request.override(**overrides)

    response = await handler(request)
    message = response.result[0] if hasattr(response, "result") else response

    if getattr(message, "tool_calls", None):
        _one_call(message)
        call = message.tool_calls[0]
        key = call["name"]
        if action := (call.get("args") or {}).get("action"):
            key = f"{key}:{action}"
        await asyncio.to_thread(cache_set, "route", query, key)
        return response

    logger.info("No tool for %r — answering conversationally.", query[:120])
    text = getattr(message, "content", "") or ""
    if isinstance(text, list):
        text = "".join(b.get("text", "") for b in text if isinstance(b, dict))
    if text.strip():
        _emit({"status": "Thinking...", "tool": "converse"})
        _emit({"token": text})
    return response


def _one_call(message) -> None:
    """Run tools one at a time: parallel calls could add to the cart before the
    search that it refers to has finished."""
    if len(message.tool_calls) > 1:
        message.tool_calls = message.tool_calls[:1]


async def _next_step(request, handler, query: str):
    """After a tool: stop at once for a single request (no extra model call),
    otherwise let the model call the next tool or add one linking sentence."""
    if not _MULTI_HINT.search(query):
        return AIMessage(content="")
    memory = getattr(getattr(request.runtime, "context", None), "memory", "") or ""
    overrides = {"model": chat(temperature=0.0, model=ROUTING_MODEL)}
    if memory:
        overrides["system_prompt"] = f"ABOUT THIS SHOPPER:\n{memory}\n\n{agent_instruction}"
    response = await handler(request.override(**overrides))
    message = response.result[0] if hasattr(response, "result") else response
    if getattr(message, "tool_calls", None):
        _one_call(message)
        return response
    text = getattr(message, "content", "") or ""
    if isinstance(text, list):
        text = "".join(b.get("text", "") for b in text if isinstance(b, dict))
    text = text.strip()
    if text and not _FACTS_RE.search(text) and len(text) <= 300:
        _emit({"token": f"\n\n{text}"})
    return response


agent = create_agent(
    model=chat(temperature=0.0, model=ROUTING_MODEL),
    tools=TOOLS,
    system_prompt=agent_instruction,
    middleware=[_route],
    context_schema=Ctx,
)


def astream_agent(query: str, user_id: int | None = None,
                  raw_query: str = "", history: list | None = None, memory: str = ""):
    """Async: yields the tools' status/token dicts as they are produced. This is
    the streaming path used by the API; one run may call several tools in turn."""
    return agent.astream(
        {"messages": [{"role": "user", "content": query}]},
        stream_mode="custom",
        context=Ctx(user_id=user_id, raw_query=raw_query or query,
                    history=list(history or []), memory=memory),
    )


_POLICY_RE = re.compile(
    r"\b(return|refund|deliver|shipping|ship|payment|cod|cash on delivery|cancel|"
    r"warranty|policy|support|contact|track|exchange|hours)\b", re.I)

_SAVED_RE = re.compile(r"\b(saved|shortlist|wishlist)\b", re.I)

_PRODUCT_RE = re.compile(
    r"\b(shoe|sneaker|boot|sandal|slipper|under\s*\d|below\s*\d|cheap|rating|rated|"
    r"brand|nike|puma|adidas|campus|sparx|skechers|reebok|bata)\b", re.I)

_ORDER_RE = re.compile(r"\b(order|orders|cart|checkout|buy|bought|purchase)\b", re.I)

_COMPARE_RE = re.compile(r"\b(compare|vs|versus|these|those|first|second|third|last one)\b", re.I)


def _looks_shopping(q: str) -> bool:
    """Obvious shopping/store message? Then skip the guardrail LLM entirely."""
    return any(rx.search(q or "") for rx in (_POLICY_RE, _SAVED_RE, _PRODUCT_RE, _ORDER_RE, _COMPARE_RE))


_GUARDRAIL_SYS = """You are the input filter for a SHOE STORE shopping assistant.
Decide whether the user's message is something this assistant should handle: searching
or buying shoes; prices, brands, ratings, stock; store policies (delivery, returns,
payment, cancellation); the user's saved items, cart, or orders; the user's shopping
preferences or what the assistant remembers about them; or ordinary shopping
chit-chat (greetings, thanks, "show more", "any cheaper"). ANYTHING unrelated — writing
poems or code, general knowledge, weather, math, jokes, other stores — is off topic.
A message that mixes an unrelated request with a shoe or store request ("a workout
plan and the right shoes for it") is SHOPPING: the shoe part must still be answered.
Reply with EXACTLY one word: SHOPPING or OFFTOPIC."""


def is_off_topic(query: str) -> bool:
    """True if the message isn't shopping/store related, so the caller can refuse it
    without running any tools.
    """
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
