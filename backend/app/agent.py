"""The shopping agent: one LangChain agent, three tools, two interchangeable providers.

Routing is the model's job — it picks a tool by name from the tool docstrings
below. Those docstrings ARE the routing prompt and the 200-case eval suite is
calibrated on their exact wording, so treat them as prompt text, not comments.

Every tool is `return_direct=True`: its output is already shopper-ready markdown
(product lists, rating counts, price-age and unsupported-filter notes), so the
agent hands it back verbatim instead of paraphrasing it through a second model
call. That also makes each run single-hop. Drop return_direct on a tool to let
the agent chain it, then re-run the eval to see whether it paid for itself.

Tools stream through LangGraph's custom channel (`_emit`) rather than returning
one blob at the end, so the caller gets one uniform status/token stream whether
the text came from an LLM or from the deterministic formatter in sql.py.
"""
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
from app.compare import compare_saved_stream_async
from app.faq import faq_chain_stream_async
from app.order_history import order_history_stream_async
from app.decompose import decompose
from app.llm_provider import ROUTING_MODEL, chat, complete
from app.sql import sql_chain_stream_async

logger = logging.getLogger(__name__)

FAQ_TOOL = "search_faq_knowledge_base"


@dataclass
class Ctx:
    """Per-request data the tools need but the model must never see.

    user_id is deliberately NOT a tool argument: as an argument the model could
    hallucinate one, or be talked into supplying someone else's, and read a
    stranger's shortlist. Injected here it stays out of the tool schema entirely.
    """
    user_id: int | None = None


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


@tool(return_direct=True)
async def compare_saved_products(query: str, runtime: ToolRuntime[Ctx]) -> str:
    """
    Use this tool ONLY when the user asks about the products THEY have SAVED or
    shortlisted — comparing them, ranking them, or choosing between them.
    Examples: "compare my saved shoes", "which of my saved ones is best value",
    "what did I save", "should I buy the saved Campus or the saved Sparx".
    Do NOT use this for searching the catalogue — that is search_product_database.
    """
    user_id = runtime.context.user_id if runtime.context else None
    # No signed-in user (e.g. the eval harness) — nothing to compare against. This
    # still goes through _drain: a tool that returns early without emitting leaves
    # the caller with no status and no tokens, which renders as "I couldn't
    # generate a response" instead of the actual explanation.
    agen = (compare_saved_stream_async(query, user_id) if user_id is not None
            else _signed_out())
    return await _drain(agen, "Reviewing your saved products...", "compare_saved_products")


async def _signed_out_orders():
    yield "I can only look up orders for a signed-in user."


@tool(return_direct=True)
async def order_history(query: str, runtime: ToolRuntime[Ctx]) -> str:
    """
    Use this tool when the user asks about orders THEY have already PLACED —
    their purchase history, what they bought, what they spent, or the status of
    an order. Examples: "what have I ordered before", "show my orders", "my
    order history", "what did I buy last time", "how much have I spent",
    "did I order the Campus ones".
    Do NOT use this for items merely SAVED or in the CART but not yet ordered —
    saved items are compare_saved_products. Do NOT use it to search the
    catalogue — that is search_product_database.
    """
    user_id = runtime.context.user_id if runtime.context else None
    # Same rule as compare_saved_products: every exit path goes through _drain,
    # or the tool emits nothing and the client renders "I couldn't generate a
    # response" instead of this explanation.
    agen = (order_history_stream_async(user_id) if user_id is not None
            else _signed_out_orders())
    return await _drain(agen, "Looking up your orders...", "order_history")


TOOLS = [search_product_database, search_faq_knowledge_base, compare_saved_products,
         order_history]

agent_instruction = """
    You are an intelligent e-commerce routing agent. Your ONLY job is to analyze the user's query
    and call the most appropriate tool. The tool descriptions say what each one is for; read them
    and pick the single best match. (This used to name two tools explicitly, which went stale as
    tools were added — the schemas are the source of truth.)
    You must NOT attempt to answer the user's question directly. Always invoke a tool.
    Pass the user's EXACT query string into the tool you select.
    """


