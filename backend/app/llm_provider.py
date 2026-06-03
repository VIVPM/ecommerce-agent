"""LLM provider switch: Gemini or Cloudflare Workers AI (gpt-oss).

Set LLM_MODEL=GEMINI or LLM_MODEL=CLOUDFLARE. The two are interchangeable peers,
not a primary and a backup: the agent, its tools, the routing and the streaming
path are identical either way — only the chat model underneath differs. Both are
LangChain chat models, so both get native tool-calling and token streaming with
no provider-specific code. Cloudflare speaks the OpenAI wire format, so a
ChatOpenAI aimed at its /v1 endpoint is the whole integration.

Embeddings ALWAYS run on Gemini: the Pinecone FAQ index is 1024-dim
gemini-embedding-001, so switching them would need a re-indexed store. Only text
generation switches. GEMINI_API_KEY is therefore required even in cloudflare mode.
"""
import logging
import os
import time
from functools import lru_cache

from langchain_core.messages import HumanMessage, SystemMessage

from app.llm_utils import with_retry

logger = logging.getLogger(__name__)

_SUPPORTED = ("GEMINI", "CLOUDFLARE")
# Required, no default — a forgotten deploy setting should fail loudly rather than
# run on a guessed provider. Set it in backend/app/.env and in the deploy environment.
_raw_model = os.getenv("LLM_MODEL")
if not _raw_model:
    raise RuntimeError(f"LLM_MODEL must be set to one of {_SUPPORTED}.")
PROVIDER = _raw_model.strip().upper()
if PROVIDER not in _SUPPORTED:
    raise RuntimeError(f"LLM_MODEL={PROVIDER!r} is not one of {_SUPPORTED}.")

GEMINI_DEFAULT = "gemini-2.5-flash"
# Per-step tiering. The query rewrite and the routing decision are cheap
# classification work; the comparison and the SQL comprehension are not.
# Cloudflare has a single model, so these are ignored there.
GEMINI_LITE = os.getenv("GEMINI_LITE_MODEL", "gemini-2.5-flash-lite")
# Routing stays on the full model by DEFAULT: the 200-case eval is calibrated on
# it and routing accuracy is the thing most likely to regress. Flip this only
# with an eval run to back it up.
ROUTING_MODEL = os.getenv("ROUTING_MODEL", GEMINI_DEFAULT)

# Cloudflare Workers AI (only validated/used when PROVIDER == "CLOUDFLARE").
CF_ACCOUNT = os.getenv("CLOUDFLARE_ACCOUNT_ID")
CF_TOKEN = os.getenv("CLOUDFLARE_API_TOKEN")
CF_MODEL = "@cf/openai/gpt-oss-20b"
# gpt-oss-20b is a reasoning model: it spends completion tokens thinking before it
# writes any content, and Workers AI defaults the cap to 256 — leave it there and the
# reply comes back empty with finish_reason="length". Keep it generous.
CF_MAX_TOKENS = 4096
_CF_BASE = f"https://api.cloudflare.com/client/v4/accounts/{CF_ACCOUNT}/ai/v1" if CF_ACCOUNT else None

if PROVIDER == "CLOUDFLARE":
    missing = [k for k, v in (("CLOUDFLARE_ACCOUNT_ID", CF_ACCOUNT),
                              ("CLOUDFLARE_API_TOKEN", CF_TOKEN)) if not v]
    if missing:
        raise RuntimeError(f"LLM_MODEL=CLOUDFLARE also needs {', '.join(missing)} in .env.")
    logger.info("LLM provider: Cloudflare Workers AI (%s)", CF_MODEL)


# --- Circuit breaker + cross-provider failover ---------------------------
# Gemini and Cloudflare are interchangeable peers (verified on all three tools,
# both providers, cold caches), so a provider outage should not be an outage.
# The breaker opens after N consecutive failures and the OTHER provider takes
# over — but only BETWEEN jobs. Half an answer from one model and half from
# another is worse than one clean failure.
BREAKER_THRESHOLD = int(os.getenv("BREAKER_THRESHOLD", "3"))
BREAKER_COOLDOWN_S = float(os.getenv("BREAKER_COOLDOWN_S", "60"))

_OTHER = {"GEMINI": "CLOUDFLARE", "CLOUDFLARE": "GEMINI"}
_breaker: dict[str, dict] = {p: {"failures": 0, "open_until": 0.0} for p in _SUPPORTED}


def _configured(provider: str) -> bool:
    """Can we actually call this provider? Failover is only possible when the
    other one has credentials — otherwise there is nothing to fail over to."""
    if provider == "GEMINI":
        return bool(os.getenv("GEMINI_API_KEY"))
    return bool(CF_ACCOUNT and CF_TOKEN)


