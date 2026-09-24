"""Dedup / result-count contract for app.sql."""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))
os.environ.setdefault("LLM_MODEL", "GEMINI")
os.environ.setdefault("DATABASE_URL", "postgresql://u:p@localhost/db")

import pandas as pd  # noqa: E402

from app import sql  # noqa: E402


def _frame(rows):
    return pd.DataFrame(rows)


class RatingFloorTest(unittest.TestCase):
    def test_prompt_has_no_hidden_rating_count_floor(self):
        """A `total_ratings >= N` floor makes "rated above 4.5" answer "nothing
        found" while real 4.8-from-30 products sit in the catalogue."""
        self.assertNotIn("total_ratings >= 50", sql.sql_prompt)

    def test_prompt_still_ranks_by_the_bayesian_score(self):
        """Removing the floor must not remove the confidence weighting — that is
        what stops a 5.0-from-3 outranking a 4.6-from-500."""
        self.assertIn("total_ratings + 50", sql.sql_prompt)


class DedupFrameTest(unittest.TestCase):
    def test_collapses_seller_variants_of_one_shoe(self):
        out = sql._dedup_frame(_frame([
            {"title": "NIKE W REVOLUTION 7", "brand": "NIKE", "price": 3200},
            {"title": "Revolution 7", "brand": "NIKE", "price": 2999},
            {"title": "Puma Smash v2", "brand": "Puma", "price": 2500},
        ]))
        self.assertEqual(len(out), 2)

    def test_keeps_the_cheapest_variant(self):
        out = sql._dedup_frame(_frame([
            {"title": "NIKE W REVOLUTION 7", "brand": "NIKE", "price": 3200},
            {"title": "Revolution 7", "brand": "NIKE", "price": 2999},
        ]))
        self.assertEqual(out.iloc[0]["price"], 2999)

    def test_preserves_the_ordering_the_sql_asked_for(self):
        """Dedup sorts by price internally to pick the cheapest; it must not
        leak that sort into the result, or "top rated" comes back price-sorted."""
        out = sql._dedup_frame(_frame([
            {"title": "Expensive", "brand": "A", "price": 9000, "avg_rating": 4.9},
            {"title": "Cheap", "brand": "B", "price": 900, "avg_rating": 4.1},
        ]))
        self.assertEqual(list(out["title"]), ["Expensive", "Cheap"])

    def test_does_not_leak_the_helper_column(self):
        """_dedup_key in the frame would reach the LLM inside the context dict."""
        out = sql._dedup_frame(_frame([{"title": "X", "brand": "A", "price": 1}]))
        self.assertNotIn("_dedup_key", out.columns)

    def test_different_brands_sharing_a_generic_title_stay_separate(self):
        """The catalogue is full of brand-less titles -- "Walking Shoes For
        Women" spans Skechers, PUMA, CAMPUS, HRX and more. A title-only key
        normalised them all to one string and hid five real products."""
        out = sql._dedup_frame(_frame([
            {"title": "Walking Shoes For Women", "brand": "Skechers", "price": 2202},
            {"title": "Walking Shoes For Women", "brand": "PUMA", "price": 3850},
            {"title": "Walking Shoes For Women", "brand": "CAMPUS", "price": 768},
        ]))
        self.assertEqual(len(out), 3)

    def test_same_brand_generic_title_still_merges(self):
        """Two listings of one product by the same brand are still duplicates."""
        out = sql._dedup_frame(_frame([
            {"title": "Walking Shoes For Women", "brand": "Fabbmate", "price": 372},
            {"title": "Walking Shoes For Women", "brand": "Fabbmate", "price": 366},
        ]))
        self.assertEqual(len(out), 1)
        self.assertEqual(out.iloc[0]["price"], 366)

    def test_brand_and_title_cannot_run_together(self):
        """Without a separator, brand "nike" + title "air90" would key the same
        as brand "nike air" + title "90"."""
        self.assertNotEqual(sql._dedup_key("Air 90", "Nike"),
                            sql._dedup_key("90", "Nike Air"))

    def test_survives_a_frame_with_no_title(self):
        out = sql._dedup_frame(_frame([{"count": 7}]))
        self.assertEqual(len(out), 1)


