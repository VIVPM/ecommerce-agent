"""LLM provider switch: Gemini (default) or Cloudflare Workers AI (gpt-oss).

Set LLM_MODEL=GEMINI (default) or LLM_MODEL=CLOUDFLARE. Gemini uses the google-genai
SDK; Cloudflare speaks the OpenAI wire format, so its /chat/completions endpoint is
called over httpx (already a dependency — no new package).

Embeddings ALWAYS run on Gemini: the Pinecone FAQ index is 1024-dim
gemini-embedding-001, so switching them would need a re-indexed store. Only text
generation switches. GEMINI_API_KEY is therefore required even in cloudflare mode.
"""
import json
import logging
import os
import re

import httpx
from google import genai
from google.genai import types

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

# Gemini client — used for generation in gemini mode, and always for embeddings.
_gm = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))
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
_CF_HEADERS = {"Authorization": f"Bearer {CF_TOKEN}", "Content-Type": "application/json"}

if PROVIDER == "CLOUDFLARE":
    missing = [k for k, v in (("CLOUDFLARE_ACCOUNT_ID", CF_ACCOUNT),
                              ("CLOUDFLARE_API_TOKEN", CF_TOKEN)) if not v]
    if missing:
        raise RuntimeError(f"LLM_MODEL=CLOUDFLARE also needs {', '.join(missing)} in .env.")
    logger.info("LLM provider: Cloudflare Workers AI (%s)", CF_MODEL)


def _cf_messages(user, system):
    msgs = []
    if system:
        msgs.append({"role": "system", "content": system})
    msgs.append({"role": "user", "content": user})
    return msgs


def complete(user, system=None, temperature=0.0, model=None, fallback=None):
    """One non-streaming completion. On Gemini, `model`/`fallback` pick the tier and
    the Flash->Pro safety net; on Cloudflare there is a single model, so both are ignored."""
    if PROVIDER == "CLOUDFLARE":
        r = httpx.post(f"{_CF_BASE}/chat/completions", headers=_CF_HEADERS, timeout=90, json={
            "model": CF_MODEL,
            "messages": _cf_messages(user, system),
            "temperature": temperature,
            "max_tokens": CF_MAX_TOKENS,
        })
        r.raise_for_status()
        return (r.json()["choices"][0]["message"].get("content") or "").strip()

    def _gen(m):
        cfg = types.GenerateContentConfig(temperature=temperature)
        if system:
            cfg.system_instruction = system
        return _gm.models.generate_content(model=m, contents=user, config=cfg).text

    try:
        return with_retry(_gen, model or GEMINI_DEFAULT)
    except Exception as e:
        if not fallback:
            raise
        logger.warning("Generation on %s failed, falling back to %s: %s", model, fallback, e)
        return with_retry(_gen, fallback)


async def stream(user, system=None, temperature=0.0, model=None):
    """Stream completion chunks (strings). Mirrors complete() across providers."""
    if PROVIDER == "CLOUDFLARE":
        async with httpx.AsyncClient(timeout=120) as client:
            async with client.stream("POST", f"{_CF_BASE}/chat/completions", headers=_CF_HEADERS, json={
                "model": CF_MODEL,
                "messages": _cf_messages(user, system),
                "temperature": temperature,
                "max_tokens": CF_MAX_TOKENS,
                "stream": True,
            }) as r:
                r.raise_for_status()
                async for line in r.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        delta = json.loads(data)["choices"][0]["delta"].get("content")
                    except (json.JSONDecodeError, KeyError, IndexError):
                        continue
                    if delta:
                        yield delta
        return

    cfg = types.GenerateContentConfig(temperature=temperature)
    if system:
        cfg.system_instruction = system
    s = await _gm.aio.models.generate_content_stream(
        model=model or GEMINI_DEFAULT, contents=user, config=cfg,
    )
    async for chunk in s:
        if chunk.text:
            yield chunk.text


def route_cloudflare(user, system, tools):
    """Cloudflare routing: gpt-oss picks a tool via a JSON reply instead of Gemini's
    native function-calling. `tools` is a list of (name, description). Returns
    (tool_name, arg); tool_name is None if it couldn't be parsed."""
    names = [n for n, _ in tools]
    catalogue = "\n".join(f"- {n}: {d.strip()}" for n, d in tools)
    routing_system = (
        f"{system}\n\nAvailable tools:\n{catalogue}\n\n"
        f'Reply with ONLY a JSON object, no prose: {{"tool": "<one of {names}>", '
        '"query": "<the user\'s query, unchanged>"}.'
    )
    try:
        out = complete(user, system=routing_system, temperature=0.0) or ""
        match = re.search(r"\{.*\}", out, re.DOTALL)
        if match:
            data = json.loads(match.group(0))
            name = data.get("tool")
            if name in names:
                return name, data.get("query") or user
    except Exception as e:
        logger.error("Cloudflare routing failed: %s", e)
    return None, user
