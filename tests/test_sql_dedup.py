"""Dedup / result-count contract for app.sql.

Three behaviours that were wrong and are easy to break again:
  1. no hidden `total_ratings >= N` floor in the SQL prompt — a floor DELETES
     threshold matches instead of ranking them
  2. dedup applies to EVERY result set, not only the >5 ones that reach the
     numbered formatter
  3. dedup runs before the counts, and a `LIMIT n` question still gets n
     distinct products

Runs offline: no database, no model, no network.
"""
import os
import sys
import unittest

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

    def test_display_caps_are_separate_numbers(self):
        """A compound query is bounded by its own branch limits, so it must not
        be held to the broad-search default of 10."""
        self.assertEqual(sql.DEFAULT_DISPLAY_ROWS, 10)
        self.assertGreater(sql.UNION_MAX_ROWS, sql.DEFAULT_DISPLAY_ROWS)


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


if __name__ == "__main__":
    unittest.main(verbosity=2)
