"""Cart + simulated orders.

This is a shopping assistant over a scraped Flipkart catalogue, not a real store:
orders are SIMULATED (COD, no payment, no fulfilment) and every message says so.
The order snapshots each item's title + price at placement time, so it stays
truthful after the nightly refresh moves the live catalogue price.

Order actions are deterministic — they return a fixed confirmation, never an LLM
generation. Only the ROUTING to these tools uses the LLM (agent.py), which is what
keeps ordering agentic without paying for a generation on every confirmation.
"""
import logging
import re

from sqlalchemy import text

logger = logging.getLogger(__name__)

_DEMO_NOTE = "_This is a demo order (simulated, cash on delivery) — no payment is taken._"


def _fmt_items(rows):
    """rows: mappings with title, price, quantity."""
    lines = []
    for r in rows:
        qty = r["quantity"] or 1
        price = r["price"]
        line = f"- {r['title']}" + (f" × {qty}" if qty > 1 else "")
        if price is not None:
            line += f" — Rs. {price * qty}"
        lines.append(line)
    return "\n".join(lines)


def place_order(user_id: int, _arg: str = "") -> str:
    """Turn the user's cart into a placed order, then clear the cart."""
    from app.db.database import SessionLocal
    from app.db.models import now_ist
    db = SessionLocal()
    try:
        cart = db.execute(text("""
            SELECT c.pid, c.quantity, p.title, p.price
              FROM cart_items c
              LEFT JOIN product p ON p.pid = c.pid
             WHERE c.user_id = :uid
             ORDER BY c.created_at
        """), {"uid": user_id}).fetchall()
        cart = [dict(r._mapping) for r in cart]

        if not cart:
            return ("Your cart is empty, so there's nothing to order yet. Add a product "
                    "to your cart from any result list and then ask me to place the order.")

        # A product can be delisted between adding it and ordering; skip priced-out rows.
        items = [c for c in cart if c["price"] is not None]
        if not items:
            return ("The items in your cart no longer have a live price (they may have been "
                    "delisted). Remove them and add something in stock, then try again.")

        total = sum(c["price"] * (c["quantity"] or 1) for c in items)
        order = db.execute(text("""
            INSERT INTO orders (user_id, status, total, created_at)
            VALUES (:uid, 'placed', :total, :now) RETURNING id
        """), {"uid": user_id, "total": total, "now": now_ist()}).scalar()
        for c in items:
            db.execute(text("""
                INSERT INTO order_items (order_id, pid, title, price, quantity)
                VALUES (:oid, :pid, :title, :price, :qty)
            """), {"oid": order, "pid": c["pid"], "title": c["title"],
                   "price": c["price"], "qty": c["quantity"] or 1})
        db.execute(text("DELETE FROM cart_items WHERE user_id = :uid"), {"uid": user_id})
        db.commit()

        return (f"✅ **Order #{order} placed** — {len(items)} item(s), total **Rs. {total}**.\n\n"
                f"{_fmt_items(items)}\n\n{_DEMO_NOTE}")
    except Exception as e:
        db.rollback()
        logger.error("place_order failed: %s", e)
        return "I couldn't place your order just now. Please try again."
    finally:
        db.close()


def view_orders(user_id: int, _arg: str = "") -> str:
    """List the user's orders, newest first, with their items."""
    from app.db.database import SessionLocal
    db = SessionLocal()
    try:
        orders = db.execute(text("""
            SELECT id, status, total, created_at FROM orders
             WHERE user_id = :uid ORDER BY id DESC
        """), {"uid": user_id}).fetchall()
        if not orders:
            return "You haven't placed any orders yet."

        out = []
        for o in orders:
            m = o._mapping
            items = db.execute(text("""
                SELECT title, price, quantity FROM order_items WHERE order_id = :oid
            """), {"oid": m["id"]}).fetchall()
            when = m["created_at"].strftime("%d %b %Y") if m["created_at"] else ""
            head = f"**Order #{m['id']}** · {m['status'].upper()} · Rs. {m['total']} · {when}"
            out.append(head + "\n" + _fmt_items([r._mapping for r in items]))
        return ("\n\n".join(out) + f"\n\n{_DEMO_NOTE}")
    finally:
        db.close()


def cancel_order(user_id: int, arg: str = "") -> str:
    """Cancel an order. Cancels the one whose number is in `arg` (e.g. "cancel order 12"),
    or the most recent still-placed order if no number is given."""
    from app.db.database import SessionLocal
    db = SessionLocal()
    try:
        m = re.search(r"\d+", arg or "")
        if m:
            oid = int(m.group())
            order = db.execute(text("""
                SELECT id, status FROM orders WHERE id = :oid AND user_id = :uid
            """), {"oid": oid, "uid": user_id}).fetchone()
            if not order:
                return f"I couldn't find order #{oid} on your account."
            if order._mapping["status"] == "cancelled":
                return f"Order #{oid} is already cancelled."
        else:
            order = db.execute(text("""
                SELECT id, status FROM orders
                 WHERE user_id = :uid AND status = 'placed'
                 ORDER BY id DESC LIMIT 1
            """), {"uid": user_id}).fetchone()
            if not order:
                return "You have no active orders to cancel."
            oid = order._mapping["id"]

        db.execute(text("UPDATE orders SET status = 'cancelled' WHERE id = :oid"), {"oid": oid})
        db.commit()
        return f"✅ **Order #{oid} cancelled.** {_DEMO_NOTE}"
    except Exception as e:
        db.rollback()
        logger.error("cancel_order failed: %s", e)
        return "I couldn't cancel that order just now. Please try again."
    finally:
        db.close()


# Thin async wrappers so the streaming caller (main.py) can treat these like the
# other tools. The result is a single deterministic block, so one yield is enough.
async def place_order_stream_async(arg: str, user_id: int):
    yield place_order(user_id, arg)


async def view_orders_stream_async(arg: str, user_id: int):
    yield view_orders(user_id, arg)


async def cancel_order_stream_async(arg: str, user_id: int):
    yield cancel_order(user_id, arg)


if __name__ == "__main__":
    # ponytail: smoke check the confirmation formatter without a DB.
    rows = [{"title": "Puma Runner", "price": 1200, "quantity": 2},
            {"title": "Campus Walk", "price": 999, "quantity": 1}]
    txt = _fmt_items(rows)
    assert "× 2" in txt and "Rs. 2400" in txt and "Rs. 999" in txt, txt
    print("orders._fmt_items OK\n" + txt)
