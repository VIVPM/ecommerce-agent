"""The dead-end metric's own contract (backend/stock_audit.py).

A metric that cries wolf gets ignored, so the rules it must not break:

  * a dead end means stock stands between the shopper and products that EXIST.
    A brand that simply has no shoes under 1000 is not a dead end -- it is an
    honest "we don't carry that", and counting it would drown the real ones.
  * the count must follow the app's SUBSTRING brand match, not the raw column.
    Grouping by brand invented two kinds of false positive on the first run:
    "ADIDAS" split from "adidas" (uppercase half entirely unavailable), and
    "adidas originals" kept apart so its 2 buyable pairs never rescued an
    "adidas rated 4.5+" search that was actually answerable.

Offline: pure summarisation only. No database, no model, no network.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))
os.environ.setdefault("LLM_MODEL", "GEMINI")
os.environ.setdefault("DATABASE_URL", "postgresql://u:p@localhost/db")

import stock_audit  # noqa: E402


class SummariseTest(unittest.TestCase):
    # (brand, filter, matches, buyable)
    ROWS = [
        ("nike", "under 3000", 11, 0),     # dead end: 11 exist, none buyable
        ("puma", "under 3000", 151, 85),   # healthy
        ("crocs", "under 1000", 0, 0),     # we just don't carry those
        ("woodland", "under 2000", 2, 0),  # dead end
    ]

    def test_counts_only_filters_that_match_something(self):
        """0 matches is not a stock problem, and counting it would bury the
        dead ends that are."""
        dead, live, _ = stock_audit.summarise(self.ROWS)
        self.assertEqual(len(live), 3)
        self.assertNotIn("crocs", [r[0] for r in dead])

    def test_a_dead_end_is_matches_with_nothing_buyable(self):
        dead, _, _ = stock_audit.summarise(self.ROWS)
        self.assertEqual({r[0] for r in dead}, {"nike", "woodland"})

    def test_stranded_counts_products_not_combinations(self):
        """13 products are unreachable, across 2 combinations. The product count
        is what says how much this costs a shopper."""
        _, _, stranded = stock_audit.summarise(self.ROWS)
        self.assertEqual(stranded, 13)

    def test_a_fully_stocked_catalogue_reports_nothing(self):
        dead, live, stranded = stock_audit.summarise([("puma", "under 3000", 10, 10)])
        self.assertEqual(dead, [])
        self.assertEqual(len(live), 1)
        self.assertEqual(stranded, 0)

    def test_no_rows_does_not_divide_by_zero(self):
        self.assertEqual(stock_audit.summarise([]), ([], [], 0))


class QueryShapeTest(unittest.TestCase):
    """The SQL is generated, so pin the two decisions that made it honest."""

    def _sql(self, dimension):
        captured = {}

        class _Conn:
            def execute(self, clause, *a, **k):
                captured["sql"] = str(clause)
                return []

        stock_audit._rows_for(_Conn(), dimension)
        return captured["sql"]

    def test_brands_are_matched_by_substring_like_the_app(self):
        """LOWER(brand) LIKE '%name%' is what the generated product SQL does; a
        GROUP BY on the raw column measures a different question."""
        sql = self._sql("price")
        self.assertIn("LIKE", sql.upper())
        self.assertIn("LOWER(product.brand)", sql)

    def test_price_ceilings_are_cumulative(self):
        """"under 3000" means everything below 3000, so a 900-rupee shoe counts
        towards every ceiling above it -- buckets would answer a question nobody
        asked."""
        sql = self._sql("price")
        for ceiling in stock_audit.PRICE_CEILINGS:
            self.assertIn(f"price < {ceiling}", sql)

    def test_rating_is_a_floor_not_a_band(self):
        sql = self._sql("rating")
        for floor in stock_audit.RATING_FLOORS:
            self.assertIn(f"avg_rating >= {floor}", sql)

    def test_buyable_is_counted_against_the_same_filter(self):
        """The whole metric is matches vs buyable under the SAME condition."""
        sql = self._sql("price")
        self.assertIn("availability = 'InStock'", sql)


if __name__ == "__main__":
    unittest.main(verbosity=2)
