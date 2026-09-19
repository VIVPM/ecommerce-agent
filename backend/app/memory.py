import logging
import re
from dotenv import load_dotenv
from pathlib import Path

from app.llm_provider import GEMINI_LITE, complete

logger = logging.getLogger(__name__)

env_path = Path(__file__).resolve().parent / ".env"
load_dotenv(dotenv_path=env_path)

# Last few messages (≈3 user/assistant turns) used for query rewriting; keeps the prompt bounded on long conversations
MAX_HISTORY_MESSAGES = 6

memory_prompt = """You are an AI assistant tasked with optimizing user queries for an e-commerce agent based on their conversation history.

The agent supports two main functions:
1. SQL Database Queries (searching for shoes with specific filters like price, brand, rating).
2. FAQ Queries (answering general questions about policies, returns, shipping, etc.).

Your objective:
Given the user's LATEST query and the recent conversation HISTORY, your job is to rewrite the LATEST query into a fully standalone, unambiguous sentence that can be understood entirely without the history.

Guidelines:
1. If the latest query contains ambiguous pronouns (it, they, these, those, this) or relative terms (cheaper, more, other colors), replace them with the actual subjects or context from the HISTORY.
2. IMPORTANT: If the user is asking for "other" options or alternatives, you MUST explicitly include what they are excluding based on the immediate history (e.g., "What payment methods are accepted other than cash on delivery?").
2b. CRITICAL: "which of these / which of those / of the ones above / from these" refers to the product list the PREVIOUS assistant turn just showed. Do NOT throw away that search and start over. Carry forward EVERY constraint from the query that produced that list and ADD the new condition to it. E.g. after "running shoes under 2000", "which of these is waterproof?" becomes "running shoes under 2000 that are waterproof" — never the whole-catalogue "which shoes are waterproof".
3. If the latest query is ALREADY standalone and clear (e.g., "Show me Puma shoes under 5000"), return the query EXACTLY as it is without changing anything.
3b. CRITICAL: never rewrite away a reference to the user's OWN saved list. Phrases
like "my saved shoes", "my shortlist", "the ones I saved", "my wishlist" are NOT
ambiguous — they mean the products this user has saved, which is separate from
anything in the HISTORY. Keep that wording intact. Rewriting "compare my saved
shoes" into "compare the Campus shoes under 1500" changes the meaning entirely
and sends the request to the wrong place.
4. Keep the rewritten query natural and concise. Do not add conversational filler.
5. Output ONLY the rewritten query string and absolutely nothing else. Neither quotes nor XML tags.

Example 1:
HISTORY: User: "Show me running shoes", Assistant: "Here are some running shoes..."
LATEST QUERY: "Are there any cheaper ones?"
OUTPUT: Are there any running shoes that are cheaper?

Example 2:
HISTORY: User: "whether cash on delivery payment is accepted?", Assistant: "Cash on delivery payment is accepted."
LATEST QUERY: "what other payments are accpeted?"
OUTPUT: What payment methods are accepted other than cash on delivery?

Example 3:
HISTORY: User: "What is your return policy?", Assistant: "You have 30 days to return."
LATEST QUERY: "Does that apply to clearance items?"
OUTPUT: Does the 30 day return policy apply to clearance items?

Example 3:
HISTORY: User: "Find Adidas shoes", Assistant: "Listing Adidas shoes..."
LATEST QUERY: "What about Nike?"
OUTPUT: Find Nike shoes.

Example 4:
HISTORY: User: "Show me top rated shoes", Assistant: "Here are the top rated shoes."
LATEST QUERY: "Do you have formal shoes in size 9?"
OUTPUT: Do you have formal shoes in size 9?

Example 5 (saved list — do NOT substitute from history):
HISTORY: User: "Show me Campus running shoes under 1500", Assistant: "Here are the top results..."
LATEST QUERY: "compare my saved shoes and tell me which to buy"
OUTPUT: compare my saved shoes and tell me which to buy

Example 6 ("which of these" — carry the prior search forward, add the new filter):
HISTORY: User: "Show me running shoes under 2000", Assistant: "Here are the top results..."
LATEST QUERY: "which of these is waterproof?"
OUTPUT: Which running shoes under 2000 are waterproof?
"""

# An ACTION aimed at a position ("save 2", "remove saved item 3", "add the first
# two to my cart") is already standalone: the number means the nth row of what was
# just shown, and there is nothing for a rewrite to resolve. Rewriting one is pure
# downside -- "remove saved items that are currently present" came back as "show me
# Puma and Nike shoes, excluding my saved items", so a delete became a search and
# nothing was removed.
#
# This is deliberately a CODE gate, not another line in the rewrite prompt. The
# prompt-rule version was tried first and lasted until the next prompt edit, then
# broke silently. Ctx.raw_query protects the number once the tool has been chosen;
# this protects the ROUTING, which sees only the rewritten text.
_ACTION_RE = re.compile(
    r"^\W*(save|add|remove|delete|clear|drop|buy|order|cancel|place)\b", re.I)
_POSITION_RE = re.compile(
    r"\b(\d{1,2}|first|second|third|fourth|fifth|sixth|seventh|eighth|ninth|tenth|"
    r"all|every|everything|both|last|it|that|this|these|those|them|"
    r"saved|shortlist|wishlist|cart)\b", re.I)


def is_direct_action(query: str) -> bool:
    """True when the message is an action on something already on screen."""
    q = (query or "").strip()
    return bool(_ACTION_RE.match(q) and _POSITION_RE.search(q))


def optimize_query(latest_query: str, history: list) -> str:
    """
    Takes the latest user query and a history of messages format [{'role': 'user/assistant', 'content': '...'}]
    and uses Gemini to rewrite the query so that it is contextually standalone.
    """
    if not history:
        return latest_query

    if is_direct_action(latest_query):
        logger.info("Direct action, left unrewritten: %r", latest_query[:80])
        return latest_query
        
    formatted_history = []
    # Only format the last few messages so the prompt stays bounded on long conversations
    for msg in history[-MAX_HISTORY_MESSAGES:]:
        role = "User" if msg.get("role") == "user" else "Assistant"
        formatted_history.append(f"{role}: {msg.get('content')}")
        
    history_text = "\n".join(formatted_history)
    
    prompt = f"HISTORY:\n{history_text}\n\nLATEST QUERY: {latest_query}\nOUTPUT:"
    
    try:
        # temperature 0 for reproducible deterministic rewrites
        # Per-step routing: rewriting a follow-up into a standalone question is
        # cheap classification work, so it runs on the lite tier. Routing itself
        # stays on the full model — that is what the eval is calibrated on.
        return (complete(prompt, system=memory_prompt, temperature=0.0,
                         model=GEMINI_LITE) or "").strip()
    except Exception as e:
        logger.error("Memory optimization failed: %s", e)
        # Fallback to the original raw query if optimization fails to prevent agent disruption
        return latest_query
