"""Compare the products a user has saved.

Unlike the SQL and FAQ paths, this is USER-SPECIFIC, so results are never cached:
the sql/faq caches key on the question text alone, so caching a comparison would
serve one user's shortlist to another asking the same question.

Reads live catalog data (price/rating/availability), so a comparison always
reflects the current state, not whatever it was when the product was saved.
"""
import asyncio
import logging
import os

from google import genai
from sqlalchemy import text

from app.db.database import SessionLocal
from app.llm_utils import with_retry

logger = logging.getLogger(__name__)

GEMINI_MODEL = 'gemini-2.5-flash'
gemini_client = genai.Client(api_key=os.getenv('GEMINI_API_KEY'))

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

    client = gemini_client

    prompt = f"THEIR SAVED PRODUCTS:\n{_context(saved)}\n\nTHEIR QUESTION: {question}"
    try:
        stream = await client.aio.models.generate_content_stream(
            model=GEMINI_MODEL,
            contents=prompt,
            config=genai.types.GenerateContentConfig(
                system_instruction=compare_prompt,
                temperature=0.2,
            ),
        )
        async for chunk in stream:
            if chunk.text:
                yield chunk.text
    except Exception as e:
        logger.error("Compare failed: %s", e)
        yield "I couldn't compare your saved products just now. Please try again."


def compare_saved(question: str, user_id: int) -> str:
    """Non-streaming variant (used by evaluate_agent.py / any sync caller)."""
    saved = fetch_saved(user_id)
    if len(saved) < 2:
        return _no_items_message(len(saved))

    client = gemini_client

    def _gen():
        return client.models.generate_content(
            model=GEMINI_MODEL,
            contents=f"THEIR SAVED PRODUCTS:\n{_context(saved)}\n\nTHEIR QUESTION: {question}",
            config=genai.types.GenerateContentConfig(
                system_instruction=compare_prompt,
                temperature=0.2,
            ),
        ).text

    try:
        return with_retry(_gen)
    except Exception as e:
        logger.error("Compare failed: %s", e)
        return "I couldn't compare your saved products just now. Please try again."
