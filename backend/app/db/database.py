import os
from sqlalchemy import create_engine
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker

DATABASE_URL = os.environ.get("DATABASE_URL")
if not DATABASE_URL:
    raise ValueError("DATABASE_URL environment variable is not set. Cloud database is required.")

if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

# Pool is 30 per engine; at 15 requests queued once past ~50 concurrent users.
# Neon's -pooler host multiplexes, so a bigger app-side pool is safe.
engine_kwargs = {
    "pool_pre_ping": True,     # Verify connection before usage
    "pool_recycle": 300,       # Recycle connections every 5 minutes
    "pool_size": 10,
    "max_overflow": 20
}

# Every connection carries a statement_timeout. Without one, a single slow query
# pins a pooled connection until Neon gives up, and 30 of those exhaust the pool
# for everyone. Set per-engine because the two carry very different risk: app
# queries are hand-written and fast; LLM-generated SQL is neither.
#
# Applied with a post-connect SET, NOT connect_args `-c statement_timeout`: Neon's
# -pooler endpoint rejects that outright ("unsupported startup parameter in
# options") and takes the app down at boot. Same mechanism the read-only engine
# below already relies on.
APP_STATEMENT_TIMEOUT_MS = int(os.getenv("APP_STATEMENT_TIMEOUT_MS", "30000"))
SQL_STATEMENT_TIMEOUT_MS = int(os.getenv("SQL_STATEMENT_TIMEOUT_MS", "15000"))

engine = create_engine(DATABASE_URL, **engine_kwargs)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

# Engine for LLM-generated SQL. Every connection is READ ONLY at the Postgres
# level, so an injected DROP/UPDATE is rejected by the database itself.
from sqlalchemy import event

readonly_engine = create_engine(DATABASE_URL, **engine_kwargs)

@event.listens_for(engine, "connect")
def _set_app_timeout(dbapi_conn, connection_record):
    cursor = dbapi_conn.cursor()
    cursor.execute(f"SET statement_timeout = {APP_STATEMENT_TIMEOUT_MS}")
    cursor.close()


@event.listens_for(readonly_engine, "connect")
def _set_readonly(dbapi_conn, connection_record):
    cursor = dbapi_conn.cursor()
    cursor.execute("SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY")
    cursor.execute(f"SET statement_timeout = {SQL_STATEMENT_TIMEOUT_MS}")
    cursor.close()