class OverfetchLimitTest(unittest.TestCase):
    def test_widens_a_trailing_limit_and_reports_what_was_asked(self):
        widened, requested = sql._overfetch_limit(
            "SELECT * FROM product ORDER BY price LIMIT 10")
        self.assertEqual(requested, 10)
        self.assertTrue(widened.rstrip().endswith("LIMIT 20"), widened)

    def test_never_exceeds_the_row_cap(self):
        widened, requested = sql._overfetch_limit("SELECT * FROM product LIMIT 400")
        self.assertEqual(requested, 400)
        self.assertTrue(widened.rstrip().endswith(f"LIMIT {sql.MAX_SQL_ROWS}"), widened)

    def test_leaves_offset_paging_alone(self):
        """Re-limiting a paged query would skip rows the user has not seen."""
        original = "SELECT * FROM product LIMIT 10 OFFSET 20"
        widened, requested = sql._overfetch_limit(original)
        self.assertEqual(widened, original)
        self.assertIsNone(requested)

    def test_no_limit_means_nothing_to_trim(self):
        original = "SELECT * FROM product WHERE brand = 'Nike'"
        widened, requested = sql._overfetch_limit(original)
        self.assertEqual(widened, original)
        self.assertIsNone(requested)

    def test_tolerates_a_trailing_semicolon(self):
        widened, requested = sql._overfetch_limit("SELECT * FROM product LIMIT 5;")
        self.assertEqual(requested, 5)
        self.assertIn("LIMIT 10", widened)


class CompoundQueryTest(unittest.TestCase):
    """"4 Nike and 5 Puma" becomes one parenthesised UNION per group. Three
    separate places rejected or mangled a query starting with "(" ."""

    UNION = ("(SELECT * FROM product WHERE LOWER(brand) LIKE LOWER('%nike%') LIMIT 4)"
             " UNION ALL "
             "(SELECT * FROM product WHERE LOWER(brand) LIKE LOWER('%puma%') LIMIT 5)")

    def test_extract_keeps_a_leading_paren(self):
        """Checking for SELECT alone rejected the tagged query, so a greedy
        fallback matched instead and dropped the "(" — producing SQL that is a
        Postgres syntax error."""
        out = sql._extract_sql(f"<SQL>{self.UNION}</SQL>")
        self.assertTrue(out.startswith("(SELECT"), out[:40])

    def test_extract_does_not_leave_the_closing_tag_in(self):
        out = sql._extract_sql(f"<SQL>{self.UNION}</SQL>")
        self.assertNotIn("</SQL>", out)

    def test_overfetch_leaves_a_union_alone(self):
        """Widening a branch would skew the group counts. The pattern anchors at
        the end of the string, so the branch it would hit is the LAST one —
        "4 Nike and 5 Puma" would come back as 4 Nike and 10 Puma."""
        widened, requested = sql._overfetch_limit(self.UNION)
        self.assertEqual(widened, self.UNION)
        self.assertIsNone(requested)

    def test_prompt_teaches_the_parenthesised_form(self):
        """Nothing but the prompt stops the model writing a bare LIMIT before
        UNION, which Postgres rejects outright."""
        self.assertIn("UNION ALL", sql.sql_prompt)
        self.assertIn("(SELECT", sql.sql_prompt)

    def test_a_compound_query_is_bounded_by_the_sum_of_its_branches(self):
        """The cap for a compound query IS what was asked for — the sum of the
        per-branch LIMITs — not the broad-search default and not a fixed
        ceiling. A ceiling was either too low (dropping rows the shopper named)
        or an arbitrary number nobody could justify."""
        self.assertEqual(sql.DEFAULT_DISPLAY_ROWS, 10)
        import re as _re
        limits = [int(n) for n in _re.findall(r"\blimit\s+(\d+)", self.UNION, _re.I)]
        self.assertEqual(sum(limits), 9)
        self.assertGreater(sum(limits), 0)


