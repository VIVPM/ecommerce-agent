# Creates the read-write and forced read-only SQLAlchemy engines.
import os
import re
from sqlalchemy import create_engine
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker

DATABASE_URL = os.environ.get("DATABASE_URL")
if not DATABASE_URL:
    raise ValueError("DATABASE_URL environment variable is not set. Cloud database is required.")

DATABASE_URL = re.sub(r"^postgres(ql)?://", "postgresql+psycopg2://", DATABASE_URL)


engine_kwargs = {
    "pool_pre_ping": True,
    "pool_recycle": 300,
    "pool_size": 10,
    "max_overflow": 20
}


APP_STATEMENT_TIMEOUT_MS = int(os.getenv("APP_STATEMENT_TIMEOUT_MS", "30000"))
SQL_STATEMENT_TIMEOUT_MS = int(os.getenv("SQL_STATEMENT_TIMEOUT_MS", "15000"))

engine = create_engine(DATABASE_URL, **engine_kwargs)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


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

