"""Cart and (simulated) order actions the agent can take."""
import logging

from sqlalchemy import text

from app.compare import fetch_saved, last_shown_products, positions, resolve_refs
from app.db.database import SessionLocal
from app.db.models import now_ist
from app.order_history import summarize

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


def place(user_id: int):
    """Turn the cart into an order and empty it, in ONE transaction."""
    db = SessionLocal()
    try:
        rows = db.execute(text("""
            SELECT c.pid, c.quantity, p.title, p.price, p.availability
              FROM cart_items c
              LEFT JOIN product p ON p.pid = c.pid
             WHERE c.user_id = :uid
             ORDER BY c.created_at
        """), {"uid": user_id}).fetchall()
        items = [dict(r._mapping) for r in rows]
        if not items:
            return None, {"code": 400, "message":
                          "Your cart is empty, so there's nothing to order yet. Add a "
                          "product from any result list and then ask me to place the order."}

        unbuyable = [i["title"] or i["pid"] for i in items if i["availability"] != "InStock"]
        if unbuyable:
            return None, {"code": 409, "message":
                          "These are no longer in stock: " + ", ".join(unbuyable[:3]) +
                          ". Remove them from your cart and I'll place the rest."}

        total = sum((i["price"] or 0) * (i["quantity"] or 1) for i in items)
        order_id = db.execute(text("""
            INSERT INTO orders (user_id, status, total, created_at)
            VALUES (:uid, 'placed', :total, :now) RETURNING id
        """), {"uid": user_id, "total": total, "now": now_ist()}).scalar()
        for i in items:
            db.execute(text("""
                INSERT INTO order_items (order_id, pid, title, price, quantity)
                VALUES (:oid, :pid, :title, :price, :qty)
            """), {"oid": order_id, "pid": i["pid"], "title": i["title"],
                   "price": i["price"], "qty": i["quantity"] or 1})
        db.execute(text("DELETE FROM cart_items WHERE user_id = :uid"), {"uid": user_id})
        db.commit()
        return {"order_id": order_id, "total": total, "items": items}, None
    except Exception as e:
        db.rollback()
        logger.error("place failed: %s", e)
        return None, {"code": 500,
                      "message": "I couldn't place your order just now. Please try again."}
    finally:
        db.close()


def cancel(user_id: int, order_id: int):
    """Cancel one placed order. Returns (ok, error)."""
    db = SessionLocal()
    try:
        res = db.execute(text("""
            UPDATE orders SET status = 'cancelled'
             WHERE id = :oid AND user_id = :uid AND status = 'placed'
        """), {"oid": order_id, "uid": user_id})
        db.commit()
        if not res.rowcount:
            return False, {"code": 404, "message":
                           f"I couldn't find order #{order_id} on your account, or it was "
                           f"already cancelled."}
        return True, None
    except Exception as e:
        db.rollback()
        logger.error("cancel failed: %s", e)
        return False, {"code": 500,
                       "message": "I couldn't cancel that order just now. Please try again."}
    finally:
        db.close()


def place_order(user_id: int, _arg: str = "") -> str:
    result, error = place(user_id)
    if error:
        return error["message"]
    return (f"✅ **Order #{result['order_id']} placed** — {len(result['items'])} item(s), "
            f"total **Rs. {result['total']:,}**.\n\n"
            f"{_fmt_items(result['items'])}\n\n{_DEMO_NOTE}")


def cancel_order(user_id: int, arg: str = "") -> str:
    """Cancel the order the shopper named. An order id is an EXPLICIT number --
    never inferred, because cancelling the wrong order cannot be undone here."""
    ids = positions(arg, limit=None)
    if not ids:
        return ('Which order should I cancel? Tell me its number, e.g. "cancel order 12" '
                '— say "my orders" if you want to see them first.')
    if len(ids) > 1:
        return (f"I can only cancel one order at a time — you named #{ids[0]} and #{ids[1]}. "
                f"Which one?")
    ok, error = cancel(user_id, ids[0])
    return f"✅ Order #{ids[0]} cancelled." if ok else error["message"]


def _saved_refs(arg: str, saved: list):
    """Resolve EXPLICIT saved-list positions for a cart add."""
    idx = positions(arg)
    if not idx:
        return [], ('Which saved item numbers should I add? For example, '
                    '"add saved items 2 and 3 to my cart."')
    bad = [n for n in idx if not 1 <= n <= len(saved)]
    if bad:
        return [], (f"You have {len(saved)} saved item(s), so there's no "
                    f"#{', #'.join(map(str, bad))}. Which numbers did you mean?")
    return [saved[n - 1] for n in idx], None


def _add_to_cart(user_id: int, picks) -> str:
    """picks: [(pid, title)]. Confirms each pid is still live before adding it."""
    db = SessionLocal()
    try:
        added, available = [], 0
        for pid, title in picks:
            row = db.execute(text("SELECT title, price FROM product WHERE pid = :pid"),
                             {"pid": pid}).fetchone()
            if not row or row._mapping["price"] is None:
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
        logger.error("_add_to_cart failed: %s", e)
        return "I couldn't add those to your cart just now. Please try again."
    finally:
        db.close()

    if not added:
        if available:
            return "Those are already in your cart."
        return "Those products are no longer available, so I couldn't add them to your cart."
    return "✅ Added to your cart:\n" + "\n".join(f"- {t}" for t in added)


def add_saved_to_cart(user_id: int, arg: str) -> str:
    """Add numbered items from the SAVED list to the cart."""
    saved = fetch_saved(user_id)
    if not saved:
        return ("You don't have any saved products yet. Save something first, or say "
                '"add items 2 and 3 to my cart" to add from a result list.')
    picks, error = _saved_refs(arg, saved)
    if error:
        return error
    return _add_to_cart(user_id, [(p["pid"], p["title"]) for p in picks])


def add_results_to_cart(user_id: int, arg: str, history) -> str:
    """Add numbered items from the latest RESULT list to the cart."""
    shown = last_shown_products(history)
    if not shown:
        return ("I don't see a product list to add from. Search for something first, then "
                'say e.g. "add items 2 and 3 to my cart".')
    picks, error = resolve_refs(arg, shown)
    if error:
        return error.replace("save", "add to your cart")
    return _add_to_cart(user_id, [(pid, title) for pid, title, _ in picks])


def manage_orders(user_id: int, action: str, arg: str, history=None) -> str:
    """Dispatch the cart/order action the router chose."""
    action = (action or "").strip().lower()
    if action == "add_results_to_cart":
        return add_results_to_cart(user_id, arg, history)
    if action == "add_to_cart":
        return add_saved_to_cart(user_id, arg)
    if action == "place":
        return place_order(user_id, arg)
    if action == "cancel":
        return cancel_order(user_id, arg)
    if action != "view":
        logger.warning("manage_orders: unknown action %r — showing orders instead.", action)
    return summarize(user_id)


async def manage_orders_stream_async(action: str, arg: str, user_id: int, history=None):
    """One deterministic block, so a single yield is enough."""
    yield manage_orders(user_id, action, arg, history)