class FormatterRendersWhatItIsGivenTest(unittest.TestCase):
    def test_no_hidden_ten_row_cap(self):
        """The formatter used to head(10) on top of whatever it was handed, which
        truncated both a compound query and a plain "show me 15 Nike shoes"."""
        rows = _frame([{"title": f"Shoe {i}", "brand": f"B{i}", "price": 100 + i}
                       for i in range(15)])
        out = sql._format_top_results(rows, "show me 15 shoes")
        self.assertEqual(out.count("[View Product]"), 15)

    def test_says_how_many_were_left_out(self):
        rows = _frame([{"title": f"Shoe {i}", "brand": f"B{i}", "price": 100 + i}
                       for i in range(10)])
        rows.attrs["total_matches"] = 380
        self.assertIn("Showing 10 of 380", sql._format_top_results(rows, "nike shoes"))

    def test_stays_quiet_when_nothing_was_left_out(self):
        rows = _frame([{"title": "Shoe", "brand": "B", "price": 100}])
        self.assertNotIn("Showing", sql._format_top_results(rows, "one shoe"))


class RequestedCountTest(unittest.TestCase):
    def test_a_limit_question_still_gets_that_many_distinct_products(self):
        """The regression: LIMIT 3 over duplicate listings used to answer with 2."""
        rows = _frame([
            {"title": "NIKE W REVOLUTION 7", "brand": "NIKE", "price": 3200},
            {"title": "Revolution 7", "brand": "NIKE", "price": 2999},
            {"title": "Puma Smash v2", "brand": "Puma", "price": 2500},
            {"title": "Adidas Lite Racer", "brand": "Adidas", "price": 2800},
        ])
        deduped = sql._dedup_frame(rows).head(3)
        self.assertEqual(len(deduped), 3)


class OutOfStockTest(unittest.TestCase):
    """"None available" is not "none exist"."""

    IN_STOCK = ("SELECT * FROM product WHERE availability = 'InStock' "
                "AND LOWER(brand) LIKE LOWER('%nike%') AND price < 3000")

    def test_the_stock_filter_is_recognised(self):
        self.assertIsNotNone(sql._STOCK_FILTER.search(self.IN_STOCK))

    def test_every_branch_of_a_compound_query_is_relaxed(self):
        """"4 Nike and 5 Puma" carries one filter per UNION branch; leaving one
        in would undercount what is actually sitting there out of stock."""
        union = ("(SELECT * FROM product WHERE availability = 'InStock' AND brand='A')"
                 " UNION ALL "
                 "(SELECT * FROM product WHERE availability = 'InStock' AND brand='B')")
        self.assertNotIn("InStock", sql._STOCK_FILTER.sub(" ", union))

    def test_a_query_without_a_stock_filter_has_nothing_to_say(self):
        """"is the Campus X in stock?" omits the filter on purpose -- there is no
        second query to run, so no stock claim should be made."""
        with mock.patch.object(sql, "run_query") as ran:
            self.assertIsNone(sql._count_ignoring_stock(
                "SELECT * FROM product WHERE LOWER(brand) LIKE LOWER('%nike%')"))
        ran.assert_not_called()

    def test_the_count_is_distinct_products_not_listings(self):
        """Consistent with every other count: seller duplicates collapse."""
        frame = _frame([
            {"title": "NIKE W REVOLUTION 7", "brand": "NIKE", "price": 3200},
            {"title": "Revolution 7", "brand": "NIKE", "price": 2999},
        ])
        with mock.patch.object(sql, "run_query", return_value=frame):
            self.assertEqual(sql._count_ignoring_stock(self.IN_STOCK), 1)

    def test_nothing_anywhere_stays_none(self):
        """Genuinely absent must keep saying absent, not claim a stock problem."""
        with mock.patch.object(sql, "run_query", return_value=pd.DataFrame()):
            self.assertIsNone(sql._count_ignoring_stock(self.IN_STOCK))
        with mock.patch.object(sql, "run_query", return_value=None):
            self.assertIsNone(sql._count_ignoring_stock(self.IN_STOCK))


