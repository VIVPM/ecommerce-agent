"""Compare the products a user has saved.

Never cached: the sql/faq caches key on question text alone, so a cached
comparison would serve one user's shortlist to another. Reads live catalogue
data, so prices and stock are current.
"""
import asyncio
import logging
import re

from sqlalchemy import text

from app.db.database import SessionLocal
from app.llm_provider import complete, stream as llm_stream

logger = logging.getLogger(__name__)

GEMINI_MODEL = 'gemini-2.5-flash'

compare_prompt = """You are helping a shopper decide between products they have shortlisted.

You will be given their saved products with live data. Compare them and help them choose.

Guidelines:
1. Lead with a clear recommendation and WHY (value for money, rating quality, price movement).
2. Weigh rating alongside the number of ratings — 4.5 from 12 people is weaker evidence than 4.2 from 3000.
   Call them RATINGS, never "reviews": they are different counts on Flipkart and ratings are always the larger.
3. If something dropped in price since they saved it, call that out; it's useful.
4. If something is not InStock, say so plainly and don't recommend it.
5. Keep it tight — a short comparison then the recommendation.
6. EVERY product you mention must be a markdown link — [Product name](url) — including
   the ones you are not recommending. A shopper cannot act on a product you name but
   don't link. Never paste raw URLs.
7. If two or more saved items share the same model and differ only in price, say so
   explicitly: they are separate SELLER LISTINGS of one shoe, not different shoes.
   That is the actual reason the prices differ and it changes the decision — the
   choice is which seller, not which shoe.
8. You know nothing about this shopper's needs — size, budget, terrain, style. Do not
   open with "yes, you should buy one". Recommend WHICH of these is the better pick
   and why, and leave whether to buy at all to them.
9. Only use the data provided. Never invent specs, sizes, colours or features you weren't given.
"""


def fetch_saved(user_id: int):
    """Saved products joined to live catalog data."""
    db = SessionLocal()
    try:
        rows = db.execute(text("""
            SELECT s.pid, s.saved_price,
                   p.title, p.brand, p.price, p.avg_rating, p.total_ratings,
                   p.availability, p.product_link
              FROM saved_products s
              LEFT JOIN product p ON p.pid = s.pid
             WHERE s.user_id = :uid
             ORDER BY s.created_at DESC
        """), {"uid": user_id}).fetchall()
        return [dict(r._mapping) for r in rows]
    finally:
        db.close()


def _context(saved):
    lines = []
    for i, s in enumerate(saved, 1):
        price, was = s.get("price"), s.get("saved_price")
        move = ""
        if price is not None and was is not None and price != was:
            delta = price - was
            move = f", price {'DOWN' if delta < 0 else 'UP'} by {abs(delta)} since they saved it (was {was})"
        rating = s.get("avg_rating")
        rating_txt = (f"{rating} from {s.get('total_ratings') or 0} ratings"
                      if rating is not None else "no ratings yet")
        lines.append(
            f"{i}. {s.get('title')} | brand: {s.get('brand')} | price: Rs. {price}{move} | "
            f"rating: {rating_txt} | availability: {s.get('availability')} | url: {s.get('product_link')}"
        )
    return "\n".join(lines)


def _no_items_message(n):
    if n == 0:
        return ("You haven't saved any products yet. Tap the heart next to any product "
                "in a result list and I'll be able to compare them for you.")
    return ("You've only saved one product so far, so there's nothing to compare it "
            "against yet. Save another and ask me again.")


async def compare_saved_stream_async(question: str, user_id: int):
    """Async streaming comparison of the user's saved products."""
    saved = await asyncio.to_thread(fetch_saved, user_id)
    if len(saved) < 2:
        yield _no_items_message(len(saved))
        return

    prompt = f"THEIR SAVED PRODUCTS:\n{_context(saved)}\n\nTHEIR QUESTION: {question}"
    try:
        async for tok in llm_stream(prompt, system=compare_prompt, temperature=0.2, model=GEMINI_MODEL):
            yield tok
    except Exception as e:
        logger.error("Compare failed: %s", e)
        yield "I couldn't compare your saved products just now. Please try again."


def compare_saved(question: str, user_id: int) -> str:
    """Non-streaming variant (used by evaluate_agent.py / any sync caller)."""
    saved = fetch_saved(user_id)
    if len(saved) < 2:
        return _no_items_message(len(saved))

    try:
        return complete(
            f"THEIR SAVED PRODUCTS:\n{_context(saved)}\n\nTHEIR QUESTION: {question}",
            system=compare_prompt, temperature=0.2, model=GEMINI_MODEL)
    except Exception as e:
        logger.error("Compare failed: %s", e)
        return "I couldn't compare your saved products just now. Please try again."


