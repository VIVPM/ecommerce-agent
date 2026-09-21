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
    """Per-request data the tools need but the model must never see.

    user_id is deliberately NOT a tool argument: as an argument the model could
    hallucinate one, or be talked into supplying someone else's, and read a
    stranger's shortlist. Injected here it stays out of the tool schema entirely.

    raw_query is the message as the shopper TYPED it, before the history-aware
    rewrite. Positional references ("save 2", "remove the first two") must resolve
    against that and never against the rewritten text: the rewriter once turned
    "remove saved items that are currently present" into a product search with an
    exclusion clause, and the removal silently never happened. A rule in the
    rewrite prompt was tried first; it held until the next prompt edit. So the
    invariant lives here, in code, where a prompt edit cannot reach it.

    history is what the shopper was just shown. "save 2" means the second product
    in the last result list, which exists only in that transcript.

    memory is what long-term recall found about this shopper, phrased for the
    model -- including when it found NOTHING, which is stated rather than left
    blank. It rides as a SYSTEM message, never appended to the question: inside
    the question it lands in the text-to-SQL input and a working search starts
    returning nothing.
    """
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
        # No signed-in user (e.g. the eval harness). Still goes through _drain: a
        # tool that returns without emitting leaves the caller with no status and
        # no tokens, which renders as "I couldn't generate a response" instead of
        # this explanation.
        return await _drain(_signed_out(), _SAVED_STATUS["compare"], "manage_saved")

    # Positional references resolve against what the shopper TYPED, not against the
    # rewritten text the model was routed on. See Ctx.raw_query.
    raw = (ctx.raw_query or query) if ctx else query

    action = (action or "").strip().lower()
    if action == "add":
        agen = save_from_results_stream_async(raw, user_id, ctx.history if ctx else None)
    elif action == "remove":
        agen = remove_saved_items_stream_async(raw, user_id)
    else:
        # Anything unrecognised falls back to COMPARE, deliberately. Compare is the
        # read-only action: guessing it costs a wasted turn, whereas guessing
        # "remove" would delete a shortlist the shopper never asked to touch.
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
    """
    Use this tool for the user's CART and ORDERS. Set `action` to one of:

    - "add_results_to_cart" - add products from the list you just showed them.
      "add items 2 and 3 to my cart", "put the first one in my cart".
    - "add_to_cart" - add from their SAVED list, when they say so explicitly.
      "add saved items 2 and 3 to my cart", "add my saved Puma to the cart".
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
        return await _drain(_signed_out_orders(), _ORDER_STATUS["view"], "manage_orders")

    # Positions and order ids come from what the shopper TYPED. See Ctx.raw_query.
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

    A cache hit returns a synthetic tool call, so the model is never called — the
    tool still executes and streams exactly as it otherwise would. The cache is
    Postgres-backed and fail-open, and runs off-loop so it cannot stall the event
    loop under load. Purge it after editing agent_instruction or any tool
    docstring above: cache_purge('route').
    """
    query = request.state["messages"][-1].content

    cached = await asyncio.to_thread(cache_get, "route", query)
    if cached:
        # Stored as "tool" or "tool:action" -- a tool with an action argument needs
        # it replayed too, or the cached call arrives without one and the tool has
        # to guess what the shopper wanted.
        name, _, action = cached.partition(":")
        if name in _TOOL_NAMES:
            return _tool_call(name, query, "cached-route", action)
        # A renamed or removed tool leaves rows behind that name something that no
        # longer exists. Calling it would fail the whole turn, so the row is
        # ignored and the model routes this one normally.
        logger.warning("Stale cached route %r is not a live tool - re-routing.", cached)

    # Re-resolve the model on EVERY call rather than using the one bound at
    # import. create_agent captures its model once, so without this override a
    # breaker trip would fail over the tools (they call chat() per use) but not
    # the routing call — the agent would keep hitting the dead provider.
    # Memory belongs in the SYSTEM prompt, not in the question and not as a second
    # system message. Appended to the question it lands inside the text-to-SQL
    # input, and a working search started answering "I couldn't find any
    # products"; as an extra system message the model returned neither a tool
    # call nor any text.
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

    # NO TOOL — and that is a valid outcome, not a failure to route.
    #
    # This used to force a call to the FAQ knowledge base so that "every reply
    # comes from a tool". That answered greetings and opinions out of the returns
    # policy. It is the same defect as an instruction reading "always invoke a
    # tool", one layer lower, and harder to see because it lives in code.
    #
    # The decision is deliberately NOT cached. A cache hit here returns a
    # synthetic tool call and skips the model entirely, which works only because
    # a tool produces the text. Conversation has no tool: the reply itself has to
    # be generated, so there is nothing a cache could save.
    #
    # Logged so the zero-tool turns are findable. That log is the
    # missing-capability backlog — an action the shopper wanted, with no tool to
    # serve it, lands here and gets improvised by the model.
    logger.info("No tool for %r — answering conversationally.", query[:120])
    # The reply has to be pushed onto the custom stream by hand. That channel is
    # what the worker forwards, and only TOOLS write to it (via _drain) — a model
    # answering directly writes nothing, so without this the shopper gets the
    # empty-response message instead of the answer that was just generated.
    text = getattr(message, "content", "") or ""
    if isinstance(text, list):   # some providers return content as blocks
        text = "".join(b.get("text", "") for b in text if isinstance(b, dict))
    if text.strip():
        _emit({"status": "Thinking...", "tool": "converse"})
        _emit({"token": text})
    return response


agent = create_agent(
    # Overridden per call by the _route middleware so failover reaches routing.
    model=chat(temperature=0.0, model=ROUTING_MODEL),
    tools=TOOLS,
    system_prompt=agent_instruction,
    middleware=[_route],
    context_schema=Ctx,
)


def _astream_one(query: str, user_id: int | None, raw_query: str = "",
                 history: list | None = None, memory: str = ""):
    """One single-hop agent run: route, call one tool, stream what it emits.

    Memory rides in the context and is folded into the SYSTEM PROMPT by the
    _route middleware, so the shopper's question reaches the tools exactly as
    asked. It is also what the route cache keys on, so the key stays the
    question rather than the question plus whatever was remembered this minute.
    """
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
