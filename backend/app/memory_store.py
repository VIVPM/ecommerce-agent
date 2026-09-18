"""Long-term, cross-session memory via Supermemory.

Sits ALONGSIDE the short-term 6-message query rewrite (app/memory.py): the rewrite
resolves the immediate follow-up ("any cheaper?"); this recalls relevant facts and
preferences from ANY of the user's past chats. Scoped per user (container_tag = the
user id), so a fact saved in one chat is recalled in another.

Fail-open by design: if SUPERMEMORY_API_KEY is unset, or a call errors, recall()
returns "" and remember() is a no-op — the app then behaves exactly as before, just
without long-term memory. A memory hiccup must never break a shopper's message.
"""
import logging
import os

logger = logging.getLogger(__name__)

_TOP_K = 5


def _client():
    key = os.getenv("SUPERMEMORY_API_KEY")
    if not key:
        return None
    try:
        from supermemory import Supermemory
        return Supermemory(api_key=key)
    except Exception as e:
        logger.error("Supermemory client init failed: %s", e)
        return None


def _tag(user_id) -> str:
    return f"user_{user_id}"


def recall(user_id, query: str) -> str:
    """Relevant long-term memories for this user + query, as a short text block ready
    to inject into a prompt. Empty string on miss / error / no key."""
    client = _client()
    if client is None or not (query or "").strip():
        return ""
    try:
        res = client.search.memories(q=query, container_tag=_tag(user_id), search_mode="documents")
        results = getattr(res, "results", None) or []
        lines = []
        for r in results[:_TOP_K]:
            text = getattr(r, "memory", None) or getattr(r, "chunk", None)
            if text:
                lines.append(str(text).strip())
        return "\n".join(lines)
    except Exception as e:
        logger.error("Supermemory recall failed: %s", e)
        return ""


def remember(user_id, text: str) -> None:
    """Store a fact / turn in the user's long-term memory. No-op on error / no key."""
    client = _client()
    if client is None or not (text or "").strip():
        return
    try:
        client.add(content=text, container_tag=_tag(user_id))
    except Exception as e:
        logger.error("Supermemory remember failed: %s", e)
