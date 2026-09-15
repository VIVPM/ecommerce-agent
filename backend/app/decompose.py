"""Split a multi-intent message into the separate questions it contains.

Every tool is `return_direct=True`, so one agent run answers exactly ONE intent
and terminates. "What's your return policy and show me Nike shoes under 3000"
used to get a policy answer and drop the product half silently.

Splitting happens HERE, before the agent, and each part then runs through the
unchanged single-hop agent. Dropping return_direct to make the agent multi-step
would have been the other option and is the wrong one: it is the only thing
stopping a second model call from paraphrasing away the verified product
formatting (rating counts, price-age notes, unsupported-filter warnings).

The dangerous failure is OVER-splitting, not under-splitting. Under-splitting is
just today's behaviour; over-splitting breaks a query that works. "Nike shoes
under 3000 rated above 4.5" is one query with three filters, and turning it into
three questions would be a regression. The prompt is therefore biased towards
leaving things whole, and every parse failure falls back to "one part".
"""
import json
import logging
import re

from app.cache import cache_get, cache_set
from app.llm_provider import GEMINI_LITE, complete

logger = logging.getLogger(__name__)

# Above this, assume the splitter has misfired and run the message whole. A
# shopper asking six real things in one breath is rarer than a bad parse, and
# six agent runs is six times the cost and latency.
MAX_PARTS = 4

_MULTI_HINT = re.compile(
    r"\b(?:and|also|plus|then|as well as)\b"   # a conjunction
    r"|\?\s*\S"                                # a "?" with more text after it
    r"|;"                                        # a semicolon
    r"|\.\s+\S",                               # a sentence boundary, then more
    re.IGNORECASE,
)

# Runs on flash-lite: this is cheap classification, not generation. Results are
# cached on the question text, so a repeated message costs nothing.
# NOTE: after editing this prompt, run cache_purge('decompose') -- stale splits
# would otherwise outlive the rules that produced them.
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

    Fails OPEN in every direction: a model error, an unparseable reply, an empty
    list or an implausible number of parts all return the original message, so
    the worst case is exactly the behaviour that existed before this module.
    """
    q = (question or "").strip()
    if not q:
        return [question]

    # Skip the model call when nothing hints at a SECOND question. A trailing "?"
    # is not such a hint -- almost every question has one, and an earlier version
    # that treated it as one burned 6s of flash-lite on "any cheaper?". This gates
    # the CALL, not the decision: anything that could plausibly be two questions
    # still goes to the model.
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
