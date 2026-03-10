from sqlalchemy import Column, Integer, String, DateTime, JSON
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
    chats = Column(JSON, default=dict) # Stores a dict of {chat_id: chat_data}
