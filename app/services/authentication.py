import hashlib
from app.db.models import User
from app.db.database import SessionLocal

def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()

def signup(username: str, password: str, st) -> str | None:
    if not username or not password:
        return "Username and password are required."
    
    db = SessionLocal()
    try:
        existing_user = db.query(User).filter(User.username == username).first()
        if existing_user:
            return "Username already exists."
            
        hashed_password = sha256(password)
        new_user = User(username=username, hashed_password=hashed_password)
        db.add(new_user)
        db.commit()
        db.refresh(new_user)
        
        st.session_state.logged_in = True
        st.session_state.user_id = new_user.id
        st.session_state.username = username
        st.session_state.chats = {}
        st.session_state.selected_chat_id = None
        return None
    except Exception as e:
        db.rollback()
        return f"Signup failed: {str(e)[:100]}"
    finally:
        db.close()

def login(username: str, password: str, st) -> str | None:
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.username == username).first()
        if not user or user.hashed_password != sha256(password):
            return "Invalid username or password."

        st.session_state.logged_in = True
        st.session_state.user_id = user.id
        st.session_state.username = username
        
        # We don't load chats here directly into state, that will be handled by chat_manager
        # upon successful login.
        return None
    except Exception as e:
        return f"Login failed: {str(e)[:100]}"
    finally:
        db.close()