def _is_open(provider: str) -> bool:
    return time.monotonic() < _breaker[provider]["open_until"]


def active_provider() -> str:
    """Which provider to use RIGHT NOW. Pick this once per job and stick with it."""
    if not _is_open(PROVIDER):
        return PROVIDER
    other = _OTHER[PROVIDER]
    if _configured(other) and not _is_open(other):
        return other
    return PROVIDER          # nothing healthy to switch to; let it fail honestly


def all_providers_open() -> bool:
    """True when every usable provider is tripped. The worker stops CONSUMING
    the queue rather than pulling jobs it is going to burn attempts failing."""
    return all(_is_open(p) for p in _SUPPORTED if _configured(p))


def note_result(provider: str, ok: bool) -> None:
    """Record one job outcome against the provider that served it."""
    b = _breaker[provider]
    if ok:
        if b["failures"]:
            logger.info("Provider %s recovered; breaker reset", provider)
        b["failures"], b["open_until"] = 0, 0.0
        return
    b["failures"] += 1
    if b["failures"] >= BREAKER_THRESHOLD and not _is_open(provider):
        b["open_until"] = time.monotonic() + BREAKER_COOLDOWN_S
        target = _OTHER[provider]
        logger.error("Breaker OPEN for %s after %d failures; %s", provider, b["failures"],
                     f"failing over to {target}" if _configured(target) else "no fallback configured")


# USD per 1M tokens, overridable without a redeploy because published rates move
# and a stale hardcoded number is worse than none. Used for a cost ESTIMATE on
# traces — it is not a billing record.
_PRICES = {
    "GEMINI":     (float(os.getenv("PRICE_IN_GEMINI", "0.30")),
                   float(os.getenv("PRICE_OUT_GEMINI", "2.50"))),
    "CLOUDFLARE": (float(os.getenv("PRICE_IN_CLOUDFLARE", "0.20")),
                   float(os.getenv("PRICE_OUT_CLOUDFLARE", "0.30"))),
}


def estimate_cost_usd(provider: str, input_tokens: int, output_tokens: int) -> float:
    """Rough spend for one job. Cost is the number you actually want on a trace —
    tokens alone don't tell you whether a spike is traffic or expense."""
    p_in, p_out = _PRICES.get(provider, (0.0, 0.0))
    return round((input_tokens * p_in + output_tokens * p_out) / 1_000_000, 6)


def breaker_state() -> dict:
    return {p: {"failures": b["failures"], "open": _is_open(p)} for p, b in _breaker.items()}


@lru_cache(maxsize=None)
def _build(provider: str, temperature: float, model: str | None):
    if provider == "CLOUDFLARE":
        from langchain_openai import ChatOpenAI
        return ChatOpenAI(model=CF_MODEL, api_key=CF_TOKEN, base_url=_CF_BASE,
                          temperature=temperature, max_tokens=CF_MAX_TOKENS, timeout=90)
    from langchain_google_genai import ChatGoogleGenerativeAI
    return ChatGoogleGenerativeAI(model=model, google_api_key=os.getenv("GEMINI_API_KEY"),
                                  temperature=temperature)


def chat(temperature: float = 0.0, model: str | None = None):
    """The LangChain chat model for the currently healthy provider — what
    create_agent binds tools to, and what complete()/stream() run on.

    `model` picks the Gemini tier; Cloudflare has a single model, so it is ignored
    there (and normalised away, so one client is shared instead of one per tier).
    Clients are cached: building them per request wastes a connection pool.
    """
    provider = active_provider()
    return _build(provider, temperature,
                  None if provider == "CLOUDFLARE" else (model or GEMINI_DEFAULT))


def _msgs(user, system):
    return ([SystemMessage(system)] if system else []) + [HumanMessage(user)]


def complete(user, system=None, temperature=0.0, model=None, fallback=None):
    """One non-streaming completion. On Gemini, `model`/`fallback` pick the tier and
    the Flash->Pro safety net; on Cloudflare there is a single model, so both are ignored."""
    msgs = _msgs(user, system)

    def _gen(m):
        return chat(temperature, m).invoke(msgs).text

    try:
        return with_retry(_gen, model)
    except Exception as e:
        if not fallback:
            raise
        logger.warning("Generation on %s failed, falling back to %s: %s", model, fallback, e)
        return with_retry(_gen, fallback)


async def stream(user, system=None, temperature=0.0, model=None):
    """Stream completion chunks (strings). Mirrors complete() across providers.

    Callbacks propagate out of this into the agent's event stream, so tokens
    produced inside a tool still surface to the caller as they arrive.
    """
    async for chunk in chat(temperature, model).astream(_msgs(user, system)):
        if chunk.text:
            yield chunk.text