# --- Save items from chat ----------------------------------------------------
# "save 2", "save the first and third", "save the Puma Smashic". The products the
# user just saw are the markdown links in recent assistant messages — each carries
# its pid in the URL (the same thing the ♡ button keys on) — so a reference resolves
# against what is actually on screen. Ambiguous or unmatched -> ask, never guess.

def positions(text: str, limit: int | None = 2) -> list:
    """Position numbers the shopper named, in order, de-duplicated.

    Bounded to `limit` digits (2 by default) so a PRICE in the sentence is not
    read as a row number: "add items 2 and 3 under 3000" used to answer "there's
    no #3000" on the path that left this unbounded. A list never has a 100th row;
    a price nearly always has more digits than one.

    limit=None lifts the bound for ids that are genuinely unbounded, such as an
    order number.
    """
    pattern = r"\b\d+\b" if limit is None else r"\b\d{1,%d}\b" % limit
    out = []
    for n in (int(m) for m in re.findall(pattern, text or "")):
        if n not in out:
            out.append(n)
    return sorted(out)


_LINK_RE = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]*[?&]pid=([A-Za-z0-9]+)[^)\s]*)\)")
_SLUG_RE = re.compile(r"flipkart\.com/([^/?]+)/p/")
_GENERIC_LINK = {"view product", "view", "link", "here", "product"}
_ORDINALS = {"first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5, "sixth": 6,
             "seventh": 7, "eighth": 8, "ninth": 9, "tenth": 10,
             "1st": 1, "2nd": 2, "3rd": 3, "4th": 4, "5th": 5, "6th": 6, "7th": 7,
             "8th": 8, "9th": 9, "10th": 10}
_NOISE = {"save", "saved", "add", "to", "my", "the", "one", "ones", "it", "this", "that",
          "please", "shortlist", "wishlist", "list", "and", "item", "items", "product",
          "products", "shoe", "shoes", "number", "no", "a", "also", "too", "both", "of"}


def _name_for(line: str, link_text: str, url: str) -> tuple:
    """Searchable name for a listed product. Search results link "View Product" after
    "N. Title: Rs. ...", so the title comes from the line; the URL slug adds the brand
    (titles often omit it). Compare answers link the name itself."""
    if link_text.strip().lower() in _GENERIC_LINK:
        name = line[:line.find("[")]
        name = re.sub(r"^\W*\d+[.)]\s*", "", name)            # "1. " prefix
        name = re.split(r":\s*Rs\.|\s-\s*Rs\.|,\s*Rs\.", name)[0]
    else:
        name = link_text
    slug = _SLUG_RE.search(url)
    return name.strip(" *:-"), (slug.group(1).replace("-", " ") if slug else "")


def last_shown_products(history) -> list:
    """[(pid, title, search_text)] from the most recent assistant message that listed
    products, in display order (so position n == the shopper's "n")."""
    for msg in reversed(history or []):
        if msg.get("role") != "assistant":
            continue
        seen, out = set(), []
        for line in (msg.get("content") or "").splitlines():
            for link_text, url, pid in _LINK_RE.findall(line):
                pid = pid.upper()
                if pid in seen:
                    continue
                seen.add(pid)
                title, slug = _name_for(line, link_text, url)
                out.append((pid, title, f"{title} {slug}".lower()))
        if out:
            return out
    return []


def resolve_refs(query: str, shown: list):
    """Map the shopper's reference(s) to shown products. Returns (matches, error)."""
    q = (query or "").lower()
    idx = set(positions(q))
    idx |= {v for k, v in _ORDINALS.items() if re.search(rf"\b{k}\b", q)}
    if re.search(r"\blast\b", q):
        idx.add(len(shown))
    if idx:
        bad = sorted(i for i in idx if not 1 <= i <= len(shown))
        if bad:
            return [], (f"I only showed {len(shown)} product(s), so there's no "
                        f"#{', #'.join(map(str, bad))}. Which number did you mean?")
        return [shown[i - 1] for i in sorted(idx)], None
    if re.search(r"\b(all|every|everything)\b", q):
        return list(shown), None

    # By name: every meaningful word must appear in exactly one shown title.
    words = [w for w in re.findall(r"[a-z0-9]+", q) if w not in _NOISE]
    if words:
        hits = [p for p in shown if all(w in p[2] for w in words)]
        if len(hits) == 1:
            return hits, None
        if len(hits) > 1:
            return [], "More than one product matches that — which number should I save?"
    return [], "Which one should I save? Tell me its number from the list (e.g. \"save 2\")."


