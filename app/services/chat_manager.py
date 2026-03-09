import uuid
from app.db.models import ChatSession
from app.db.database import SessionLocal
import streamlit as st
from datetime import datetime, timezone, timedelta

IST = timezone(timedelta(hours=5, minutes=30))
def now_ist():
    return datetime.now(IST)

def load_user_chats(user_id: int):
    db = SessionLocal()
    try:
        chats = db.query(ChatSession).filter(ChatSession.user_id == user_id).order_by(ChatSession.updated_at.desc()).all()
        
        st.session_state.chats = {
            c.id: {
                "id": c.id,
                "title": c.title,
                "messages": c.messages or [],
                "created_at": c.created_at.isoformat() if c.created_at else "",
                "updated_at": c.updated_at.isoformat() if c.updated_at else ""
            } for c in chats
        }
    finally:
        db.close()

def create_new_chat(st) -> str:
    new_chat_id = str(uuid.uuid4())
    ts = now_ist()
    chat_dict = {
        "id": new_chat_id, 
        "title": "New Chat", 
        "messages": [], 
        "created_at": ts.isoformat(), 
        "updated_at": ts.isoformat()
    }
    
    db = SessionLocal()
    try:
        new_session = ChatSession(
            id=new_chat_id,
            user_id=st.session_state.user_id,
            title="New Chat",
            created_at=ts,
            updated_at=ts,
            messages=[]
        )
        db.add(new_session)
        db.commit()
    except Exception as e:
        print(f"Error creating chat: {e}")
    finally:
        db.close()
        
    st.session_state.chats[new_chat_id] = chat_dict
    st.session_state.selected_chat_id = new_chat_id
    st.session_state.messages = []
    return new_chat_id

def persist_chat(chat_id: str):
    if chat_id not in st.session_state.chats:
        return
        
    chat = st.session_state.chats[chat_id]
    db = SessionLocal()
    try:
        session = db.query(ChatSession).filter(ChatSession.id == chat_id).first()
        if session:
            session.title = chat["title"]
            session.messages = chat["messages"]
            session.updated_at = now_ist()
            db.commit()
    except Exception as e:
        print(f"Error persisting chat: {e}")
    finally:
        db.close()
