"""A shopper's own Gemini key: encrypted storage, lookup, and the credit rule it unlocks."""
import base64
import hashlib
import logging
import os
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken

from app.db.database import SessionLocal
from app.db.models import UserApiKey, now_ist
from app.llm_provider import PROVIDER

logger = logging.getLogger(__name__)

_fernet = Fernet(base64.urlsafe_b64encode(
    hashlib.sha256(os.environ["JWT_SECRET"].encode()).digest()))


def own_gemini_key(user_id: int) -> Optional[str]:
    """The shopper's saved Gemini key, or None if absent or no longer decryptable."""
    db = SessionLocal()
    try:
        row = db.get(UserApiKey, user_id)
    finally:
        db.close()
    if not row or not row.gemini_api_key_enc:
        return None
    try:
        return _fernet.decrypt(row.gemini_api_key_enc.encode()).decode()
    except InvalidToken:
        logger.warning("Stored Gemini key for user %s can't be decrypted; ignoring it", user_id)
        return None


def save_gemini_key(user_id: int, key: str) -> None:
    """Encrypt and store (or replace) the shopper's Gemini key."""
    db = SessionLocal()
    try:
        row = db.get(UserApiKey, user_id) or UserApiKey(user_id=user_id)
        row.gemini_api_key_enc = _fernet.encrypt(key.encode()).decode()
        row.updated_at = now_ist()
        db.add(row)
        db.commit()
    finally:
        db.close()


def delete_gemini_key(user_id: int) -> None:
    """Forget the shopper's Gemini key."""
    db = SessionLocal()
    try:
        db.query(UserApiKey).filter(UserApiKey.user_id == user_id).delete()
        db.commit()
    finally:
        db.close()


def is_unlimited(own_key: Optional[str]) -> bool:
    """Credits cap the app's Gemini spend, so they lift only when the shopper's own key pays for it."""
    return bool(own_key) and PROVIDER == "GEMINI"


def is_key_error(e: Exception) -> bool:
    """True when Gemini refused the credentials themselves (invalid, revoked or out of quota)."""
    text = str(e)
    return any(s in text for s in ("API_KEY_INVALID", "API key not valid", "PERMISSION_DENIED",
                                   "RESOURCE_EXHAUSTED", "API key expired"))
