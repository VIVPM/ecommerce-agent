from sqlalchemy import Column, Integer, String, DateTime, Text, Index
from app.db.database import Base
from datetime import datetime, timezone, timedelta

IST = timezone(timedelta(hours=5, minutes=30))
def now_ist():
    return datetime.now(IST)

class EcommerceAccount(Base):
    __tablename__ = "ecommerce_accounts"

    id = Column(Integer, primary_key=True, index=True)
    username = Column(String, unique=True, index=True)
    hashed_password = Column(String)
    # (legacy `chats` JSON blob removed — history lives in chat_sessions/chat_messages)


class LoginFailure(Base):
    """One row per failed login attempt. DB-backed (not in-memory) so the lockout
    holds across multiple API instances, not just one process."""
    __tablename__ = "login_failures"

    id = Column(Integer, primary_key=True, index=True)
    username = Column(String, index=True)
    created_at = Column(DateTime(timezone=True), default=now_ist)


class Chat(Base):
    """One chat session. Replaces the per-chat entry that used to live inside the
    ecommerce_accounts.chats JSON blob. Named chat_sessions to avoid a legacy
    `chats` table left over from an earlier version of the app."""
    __tablename__ = "chat_sessions"

    id = Column(String, primary_key=True)  # uuid
    user_id = Column(Integer, index=True)
    title = Column(String, default="New Chat")
    created_at = Column(DateTime(timezone=True), default=now_ist)
    updated_at = Column(DateTime(timezone=True), default=now_ist)


class Message(Base):
    """One message in a chat. Separate rows mean concurrent messages can't clobber
    each other the way a shared JSON blob could. Ordered by autoincrement id."""
    __tablename__ = "chat_messages"

    id = Column(Integer, primary_key=True)
    chat_id = Column(String, index=True)
    user_id = Column(Integer, index=True)
    role = Column(String)  # "user" | "assistant"
    content = Column(Text)
    created_at = Column(DateTime(timezone=True), default=now_ist)


# Composite index for the common "all messages of a chat, in order" read.
Index("ix_chat_messages_chat_id_id", Message.chat_id, Message.id)


class SavedProduct(Base):
    """A product a user shortlisted. Keyed by pid (Flipkart's product id) because
    that's the product's identity — the URL varies with tracking params.

    saved_price records the price AT SAVE TIME, which is what makes price-drop
    detection possible: compare it against product.price, which the refresh
    pipeline keeps current. No scheduler needed for the comparison itself.
    """
    __tablename__ = "saved_products"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, index=True)
    pid = Column(String, index=True)
    saved_price = Column(Integer)          # price when saved; NULL if unknown
    created_at = Column(DateTime(timezone=True), default=now_ist)


# One row per (user, product) — saving twice is idempotent, not a duplicate.
Index("uq_saved_user_pid", SavedProduct.user_id, SavedProduct.pid, unique=True)


class LLMCache(Base):
    """Cache for deterministic LLM outputs (generated SQL, FAQ answers, routing).

    Postgres-backed rather than in-process because the free-tier instance spins
    down when idle — an in-memory cache would be cold almost every request. A
    ~30ms round-trip is nothing against the 3-5s call it skips. TTL is enforced
    at READ time, so there's no cleanup job.

    After re-ingesting the FAQ corpus, purge stale answers:
        DELETE FROM llm_cache WHERE kind = 'faq';
    """
    __tablename__ = "llm_cache"

    key = Column(String, primary_key=True)   # sha256 of kind + normalized question
    kind = Column(String)                    # 'sql' | 'faq' | 'route'
    value = Column(Text)
    created_at = Column(DateTime(timezone=True), default=now_ist)
