"""Resolving "save 2" / "remove the first two" against what is on screen."""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))
os.environ.setdefault("LLM_MODEL", "GEMINI")
os.environ.setdefault("DATABASE_URL", "postgresql://u:p@localhost/db")

from app import compare  # noqa: E402

RESULTS = """Here are the top matches:

1. Nike Revolution 7 Running Shoes: Rs. 3,295 [View Product](https://www.flipkart.com/nike-revolution-7/p/itm123?pid=SHOAAA111)
2. Puma Smash v2 Sneakers: Rs. 2,499 [View Product](https://www.flipkart.com/puma-smash-v2/p/itm456?pid=SHOBBB222)
3. Campus Mike Walking Shoes: Rs. 1,199 [View Product](https://www.flipkart.com/campus-mike/p/itm789?pid=SHOCCC333)
"""

HISTORY = [
    {"role": "user", "content": "show me shoes under 3500"},
    {"role": "assistant", "content": RESULTS},
]


class LastShownTest(unittest.TestCase):
    def test_reads_the_products_in_display_order(self):
        shown = compare.last_shown_products(HISTORY)
        self.assertEqual([p[0] for p in shown], ["SHOAAA111", "SHOBBB222", "SHOCCC333"])

    def test_position_two_is_the_second_product(self):
        """The whole contract: the shopper's "2" is this list's index 2."""
        self.assertIn("Puma", compare.last_shown_products(HISTORY)[1][1])

    def test_the_title_comes_from_the_line_not_the_link_text(self):
        """Every link says "View Product"; using it as the name would make all
        three products identical and name-matching useless."""
        for _, title, _ in compare.last_shown_products(HISTORY):
            self.assertNotIn("View Product", title)

    def test_the_brand_from_the_url_slug_is_searchable(self):
        """Catalogue titles often omit the brand, so "save the nike one" has to
        match on something -- the slug carries it."""
        shown = compare.last_shown_products(HISTORY)
        self.assertTrue(any("nike" in s[2] for s in shown))

    def test_uses_the_most_recent_list_not_the_first(self):
        newer = HISTORY + [
            {"role": "user", "content": "any cheaper?"},
            {"role": "assistant", "content":
                "1. Sparx Mens Shoes: Rs. 899 "
                "[View Product](https://www.flipkart.com/sparx-mens/p/itm9?pid=SHODDD444)"},
        ]
        self.assertEqual([p[0] for p in compare.last_shown_products(newer)], ["SHODDD444"])

    def test_no_list_is_empty_not_an_error(self):
        self.assertEqual(compare.last_shown_products(
            [{"role": "assistant", "content": "Our return window is 7 days."}]), [])
        self.assertEqual(compare.last_shown_products([]), [])
        self.assertEqual(compare.last_shown_products(None), [])


class ResolveRefsTest(unittest.TestCase):
    def setUp(self):
        self.shown = compare.last_shown_products(HISTORY)

    def test_save_2(self):
        picks, err = compare.resolve_refs("save 2", self.shown)
        self.assertIsNone(err)
        self.assertEqual([p[0] for p in picks], ["SHOBBB222"])

    def test_the_first_and_third(self):
        picks, err = compare.resolve_refs("save the first and third", self.shown)
        self.assertIsNone(err)
        self.assertEqual([p[0] for p in picks], ["SHOAAA111", "SHOCCC333"])

    def test_by_brand_name(self):
        picks, err = compare.resolve_refs("save the nike one", self.shown)
        self.assertIsNone(err)
        self.assertEqual([p[0] for p in picks], ["SHOAAA111"])

    def test_all(self):
        picks, err = compare.resolve_refs("save all of them", self.shown)
        self.assertIsNone(err)
        self.assertEqual(len(picks), 3)

    def test_a_price_in_the_sentence_is_not_a_position(self):
        """"add items 2 and 3 under 3000" once answered "there's no #3000"."""
        picks, err = compare.resolve_refs("save items 2 and 3 under 3000", self.shown)
        self.assertIsNone(err, err)
        self.assertEqual([p[0] for p in picks], ["SHOBBB222", "SHOCCC333"])

    def test_out_of_range_asks_instead_of_guessing(self):
        picks, err = compare.resolve_refs("save 9", self.shown)
        self.assertEqual(picks, [])
        self.assertIn("9", err)

    def test_ambiguous_name_asks_instead_of_guessing(self):
        shown = [("P1", "Puma Smash v2", "puma smash v2"),
                 ("P2", "Puma Smash v3", "puma smash v3")]
        picks, err = compare.resolve_refs("save the puma smash", shown)
        self.assertEqual(picks, [])
        self.assertTrue(err)

    def test_a_bare_save_asks_which_one(self):
        picks, err = compare.resolve_refs("save it", self.shown)
        self.assertEqual(picks, [])
        self.assertTrue(err)


