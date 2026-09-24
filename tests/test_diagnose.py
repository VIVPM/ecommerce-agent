"""Why-is-this-empty diagnosis (app.diagnose)."""
import os
import sys
import unittest
from unittest import mock

import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))
os.environ.setdefault("LLM_MODEL", "GEMINI")
os.environ.setdefault("DATABASE_URL", "postgresql://u:p@localhost/db")

from app import diagnose  # noqa: E402

FULL = ("SELECT * FROM product WHERE availability = 'InStock' "
        "AND LOWER(brand) LIKE LOWER('%nike%') "
        "AND LOWER(title) LIKE '%running%' "
        "AND LOWER(title) NOT LIKE '%women%' "
        "AND price < 3000 AND avg_rating > 4 ORDER BY price LIMIT 10")

ANCHORED = ("SELECT * FROM product WHERE availability = 'InStock' "
            "AND avg_rating > (SELECT MAX(avg_rating) FROM product "
            "WHERE LOWER(title) LIKE LOWER('%Sparx SM 852%')) "
            "AND LOWER(title) LIKE '%sneaker%'")


def _rows(n):
    return pd.DataFrame([{"pid": f"P{i}", "title": f"t{i}", "brand": "b",
                          "price": 100 + i} for i in range(n)])


class SplitTest(unittest.TestCase):
    def test_splits_top_level_conditions(self):
        self.assertEqual(len(diagnose.split_conditions(FULL)), 6)

    def test_a_subquery_is_never_torn_in_half(self):
        """The anchor's own WHERE contains AND; splitting naively produces SQL
        that cannot run, and the diagnosis would then be a stream of errors."""
        parts = diagnose.split_conditions(ANCHORED)
        self.assertEqual(len(parts), 3)
        self.assertTrue(any("SELECT MAX" in p and p.count("(") == p.count(")")
                            for p in parts))

    def test_order_by_is_not_treated_as_a_condition(self):
        self.assertTrue(all("ORDER BY" not in p for p in diagnose.split_conditions(FULL)))

    def test_no_where_clause_is_empty_not_an_error(self):
        self.assertEqual(diagnose.split_conditions("SELECT * FROM product"), [])


class RebuildTest(unittest.TestCase):
    def test_dropping_one_condition_keeps_the_rest(self):
        parts = diagnose.split_conditions(FULL)
        out = diagnose._rebuild(FULL, parts[1:])
        self.assertNotIn("InStock", out)
        self.assertIn("nike", out)
        self.assertIn("ORDER BY", out)

    def test_dropping_everything_stays_valid_sql(self):
        """An empty WHERE is a syntax error, so it becomes WHERE TRUE."""
        self.assertIn("WHERE TRUE", diagnose._rebuild(FULL, []))


class DescribeTest(unittest.TestCase):
    def test_conditions_read_as_a_shopper_would_say_them(self):
        said = [diagnose.describe(c) for c in diagnose.split_conditions(FULL)]
        self.assertIn("in stock", said)
        self.assertIn("brand nike", said)
        self.assertIn("price under Rs. 3000", said)
        self.assertIn("men's (excludes women's)", said)

    def test_an_anchor_is_read_whole_not_as_a_title_match(self):
        """Labelled by its inner title LIKE, "better rated than the Sparx SM 852"
        became 'title contains "Sparx SM 852"' -- and the model, handed that,
        told the shopper they wanted shoes better rated than themselves."""
        anchor = [c for c in diagnose.split_conditions(ANCHORED) if "SELECT" in c][0]
        self.assertEqual(diagnose.describe(anchor), 'rated better than "Sparx SM 852"')

    def test_a_cheaper_than_anchor_reads_as_cheaper(self):
        cond = ("price < (SELECT MIN(price) FROM product "
                "WHERE LOWER(title) LIKE LOWER('%campus mike%'))")
        self.assertEqual(diagnose.describe(cond), 'cheaper than "campus mike"')


class UnresolvedAnchorTest(unittest.TestCase):
    def test_a_missing_anchor_is_named(self):
        """MAX() over no rows is NULL, and `rating > NULL` is false for every
        product -- so the search looks like "nothing is better rated" when the
        truth is that the reference product was never found."""
        with mock.patch.object(diagnose, "_ANCHOR", diagnose._ANCHOR):
            out = diagnose.unresolved_anchor(ANCHORED, lambda q: pd.DataFrame())
        self.assertEqual(out, "Sparx SM 852")

    def test_a_findable_anchor_is_not_flagged(self):
        self.assertIsNone(diagnose.unresolved_anchor(ANCHORED, lambda q: _rows(1)))

    def test_a_query_without_an_anchor_is_not_flagged(self):
        self.assertIsNone(diagnose.unresolved_anchor(FULL, lambda q: pd.DataFrame()))


