"""Encrypted shopper credentials and provider choice for generation billing."""
import base64
import hashlib
import logging
import os

from cryptography.fernet import Fernet, InvalidToken

from app.db.database import SessionLocal
from app.db.models import UserApiKey, UserProvider, now_ist

logger = logging.getLogger(__name__)

_fernet = Fernet(base64.urlsafe_b64encode(
    hashlib.sha256(os.environ["JWT_SECRET"].encode()).digest()))


def _decrypt(value: str | None, user_id: int) -> str | None:
    if not value:
        return None
    try:
        return _fernet.decrypt(value.encode()).decode()
    except InvalidToken:
        logger.warning("Stored provider credential for user %s can't be decrypted", user_id)
        return None


def provider_settings(user_id: int) -> dict:
    """Read the selected provider and credentials; legacy Gemini users stay on BYOK."""
    db = SessionLocal()
    try:
        row = db.get(UserProvider, user_id)
        key_row = db.get(UserApiKey, user_id)
        gemini_key = _decrypt(key_row.gemini_api_key_enc, user_id) if key_row else None
        cloudflare_token = _decrypt(row.cloudflare_token_enc, user_id) if row else None
        return {
            "selected": row.selected if row else ("GEMINI" if gemini_key else "FREE"),
            "gemini_key": gemini_key,
            "cloudflare_account_id": row.cloudflare_account_id if row else None,
            "cloudflare_token": cloudflare_token,
        }
    finally:
        db.close()


def is_unlimited(settings: dict) -> bool:
    """Lift message/token credits only when the selected generation provider is user-paid."""
    if settings["selected"] == "GEMINI":
        return bool(settings["gemini_key"])
    if settings["selected"] == "CLOUDFLARE":
        return bool(settings["cloudflare_account_id"] and settings["cloudflare_token"])
    return False


def select_provider(user_id: int, provider: str) -> None:
    if provider not in ("FREE", "GEMINI", "CLOUDFLARE"):
        raise ValueError("Unknown provider")
    db = SessionLocal()
    try:
        row = db.get(UserProvider, user_id) or UserProvider(user_id=user_id)
        key_row = db.get(UserApiKey, user_id) if provider == "GEMINI" else None
        if provider == "GEMINI" and not _decrypt(
                key_row.gemini_api_key_enc if key_row else None, user_id):
            raise ValueError("Save a Gemini key before selecting Gemini")
        if provider == "CLOUDFLARE" and not (row.cloudflare_account_id and
                                                _decrypt(row.cloudflare_token_enc, user_id)):
            raise ValueError("Save Cloudflare credentials before selecting Cloudflare")
        row.selected = provider
        db.add(row)
        db.commit()
    finally:
        db.close()


def save_gemini_key(user_id: int, key: str) -> None:
    db = SessionLocal()
    try:
        row = db.get(UserApiKey, user_id) or UserApiKey(user_id=user_id)
        row.gemini_api_key_enc = _fernet.encrypt(key.encode()).decode()
        row.updated_at = now_ist()
        choice = db.get(UserProvider, user_id) or UserProvider(user_id=user_id)
        choice.selected = "GEMINI"
        db.add_all((row, choice))
        db.commit()
    finally:
        db.close()


def save_cloudflare(user_id: int, account_id: str, token: str) -> None:
    db = SessionLocal()
    try:
        row = db.get(UserProvider, user_id) or UserProvider(user_id=user_id)
        row.cloudflare_account_id = account_id
        row.cloudflare_token_enc = _fernet.encrypt(token.encode()).decode()
        row.selected = "CLOUDFLARE"
        db.add(row)
        db.commit()
    finally:
        db.close()


def delete_gemini_key(user_id: int) -> None:
    db = SessionLocal()
    try:
        db.query(UserApiKey).filter(UserApiKey.user_id == user_id).delete()
        row = db.get(UserProvider, user_id)
        if row and row.selected == "GEMINI":
            row.selected = "FREE"
        db.commit()
    finally:
        db.close()


def delete_cloudflare(user_id: int) -> None:
    db = SessionLocal()
    try:
        row = db.get(UserProvider, user_id)
        if row:
            row.cloudflare_account_id = None
            row.cloudflare_token_enc = None
            if row.selected == "CLOUDFLARE":
                row.selected = "FREE"
            db.commit()
    finally:
        db.close()


def is_key_error(e: Exception) -> bool:
    """True when a shopper's credentials are rejected or exhausted."""
    text = str(e)
    return any(s in text for s in ("API_KEY_INVALID", "API key not valid", "PERMISSION_DENIED",
                                   "RESOURCE_EXHAUSTED", "API key expired", "AuthenticationError",
                                   "PermissionDeniedError", "RateLimitError", "401", "403", "429"))