class ResolveSavedRefsTest(unittest.TestCase):
    SAVED = [{"pid": "SHOAAA111", "title": "Nike Revolution 7"},
             {"pid": "SHOBBB222", "title": "Puma Smash v2"}]

    def test_remove_by_number(self):
        picks, err = compare.resolve_saved_refs("remove saved item 2", self.SAVED)
        self.assertIsNone(err)
        self.assertEqual([p["pid"] for p in picks], ["SHOBBB222"])

    def test_the_rewrite_bug_phrase_clears_the_list(self):
        """This exact sentence was rewritten into a product search, and nothing
        was ever removed."""
        picks, err = compare.resolve_saved_refs(
            "remove saved items that are currently present", self.SAVED)
        self.assertIsNone(err)
        self.assertEqual(len(picks), 2)

    def test_remove_by_name(self):
        picks, err = compare.resolve_saved_refs("remove the puma from my saved", self.SAVED)
        self.assertIsNone(err)
        self.assertEqual([p["pid"] for p in picks], ["SHOBBB222"])

    def test_out_of_range_asks(self):
        picks, err = compare.resolve_saved_refs("remove saved item 7", self.SAVED)
        self.assertEqual(picks, [])
        self.assertIn("7", err)


class ToolContractTest(unittest.TestCase):
    def test_the_saved_tool_takes_no_user_id(self):
        """A user id the model can write is a user id it can be talked into
        changing -- it rides in the runtime context instead."""
        from app.agent import manage_saved
        self.assertEqual(set(manage_saved.args), {"action", "query"})

    def test_each_action_reaches_the_matching_handler(self):
        """Dispatch, with the handlers stubbed -- no database, no model."""
        for action, expected in (("add", "SAVE"), ("remove", "REMOVE"), ("compare", "COMPARE")):
            self.assertEqual(_dispatch(action), expected)

    def test_an_unknown_action_falls_back_to_the_read_only_one(self):
        """Guessing "compare" costs a wasted turn; guessing "remove" deletes a
        shortlist the shopper never asked to touch."""
        for action in ("", "   ", "banana", None):
            self.assertEqual(_dispatch(action), "COMPARE", action)

    def test_positions_resolve_against_the_typed_message(self):
        """The rewritten query says something else entirely; "save 2" must still
        mean 2. This is the invariant that lived in a prompt and broke."""
        seen = _dispatch("add", query="show me Puma shoes similar to the second one",
                         raw_query="save 2")
        self.assertEqual(seen, "SAVE")
        self.assertEqual(_LAST["query"], "save 2")


_LAST = {}


def _dispatch(action, query="compare my saved", raw_query="", history=None):
    """Call the tool with the three handlers stubbed; return which one ran."""
    import asyncio
    from unittest import mock

    from app import agent as agent_mod

    def _stub(label):
        async def gen(q, *a, **k):
            _LAST["query"] = q
            yield label
        return gen

    class _RT:
        context = agent_mod.Ctx(user_id=1, raw_query=raw_query or query, history=history)

    with mock.patch.object(agent_mod, "save_from_results_stream_async", _stub("SAVE")),          mock.patch.object(agent_mod, "remove_saved_items_stream_async", _stub("REMOVE")),          mock.patch.object(agent_mod, "compare_saved_stream_async", _stub("COMPARE")):
        return asyncio.run(agent_mod.manage_saved.coroutine(
            action=action, query=query, runtime=_RT()))


class DirectActionTest(unittest.TestCase):
    """The rewrite must not touch an action aimed at something already on screen."""

    def test_actions_on_a_position_are_left_alone(self):
        from app.memory import is_direct_action
        for q in ("save 2", "save the first and third", "remove saved item 2",
                  "remove saved items that are currently present",
                  "clear my saved items", "clear my cart",
                  "add items 2 and 3 to my cart", "cancel order 12",
                  "add that to my list", "save it"):
            self.assertTrue(is_direct_action(q), q)

    def test_ordinary_questions_still_get_rewritten(self):
        """Over-triggering is the dangerous direction: "any cheaper ones?" NEEDS
        the history folded in, and skipping the rewrite would break it."""
        from app.memory import is_direct_action
        for q in ("show me nike shoes under 3000", "are there any cheaper ones?",
                  "which of these is waterproof?", "what is your return policy",
                  "compare my saved shoes", "what did I save",
                  "add a bit more detail about the return window"):
            self.assertFalse(is_direct_action(q), q)


if __name__ == "__main__":
    unittest.main(verbosity=2)
