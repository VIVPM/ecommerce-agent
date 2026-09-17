"""Answer "what have I ordered before?" from the user's own order rows.

Deliberately NOT text-to-SQL. The catalogue search generates SQL because the
question space there is open-ended; an order history has exactly one shape, so a
fixed, parameterised query is both cheaper and impossible to talk into reading
someone else's orders — user_id is a bound parameter, never model output.

Also deliberately NOT an LLM call. The rows already are the answer; sending them
through a model to be reworded costs money and latency and adds a chance of the
numbers changing on the way out.
"""
import logging

from sqlalchemy import text

from app.db.database import SessionLocal

logger = logging.getLogger(__name__)

# Enough to answer "what have I ordered", not so many that the reply is a wall.
MAX_ORDERS = 10


def _rows(user_id: int):
    db = SessionLocal()
    try:
        return db.execute(text("""
            SELECT o.id, o.status, o.total, o.created_at,
                   i.title, i.price, i.quantity
              FROM orders o
              LEFT JOIN order_items i ON i.order_id = o.id
             WHERE o.user_id = :uid
             ORDER BY o.created_at DESC, i.id
        """), {"uid": user_id}).fetchall()
    finally:
        db.close()


def summarize(user_id: int) -> str:
    """Markdown summary of this user's orders, shopper-ready (return_direct)."""
    try:
        rows = _rows(user_id)
    except Exception as e:
        logger.error("Order history lookup failed: %s", e)
        return "I couldn't look up your orders just now. Please try again."

    if not rows:
        return ("You haven't placed any orders yet. Add something to your cart and "
                "place an order, and it'll show up here.")

    orders, seen = [], {}
    for r in rows:
        m = r._mapping
        o = seen.get(m["id"])
        if o is None:
            o = {"id": m["id"], "status": m["status"], "total": m["total"],
                 "created_at": m["created_at"], "items": []}
            seen[m["id"]] = o
            orders.append(o)
        if m["title"]:
            o["items"].append((m["title"], m["price"], m["quantity"] or 1))

    placed = [o for o in orders if o["status"] == "placed"]
    cancelled = [o for o in orders if o["status"] == "cancelled"]
    spent = sum(o["total"] or 0 for o in placed)

    # Lead with the answer to "what have I ordered", then the detail.
    head = f"You've placed **{len(placed)}** order{'s' if len(placed) != 1 else ''}"
    if spent:
        head += f", **Rs. {spent:,}** in total"
    if cancelled:
        head += f" ({len(cancelled)} cancelled)"
    lines = [head + ":", ""]

    for o in orders[:MAX_ORDERS]:
        when = o["created_at"].strftime("%d %b %Y") if o["created_at"] else "—"
        tag = " — *cancelled*" if o["status"] == "cancelled" else ""
        lines.append(f"**Order #{o['id']}** · {when} · Rs. {o['total'] or 0:,}{tag}")
        for title, price, qty in o["items"]:
            qty_str = f" ×{qty}" if qty and qty > 1 else ""
            lines.append(f"  - {title}{qty_str} — Rs. {price if price is not None else '—'}")
        lines.append("")

    if len(orders) > MAX_ORDERS:
        lines.append(f"*(Showing your {MAX_ORDERS} most recent of {len(orders)} orders.)*")

    return "\n".join(lines).strip()


async def order_history_stream_async(user_id: int):
    """Async generator so this tool streams like every other one.

    One chunk, because the whole answer is built from rows already in hand —
    there is nothing to stream progressively and pretending otherwise would just
    add latency.
    """
    import asyncio
    yield await asyncio.to_thread(summarize, user_id)
