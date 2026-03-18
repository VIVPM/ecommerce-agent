import os
from sqlalchemy import create_engine
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker

DATABASE_URL = os.environ.get("DATABASE_URL")
if not DATABASE_URL:
    raise ValueError("DATABASE_URL environment variable is not set. Cloud database is required.")

if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

# Postgres/Neon specific settings to prevent "server closed connection" errors
engine_kwargs = {
    "pool_pre_ping": True,     # Verify connection before usage
    "pool_recycle": 300,       # Recycle connections every 5 minutes
    "pool_size": 5,
    "max_overflow": 10
}

engine = create_engine(DATABASE_URL, **engine_kwargs)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

# Read-only engine for LLM-generated SQL queries.
# Every connection is forced into READ ONLY mode at the Postgres level,
# so even if the LLM injects DROP/INSERT/UPDATE, Postgres will reject it.
from sqlalchemy import event

readonly_engine = create_engine(DATABASE_URL, **engine_kwargs)

@event.listens_for(readonly_engine, "connect")
def _set_readonly(dbapi_conn, connection_record):
    cursor = dbapi_conn.cursor()
    cursor.execute("SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY")
    cursor.close()

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
