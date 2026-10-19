"""Rate limits: signup and login per username, provider changes per user (no real keys)."""
import os
import sys
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))
from dotenv import load_dotenv  # noqa: E402

load_dotenv(os.path.join(os.path.dirname(__file__), "..", "backend", "app", ".env"))
import main  # noqa: E402
from app.db.models import EcommerceAccount, LoginFailure  # noqa: E402


class AuthRateLimitTest(unittest.TestCase):
    def test_signup_and_correct_password_login(self):
        engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                               poolclass=StaticPool)
        EcommerceAccount.__table__.create(engine)
        LoginFailure.__table__.create(engine)
        with patch("main.SessionLocal", sessionmaker(bind=engine)):
            main._auth_attempts.clear()
            client = TestClient(main.app)
            signups = [client.post("/api/auth/signup", json={
                "username": "ratelimit0@example.com", "password": "ValidPass123"})
                for _ in range(6)]
            self.assertEqual([r.status_code for r in signups], [200] + [400] * 4 + [429])
            other = client.post("/api/auth/signup", json={
                "username": "someone.else@example.com", "password": "ValidPass123"})
            self.assertEqual(other.status_code, 200)
            logins = [client.post("/api/auth/login", json={
                "username": "ratelimit0@example.com", "password": "ValidPass123"})
                for _ in range(11)]
            self.assertEqual([r.status_code for r in logins], [200] * 10 + [429])
        engine.dispose()

    def test_provider_selection_per_user(self):
        token = main.create_token(900001, "rate@example.com")
        client = TestClient(main.app)
        with patch("main.select_provider"):
            codes = [client.put("/api/account/provider", json={"provider": "FREE"},
                                headers={"Authorization": f"Bearer {token}"}).status_code
                     for _ in range(11)]
        self.assertEqual(codes, [200] * 10 + [429])

    def test_credential_validation_per_user_without_provider_calls(self):
        gemini = TestClient(main.app)
        token = main.create_token(900002, "rate-gemini@example.com")
        with patch("google.genai.Client"), patch("main.save_gemini_key"):
            codes = [gemini.put("/api/account/api-keys", json={"gemini_api_key": "x" * 25},
                                headers={"Authorization": f"Bearer {token}"}).status_code
                     for _ in range(11)]
        self.assertEqual(codes, [200] * 10 + [429])

        cloudflare = TestClient(main.app)
        token = main.create_token(900003, "rate-cloudflare@example.com")
        with patch("httpx.post") as external, patch("main.save_cloudflare"):
            external.return_value.json.return_value = {"success": True}
            codes = [cloudflare.put("/api/account/api-keys/cloudflare", json={
                "account_id": "a" * 32, "api_token": "x" * 25},
                headers={"Authorization": f"Bearer {token}"}).status_code
                for _ in range(11)]
        self.assertEqual(codes, [200] * 10 + [429])


if __name__ == "__main__":
    unittest.main()
