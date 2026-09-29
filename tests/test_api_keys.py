"""A shopper's own Gemini key: which key a job uses, and when credits lift."""
import asyncio
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))
os.environ.setdefault("LLM_MODEL", "GEMINI")
os.environ.setdefault("DATABASE_URL", "postgresql://u:p@localhost/db")
os.environ.setdefault("JWT_SECRET", "test-secret")
os.environ.setdefault("GEMINI_API_KEY", "app-key")

from app import api_keys, llm_provider  # noqa: E402


class KeyScopeTest(unittest.TestCase):
    def test_own_key_replaces_the_app_key_only_inside_its_own_task(self):
        """Jobs run as separate tasks; one shopper's key must never reach another job."""
        async def job(key):
            llm_provider.use_gemini_key(key)
            await asyncio.sleep(0)
            return llm_provider.gemini_key()

        async def both():
            return await asyncio.gather(job("shopper-key"), job(None))

        self.assertEqual(asyncio.run(both()), ["shopper-key", os.environ["GEMINI_API_KEY"]])

    def test_credits_lift_only_when_the_shoppers_key_pays(self):
        self.assertFalse(api_keys.is_unlimited(None))
        self.assertTrue(api_keys.is_unlimited("AIza-own"))
        with mock.patch.object(api_keys, "PROVIDER", "CLOUDFLARE"):
            self.assertFalse(api_keys.is_unlimited("AIza-own"))

    def test_stored_key_round_trips_encrypted(self):
        token = api_keys._fernet.encrypt(b"AIza-secret").decode()
        self.assertNotIn("AIza-secret", token)
        row = mock.Mock(gemini_api_key_enc=token)
        with mock.patch.object(api_keys, "SessionLocal") as session:
            session.return_value.get.return_value = row
            self.assertEqual(api_keys.own_gemini_key(7), "AIza-secret")


if __name__ == "__main__":
    unittest.main()
