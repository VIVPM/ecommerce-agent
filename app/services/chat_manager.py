import uuid
from app.db.models import EcommerceAccount
from app.db.database import SessionLocal
import streamlit as st
from datetime import datetime, timezone, timedelta

IST = timezone(timedelta(hours=5, minutes=30))
def now_ist():
    return datetime.now(IST)

def load_user_chats(user_id: int):
    db = SessionLocal()
    try:
        user = db.query(EcommerceAccount).filter(EcommerceAccount.id == user_id).first()
        st.session_state.chats = user.chats if user and user.chats else {}
    finally:
        db.close()

def create_new_chat(st) -> str:
    # Check if an empty "New Chat" already exists
    for chat_id, chat_data in st.session_state.get("chats", {}).items():
        if not chat_data.get("messages") and chat_data.get("title") == "New Chat":
            st.session_state.selected_chat_id = chat_id
            st.session_state.messages = []
            return chat_id

    new_chat_id = str(uuid.uuid4())
    ts = now_ist().isoformat()
    chat_dict = {
        "id": new_chat_id, 
        "title": "New Chat", 
        "messages": [], 
        "created_at": ts, 
        "updated_at": ts
    }
    
    st.session_state.chats[new_chat_id] = chat_dict
    st.session_state.selected_chat_id = new_chat_id
    st.session_state.messages = []
    
    persist_chat(new_chat_id)
    return new_chat_id

def persist_chat(chat_id: str):
    if chat_id not in st.session_state.chats:
        return
        
    chat = st.session_state.chats[chat_id]
    chat["updated_at"] = now_ist().isoformat()
    
    db = SessionLocal()
    try:
        user = db.query(EcommerceAccount).filter(EcommerceAccount.id == st.session_state.user_id).first()
        if user:
            # Create a shallow copy to trigger SQLAlchemy dirty tracking on JSON columns
            chats_dict = dict(user.chats) if user.chats else {}
            chats_dict[chat_id] = chat
            user.chats = chats_dict
            db.commit()
    except Exception as e:
        print(f"Error persisting chat: {e}")
    finally:
        db.close()
