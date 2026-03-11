import os
from sqlalchemy import create_engine
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker

DATABASE_URL = os.environ.get("DATABASE_URL")
if DATABASE_URL and DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

# Default to local sqlite if no postgres provided
if not DATABASE_URL:
    DATABASE_URL = "sqlite:///./ecommerce.db"

connect_args = {}
engine_kwargs = {}

if DATABASE_URL.startswith("sqlite"):
    connect_args = {"check_same_thread": False}
else:
    # Postgres/Neon specific settings to prevent "server closed connection" errors
    engine_kwargs = {
        "pool_pre_ping": True,     # Verify connection before usage
        "pool_recycle": 300,       # Recycle connections every 5 minutes
        "pool_size": 5,
        "max_overflow": 10
    }

engine = create_engine(DATABASE_URL, connect_args=connect_args, **engine_kwargs)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