class ProbeTest(unittest.TestCase):
    def test_reports_what_each_condition_costs(self):
        counts = iter([_rows(115), _rows(0), _rows(0), _rows(0), _rows(0), _rows(0)])
        out = diagnose.probe(FULL, lambda q: next(counts), lambda df: df)
        self.assertEqual(out[0][1], 115)
        self.assertEqual(len(out), 6)

    def test_declines_a_single_condition(self):
        """One condition cannot be 'the blocker among several'."""
        with mock.patch.object(diagnose, "split_conditions", return_value=["x"]):
            self.assertEqual(diagnose.probe(FULL, lambda q: _rows(1), lambda d: d), [])

    def test_declines_an_unreasonable_number_of_conditions(self):
        """A dead end should not turn into a scan."""
        many = ["c"] * (diagnose.MAX_CONDITIONS + 1)
        with mock.patch.object(diagnose, "split_conditions", return_value=many):
            self.assertEqual(diagnose.probe(FULL, lambda q: _rows(1), lambda d: d), [])


class ExplainTest(unittest.TestCase):
    def test_the_model_is_given_facts_not_sql(self):
        captured = {}

        def fake_complete(prompt, **kw):
            captured["prompt"] = prompt
            return "Try dropping the brand."

        counts = iter([_rows(115)] + [_rows(0)] * 5)
        with mock.patch.object(diagnose, "complete", fake_complete):
            diagnose.explain("nike running for men under 3000", FULL,
                             lambda q: next(counts), lambda df: df)
        self.assertIn("brand nike", captured["prompt"])
        self.assertNotIn("SELECT", captured["prompt"])
        self.assertIn("115", captured["prompt"])

    def test_a_failure_here_never_breaks_the_answer(self):
        """The caller falls back to the ordinary message; an explanation is a
        nicety and must not cost the shopper their reply."""
        with mock.patch.object(diagnose, "complete", side_effect=RuntimeError("down")):
            self.assertEqual(
                diagnose.explain("q", FULL, lambda q: _rows(1), lambda df: df), "")

    def test_an_unfindable_anchor_short_circuits_the_probe(self):
        """No amount of relaxing other conditions explains a missing anchor."""
        with mock.patch.object(diagnose, "complete") as model:
            out = diagnose.explain("better rated than the Sparx SM 852", ANCHORED,
                                   lambda q: pd.DataFrame(), lambda df: df)
        model.assert_not_called()
        self.assertIn("Sparx SM 852", out)


class CompoundTest(unittest.TestCase):
    """"4 Nike and 5 Puma" is two searches glued with UNION. Probed whole, only
    the FIRST branch was ever read, so a count for Nike could be told to the
    shopper as if it covered Puma too."""

    UNION = ("(SELECT * FROM product WHERE availability = 'InStock' "
             "AND LOWER(brand) LIKE '%nike%' AND price < 500 LIMIT 4) UNION ALL "
             "(SELECT * FROM product WHERE availability = 'InStock' "
             "AND LOWER(brand) LIKE '%puma%' AND price < 500 LIMIT 5)")

    def test_each_group_is_diagnosed_and_labelled(self):
        captured = {}

        def fake_complete(prompt, **kw):
            captured["prompt"] = prompt
            return "ok"

        with mock.patch.object(diagnose, "complete", fake_complete):
            diagnose.explain("4 nike and 5 puma under 500", self.UNION,
                             lambda q: _rows(3), lambda df: df)
        self.assertIn("GROUP 1", captured["prompt"])
        self.assertIn("GROUP 2", captured["prompt"])
        self.assertIn("brand nike", captured["prompt"])
        self.assertIn("brand puma", captured["prompt"])

    def test_a_plain_query_gets_no_group_label(self):
        captured = {}

        def fake_complete(prompt, **kw):
            captured["prompt"] = prompt
            return "ok"

        with mock.patch.object(diagnose, "complete", fake_complete):
            diagnose.explain("q", FULL, lambda q: _rows(3), lambda df: df)
        self.assertNotIn("GROUP 1:", captured["prompt"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
