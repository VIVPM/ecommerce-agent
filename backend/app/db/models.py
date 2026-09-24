"""ORM models for accounts, chats, messages, saved items, cart, orders and the LLM cache."""
from sqlalchemy import BigInteger, Boolean, Column, Integer, String, DateTime, Text, Index
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

    id = Column(String, primary_key=True)
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
    role = Column(String)
    content = Column(Text)
    created_at = Column(DateTime(timezone=True), default=now_ist)


Index("ix_chat_messages_chat_id_id", Message.chat_id, Message.id)


class SavedProduct(Base):
    """A product a user shortlisted. Keyed by pid (Flipkart's product id) because
    that's the product's identity — the URL varies with tracking params.
    """
    __tablename__ = "saved_products"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, index=True)
    pid = Column(String, index=True)
    saved_price = Column(Integer)
    created_at = Column(DateTime(timezone=True), default=now_ist)


Index("uq_saved_user_pid", SavedProduct.user_id, SavedProduct.pid, unique=True)


class CartItem(Base):
    """A product in a user's cart. Keyed by pid like SavedProduct, for the same
    reason: the URL carries tracking params, the pid is the identity.
    """
    __tablename__ = "cart_items"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, index=True)
    pid = Column(String, index=True)
    quantity = Column(Integer, default=1)
    created_at = Column(DateTime(timezone=True), default=now_ist)


Index("uq_cart_user_pid", CartItem.user_id, CartItem.pid, unique=True)


class Order(Base):
    """A placed order. Cancellable while placed; never deleted, so the history
    stays honest about what was ordered and then called off."""
    __tablename__ = "orders"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, index=True)
    status = Column(String, default="placed")
    total = Column(Integer)
    created_at = Column(DateTime(timezone=True), default=now_ist)


class OrderItem(Base):
    """One line of an order, with the title and price COPIED IN."""
    __tablename__ = "order_items"

    id = Column(Integer, primary_key=True)
    order_id = Column(Integer, index=True)
    pid = Column(String)
    title = Column(String)
    price = Column(Integer)
    quantity = Column(Integer, default=1)


class LLMCache(Base):
    """Cache for deterministic LLM outputs (generated SQL, FAQ answers, routing)."""
    __tablename__ = "llm_cache"

    key = Column(String, primary_key=True)
    kind = Column(String)
    value = Column(Text)
    created_at = Column(DateTime(timezone=True), default=now_ist)


JOB_STATUSES = ("queued", "running", "succeeded", "failed", "cancelled")


class Job(Base):
    """One agent run, owned by one user."""
    __tablename__ = "jobs"

    id = Column(String, primary_key=True)
    user_id = Column(Integer, index=True)
    chat_id = Column(String, index=True)
    status = Column(String, default="queued", index=True)
    query = Column(Text)
    history = Column(Text)
    tool = Column(String)
    result = Column(Text)
    error = Column(Text)
    cancel_requested = Column(Boolean, default=False)
    attempts = Column(Integer, default=0)
    emitted = Column(Boolean, default=False)
    idempotency_key = Column(String)
    input_tokens = Column(Integer, default=0)
    output_tokens = Column(Integer, default=0)
    cached_tokens = Column(Integer, default=0)
    image_query = Column(String, nullable=True)
    ttft_ms = Column(Integer)
    provider = Column(String)
    lease_until = Column(DateTime(timezone=True))
    worker_id = Column(String)
    created_at = Column(DateTime(timezone=True), default=now_ist, index=True)
    started_at = Column(DateTime(timezone=True))
    finished_at = Column(DateTime(timezone=True))


Index("ix_jobs_claim", Job.status, Job.created_at)
Index("ix_jobs_user_status", Job.user_id, Job.status)


class JobEvent(Base):
    """Durable event log for one job."""
    __tablename__ = "job_events"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    job_id = Column(String, index=True)
    seq = Column(Integer)
    type = Column(String)
    data = Column(Text)
    created_at = Column(DateTime(timezone=True), default=now_ist)


Index("ix_job_events_job_seq", JobEvent.job_id, JobEvent.seq, unique=True)
