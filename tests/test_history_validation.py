"""A full product list must survive as history for positional actions."""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))
from dotenv import load_dotenv  # noqa: E402

load_dotenv(os.path.join(os.path.dirname(__file__), "..", "backend", "app", ".env"))
from main import QueryRequest  # noqa: E402


class HistoryValidationTest(unittest.TestCase):
    def test_real_sized_result_list_is_accepted(self):
        # A measured 10-result list was 13,819 characters. "save 2" needs that
        # prior assistant message intact to resolve the number, so rejecting it
        # before the agent runs is a product bug, not input hardening.
        content = "x" * 13_819
        request = QueryRequest(query="save 2", history=[{"role": "assistant", "content": content}])
        self.assertEqual(request.history[0]["content"], content)

    def test_history_count_is_still_bounded(self):
        with self.assertRaises(ValueError):
            QueryRequest(query="save 2", history=[{"role": "assistant", "content": "x"}] * 51)


if __name__ == "__main__":
    unittest.main(verbosity=2)
