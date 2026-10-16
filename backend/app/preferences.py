"""Saving a stated shopping preference — routed by the LLM (agent.py), NOT by phrase rules."""
import re

from app.memory_store import contains_personal_data, remember

_FILLER_RE = re.compile(r"^\s*(please\s+)?(remember|note|keep in mind)( that)?[:,]?\s+", re.I)


def note_preference(user_id, query: str) -> str:
    """Store a lasting preference the user stated, and acknowledge it by echoing it back."""
    if contains_personal_data(query):
        return ("I can't store phone numbers, email addresses or card details, so I "
                "haven't saved that — please don't share them here. I can remember "
                "shopping preferences like favourite brands or a budget.")
    remember(user_id, query)
    noted = _FILLER_RE.sub("", query).strip().rstrip(".")
    if not noted:
        return "Got it — I'll remember that for next time."
    return f"Got it — noted: \"{noted}\". I'll keep it in mind next time you shop."


async def note_preference_stream_async(query: str, user_id):
    yield note_preference(user_id, query)
