"""The shopping agent: one LangChain agent, three tools, two interchangeable providers."""
import asyncio
import logging
import re
from dataclasses import dataclass

from langchain.agents import create_agent
from langchain.agents.middleware import wrap_model_call
from langchain.tools import ToolRuntime, tool
from langchain_core.messages import AIMessage
from langgraph.config import get_stream_writer

from app.cache import cache_get, cache_set
from app.compare import (compare_saved_stream_async,
                         remove_saved_items_stream_async,
                         save_from_results_stream_async)
from app.faq import faq_chain_stream_async
from app.orders import manage_orders_stream_async
from app.preferences import note_preference_stream_async
from app.decompose import decompose
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


def _emit(payload: dict):
    """Push one chunk onto the agent's custom stream. No-ops when a tool is called
    outside the agent (the eval harness, any sync caller)."""
    try:
        get_stream_writer()(payload)
    except RuntimeError:
        pass


async def _drain(agen, status: str, tool_name: str) -> str:
    """Stream a tool's chunks to the caller and return the assembled text, which is
    what return_direct hands back as the final answer."""
    _emit({"status": status, "tool": tool_name})
    parts = []
    async for token in agen:
        if token:
            parts.append(token)
            _emit({"token": token})
    return "".join(parts)


@tool(return_direct=True)
async def search_product_database(query: str) -> str:
    """
    Use this tool ONLY when the user is explicitly looking to buy shoes, searching for products,
    filtering by price, brand, rating, or asking about specific inventory (e.g., "Puma shoes under 5000", "cheapest running shoes").
    """
    return await _drain(sql_chain_stream_async(query),
                        "Searching products...", "search_product_database")


@tool(return_direct=True)
async def search_faq_knowledge_base(query: str) -> str:
    """
    Use this tool ONLY when the user is asking general questions about store policies,
    returns, refunds, shipping times, payment methods, or contacting customer support.
    """
    return await _drain(faq_chain_stream_async(query),
                        "Searching the knowledge base...", FAQ_TOOL)


async def _signed_out():
    yield "I can only compare saved products for a signed-in user."


_SAVED_STATUS = {"add": "Saving that for you...",
                 "remove": "Updating your saved list...",
                 "compare": "Reviewing your saved products..."}


@tool(return_direct=True)
async def manage_saved(action: str, query: str, runtime: ToolRuntime[Ctx]) -> str:
    """    Use this tool for anything to do with the user's SAVED items (their shortlist
    or wishlist). Set `action` to one of:
    """
    ctx = runtime.context
    user_id = ctx.user_id if ctx else None
    if user_id is None:
        return await _drain(_signed_out(), _SAVED_STATUS["compare"], "manage_saved")

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
                        "manage_saved")


async def _signed_out_orders():
    yield "I can only handle carts and orders for a signed-in user."


_ORDER_STATUS = {"add_to_cart": "Adding that to your cart...",
                 "add_results_to_cart": "Adding that to your cart...",
                 "place": "Placing your order...",
                 "cancel": "Cancelling that order...",
                 "view": "Looking up your orders..."}


@tool(return_direct=True)
async def manage_orders(action: str, query: str, runtime: ToolRuntime[Ctx]) -> str:
    """Use this tool for the user's CART and ORDERS. Set `action` to one of:"""
    ctx = runtime.context
    user_id = ctx.user_id if ctx else None
    if user_id is None:
        return await _drain(_signed_out_orders(), _ORDER_STATUS["view"], "manage_orders")

    raw = (ctx.raw_query or query) if ctx else query
    action = (action or "").strip().lower()
    agen = manage_orders_stream_async(action, raw, user_id, ctx.history if ctx else None)
    return await _drain(agen, _ORDER_STATUS.get(action, _ORDER_STATUS["view"]),
                        "manage_orders")


async def _signed_out_pref():
    yield "I can only remember preferences for a signed-in user."


@tool(return_direct=True)
async def save_preference(query: str, runtime: ToolRuntime[Ctx]) -> str:
    """
    Use this tool when the user tells you a lasting preference about THEMSELVES to
    remember for next time — favourite brands, a usual budget, their size, or the
    kind of shoe they wear. Examples: "remember I like Puma and Nike", "note that
    my budget is under 3000", "I always buy running shoes", "keep in mind I wear
    size 9".
    This tool SAVES; it does not retrieve. A question about what they like, or
    what they have told you before, is ordinary conversation — not this tool.
    Do NOT use it for a one-off request like "show me Puma under 3000": that is a
    search (search_product_database), not a stated preference.
    """
    user_id = runtime.context.user_id if runtime.context else None
    agen = (note_preference_stream_async(query, user_id) if user_id is not None
            else _signed_out_pref())
    return await _drain(agen, "Noting that for next time...", "save_preference")


