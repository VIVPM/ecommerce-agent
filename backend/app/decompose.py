"""Split a multi-intent message into the separate questions it contains."""
import json
import logging
import re

from app.cache import cache_get, cache_set
from app.llm_provider import GEMINI_LITE, complete

logger = logging.getLogger(__name__)

MAX_PARTS = 4

_MULTI_HINT = re.compile(
    r"\b(?:and|also|plus|then|as well as)\b"
    r"|\?\s*\S"
    r"|;"
    r"|\.\s+\S",
    re.IGNORECASE,
)

DECOMPOSE_PROMPT = """You split a shopper's message into the SEPARATE questions it contains.

This store's assistant can do exactly three things:
  PRODUCT  - search the shoe catalogue (price, brand, rating, stock)
  FAQ      - answer store policy (delivery, returns, payment, cancellation)
  COMPARE  - compare the products THIS shopper has saved

Return JSON only: {"parts": ["...", "..."]}

Rules:
- Split ONLY when the message asks for two or more of PRODUCT / FAQ / COMPARE.
- Two questions about the SAME one of those is ONE part. "What are the payment
  options and delivery charges?" is ONE part.
- A single request with several FILTERS is ONE part. "Nike shoes under 3000
  rated above 4.5 in stock" is ONE part, not three.
- A single request naming several ATTRIBUTES or uses is ONE part. "shoes for
  running and walking" is ONE part.
- COMPARE means ONLY the items this shopper has already SAVED. If "compare" or
  "which is best" refers to products from another part of THIS message, or to
  search results, it is NOT a separate part -- the product search already
  returns a ranked, comparable list. "Show me shoes under 3000 and compare
  them" is ONE part.
- Never invent a question the shopper did not ask.
- Each part must stand alone: resolve "them", "those", "it" into what they
  refer to, so each part is answerable on its own.
- Keep the shopper's own wording wherever you can.
- If it is a single question, return exactly one part.

Message: {q}"""


def _parse(raw: str, question: str) -> list[str]:
    """Pull the parts out of the model's reply, falling back to one part."""
    m = re.search(r"\{.*\}", raw or "", re.S)
    if not m:
        return [question]
    try:
        parts = json.loads(m.group(0)).get("parts") or []
    except (json.JSONDecodeError, AttributeError):
        return [question]
    parts = [p.strip() for p in parts if isinstance(p, str) and p.strip()]
    if not parts or len(parts) > MAX_PARTS:
        return [question]
    return parts


def decompose(question: str) -> list[str]:
    """Return the question's parts -- always at least one, and [question] itself
    whenever splitting is unnecessary, uncertain, or fails.
    """
    q = (question or "").strip()
    if not q:
        return [question]

    if not _MULTI_HINT.search(q):
        return [q]

    if cached := cache_get("decompose", q):
        try:
            parts = json.loads(cached)
            if isinstance(parts, list) and parts:
                return parts
        except json.JSONDecodeError:
            pass

    try:
        raw = complete(DECOMPOSE_PROMPT.replace("{q}", q),
                       temperature=0.0, model=GEMINI_LITE)
    except Exception as e:
        logger.warning("decompose failed, running the message whole: %s", e)
        return [q]

    parts = _parse(raw, q)
    cache_set("decompose", q, json.dumps(parts))
    if len(parts) > 1:
        logger.info("Decomposed %r into %d parts: %s", q[:80], len(parts), parts)
    return parts
