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


# --- Job queue (v0) -------------------------------------------------------
# The agent used to run inside the HTTP request, so a closed tab, a dropped
# connection or a deploy destroyed the answer AND still cost a credit. A job
# outlives the request that created it. Postgres is the queue: no new service,
# and `FOR UPDATE SKIP LOCKED` is a real queue primitive.

JOB_STATUSES = ("queued", "running", "succeeded", "failed", "cancelled")


class Job(Base):
    """One agent run, owned by one user.

    `lease_until` is the visibility timeout: a worker claims a job by stamping a
    lease, and the reaper reclaims anything whose lease expired (a crashed or
    redeployed worker). `emitted` records whether any token reached the client —
    it is what decides whether a reclaimed job is safe to requeue. Re-running a
    job that already produced output would bill the model twice for one answer,
    which is exactly the "retry the transport, never the reasoning" rule.
    """
    __tablename__ = "jobs"

    # uuid4 hex, not a sequence: a job id is handed to the browser, so it must
    # not be guessable or enumerable.
    id = Column(String, primary_key=True)
    user_id = Column(Integer, index=True)     # tenant scope, always from the token
    chat_id = Column(String, index=True)
    status = Column(String, default="queued", index=True)
    query = Column(Text)
    history = Column(Text)                    # JSON, already bounded by QueryRequest
    tool = Column(String)                     # which tool answered; drives follow-up chips
    result = Column(Text)
    error = Column(Text)
    # Cancellation is a flag the worker checks between chunks, not a kill signal.
    cancel_requested = Column(Boolean, default=False)
    attempts = Column(Integer, default=0)
    emitted = Column(Boolean, default=False)
    # Idempotency: a retried POST with the same key returns the SAME job instead
    # of running the agent again and billing twice.
    idempotency_key = Column(String)
    # What this run actually cost. A message count treats a 50-token question and
    # a 40k-token one alike; tokens are what the provider charges for.
    # cached_tokens is the prompt-cache read portion of input_tokens.
    input_tokens = Column(Integer, default=0)
    output_tokens = Column(Integer, default=0)
    cached_tokens = Column(Integer, default=0)
    # Time to FIRST token, in ms. Separate from total duration on purpose: on a
    # streaming UI this is the latency a user actually feels, and the two move
    # independently — a fast first token with a slow tail reads as responsive.
    ttft_ms = Column(Integer)
    provider = Column(String)     # which provider served it (failover makes this vary)
    lease_until = Column(DateTime(timezone=True))
    worker_id = Column(String)
    created_at = Column(DateTime(timezone=True), default=now_ist, index=True)
    started_at = Column(DateTime(timezone=True))
    finished_at = Column(DateTime(timezone=True))


# The claim query orders by created_at within status — FIFO, one index.
Index("ix_jobs_claim", Job.status, Job.created_at)
# "How many jobs does this user have in flight?" — the concurrency cap.
Index("ix_jobs_user_status", Job.user_id, Job.status)


class JobEvent(Base):
    """Durable event log for one job.

    The worker appends here whether or not anyone is listening. That is what
    makes a dropped connection survivable: a reconnecting client replays from
    its last `seq` and then tails, instead of losing the answer.

    Tokens are coalesced before they land here (see worker.py) — one row per
    token would mean ~300 inserts for a single comparison answer.
    """
    __tablename__ = "job_events"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    job_id = Column(String, index=True)
    seq = Column(Integer)                     # per-job monotonic; the resume cursor
    type = Column(String)                     # 'status' | 'token' | 'done' | 'error'
    data = Column(Text)
    created_at = Column(DateTime(timezone=True), default=now_ist)


# Every read is "events for this job after this seq", so index the pair.
Index("ix_job_events_job_seq", JobEvent.job_id, JobEvent.seq, unique=True)