def save_from_results(user_id: int, query: str, history) -> str:
    """Save the referenced product(s) from the latest result list to the shortlist."""
    shown = last_shown_products(history)
    if not shown:
        return ("I don't see a product list to save from. Search for something first, then "
                "say e.g. \"save 2\" — or tap the ♡ on any result.")
    picks, err = resolve_refs(query, shown)
    if err:
        return err

    from app.db.models import now_ist
    db = SessionLocal()
    try:
        saved = []
        for pid, title, _ in picks:
            row = db.execute(text("SELECT price FROM product WHERE pid = :pid"),
                             {"pid": pid}).fetchone()
            if not row:
                continue   # delisted since it was shown
            db.execute(text("""
                INSERT INTO saved_products (user_id, pid, saved_price, created_at)
                VALUES (:uid, :pid, :price, :now)
                ON CONFLICT (user_id, pid) DO NOTHING
            """), {"uid": user_id, "pid": pid, "price": row._mapping["price"], "now": now_ist()})
            saved.append(title)
        db.commit()
    except Exception as e:
        db.rollback()
        logger.error("save_from_results failed: %s", e)
        return "I couldn't save that just now. Please try again."
    finally:
        db.close()

    if not saved:
        return "That product is no longer listed, so I couldn't save it."
    names = "\n".join(f"- {t}" for t in saved)
    return f"✅ Saved to your list:\n{names}\n\nSay \"compare my saved\" when you're ready to pick."


async def save_from_results_stream_async(query: str, user_id: int, history):
    yield await asyncio.to_thread(save_from_results, user_id, query, history)


_REMOVE_NOISE = _NOISE | {"remove", "delete", "clear", "from", "currently", "present"}


def resolve_saved_refs(query: str, saved: list):
    """Resolve a removal request against the user's live saved list, never history."""
    q = (query or "").lower()
    indexes = set(positions(q))
    indexes |= {v for k, v in _ORDINALS.items() if re.search(rf"\b{k}\b", q)}
    if indexes:
        bad = sorted(i for i in indexes if not 1 <= i <= len(saved))
        if bad:
            return [], (f"You have {len(saved)} saved item(s), so there's no "
                        f"#{', #'.join(map(str, bad))}. Which numbers did you mean?")
        return [saved[i - 1] for i in sorted(indexes)], None

    # "remove saved items that are currently present" means clear the live saved list.
    if re.search(r"\b(all|every|everything|clear)\b|currently\s+present|saved\s+items?", q):
        return list(saved), None

    words = [w for w in re.findall(r"[a-z0-9]+", q) if w not in _REMOVE_NOISE]
    hits = [item for item in saved if words and all(w in (item.get("title") or "").lower()
                                                          for w in words)]
    if len(hits) == 1:
        return hits, None
    if len(hits) > 1:
        return [], "More than one saved product matches that — which number should I remove?"
    return [], "Which saved item should I remove? Tell me its number, e.g. \"remove saved item 2\"."


def remove_saved_items(user_id: int, query: str) -> str:
    """Remove selected (or explicitly all) saved products from the live shortlist."""
    saved = fetch_saved(user_id)
    if not saved:
        return "You don't have any saved products to remove."
    picks, error = resolve_saved_refs(query, saved)
    if error:
        return error

    db = SessionLocal()
    try:
        removed = []
        for item in picks:
            deleted = db.execute(text("""
                DELETE FROM saved_products WHERE user_id = :uid AND pid = :pid
                RETURNING pid
            """), {"uid": user_id, "pid": item["pid"]}).scalar()
            if deleted:
                removed.append(item["title"])
        db.commit()
    except Exception as e:
        db.rollback()
        logger.error("remove_saved_items failed: %s", e)
        return "I couldn't remove those saved products just now. Please try again."
    finally:
        db.close()

    return "✅ Removed from your saved list:\n" + "\n".join(f"- {title}" for title in removed)


async def remove_saved_items_stream_async(query: str, user_id: int):
    yield await asyncio.to_thread(remove_saved_items, user_id, query)

