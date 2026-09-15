"""Contract for app.decompose -- the multi-intent query splitter.

Every tool is return_direct=True, so one agent run answers ONE intent. decompose
splits a two-intent message BEFORE the agent so each part gets its own run.

What is pinned here is the SAFETY behaviour, all of which is testable offline:
the gate that decides whether to spend a model call at all, and the fail-open
paths. Split QUALITY depends on the model and is not asserted here -- that was
measured against a 25-case suite when the prompt was written, and belongs in the
eval, not in a unit test that would then need network and money to run.

The bias throughout: returning ONE part is always the safe answer. Under-
splitting is the behaviour that existed before this module; over-splitting
breaks a query that works today.
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))
os.environ.setdefault("LLM_MODEL", "GEMINI")
os.environ.setdefault("DATABASE_URL", "postgresql://u:p@localhost/db")

from app import decompose as d  # noqa: E402


def run(question, reply='{"parts": ["a", "b"]}', side_effect=None):
    """Call decompose with the model and cache stubbed. Returns (parts, calls)."""
    with mock.patch.object(d, "cache_get", return_value=None), \
         mock.patch.object(d, "cache_set"), \
         mock.patch.object(d, "complete", side_effect=side_effect,
                           return_value=reply) as m:
        return d.decompose(question), m.call_count


class GateTest(unittest.TestCase):
    """The gate decides whether to SPEND a model call, not what the answer is."""

    def test_plain_question_costs_no_model_call(self):
        parts, calls = run("top rated Puma sneakers")
        self.assertEqual(calls, 0)
        self.assertEqual(parts, ["top rated Puma sneakers"])

    def test_a_trailing_question_mark_is_not_a_hint(self):
        """Almost every question ends in "?". An earlier gate treated that as a
        hint and spent 6s of flash-lite deciding "any cheaper?" was one part."""
        self.assertEqual(run("any cheaper?")[1], 0)
        self.assertEqual(run("How long does delivery take?")[1], 0)

    def test_and_inside_a_word_is_not_a_conjunction(self):
        """Without word boundaries "and" matches inside "brand" and "sandals"."""
        self.assertEqual(run("Show me Nike brand shoes")[1], 0)
        self.assertEqual(run("sandals for women")[1], 0)

    def test_filters_alone_never_reach_the_model(self):
        self.assertEqual(run("Nike shoes under 3000 rated above 4.5 in stock")[1], 0)

    def test_real_hints_do_reach_the_model(self):
        for q in ("What's your return policy and show me Nike shoes",
                  "Can I cancel? Also show me shoes under 1500",
                  "I want shoes. Do you deliver on Sundays?",
                  "show me shoes; what is the refund window"):
            self.assertEqual(run(q)[1], 1, q)


class FailOpenTest(unittest.TestCase):
    """Every failure returns the original message -- never fewer, never garbage."""

    def test_unparseable_reply(self):
        parts, _ = run("shoes and returns?", reply="sorry, I cannot help")
        self.assertEqual(parts, ["shoes and returns?"])

    def test_malformed_json(self):
        parts, _ = run("shoes and returns?", reply='{"parts": [broken')
        self.assertEqual(parts, ["shoes and returns?"])

    def test_empty_parts_list(self):
        parts, _ = run("shoes and returns?", reply='{"parts": []}')
        self.assertEqual(parts, ["shoes and returns?"])

    def test_model_raising_does_not_break_the_message(self):
        parts, _ = run("shoes and returns?", side_effect=RuntimeError("503"))
        self.assertEqual(parts, ["shoes and returns?"])

    def test_absurd_part_count_is_treated_as_a_misfire(self):
        """A shopper asking five things at once is rarer than a bad parse, and
        five agent runs is five times the cost."""
        many = '{"parts": ["a", "b", "c", "d", "e"]}'
        parts, _ = run("shoes and returns?", reply=many)
        self.assertEqual(parts, ["shoes and returns?"])

    def test_at_the_limit_it_still_splits(self):
        four = '{"parts": ["a", "b", "c", "d"]}'
        parts, _ = run("shoes and returns?", reply=four)
        self.assertEqual(len(parts), d.MAX_PARTS)

    def test_blank_input(self):
        self.assertEqual(run("")[0], [""])
        self.assertEqual(run("   ")[1], 0)


class CacheTest(unittest.TestCase):
    def test_a_cached_split_skips_the_model(self):
        with mock.patch.object(d, "cache_get", return_value='["one", "two"]'), \
             mock.patch.object(d, "complete") as m:
            self.assertEqual(d.decompose("shoes and returns?"), ["one", "two"])
            m.assert_not_called()

    def test_a_corrupt_cache_entry_falls_through_to_the_model(self):
        with mock.patch.object(d, "cache_get", return_value="not json"), \
             mock.patch.object(d, "cache_set"), \
             mock.patch.object(d, "complete", return_value='{"parts": ["x"]}') as m:
            self.assertEqual(d.decompose("shoes and returns?"), ["x"])
            m.assert_called_once()


if __name__ == "__main__":
    unittest.main(verbosity=2)
