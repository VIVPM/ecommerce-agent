"""Long-term, cross-session memory via Supermemory."""
import logging
import os

logger = logging.getLogger(__name__)

_TOP_K = 5

_BROAD_QUERY = "shopper preferences favourite brands budget past searches"


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
        lines = _search(client, user_id, query)
        if not lines:
            lines = _search(client, user_id, _BROAD_QUERY)
            if lines:
                logger.info("Recall missed %r; the broad retry found %d.",
                            query[:60], len(lines))
        return "\n".join(lines)
    except Exception as e:
        logger.error("Supermemory recall failed: %s", e)
        return ""


def _search(client, user_id, query: str) -> list:
    res = client.search.memories(q=query, container_tag=_tag(user_id),
                                 search_mode="documents")
    lines = []
    for r in (getattr(res, "results", None) or [])[:_TOP_K]:
        text = getattr(r, "memory", None) or getattr(r, "chunk", None)
        if text:
            lines.append(str(text).strip())
    return lines


def remember(user_id, text: str) -> None:
    """Store a fact / turn in the user's long-term memory. No-op on error / no key."""
    client = _client()
    if client is None or not (text or "").strip():
        return
    try:
        client.add(content=text, container_tag=_tag(user_id))
    except Exception as e:
        logger.error("Supermemory remember failed: %s", e)