TOOLS = [search_product_database, search_faq_knowledge_base, manage_saved,
         manage_orders, save_preference]
_TOOL_NAMES = {t.name for t in TOOLS}

agent_instruction = """You are a warm, natural shopping assistant for an online SHOE STORE.

WHEN A TOOL FITS, CALL IT. The tool descriptions say what each is for; read them and
pick the single best match, passing the user's EXACT query string as the argument.

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
ask ("Want me to search Puma running shoes under 2500?"). If the message carries no
remembered context about the shopper and they ask about their own history, say plainly
that you don't have anything remembered yet rather than implying you will retrieve it.

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
    query = request.state["messages"][-1].content

    cached = await asyncio.to_thread(cache_get, "route", query)
    if cached:
        name, _, action = cached.partition(":")
        if name in _TOOL_NAMES:
            return _tool_call(name, query, "cached-route", action)
        logger.warning("Stale cached route %r is not a live tool - re-routing.", cached)

    memory = getattr(getattr(request.runtime, "context", None), "memory", "") or ""
    overrides = {"model": chat(temperature=0.0, model=ROUTING_MODEL)}
    if memory:
        overrides["system_prompt"] = f"{agent_instruction}\n\nABOUT THIS SHOPPER:\n{memory}"
    request = request.override(**overrides)

    response = await handler(request)
    message = response.result[0] if hasattr(response, "result") else response

    if getattr(message, "tool_calls", None):
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


agent = create_agent(
    model=chat(temperature=0.0, model=ROUTING_MODEL),
    tools=TOOLS,
    system_prompt=agent_instruction,
    middleware=[_route],
    context_schema=Ctx,
)


def _astream_one(query: str, user_id: int | None, raw_query: str = "",
                 history: list | None = None, memory: str = ""):
    """One single-hop agent run: route, call one tool, stream what it emits."""
    return agent.astream(
        {"messages": [{"role": "user", "content": query}]},
        stream_mode="custom",
        context=Ctx(user_id=user_id, raw_query=raw_query or query, history=history,
                    memory=memory),
    )


async def astream_agent(query: str, user_id: int | None = None,
                        raw_query: str = "", history: list | None = None,
                        memory: str = ""):
    """Async: yields the tools' status/token dicts as they are produced. This is
    the streaming path used by the API.
    """
    parts = await asyncio.to_thread(decompose, query)

    for i, part in enumerate(parts):
        if len(parts) > 1:
            if i:
                yield {"token": "\n\n---\n\n"}
            yield {"status": f"Answering part {i + 1} of {len(parts)}: {part[:60]}"}

        async for chunk in _astream_one(part, user_id, raw_query, history, memory):
            yield chunk


async def _arun_one(query: str, user_id: int | None) -> str:
    result = await agent.ainvoke(
        {"messages": [{"role": "user", "content": query}]},
        context=Ctx(user_id=user_id),
    )
    return result["messages"][-1].content


async def arun_agent(query: str, user_id: int | None = None) -> str:
    """Non-streaming twin of astream_agent -- splits the same way, so the eval
    exercises the same path production does."""
    parts = await asyncio.to_thread(decompose, query)
    if len(parts) == 1:
        return await _arun_one(parts[0], user_id)
    answers = [await _arun_one(p, user_id) for p in parts]
    return ("\n\n---\n\n").join(answers)


def run_agent(query: str, user_id: int | None = None) -> str:
    """Non-streaming variant used by test/evaluate_agent_tuned.py. Stays sync and
    thread-safe: the eval harness runs its cases in a thread pool, and each worker
    thread gets its own event loop."""
    return asyncio.run(arun_agent(query, user_id))


_POLICY_RE = re.compile(
    r"\b(return|refund|deliver|shipping|ship|payment|cod|cash on delivery|cancel|"
    r"warranty|policy|support|contact|track|exchange|hours)\b", re.I)

_SAVED_RE = re.compile(r"\b(saved|shortlist|wishlist)\b", re.I)

_PRODUCT_RE = re.compile(
    r"\b(shoe|sneaker|boot|sandal|slipper|under\s*\d|below\s*\d|cheap|rating|rated|"
    r"brand|nike|puma|adidas|campus|sparx|skechers|reebok|bata)\b", re.I)

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
