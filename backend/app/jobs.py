"""Postgres-backed job queue (v0).

Why Postgres and not Redis: the app already owns a Neon database, and
`FOR UPDATE SKIP LOCKED` is a real queue primitive — it gives atomic claim,
FIFO ordering and crash recovery without adding a service to run, secure and
pay for. Ceiling to know: this starts creaking past roughly four concurrent
workers or a high poll rate. That is when Redis earns its place, not before.

Two tables (models.py): `jobs` holds one row per agent run, `job_events` is the
durable event log the worker appends to whether or not a client is listening.
"""
import json
import logging
import uuid

from sqlalchemy import text

from app.db.database import SessionLocal
from app.db.models import now_ist

logger = logging.getLogger(__name__)

# How long a worker may hold a job before the reaper assumes it died. Must be
# comfortably above the slowest realistic agent run.
LEASE_SECONDS = 300
# A job is only ever retried when it produced NOTHING, so this stays small.
MAX_ATTEMPTS = 2


def _session():
    return SessionLocal()


def find_by_idempotency_key(user_id: int, key: str, db=None):
    """Return an existing job id for this (user, key), or None.

    Without this, a client that retries a submit — a flaky network, an
    impatient double-tap — runs the agent twice and pays twice for one answer.
    """
    own = db is None
    db = db or _session()
    try:
        return db.execute(text("""
            SELECT id FROM jobs WHERE user_id = :uid AND idempotency_key = :k
        """), {"uid": user_id, "k": key}).scalar()
    finally:
        if own:
            db.close()


def record_usage(job_id: str, input_tokens: int, output_tokens: int, cached_tokens: int,
                 ttft_ms: int | None = None, provider: str | None = None) -> None:
    db = _session()
    try:
        db.execute(text("""
            UPDATE jobs SET input_tokens = :i, output_tokens = :o, cached_tokens = :c,
                            ttft_ms = :t, provider = :p
             WHERE id = :id
        """), {"id": job_id, "i": input_tokens, "o": output_tokens, "c": cached_tokens,
               "t": ttft_ms, "p": provider})
        db.commit()
    except Exception as e:
        logger.warning("record_usage failed for %s: %s", job_id, e)
    finally:
        db.close()


def tokens_used_today(user_id: int, since) -> int:
    """Tokens this user has spent since IST midnight. This is the real cost
    meter: a message count charges a 50-token question and a 40k-token one the
    same, which is exactly the thing a per-message cap fails to bound."""
    db = _session()
    try:
        return db.execute(text("""
            SELECT COALESCE(SUM(COALESCE(input_tokens, 0) + COALESCE(output_tokens, 0)), 0)
              FROM jobs WHERE user_id = :uid AND created_at >= :since
        """), {"uid": user_id, "since": since}).scalar() or 0
    finally:
        db.close()


def create_job(user_id: int, chat_id: str, query: str, history: list,
               idempotency_key: str | None = None, db=None) -> str:
    """Persist a job BEFORE it is queued — inserting the row IS the enqueue, so
    there is no window where the caller was told "accepted" but nothing exists.

    Pass an existing session to enrol this in a caller's transaction: the API
    saves the user's message and creates the job together, so a chat can never
    end up showing a question with no job behind it.
    """
    job_id = uuid.uuid4().hex
    own = db is None
    db = db or _session()
    try:
        db.execute(text("""
            INSERT INTO jobs (id, user_id, chat_id, status, query, history,
                              cancel_requested, attempts, emitted, created_at,
                              idempotency_key, input_tokens, output_tokens, cached_tokens)
            VALUES (:id, :uid, :cid, 'queued', :q, :h, false, 0, false, :now, :k, 0, 0, 0)
        """), {"id": job_id, "uid": user_id, "cid": chat_id, "q": query,
               "h": json.dumps(history or []), "now": now_ist(), "k": idempotency_key})
        if own:
            db.commit()
        return job_id
    finally:
        if own:
            db.close()