class ShortfallNoteTest(unittest.TestCase):
    """Asked for 5, shown 4 -- say why, instead of leaving it to be noticed."""

    def _frame_with(self, rows, **attrs):
        f = _frame(rows)
        f.attrs.update(attrs)
        return f

    def test_names_what_was_asked_and_what_is_missing(self):
        note = sql._stock_shortfall_note(self._frame_with(
            [{"title": "A"}, {"title": "B"}],
            out_of_stock_shortfall=3, asked_for=5))
        self.assertIn("2 of the 5", note)
        self.assertIn("3 more", note)
        self.assertIn("out of stock", note)

    def test_the_verb_agrees_with_the_count(self):
        """"match" is the verb, so it agrees the opposite way to the noun."""
        one = sql._stock_shortfall_note(self._frame_with(
            [{"title": "A"}], out_of_stock_shortfall=1, asked_for=2))
        self.assertIn("1 more matches", one)
        self.assertIn("is out of stock", one)
        many = sql._stock_shortfall_note(self._frame_with(
            [{"title": "A"}], out_of_stock_shortfall=2, asked_for=3))
        self.assertIn("2 more match ", many)
        self.assertIn("are out of stock", many)

    def test_silent_when_the_count_was_met(self):
        self.assertEqual(sql._stock_shortfall_note(self._frame_with([{"title": "A"}])), "")

    def test_silent_when_no_count_was_named(self):
        """No count named means no shortfall to explain -- and no second query."""
        self.assertEqual(sql._stock_shortfall_note(self._frame_with(
            [{"title": "A"}], out_of_stock_shortfall=3)), "")


class NearestBuyableTest(unittest.TestCase):
    """A dead end should say where "yes" starts, not just "no"."""

    IN_STOCK = ("SELECT * FROM product WHERE availability = 'InStock' "
                "AND LOWER(brand) LIKE LOWER('%nike%') AND price < 3000 "
                "ORDER BY price LIMIT 10")

    def test_the_price_ceiling_is_dropped_but_stock_is_kept(self):
        """The question becomes "cheapest Nike I can actually sell", so the
        budget goes and every other condition stays."""
        relaxed = sql._PRICE_FILTER.sub(" ", self.IN_STOCK)
        self.assertNotIn("price < 3000", relaxed)
        self.assertIn("InStock", relaxed)
        self.assertIn("nike", relaxed.lower())

    def test_the_trailing_limit_goes_too(self):
        """The generated LIMIT orders by RANK, so the cheapest row need not be
        inside it -- taking min() over a ranked top-10 answers the wrong thing."""
        with mock.patch.object(sql, "run_query",
                               return_value=_frame([{"price": 3916}])) as ran:
            sql._cheapest_buyable(self.IN_STOCK)
        self.assertNotIn("LIMIT", ran.call_args[0][0].upper())

    def test_returns_the_cheapest_price(self):
        frame = _frame([{"price": 5200}, {"price": 3916}, {"price": 4100}])
        with mock.patch.object(sql, "run_query", return_value=frame):
            self.assertEqual(sql._cheapest_buyable(self.IN_STOCK), 3916)

    def test_no_price_filter_means_no_claim(self):
        """"show me nike shoes" named no budget, so there is no ceiling to relax
        and nothing to offer."""
        with mock.patch.object(sql, "run_query") as ran:
            self.assertIsNone(sql._cheapest_buyable(
                "SELECT * FROM product WHERE availability = 'InStock'"))
        ran.assert_not_called()

    def test_nothing_buyable_at_any_price_stays_none(self):
        """A brand with no stock at all must not produce an empty promise."""
        with mock.patch.object(sql, "run_query", return_value=pd.DataFrame()):
            self.assertIsNone(sql._cheapest_buyable(self.IN_STOCK))


class TypeNounTest(unittest.TestCase):
    """The product TYPE the shopper named must survive into the WHERE clause."""

    def test_the_prompt_forbids_dropping_the_type(self):
        self.assertIn("NEVER DROP THE PRODUCT TYPE", sql.sql_prompt)

    def test_the_word_list_is_examples_not_a_whitelist(self):
        """A longer list only moves the boundary: loafer, derby, oxford, wedge
        and heel are all searchable and none was named."""
        self.assertIn("EXAMPLES, not the whole set", sql.sql_prompt)

    def test_the_type_nouns_that_exist_are_named(self):
        for noun in ("boot", "loafer", "sandal", "heel"):
            self.assertIn(noun, sql.sql_prompt)


if __name__ == "__main__":
    unittest.main(verbosity=2)
