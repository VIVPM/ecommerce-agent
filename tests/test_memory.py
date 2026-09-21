"""Long-term memory contract (app/memory_store).

Why this file exists, from the original build's worst bug: the per-turn memory
write used a dict key that did not exist, raised on EVERY successful turn, and
nobody noticed for weeks — because it ran after the response was already sent
and the wrapper swallows every error. Long-term memory was simply never written.

Fail-open is correct in the request path: a memory outage must not break a
shopper's message. But fail-open is also what hid the bug. So the rule is one
test path that does NOT swallow — these assert the call actually reaches the
client with the right arguments, instead of asserting "it didn't crash".

Offline: the Supermemory client is stubbed. No key, no network.
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))
os.environ.setdefault("LLM_MODEL", "GEMINI")
os.environ.setdefault("DATABASE_URL", "postgresql://u:p@localhost/db")

from app import memory_store  # noqa: E402


class RememberTest(unittest.TestCase):
    def test_the_write_actually_reaches_the_client(self):
        """The regression that matters: assert the call happened, not that the
        wrapper stayed quiet."""
        client = mock.Mock()
        with mock.patch.object(memory_store, "_client", return_value=client):
            memory_store.remember(42, "User asked: show me Puma under 3000")
        client.add.assert_called_once()
        kwargs = client.add.call_args.kwargs
        self.assertEqual(kwargs["content"], "User asked: show me Puma under 3000")
        self.assertEqual(kwargs["container_tag"], "user_42")

    def test_memory_is_scoped_per_user(self):
        """container_tag is the only thing stopping one shopper's memory being
        recalled for another."""
        client = mock.Mock()
        with mock.patch.object(memory_store, "_client", return_value=client):
            memory_store.remember(1, "likes Puma")
            memory_store.remember(2, "likes Nike")
        tags = [c.kwargs["container_tag"] for c in client.add.call_args_list]
        self.assertEqual(tags, ["user_1", "user_2"])

    def test_empty_text_is_not_stored(self):
        client = mock.Mock()
        with mock.patch.object(memory_store, "_client", return_value=client):
            memory_store.remember(1, "")
            memory_store.remember(1, "   ")
        client.add.assert_not_called()

    def test_a_failing_client_does_not_raise(self):
        """Fail-open in the request path — the shopper's message must survive a
        memory outage."""
        client = mock.Mock()
        client.add.side_effect = RuntimeError("supermemory down")
        with mock.patch.object(memory_store, "_client", return_value=client):
            memory_store.remember(1, "likes Puma")   # must not raise

    def test_no_api_key_is_a_silent_no_op(self):
        with mock.patch.object(memory_store, "_client", return_value=None):
            memory_store.remember(1, "likes Puma")   # must not raise


class RecallTest(unittest.TestCase):
    def _client_returning(self, memories):
        client = mock.Mock()
        client.search.memories.return_value = mock.Mock(
            results=[mock.Mock(memory=m, chunk=None) for m in memories])
        return client

    def test_returns_the_remembered_lines(self):
        client = self._client_returning(["likes Puma", "budget under 3000"])
        with mock.patch.object(memory_store, "_client", return_value=client):
            out = memory_store.recall(7, "running shoes")
        self.assertIn("likes Puma", out)
        self.assertIn("budget under 3000", out)
        self.assertEqual(client.search.memories.call_args.kwargs["container_tag"], "user_7")

    def test_caps_how_much_is_injected(self):
        """Recall output goes straight into a prompt, so it is bounded."""
        client = self._client_returning([f"fact {i}" for i in range(20)])
        with mock.patch.object(memory_store, "_client", return_value=client):
            out = memory_store.recall(1, "shoes")
        self.assertEqual(len(out.splitlines()), memory_store._TOP_K)

    def test_a_miss_is_empty_not_none(self):
        """Callers concatenate this into a prompt; None would render as 'None'."""
        client = self._client_returning([])
        with mock.patch.object(memory_store, "_client", return_value=client):
            self.assertEqual(memory_store.recall(1, "shoes"), "")

    def test_failure_and_no_key_both_give_empty(self):
        client = mock.Mock()
        client.search.memories.side_effect = RuntimeError("down")
        with mock.patch.object(memory_store, "_client", return_value=client):
            self.assertEqual(memory_store.recall(1, "shoes"), "")
        with mock.patch.object(memory_store, "_client", return_value=None):
            self.assertEqual(memory_store.recall(1, "shoes"), "")

    def test_blank_query_does_not_call_out(self):
        client = mock.Mock()
        with mock.patch.object(memory_store, "_client", return_value=client):
            memory_store.recall(1, "")
        client.search.memories.assert_not_called()


class BroadRetryTest(unittest.TestCase):
    """Recall is a SIMILARITY search, so a question can miss memories that exist.

    Measured in the original build: "running shoes" found the stored memory and
    "what was I looking at before?" did not -- the second shares no vocabulary
    with anything worth storing. One broad retry covers the vocabulary gap.
    """

    def _client(self, per_query):
        client = mock.Mock()

        def search(q, **kwargs):
            return mock.Mock(results=[mock.Mock(memory=m, chunk=None)
                                      for m in per_query.get(q, [])])

        client.search.memories.side_effect = search
        return client

    def test_a_miss_retries_once_with_a_broad_query(self):
        client = self._client({memory_store._BROAD_QUERY: ["likes Puma"]})
        with mock.patch.object(memory_store, "_client", return_value=client):
            out = memory_store.recall(1, "what was I looking at before?")
        self.assertIn("likes Puma", out)
        self.assertEqual(client.search.memories.call_count, 2)

    def test_a_hit_does_not_pay_for_a_second_call(self):
        client = self._client({"running shoes": ["likes Puma"]})
        with mock.patch.object(memory_store, "_client", return_value=client):
            out = memory_store.recall(1, "running shoes")
        self.assertIn("likes Puma", out)
        self.assertEqual(client.search.memories.call_count, 1)

    def test_the_retry_stays_inside_this_users_memories(self):
        """The broad query is broad; the container tag is what keeps it from
        being another shopper's memory."""
        client = self._client({memory_store._BROAD_QUERY: ["likes Puma"]})
        with mock.patch.object(memory_store, "_client", return_value=client):
            memory_store.recall(9, "what was I looking at before?")
        tags = {c.kwargs["container_tag"] for c in client.search.memories.call_args_list}
        self.assertEqual(tags, {"user_9"})

    def test_a_genuinely_empty_memory_is_still_empty(self):
        client = self._client({})
        with mock.patch.object(memory_store, "_client", return_value=client):
            self.assertEqual(memory_store.recall(1, "anything"), "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