def claim_job(worker_id: str):
    """Atomically take the oldest queued job. Returns a dict or None.

    SKIP LOCKED is what makes this safe with more than one worker: a row already
    being claimed elsewhere is stepped over rather than waited on.
    """
    db = _session()
    try:
        row = db.execute(text("""
            WITH last_served AS (
                -- When was each user last given a slot? NULL means never.
                SELECT user_id, MAX(started_at) AS last_start
                  FROM jobs WHERE started_at IS NOT NULL GROUP BY user_id
            ),
            head_of_queue AS (
                -- Each waiting user's OWN oldest job, plus how long ago that user
                -- was last served.
                SELECT j.id, j.created_at, ls.last_start,
                       ROW_NUMBER() OVER (PARTITION BY j.user_id ORDER BY j.created_at) AS rn
                  FROM jobs j
                  LEFT JOIN last_served ls ON ls.user_id = j.user_id
                 WHERE j.status = 'queued'
            ),
            candidates AS (
                -- Fair queuing: serve the user who waited longest since their last
                -- turn, not whoever queued first. Ranking by position among the
                -- REMAINING queue does not work — once a user's first job is
                -- claimed their second becomes rank 1 again and wins the tie, which
                -- collapses straight back to FIFO. `last_start` is the thing that
                -- actually moves when a user is served.
                -- A shortlist, not a single row: with several claimers the top pick
                -- may already be locked, and SKIP LOCKED on one candidate would
                -- report "queue empty" while work was waiting.
                SELECT id, last_start, created_at
                  FROM head_of_queue WHERE rn = 1
                 ORDER BY last_start NULLS FIRST, created_at
                 LIMIT 20
            ),
            next_job AS (
                SELECT j.id
                  FROM jobs j
                  JOIN candidates c ON c.id = j.id
                 WHERE j.status = 'queued'
                 ORDER BY c.last_start NULLS FIRST, c.created_at
                 FOR UPDATE OF j SKIP LOCKED
                 LIMIT 1
            )
            UPDATE jobs j
               SET status      = 'running',
                   worker_id   = :wid,
                   lease_until = :lease,
                   attempts    = j.attempts + 1,
                   started_at  = COALESCE(j.started_at, :now)
              FROM next_job
             WHERE j.id = next_job.id
            RETURNING j.id, j.user_id, j.chat_id, j.query, j.history, j.attempts
        """), {"wid": worker_id, "now": now_ist(),
               "lease": now_ist() + _delta(LEASE_SECONDS)}).fetchone()
        db.commit()
        if not row:
            return None
        job = dict(row._mapping)
        job["history"] = json.loads(job["history"] or "[]")
        return job
    finally:
        db.close()


def queue_depth() -> int:
    """How many jobs are waiting. Drives worker concurrency — this is the signal
    'queue-depth autoscaling' scales on, whether the unit is a machine or, here,
    a coroutine slot."""
    db = _session()
    try:
        return db.execute(text("SELECT COUNT(*) FROM jobs WHERE status = 'queued'")).scalar() or 0
    except Exception as e:
        logger.warning("queue_depth failed: %s", e)
        return 0
    finally:
        db.close()


def _delta(seconds: int):
    from datetime import timedelta
    return timedelta(seconds=seconds)


def heartbeat(job_id: str) -> bool:
    """Extend the lease and report whether the job should keep running.

    Returns False if the job was cancelled — this is the flag the worker checks
    between chunks, which is what makes cancellation cooperative rather than a
    kill signal that could leave a half-written answer.
    """
    db = _session()
    try:
        row = db.execute(text("""
            UPDATE jobs SET lease_until = :lease
             WHERE id = :id AND status = 'running'
            RETURNING cancel_requested
        """), {"id": job_id, "lease": now_ist() + _delta(LEASE_SECONDS)}).fetchone()
        db.commit()
        return bool(row) and not row[0]
    except Exception as e:
        logger.warning("heartbeat failed for %s: %s", job_id, e)
        return True     # never kill a running job because the heartbeat glitched
    finally:
        db.close()


def next_seq(job_id: str) -> int:
    """Highest seq already stored for this job. Called ONCE when a worker picks
    the job up; the worker then counts in memory. A requeued job resumes above
    its existing events instead of colliding with them."""
    db = _session()
    try:
        return db.execute(text("SELECT COALESCE(MAX(seq), 0) FROM job_events WHERE job_id = :id"),
                          {"id": job_id}).scalar() or 0
    finally:
        db.close()


def append_event(job_id: str, seq: int, etype: str, data: str) -> None:
    """Append one event. Deliberately a single INSERT with the seq supplied by
    the caller: deriving it here with `MAX(seq)` meant an extra scan on every
    token chunk, and the worker is the only writer for a given job anyway. This
    runs once per coalesced chunk, so its cost is paid ~100x per answer."""
    db = _session()
    try:
        db.execute(text("""
            INSERT INTO job_events (job_id, seq, type, data, created_at)
            VALUES (:id, :seq, :t, :d, :now)
        """), {"id": job_id, "seq": seq, "t": etype, "d": data, "now": now_ist()})
        db.commit()
    finally:
        db.close()


def mark_emitted(job_id: str) -> None:
    """Record that output reached the client. Called once, on the first token —
    not per chunk. This is what stops a reclaimed job being re-run and re-billed."""
    db = _session()
    try:
        db.execute(text("UPDATE jobs SET emitted = true WHERE id = :id"), {"id": job_id})
        db.commit()
    finally:
        db.close()


