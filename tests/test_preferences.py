"""Saving a stated preference (app.preferences)."""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))
os.environ.setdefault("LLM_MODEL", "GEMINI")
os.environ.setdefault("DATABASE_URL", "postgresql://u:p@localhost/db")

from app import preferences  # noqa: E402


class EchoTest(unittest.TestCase):
    """Bug #17: every saved preference produced a byte-identical reply."""

    def setUp(self):
        patcher = mock.patch.object(preferences, "remember")
        self.remember = patcher.start()
        self.addCleanup(patcher.stop)

    def test_different_preferences_give_different_replies(self):
        replies = {preferences.note_preference(1, q) for q in (
            "remember I like Puma and Nike",
            "note that my budget is under 2000",
            "keep in mind I wear size 9",
        )}
        self.assertEqual(len(replies), 3, replies)

    def test_the_reply_echoes_what_was_said(self):
        out = preferences.note_preference(1, "remember I like Puma and Nike")
        self.assertIn("Puma and Nike", out)

    def test_command_filler_is_stripped_from_the_echo(self):
        """The echo should read as the preference, not as the instruction that
        wrapped it -- 'noted: "remember I like Puma"' reads like a parrot."""
        for q in ("remember I like Puma", "please note that I like Puma",
                  "keep in mind I like Puma", "Note: I like Puma"):
            self.assertIn("I like Puma", preferences.note_preference(1, q))
            self.assertNotIn("emember", preferences.note_preference(1, q))

    def test_filler_only_message_still_acknowledges(self):
        """Stripping can leave nothing; an empty echo must not become 'noted: ""'."""
        out = preferences.note_preference(1, "remember that")
        self.assertTrue(out.strip())
        self.assertNotIn('""', out)

    def test_the_preference_actually_reaches_long_term_memory(self):
        """Fail-open hides a dead write -- assert the call, per notes section 9."""
        preferences.note_preference(42, "remember I like Puma")
        self.remember.assert_called_once_with(42, "remember I like Puma")


class PersonalDataTest(unittest.TestCase):
    """A typed phone, email or card number must never be echoed or stored."""

    def setUp(self):
        patcher = mock.patch.object(preferences, "remember")
        self.remember = patcher.start()
        self.addCleanup(patcher.stop)

    def test_personal_data_is_refused_and_not_stored(self):
        for q in ("remember my card number 4111 1111 1111 1111 for next time",
                  "my phone is 9876543210, keep it in mind",
                  "remember my email ravi.k@gmail.com"):
            reply = preferences.note_preference(1, q)
            self.assertIn("can't store", reply)
            self.assertNotIn("4111", reply)
            self.assertNotIn("9876543210", reply)
            self.assertNotIn("ravi.k@gmail.com", reply)
        self.remember.assert_not_called()

    def test_budgets_and_brands_are_still_saved(self):
        for q in ("my budget is usually under 3000", "remember i like puma and nike",
                  "i wear size 9 and spend under 10000"):
            preferences.note_preference(1, q)
        self.assertEqual(self.remember.call_count, 3)

    def test_the_memory_store_itself_refuses_personal_data(self):
        from app import memory_store
        client = mock.Mock()
        with mock.patch.object(memory_store, "_client", return_value=client):
            memory_store.remember(1, "User asked: call me on 9876543210")
            memory_store.remember(1, "User asked: puma shoes under 3000")
        self.assertEqual(client.add.call_count, 1)


class StreamWrapperTest(unittest.TestCase):
    def test_the_async_wrapper_yields_the_same_text(self):
        import asyncio

        async def collect():
            with mock.patch.object(preferences, "remember"):
                return "".join([c async for c in
                                preferences.note_preference_stream_async(
                                    "remember I like Puma", 1)])

        self.assertIn("Puma", asyncio.run(collect()))


if __name__ == "__main__":
    unittest.main(verbosity=2)
