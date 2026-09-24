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
