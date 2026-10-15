"""User-owned provider credentials, selection, and credit boundaries (no network)."""
import asyncio
import os
import sys
import unittest
from unittest import mock

import httpx
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))
os.environ.setdefault("LLM_MODEL", "GEMINI")
os.environ.setdefault("DATABASE_URL", "postgresql://u:p@localhost/db")
os.environ.setdefault("JWT_SECRET", "test-secret")
os.environ.setdefault("GEMINI_API_KEY", "app-key")

from app import api_keys, llm_provider  # noqa: E402
from app.db.models import Chat, Job, Message, UserApiKey, UserProvider  # noqa: E402


class KeyScopeTest(unittest.TestCase):
    def test_provider_context_stays_in_its_own_task(self):
        async def job(mode, key, account=None, token=None):
            llm_provider.use_job_provider(mode, account, token)
            llm_provider.use_gemini_key(key)
            await asyncio.sleep(0)
            return llm_provider.active_provider(), llm_provider.gemini_key(), llm_provider._user_cloudflare.get()

        async def both():
            return await asyncio.gather(
                job("CLOUDFLARE", None, "abcd" * 8, "own-token"),
                job("GEMINI", "shopper-key"),
            )

        cloudflare, gemini = asyncio.run(both())
        self.assertEqual(cloudflare, ("CLOUDFLARE", "app-key", ("abcd" * 8, "own-token")))
        self.assertEqual(gemini, ("GEMINI", "shopper-key", None))

        llm_provider.use_job_provider("CLOUDFLARE", "abcd" * 8, "own-token")
        with mock.patch.object(llm_provider, "_build", return_value="model") as build:
            self.assertEqual(llm_provider.chat(), "model")
            build.assert_called_once_with("CLOUDFLARE", 0.0, None, "own-token", "abcd" * 8)
        llm_provider.use_job_provider("GEMINI")
        llm_provider.use_gemini_key("shopper-key")
        with mock.patch.object(llm_provider, "_build", return_value="model") as build:
            self.assertEqual(llm_provider.chat(), "model")
            build.assert_called_once_with("GEMINI", 0.0, llm_provider.GEMINI_DEFAULT, "shopper-key")
        llm_provider.use_job_provider(None)
        llm_provider.use_gemini_key(None)

    def test_selection_and_removal_change_credit_payer(self):
        engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        UserApiKey.__table__.create(engine)
        UserProvider.__table__.create(engine)
        sessions = sessionmaker(bind=engine)
        with mock.patch.object(api_keys, "SessionLocal", sessions):
            settings = api_keys.provider_settings(7)
            self.assertEqual(settings["selected"], "FREE")
            self.assertFalse(api_keys.is_unlimited(settings))

            api_keys.save_gemini_key(7, "AIza-secret")
            settings = api_keys.provider_settings(7)
            self.assertEqual(settings["selected"], "GEMINI")
            self.assertTrue(api_keys.is_unlimited(settings))
            self.assertEqual(settings["gemini_key"], "AIza-secret")
            api_keys.select_provider(7, "FREE")
            self.assertFalse(api_keys.is_unlimited(api_keys.provider_settings(7)))

            api_keys.save_cloudflare(7, "abcd" * 8, "cf-token-secret")
            settings = api_keys.provider_settings(7)
            self.assertEqual(settings["selected"], "CLOUDFLARE")
            self.assertTrue(api_keys.is_unlimited(settings))
            with sessions() as db:
                self.assertNotIn("cf-token-secret", db.get(UserProvider, 7).cloudflare_token_enc)
                self.assertNotIn("AIza-secret", db.get(UserApiKey, 7).gemini_api_key_enc)

            api_keys.delete_cloudflare(7)
            self.assertEqual(api_keys.provider_settings(7)["selected"], "FREE")
            with self.assertRaises(ValueError):
                api_keys.select_provider(7, "CLOUDFLARE")
            api_keys.select_provider(7, "GEMINI")
            api_keys.delete_gemini_key(7)
            self.assertFalse(api_keys.is_unlimited(api_keys.provider_settings(7)))
            with sessions() as db:
                db.add(UserApiKey(user_id=8, gemini_api_key_enc=
                                  api_keys._fernet.encrypt(b"legacy-key").decode()))
                db.commit()
            self.assertEqual(api_keys.provider_settings(8)["selected"], "GEMINI")
        engine.dispose()


    def test_http_provider_choice_updates_credits_without_exposing_tokens(self):
        with mock.patch("sqlalchemy.sql.schema.MetaData.create_all"):
            import main

        engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        UserApiKey.__table__.create(engine)
        UserProvider.__table__.create(engine)
        Chat.__table__.create(engine)
        Message.__table__.create(engine)
        Job.__table__.create(engine)
        sessions = sessionmaker(bind=engine)
        with sessions() as db:
            db.add(Chat(id="chat-9", user_id=9, title="New Chat"))
            db.commit()
        headers = {"Authorization": f"Bearer {main.create_token(9, 'shopper')}"}
        with mock.patch.object(api_keys, "SessionLocal", sessions), \
             mock.patch.object(main, "SessionLocal", sessions), \
             mock.patch.object(main, "DAILY_MESSAGE_CAP", 5), \
             mock.patch("main._messages_used_today", return_value=100), \
             mock.patch("main.jobs.active_job_count", return_value=0), \
             mock.patch("main.jobs.tokens_used_today", return_value=0), \
             mock.patch("httpx.post") as cloudflare:
            cloudflare.return_value.json.return_value = {"success": True}
            client = TestClient(main.app)
            free = client.get("/api/account/credits", headers=headers)
            self.assertEqual(free.json()["unlimited"], False)

            saved = client.put("/api/account/api-keys/cloudflare", headers=headers,
                               json={"account_id": "abcd" * 8, "api_token": "token-" * 6})
            self.assertEqual(saved.status_code, 200, saved.text)
            status = client.get("/api/account/api-keys", headers=headers).json()
            self.assertEqual(status["selected"], "CLOUDFLARE")
            self.assertNotIn("token-token", str(status))
            self.assertEqual(client.get("/api/account/credits", headers=headers).json()["unlimited"], True)
            sent = client.post("/api/chats/chat-9/message", headers=headers,
                               json={"query": "Nike shoes", "history": []})
            self.assertEqual(sent.status_code, 202, sent.text)
            with sessions() as db:
                self.assertEqual(db.get(Job, sent.json()["job_id"]).billing_mode, "CLOUDFLARE")

            switched = client.put("/api/account/provider", headers=headers,
                                  json={"provider": "FREE"})
            self.assertEqual(switched.status_code, 200)
            self.assertEqual(client.get("/api/account/credits", headers=headers).json()["unlimited"], False)
            limited = client.post("/api/chats/chat-9/message", headers=headers,
                                  json={"query": "Puma shoes", "history": []})
            self.assertEqual(limited.status_code, 429)
            client.delete("/api/account/api-keys/cloudflare", headers=headers)
            rejected = client.put("/api/account/provider", headers=headers,
                                  json={"provider": "CLOUDFLARE"})
            self.assertEqual(rejected.status_code, 400)
            cloudflare.return_value.raise_for_status.side_effect = httpx.HTTPStatusError(
                "unauthorized", request=httpx.Request("POST", "https://api.cloudflare.com"),
                response=httpx.Response(401))
            bad_key = client.put("/api/account/api-keys/cloudflare", headers=headers,
                                 json={"account_id": "abcd" * 8, "api_token": "invalid" * 5})
            self.assertEqual(bad_key.status_code, 400)
            self.assertEqual(api_keys.provider_settings(9)["selected"], "FREE")
        engine.dispose()


    def test_paid_job_never_uses_app_key_after_credentials_are_removed(self):
        from dotenv import load_dotenv
        load_dotenv(os.path.join(os.path.dirname(__file__), "..", "backend", "app", ".env"))
        from app import jobs, worker

        db = mock.Mock()
        jobs.create_job(9, "chat", "query", [], billing_mode="CLOUDFLARE", db=db)
        self.assertEqual(db.execute.call_args.args[1]["mode"], "CLOUDFLARE")

        class Emitter:
            text_parts = []
            first_token_at = None

            def __init__(self, *_args):
                pass

            def terminal(self, *_args):
                pass

            async def close(self):
                pass

        job = {"id": "job-1", "user_id": 9, "chat_id": "chat", "query": "shoes",
               "billing_mode": "CLOUDFLARE"}
        with mock.patch.object(worker.jobs, "next_seq", return_value=0), \
             mock.patch.object(worker, "provider_settings", return_value={
                 "gemini_key": None, "cloudflare_account_id": None, "cloudflare_token": None}), \
             mock.patch.object(worker, "_Emitter", Emitter), \
             mock.patch.object(worker.jobs, "finish_job") as finish, \
             mock.patch.object(worker.jobs, "record_usage"), \
             mock.patch.object(worker.llm_provider, "chat") as chat, \
             mock.patch.object(worker.llm_provider, "note_result") as breaker:
            asyncio.run(worker.execute(job))
            chat.assert_not_called()
            breaker.assert_not_called()
            self.assertEqual(finish.call_args.args[:2], ("job-1", "failed"))
            self.assertIn("Cloudflare credentials", finish.call_args.kwargs["error"])


if __name__ == "__main__":
    unittest.main()
