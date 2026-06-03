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
from dataclasses import dataclass

from langchain.agents import create_agent
from langchain.agents.middleware import wrap_model_call
from langchain.tools import ToolRuntime, tool
from langchain_core.messages import AIMessage
from langgraph.config import get_stream_writer

from app.cache import cache_get, cache_set
from app.compare import compare_saved_stream_async
from app.faq import faq_chain_stream_async
from app.llm_provider import ROUTING_MODEL, chat
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


TOOLS = [search_product_database, search_faq_knowledge_base, compare_saved_products]

agent_instruction = """
    You are an intelligent e-commerce routing agent. Your ONLY job is to analyze the user's query
    and call the most appropriate tool (`search_product_database` or `search_faq_knowledge_base`).
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


def astream_agent(query: str, user_id: int | None = None):
    """Async: yields the tools' status/token dicts as they are produced. This is
    the streaming path used by the API."""
    return agent.astream(
        {"messages": [{"role": "user", "content": query}]},
        stream_mode="custom",
        context=Ctx(user_id=user_id),
    )


async def arun_agent(query: str, user_id: int | None = None) -> str:
    result = await agent.ainvoke(
        {"messages": [{"role": "user", "content": query}]},
        context=Ctx(user_id=user_id),
    )
    return result["messages"][-1].content


def run_agent(query: str, user_id: int | None = None) -> str:
    """Non-streaming variant used by test/evaluate_agent_tuned.py. Stays sync and
    thread-safe: the eval harness runs its cases in a thread pool, and each worker
    thread gets its own event loop."""
    return asyncio.run(arun_agent(query, user_id))
