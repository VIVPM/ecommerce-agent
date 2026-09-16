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


def _saved_refs(arg: str, saved: list):
    """Resolve explicit saved-list positions. Never infer a product from a vague phrase."""
    positions = sorted({int(n) for n in re.findall(r"\b\d+\b", arg or "")})
    if not positions:
        return [], 'Which saved item numbers should I add? For example, "add saved items 2 and 3 to my cart."'
    invalid = [n for n in positions if not 1 <= n <= len(saved)]
    if invalid:
        return [], (f"You have {len(saved)} saved item(s), so there's no "
                    f"#{', #'.join(map(str, invalid))}. Which numbers did you mean?")
    return [saved[n - 1] for n in positions], None


def add_saved_to_cart(user_id: int, arg: str) -> str:
    """Add explicitly numbered saved products to the cart at quantity one."""
    from app.compare import fetch_saved  # compare list ordering is the numbered-list ordering
    from app.db.database import SessionLocal
    from app.db.models import now_ist

    saved = fetch_saved(user_id)
    picks, error = _saved_refs(arg, saved)
    if error:
        return error

    db = SessionLocal()
    try:
        added, available = [], 0
        for item in picks:
            # A saved listing may have disappeared since it was saved; do not add a
            # dead pid to the cart. Cart quantities are deliberately fixed at one.
            if not item.get("title") or item.get("price") is None:
                continue
            available += 1
            inserted = db.execute(text("""
                INSERT INTO cart_items (user_id, pid, quantity, created_at)
                VALUES (:uid, :pid, 1, :now)
                ON CONFLICT (user_id, pid) DO NOTHING
                RETURNING pid
            """), {"uid": user_id, "pid": item["pid"], "now": now_ist()}).scalar()
            if inserted:
                added.append(item["title"])
        db.commit()
    except Exception as e:
        db.rollback()
        logger.error("add_saved_to_cart failed: %s", e)
        return "I couldn't add those saved items to your cart just now. Please try again."
    finally:
        db.close()

    if not added:
        if available:
            return "Those saved items are already in your cart."
        return "Those saved products are no longer available, so I couldn't add them to your cart."
    return "✅ Added to your cart:\n" + "\n".join(f"- {title}" for title in added)


def add_results_to_cart(user_id: int, arg: str, history) -> str:
    """Add explicitly numbered products from the latest result list to the cart."""
    from app.compare import last_shown_products, resolve_refs
    from app.db.database import SessionLocal
    from app.db.models import now_ist

    shown = last_shown_products(history)
    if not shown:
        return ('I don\'t see a product list to add from. Search first, then say e.g. '
                '"add items 2 and 3 to my cart."')
    picks, error = resolve_refs(arg, shown)
    if error:
        return error.replace("save", "add to your cart")

    db = SessionLocal()
    try:
        added, available = [], 0
        for pid, title, _ in picks:
            # Confirm it still exists before adding the pid saved in the chat link.
            product = db.execute(text("SELECT title, price FROM product WHERE pid = :pid"),
                                 {"pid": pid}).fetchone()
            if not product or product._mapping["price"] is None:
                continue
            available += 1
            inserted = db.execute(text("""
                INSERT INTO cart_items (user_id, pid, quantity, created_at)
                VALUES (:uid, :pid, 1, :now)
                ON CONFLICT (user_id, pid) DO NOTHING
                RETURNING pid
            """), {"uid": user_id, "pid": pid, "now": now_ist()}).scalar()
            if inserted:
                added.append(title)
        db.commit()
    except Exception as e:
        db.rollback()
        logger.error("add_results_to_cart failed: %s", e)
        return "I couldn't add those products to your cart just now. Please try again."
    finally:
        db.close()

    if not added:
        if available:
            return "Those products are already in your cart."
        return "Those products are no longer available, so I couldn't add them to your cart."
    return "✅ Added to your cart:\n" + "\n".join(f"- {title}" for title in added)


def manage_orders(user_id: int, action: str, arg: str, history=None) -> str:
    """Dispatch a cart/order action the router chose. Unknown/missing action falls back
    to the read-only 'view' so a mis-route can never place or cancel by accident."""
    if action == "add_results_to_cart":
        return add_results_to_cart(user_id, arg, history)
    if action == "add_to_cart":
        return add_saved_to_cart(user_id, arg)
    if action == "place":
        return place_order(user_id, arg)
    if action == "cancel":
        return cancel_order(user_id, arg)
    return view_orders(user_id, arg)


# Thin async wrapper so the streaming caller (main.py) can treat this like the other
# tools. The result is a single deterministic block, so one yield is enough.
async def manage_orders_stream_async(action: str, arg: str, user_id: int, history=None):
    yield manage_orders(user_id, action, arg, history)


if __name__ == "__main__":
    # ponytail: smoke checks pure formatting/reference logic without a DB.
    rows = [{"title": "Puma Runner", "price": 1200, "quantity": 2},
            {"title": "Campus Walk", "price": 999, "quantity": 1}]
    txt = _fmt_items(rows)
    assert "× 2" in txt and "Rs. 2400" in txt and "Rs. 999" in txt, txt
    saved = [{"pid": "P1", "title": "Saved One"}, {"pid": "P2", "title": "Saved Two"},
             {"pid": "P3", "title": "Saved Three"}]
    assert _saved_refs("add saved items 2 and 3 to cart", saved) == ([saved[1], saved[2]], None)
    assert _saved_refs("add saved item 4", saved)[1]
    assert _saved_refs("add this one", saved)[1]
    print("orders helpers OK\n" + txt)
