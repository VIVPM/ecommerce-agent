"""Offline contracts for the ranking evaluator.

The first NDCG run reported 0.995 while measuring nothing: its candidate pool
silently stayed equal to the shown list. The second result was overwritten by a
concurrent stale run. These tests cover the pure parts that must not drift again;
retrieval itself is verified by the live run (shown=10, pool=30).
"""
import csv
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend", "test"))
os.environ.setdefault("LLM_MODEL", "GEMINI")
os.environ.setdefault("DATABASE_URL", "postgresql://u:p@localhost/db")

import evaluate_ranking as ranking  # noqa: E402


class NdcgTest(unittest.TestCase):
    def test_perfect_order_is_one(self):
        self.assertEqual(ranking.ndcg(["E", "S", "I"], ["E", "S", "I"]), 1.0)

    def test_a_buried_exact_match_costs_score(self):
        shown = ["S", "S", "E"]
        pool = ["E", "S", "S"]
        self.assertLess(ranking.ndcg(shown, pool), 1.0)

    def test_uniform_labels_are_one_in_any_order(self):
        """The known limitation: filtering can make every candidate Exact, in
        which case NDCG has no ranking signal. The report must be interpreted by
        query shape rather than treating 1.0 as evidence of ordering quality."""
        self.assertEqual(ranking.ndcg(["E"] * 10, ["E"] * 30), 1.0)

    def test_no_relevant_candidate_has_no_score(self):
        self.assertIsNone(ranking.ndcg(["I"] * 10, ["I"] * 30))


class PoolSqlTest(unittest.TestCase):
    def test_widens_every_union_branch_proportionally(self):
        query = ("(SELECT * FROM product WHERE brand='Nike' LIMIT 4) UNION ALL "
                 "(SELECT * FROM product WHERE brand='Puma' LIMIT 5)")
        out = ranking._pool_sql(query)
        self.assertIn("LIMIT 13", out)
        self.assertIn("LIMIT 17", out)

    def test_widens_only_a_plain_trailing_limit(self):
        out = ranking._pool_sql("SELECT * FROM product WHERE price < 3000 LIMIT 10")
        self.assertTrue(out.endswith(f"LIMIT {ranking.POOL_SIZE}"), out)

    def test_leaves_internal_relative_subquery_limit_alone(self):
        query = ("SELECT * FROM product WHERE price < "
                 "(SELECT price FROM product WHERE title='X' LIMIT 1)")
        self.assertEqual(ranking._pool_sql(query), query)


class LabelCacheKeyTest(unittest.TestCase):
    def test_prompt_change_invalidates_labels(self):
        before = ranking._key("q", ["P1"])
        with mock.patch.object(ranking, "LABEL_PROMPT", ranking.LABEL_PROMPT + " stricter"):
            after = ranking._key("q", ["P1"])
        self.assertNotEqual(before, after)

    def test_reference_context_is_part_of_the_key(self):
        self.assertNotEqual(ranking._key("q", ["P1"], "anchor A"),
                            ranking._key("q", ["P1"], "anchor B"))

    def test_product_set_is_part_of_the_key(self):
        self.assertNotEqual(ranking._key("q", ["P1"]), ranking._key("q", ["P2"]))


class ReferenceContextTest(unittest.TestCase):
    def test_relative_query_receives_anchor_metadata(self):
        frame = pd.DataFrame([{"title": "CAMPUS MIKE Running Shoes For Men",
                               "brand": "CAMPUS", "price": 1214,
                               "avg_rating": 4.2, "total_ratings": 500}])
        with mock.patch.object(ranking.sql, "run_query", return_value=frame):
            context = ranking._reference_context(
                "shoes cheaper than the Campus Mike")
        self.assertIn("Running Shoes For Men", context)
        self.assertIn("1214", context)
        self.assertIn("500", context)

    def test_ordinary_query_pays_no_anchor_lookup(self):
        with mock.patch.object(ranking.sql, "run_query") as queried:
            self.assertEqual(ranking._reference_context("puma under 3000"), "")
        queried.assert_not_called()


class BlindSampleTest(unittest.TestCase):
    def test_model_labels_are_kept_out_of_the_csv(self):
        products = [{"pid": f"P{i}", "title": f"Shoe {i}", "brand": "A",
                     "price": i, "rating": 4.0, "reviews": 10}
                    for i in range(12)]
        labels = ["E"] * 4 + ["S"] * 4 + ["I"] * 4
        results = [{"query": "show me nike shoes", "pool": products,
                    "pool_labels": labels}]
        with tempfile.TemporaryDirectory() as td:
            results_file = os.path.join(td, "results.json")
            sample_file = os.path.join(td, "sample.csv")
            key_file = os.path.join(td, "key.json")
            with open(results_file, "w", encoding="utf-8") as f:
                json.dump(results, f)
            with mock.patch.object(ranking, "RESULTS_FILE", results_file), \
                 mock.patch.object(ranking, "SAMPLE_FILE", sample_file), \
                 mock.patch.object(ranking, "SAMPLE_KEY_FILE", key_file):
                ranking.sample(9)
            with open(sample_file, encoding="utf-8") as f:
                rows = list(csv.DictReader(f))
            self.assertNotIn("model_label", rows[0])
            self.assertTrue(all(not r["your_label_E_S_C_I"] for r in rows))
            with open(key_file, encoding="utf-8") as f:
                key = json.load(f)
            self.assertEqual(set(key.values()), {"E", "S", "I"})


if __name__ == "__main__":
    unittest.main(verbosity=2)
