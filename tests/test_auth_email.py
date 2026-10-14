"""Email usernames and existing usernames both work at the auth boundary."""
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
from main import LoginRequest, SignupRequest, app  # noqa: E402
from app.db.models import EcommerceAccount, LoginFailure  # noqa: E402


class AuthEmailTest(unittest.TestCase):
    def test_email_signup_and_login_keep_same_identifier(self):
        email = "ava.patel+shopper@example.com"
        signup = SignupRequest(username=f" {email} ", password="ValidPass123")
        login = LoginRequest(username=f" {email} ", password="ValidPass123")
        self.assertEqual(signup.username, login.username)
        self.assertEqual(signup.username, email)
        long_email = "averylongfirstname.longlastname@example.com"
        self.assertEqual(SignupRequest(username=long_email, password="ValidPass123").username,
                         long_email)

    def test_existing_username_still_works(self):
        self.assertEqual(SignupRequest(username="shopper_01", password="ValidPass123").username,
                         "shopper_01")

    def test_signup_then_login_through_http(self):
        engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                               poolclass=StaticPool)
        EcommerceAccount.__table__.create(engine)
        LoginFailure.__table__.create(engine)
        sessions = sessionmaker(bind=engine)
        with patch("main.SessionLocal", sessions):
            client = TestClient(app)
            email = "ava.patel@example.com"
            signup = client.post("/api/auth/signup", json={"username": email, "password": "ValidPass123"})
            self.assertEqual(signup.status_code, 200, signup.text)
            login = client.post("/api/auth/login", json={"username": f" {email} ",
                                                        "password": "ValidPass123"})
            self.assertEqual(login.status_code, 200, login.text)
            self.assertEqual(login.json()["user_id"], signup.json()["user_id"])
            self.assertEqual(login.json()["username"], email)
        engine.dispose()

    def test_malformed_emails_and_unsafe_usernames_are_rejected(self):
        for value in ("ava..patel@example.com", "ava@-example.com", "ava@example..com",
                      "ava@example", "ava@evil.\ncom", "ava.patel", "x" * 255):
            with self.subTest(value=value), self.assertRaises(ValueError):
                SignupRequest(username=value, password="ValidPass123")


if __name__ == "__main__":
    unittest.main(verbosity=2)
