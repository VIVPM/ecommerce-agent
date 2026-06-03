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


@lru_cache(maxsize=None)
def _build(temperature: float, model: str | None):
    if PROVIDER == "CLOUDFLARE":
        from langchain_openai import ChatOpenAI
        return ChatOpenAI(model=CF_MODEL, api_key=CF_TOKEN, base_url=_CF_BASE,
                          temperature=temperature, max_tokens=CF_MAX_TOKENS, timeout=90)
    from langchain_google_genai import ChatGoogleGenerativeAI
    return ChatGoogleGenerativeAI(model=model, google_api_key=os.getenv("GEMINI_API_KEY"),
                                  temperature=temperature)


def chat(temperature: float = 0.0, model: str | None = None):
    """The LangChain chat model for the configured provider — what create_agent
    binds tools to, and what complete()/stream() run on.

    `model` picks the Gemini tier; Cloudflare has a single model, so it is ignored
    there (and normalised away, so one client is shared instead of one per tier).
    Clients are cached: building them per request wastes a connection pool.
    """
    return _build(temperature, None if PROVIDER == "CLOUDFLARE" else (model or GEMINI_DEFAULT))


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
