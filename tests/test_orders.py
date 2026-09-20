"""Cart and order actions (app.orders).

What these pin down, all of it learned the hard way:

  * an order id or a cart position is read from EXPLICIT numbers, and one parser
    does it everywhere. Two parsers that merely agreed today is what produced
    "there's no #3000" on the cart path while the save path handled the same
    sentence
  * a destructive action with no number ASKS. Cancelling the wrong order cannot
    be undone here, so an unnamed "cancel my order" must never pick one
  * an unknown action falls back to the read-only VIEW, so a mis-route shows
    orders rather than placing or cancelling one

Offline: the database layer is stubbed. No DB, no model, no network.
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))
os.environ.setdefault("LLM_MODEL", "GEMINI")
os.environ.setdefault("DATABASE_URL", "postgresql://u:p@localhost/db")

from app import orders  # noqa: E402
from app.compare import positions  # noqa: E402


class PositionParserTest(unittest.TestCase):
    """One parser, used by every path that reads a number out of a message."""

    def test_a_price_is_not_a_position(self):
        """"add items 2 and 3 under 3000" answered "there's no #3000" on the
        cart path, because that parser was unbounded while the save one was not."""
        self.assertEqual(positions("add items 2 and 3 under 3000"), [2, 3])

    def test_a_year_is_not_a_position(self):
        self.assertEqual(positions("add the 2024 model, item 2"), [2])

    def test_an_order_id_may_be_long(self):
        """Order ids are genuinely unbounded, unlike a row number."""
        self.assertEqual(positions("cancel order 1247", limit=None), [1247])

    def test_duplicates_collapse_and_order_is_stable(self):
        self.assertEqual(positions("items 3 and 1 and 3"), [1, 3])

    def test_no_numbers_is_empty(self):
        self.assertEqual(positions("add them all"), [])
        self.assertEqual(positions(""), [])
        self.assertEqual(positions(None), [])


class CancelSafetyTest(unittest.TestCase):
    def test_no_number_asks_and_cancels_nothing(self):
        with mock.patch.object(orders, "cancel") as cancelled:
            out = orders.cancel_order(1, "cancel my order")
        cancelled.assert_not_called()
        self.assertIn("which", out.lower())

    def test_two_numbers_asks_rather_than_picking_one(self):
        with mock.patch.object(orders, "cancel") as cancelled:
            out = orders.cancel_order(1, "cancel orders 12 and 13")
        cancelled.assert_not_called()
        self.assertIn("12", out)
        self.assertIn("13", out)

    def test_an_explicit_number_goes_through(self):
        with mock.patch.object(orders, "cancel", return_value=(True, None)) as cancelled:
            out = orders.cancel_order(7, "cancel order 12")
        cancelled.assert_called_once_with(7, 12)
        self.assertIn("12", out)

    def test_a_refusal_reaches_the_shopper_as_prose(self):
        err = {"code": 404, "message": "I couldn't find order #12 on your account."}
        with mock.patch.object(orders, "cancel", return_value=(False, err)):
            self.assertEqual(orders.cancel_order(1, "cancel order 12"), err["message"])


class SavedRefsTest(unittest.TestCase):
    SAVED = [{"pid": "A", "title": "Nike"}, {"pid": "B", "title": "Puma"}]

    def test_explicit_numbers_resolve(self):
        picks, err = orders._saved_refs("add saved items 1 and 2 to my cart", self.SAVED)
        self.assertIsNone(err)
        self.assertEqual([p["pid"] for p in picks], ["A", "B"])

    def test_a_vague_phrase_asks(self):
        """Stricter than the compare path on purpose: the cart is a step away
        from buying, so "add my saved ones" must not guess."""
        picks, err = orders._saved_refs("add my saved ones to the cart", self.SAVED)
        self.assertEqual(picks, [])
        self.assertTrue(err)

    def test_out_of_range_asks(self):
        picks, err = orders._saved_refs("add saved item 9", self.SAVED)
        self.assertEqual(picks, [])
        self.assertIn("9", err)


class DispatchTest(unittest.TestCase):
    def _dispatch(self, action):
        calls = {}
        with mock.patch.object(orders, "add_results_to_cart",
                               side_effect=lambda *a: calls.setdefault("r", "RESULTS")), \
             mock.patch.object(orders, "add_saved_to_cart",
                               side_effect=lambda *a: calls.setdefault("r", "SAVED")), \
             mock.patch.object(orders, "place_order",
                               side_effect=lambda *a: calls.setdefault("r", "PLACE")), \
             mock.patch.object(orders, "cancel_order",
                               side_effect=lambda *a: calls.setdefault("r", "CANCEL")), \
             mock.patch.object(orders, "summarize",
                               side_effect=lambda *a: calls.setdefault("r", "VIEW")):
            orders.manage_orders(1, action, "arg", None)
        return calls.get("r")

    def test_each_action_reaches_its_handler(self):
        for action, expected in (("add_results_to_cart", "RESULTS"),
                                 ("add_to_cart", "SAVED"),
                                 ("place", "PLACE"),
                                 ("cancel", "CANCEL"),
                                 ("view", "VIEW")):
            self.assertEqual(self._dispatch(action), expected, action)

    def test_an_unknown_action_shows_orders_rather_than_acting(self):
        """The fail-safe: a mis-route must not place or cancel anything."""
        for action in ("", "   ", "banana", None, "PLACE ORDER NOW"):
            self.assertEqual(self._dispatch(action), "VIEW", action)


class PlaceRulesTest(unittest.TestCase):
    def _place_with(self, rows):
        db = mock.MagicMock()
        db.execute.return_value.fetchall.return_value = [
            mock.Mock(_mapping=r) for r in rows]
        with mock.patch.object(orders, "SessionLocal", return_value=db):
            return orders.place(1)

    def test_an_out_of_stock_item_is_refused_not_dropped(self):
        """Ordering something unbuyable and saying nothing is worse than making
        the shopper take it out. This rule is shared with the HTTP endpoint --
        one copy, so it cannot stop refusing in one place only."""
        result, error = self._place_with([
            {"pid": "A", "quantity": 1, "title": "Nike", "price": 100,
             "availability": "InStock"},
            {"pid": "B", "quantity": 1, "title": "Puma", "price": 200,
             "availability": "OutOfStock"},
        ])
        self.assertIsNone(result)
        self.assertEqual(error["code"], 409)
        self.assertIn("Puma", error["message"])

    def test_an_empty_cart_is_a_refusal_not_an_empty_order(self):
        result, error = self._place_with([])
        self.assertIsNone(result)
        self.assertEqual(error["code"], 400)

    def test_the_tool_surfaces_the_refusal_as_prose(self):
        err = {"code": 409, "message": "These are no longer in stock: Puma."}
        with mock.patch.object(orders, "place", return_value=(None, err)):
            self.assertEqual(orders.place_order(1), err["message"])


class ToolContractTest(unittest.TestCase):
    def test_the_orders_tool_takes_no_user_id(self):
        from app.agent import manage_orders
        self.assertEqual(set(manage_orders.args), {"action", "query"})


if __name__ == "__main__":
    unittest.main(verbosity=2)
