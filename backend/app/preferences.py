"""Durable, cross-session shopping preferences.

A user states a lasting preference in chat ("remember I prefer Puma under 3000")
and it's stored as one short natural-language summary, then folded into every
product search so they don't repeat it each session. Kept as free text (not rigid
columns) so it can hold brands, a budget, gender/type — whatever they say — and the
text-to-SQL search already honours a phrase like "only Puma, under 3000".
"""
import logging
import re

logger = logging.getLogger(__name__)

# Phrases that mean "I'm setting/asking about a lasting preference".
_PREF_RE = re.compile(
    r"\b(remember (that|to|my|i)|i prefer|i (usually|always|only) (want|buy|wear|like)|"
    r"i never (want|buy|wear)|my budget|set my preferen|save my preferen)\b", re.I)
_ASK_RE = re.compile(r"\b(what('?s| are| is)|show me?|list) (my )?preferen", re.I)
_CLEAR_RE = re.compile(r"\b(forget|clear|reset|remove|delete)\b[^.]*\bpreferen", re.I)

_MERGE_SYS = """A shopper is telling a SHOE store their lasting shopping preferences
(favourite brands, a budget / price ceiling, preferred gender or shoe type). You are
given their CURRENT saved preferences and their NEW message. Return the UPDATED
preferences as ONE short line, e.g. "Prefers Puma and Nike; budget under 3000; men's
shoes". Merge the new info into the current, overwrite anything it changes, keep it
concise. Return ONLY that one line — no quotes, no preamble."""


def looks_like_pref(msg: str) -> bool:
    """Cheap gate: is this message setting, asking about, or clearing preferences?"""
    q = msg or ""
    return bool(_PREF_RE.search(q)) or bool(_ASK_RE.search(q)) or bool(_CLEAR_RE.search(q))


def get_prefs(user_id: int) -> str:
    from sqlalchemy import text
    from app.db.database import SessionLocal
    db = SessionLocal()
    try:
        row = db.execute(
            text("SELECT preferences FROM user_preferences WHERE user_id = :u"),
            {"u": user_id},
        ).fetchone()
        return (row[0] if row else "") or ""
    finally:
        db.close()


def set_prefs(user_id: int, prefs: str):
    from sqlalchemy import text
    from app.db.database import SessionLocal
    from app.db.models import now_ist
    db = SessionLocal()
    try:
        db.execute(text("""
            INSERT INTO user_preferences (user_id, preferences, updated_at)
            VALUES (:u, :p, :n)
            ON CONFLICT (user_id) DO UPDATE SET preferences = :p, updated_at = :n
        """), {"u": user_id, "p": prefs, "n": now_ist()})
        db.commit()
    finally:
        db.close()


def clear_prefs(user_id: int):
    from sqlalchemy import text
    from app.db.database import SessionLocal
    db = SessionLocal()
    try:
        db.execute(text("DELETE FROM user_preferences WHERE user_id = :u"), {"u": user_id})
        db.commit()
    finally:
        db.close()


def handle_preference(user_id: int, msg: str) -> str:
    """Set / view / clear the user's preferences from a chat message; returns the reply."""
    from app.llm_provider import complete

    if _CLEAR_RE.search(msg or ""):
        clear_prefs(user_id)
        return "Done — I've cleared your saved preferences."

    current = get_prefs(user_id)

    # A pure question ("what are my preferences?") — show, don't overwrite.
    if _ASK_RE.search(msg or "") and not _PREF_RE.search(msg or ""):
        if current:
            return f"Here's what I have saved: **{current}**. Say \"forget my preferences\" to clear them."
        return ("You haven't saved any preferences yet. Tell me things like "
                "\"remember I prefer Puma under 3000\" and I'll apply them to future searches.")

    try:
        summary = (complete(
            f"CURRENT: {current or '(none)'}\nNEW MESSAGE: {msg}",
            system=_MERGE_SYS, temperature=0.0) or "").strip().strip('"')
        if not summary:
            return "I couldn't catch a preference there — try \"remember I prefer Nike under 3000\"."
        set_prefs(user_id, summary)
        return f"Got it — I'll remember this for next time: **{summary}**"
    except Exception as e:
        logger.error("Preference update failed: %s", e)
        return "I couldn't save that preference just now. Please try again."


if __name__ == "__main__":
    # ponytail: offline sanity for the intent gates (no DB / LLM).
    assert looks_like_pref("remember I prefer Puma")
    assert looks_like_pref("what are my preferences?")
    assert _CLEAR_RE.search("please forget my preferences")
    assert not looks_like_pref("show me nike shoes")
    print("preferences intent gates OK")