def read_events(job_id: str, after_seq: int = 0, limit: int = 500):
    db = _session()
    try:
        rows = db.execute(text("""
            SELECT seq, type, data FROM job_events
             WHERE job_id = :id AND seq > :after
             ORDER BY seq LIMIT :lim
        """), {"id": job_id, "after": after_seq, "lim": limit}).fetchall()
        return [dict(r._mapping) for r in rows]
    finally:
        db.close()


def finish_job(job_id: str, status: str, result: str | None = None,
               tool: str | None = None, error: str | None = None) -> None:
    db = _session()
    try:
        db.execute(text("""
            UPDATE jobs
               SET status = :s, result = :r, tool = :t, error = :e,
                   finished_at = :now, lease_until = NULL
             WHERE id = :id
        """), {"id": job_id, "s": status, "r": result, "t": tool,
               "e": error, "now": now_ist()})
        db.commit()
    finally:
        db.close()


def get_job(job_id: str, user_id: int):
    """Always scoped by user_id — a job id alone must never be enough to read
    someone else's answer."""
    db = _session()
    try:
        row = db.execute(text("""
            SELECT id, status, tool, result, error, created_at, finished_at
              FROM jobs WHERE id = :id AND user_id = :uid
        """), {"id": job_id, "uid": user_id}).fetchone()
        return dict(row._mapping) if row else None
    finally:
        db.close()


def request_cancel(job_id: str, user_id: int) -> bool:
    """Set the cancel flag. A queued job is cancelled outright; a running one is
    left for the worker to notice on its next heartbeat."""
    db = _session()
    try:
        row = db.execute(text("""
            UPDATE jobs
               SET cancel_requested = true,
                   status      = CASE WHEN status = 'queued' THEN 'cancelled' ELSE status END,
                   finished_at = CASE WHEN status = 'queued' THEN :now ELSE finished_at END
             WHERE id = :id AND user_id = :uid AND status IN ('queued', 'running')
            RETURNING id
        """), {"id": job_id, "uid": user_id, "now": now_ist()}).fetchone()
        db.commit()
        return row is not None
    finally:
        db.close()


def active_job_count(user_id: int) -> int:
    """Backpressure: how many jobs this user already has in flight."""
    db = _session()
    try:
        return db.execute(text("""
            SELECT COUNT(*) FROM jobs
             WHERE user_id = :uid AND status IN ('queued', 'running')
        """), {"uid": user_id}).scalar() or 0
    finally:
        db.close()


def reap_expired() -> int:
    """Reclaim jobs whose lease expired — a worker that crashed or was redeployed.

    A job that emitted nothing is safe to requeue: no tokens reached the user and
    no answer was billed for. A job that already streamed output is marked failed
    instead, because re-running it would pay for the same answer twice. That
    distinction is the whole reason `emitted` exists.
    """
    db = _session()
    try:
        rows = db.execute(text("""
            UPDATE jobs
               SET status = CASE WHEN emitted OR attempts >= :max THEN 'failed' ELSE 'queued' END,
                   error  = CASE WHEN emitted THEN 'Interrupted after partial output; not retried.'
                                 WHEN attempts >= :max THEN 'Gave up after repeated interruptions.'
                                 ELSE NULL END,
                   finished_at = CASE WHEN emitted OR attempts >= :max THEN :now ELSE NULL END,
                   lease_until = NULL,
                   worker_id   = NULL
             WHERE status = 'running' AND lease_until IS NOT NULL AND lease_until < :now
            RETURNING id
        """), {"now": now_ist(), "max": MAX_ATTEMPTS}).fetchall()
        db.commit()
        if rows:
            logger.warning("Reaped %d expired job(s)", len(rows))
        return len(rows)
    finally:
        db.close()


def release_job(job_id: str, emitted: bool) -> None:
    """Hand a job back at shutdown. Nothing streamed yet -> requeue it, and some
    other worker (or this one after a restart) picks it up. Output already
    streamed -> fail it, because re-running would pay for the same answer twice.
    Identical rule to reap_expired(); shutdown is just a tidier crash.
    """
    db = _session()
    try:
        db.execute(text("""
            UPDATE jobs
               SET status      = :s,
                   error       = :e,
                   finished_at = :fin,
                   lease_until = NULL,
                   worker_id   = NULL
             WHERE id = :id AND status = 'running'
        """), {"id": job_id,
               "s": "failed" if emitted else "queued",
               "e": "Interrupted by shutdown after partial output; not retried." if emitted else None,
               "fin": now_ist() if emitted else None})
        db.commit()
        logger.info("Released job %s at shutdown (%s)", job_id, "failed" if emitted else "requeued")
    except Exception as e:
        logger.error("release_job failed for %s: %s", job_id, e)
    finally:
        db.close()