def _tool_call(name: str, query: str, call_id: str) -> AIMessage:
    return AIMessage(content="", tool_calls=[{"name": name, "args": {"query": query}, "id": call_id}])


@wrap_model_call
async def _route(request, handler):
    """Routing is temperature-0, so the tool choice is cacheable. Only the tool
    NAME is stored; the argument is the query itself.

    A cache hit returns a synthetic tool call, so the model is never called — the
    tool still executes and streams exactly as it otherwise would. The cache is
    Postgres-backed and fail-open, and runs off-loop so it cannot stall the event
    loop under load. Purge it after editing agent_instruction or any tool
    docstring above: cache_purge('route').
    """
    query = request.state["messages"][-1].content

    cached = await asyncio.to_thread(cache_get, "route", query)
    if cached:
        return _tool_call(cached, query, "cached-route")

    # Re-resolve the model on EVERY call rather than using the one bound at
    # import. create_agent captures its model once, so without this override a
    # breaker trip would fail over the tools (they call chat() per use) but not
    # the routing call — the agent would keep hitting the dead provider.
    request = request.override(model=chat(temperature=0.0, model=ROUTING_MODEL))

    response = await handler(request)
    message = response.result[0] if hasattr(response, "result") else response

    if getattr(message, "tool_calls", None):
        await asyncio.to_thread(cache_set, "route", query, message.tool_calls[0]["name"])
        return response

    # The model answered instead of routing. Every reply has to come from a tool,
    # so fall back to the FAQ knowledge base rather than let an ungrounded answer
    # reach the shopper.
    logger.warning("Model returned no tool call for %r; falling back to the FAQ base.", query[:80])
    return _tool_call(FAQ_TOOL, query, "fallback-route")


agent = create_agent(
    # Overridden per call by the _route middleware so failover reaches routing.
    model=chat(temperature=0.0, model=ROUTING_MODEL),
    tools=TOOLS,
    system_prompt=agent_instruction,
    middleware=[_route],
    context_schema=Ctx,
)


def _astream_one(query: str, user_id: int | None):
    """One single-hop agent run: route, call one tool, stream what it emits."""
    return agent.astream(
        {"messages": [{"role": "user", "content": query}]},
        stream_mode="custom",
        context=Ctx(user_id=user_id),
    )


async def astream_agent(query: str, user_id: int | None = None):
    """Async: yields the tools' status/token dicts as they are produced. This is
    the streaming path used by the API.

    A message spanning two intents is split first and each part is run through
    the single-hop agent in turn, so the caller still sees ONE uniform stream and
    needs to know nothing about parts. Splitting here rather than making the
    agent multi-step is what keeps return_direct -- and therefore the verified
    product formatting -- intact.

    Parts run sequentially, not concurrently: they share the provider's rate
    limit and one job's token budget, and the shopper reads top to bottom anyway.
    """
    parts = await asyncio.to_thread(decompose, query)

    for i, part in enumerate(parts):
        if len(parts) > 1:
            # A separator BEFORE each part after the first, so the answers do not
            # run together into one wall of markdown.
            if i:
                yield {"token": "\n\n---\n\n"}
            yield {"status": f"Answering part {i + 1} of {len(parts)}: {part[:60]}"}

        async for chunk in _astream_one(part, user_id):
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


# --- Input guardrail --------------------------------------------------------
# Reject clearly off-topic messages (poems, weather, general knowledge) before
# spending any routing or tool calls. A keyword pre-check lets obvious shopping
# messages straight through; only the ambiguous ones pay for an LLM check, and
# that verdict is cached like the other deterministic outputs.
#
# NOTE: after editing _GUARDRAIL_SYS, run cache_purge('guardrail').

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

    Fails OPEN (returns False) so a model hiccup never blocks a real shopper — the
    cost of letting one odd message through is a wasted call, the cost of blocking
    a genuine one is a broken product.
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
