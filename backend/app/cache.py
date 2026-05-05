"""Postgres-backed cache for deterministic LLM outputs.

No external service: it reuses the app's own database. Every function here is
fail-open — if the cache errors, we log and fall through to the real call, so a
cache problem can never break a request.
"""
import hashlib
import logging
from datetime import timedelta

from sqlalchemy import select, delete
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.db.database import SessionLocal
from app.db.models import LLMCache, now_ist

logger = logging.getLogger(__name__)

CACHE_TTL_DAYS = 7


def _key(kind: str, question: str) -> str:
    """Key on the QUESTION ONLY — never the api_key. The output (SQL, tool,
    answer) doesn't depend on whose key produced it, and keying on a
    user-supplied secret would both fragment the cache and store a secret."""
    normalized = " ".join(question.lower().split())
    return hashlib.sha256(f"{kind}:{normalized}".encode("utf-8")).hexdigest()


def cache_get(kind: str, question: str):
    """Return the cached value, or None on miss/expiry. Never raises."""
    try:
        cutoff = now_ist() - timedelta(days=CACHE_TTL_DAYS)
        db = SessionLocal()
        try:
            value = db.execute(
                select(LLMCache.value).where(
                    LLMCache.key == _key(kind, question),
                    LLMCache.created_at >= cutoff,
                )
            ).scalar_one_or_none()
            if value is not None:
                logger.info("cache HIT (%s)", kind)
            return value
        finally:
            db.close()
    except Exception as e:
        logger.warning("cache_get failed (%s) — continuing uncached: %s", kind, e)
        return None


def cache_set(kind: str, question: str, value: str) -> None:
    """Store a cache entry. Never raises. Uses an upsert so two concurrent
    misses on the same question refresh the row instead of colliding on the PK."""
    if not value:
        return
    try:
        db = SessionLocal()
        try:
            ins = pg_insert(LLMCache.__table__).values(
                key=_key(kind, question), kind=kind, value=value, created_at=now_ist()
            )
            db.execute(ins.on_conflict_do_update(
                index_elements=["key"],
                set_={"value": ins.excluded.value, "created_at": ins.excluded.created_at},
            ))
            db.commit()
        finally:
            db.close()
    except Exception as e:
        logger.warning("cache_set failed (%s) — continuing: %s", kind, e)


def cache_purge(kind: str) -> int:
    """Drop every entry of one kind; returns the number removed. Run this after
    changing whatever a cached output was derived from — the FAQ corpus for
    'faq', the SQL prompt/schema for 'sql', the routing instruction for 'route' —
    otherwise stale entries survive until the TTL expires."""
    try:
        db = SessionLocal()
        try:
            n = db.execute(delete(LLMCache).where(LLMCache.kind == kind)).rowcount
            db.commit()
            logger.info("cache purged: %d entries of kind '%s'", n, kind)
            return n
        finally:
            db.close()
    except Exception as e:
        logger.warning("cache_purge failed (%s): %s", kind, e)
        return 0
