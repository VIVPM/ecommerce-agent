import os
import sys
from pathlib import Path
from dotenv import load_dotenv

# CRITICAL: Load .env BEFORE importing app modules so DATABASE_URL is set
# when database.py initializes the SQLAlchemy engine
backend_root = Path(__file__).resolve().parent
env_path = backend_root / "app" / ".env"
load_dotenv(dotenv_path=env_path)

from fastapi import FastAPI, HTTPException, Depends, Header
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import List, Optional

# Add the backend directory to sys.path so 'app.xyz' imports work
sys.path.append(str(backend_root))

from app.db.database import engine, Base, SessionLocal
from app.db.models import EcommerceAccount
from app.agent import run_agent
from app.memory import optimize_query
import hashlib
import copy
from sqlalchemy.orm.attributes import flag_modified

# Initialize DB
Base.metadata.create_all(bind=engine)

app = FastAPI(title="E-commerce Agent API")

# Enable CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Allows all origins
    allow_credentials=True,
    allow_methods=["*"],  # Allows all methods
    allow_headers=["*"],  # Allows all headers
)

# --- Pydantic Models ---
class AuthRequest(BaseModel):
    username: str
    password: str

class ChatMessage(BaseModel):
    role: str
    content: str

class ChatSession(BaseModel):
    id: str
    title: str
    messages: List[ChatMessage]
    created_at: str
    updated_at: str

class QueryRequest(BaseModel):
    query: str
    history: List[dict]

class QueryResponse(BaseModel):
    response: str

# Helper to hash passwords
def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()

# Dependency to get DB session
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# --- Auth Endpoints ---
@app.post("/api/auth/signup")
def signup(request: AuthRequest):
    db = SessionLocal()
    try:
        existing_user = db.query(EcommerceAccount).filter(EcommerceAccount.username == request.username).first()
        if existing_user:
            raise HTTPException(status_code=400, detail="Username already exists.")
            
        hashed_password = sha256(request.password)
        new_user = EcommerceAccount(username=request.username, hashed_password=hashed_password, chats={})
        db.add(new_user)
        db.commit()
        db.refresh(new_user)
        
        return {"user_id": new_user.id, "username": new_user.username, "message": "Signup successful"}
    finally:
        db.close()

@app.post("/api/auth/login")
def login(request: AuthRequest):
    db = SessionLocal()
    try:
        user = db.query(EcommerceAccount).filter(EcommerceAccount.username == request.username).first()
        if not user or user.hashed_password != sha256(request.password):
            raise HTTPException(status_code=401, detail="Invalid username or password.")
            
        return {"user_id": user.id, "username": user.username, "message": "Login successful"}
    finally:
        db.close()

# --- Chat Endpoints ---
@app.get("/api/chats/{user_id}")
def get_chats(user_id: int):
    db = SessionLocal()
    try:
        user = db.query(EcommerceAccount).filter(EcommerceAccount.id == user_id).first()
        if not user:
            raise HTTPException(status_code=404, detail="User not found")
        return {"chats": user.chats if user.chats else {}}
    finally:
        db.close()

from datetime import datetime, timezone, timedelta
import uuid

IST = timezone(timedelta(hours=5, minutes=30))
def now_ist():
    return datetime.now(IST)

@app.post("/api/chats/{user_id}/new")
def create_new_chat(user_id: int):
    db = SessionLocal()
    try:
        user = db.query(EcommerceAccount).filter(EcommerceAccount.id == user_id).first()
        if not user:
            raise HTTPException(status_code=404, detail="User not found")
        
        chats_dict = copy.deepcopy(user.chats) if user.chats else {}
        
        # Check if empty chat exists
        for chat_id, chat_data in chats_dict.items():
            if not chat_data.get("messages") and chat_data.get("title") == "New Chat":
                return {"chat_id": chat_id, "chat": chat_data}
                
        new_chat_id = str(uuid.uuid4())
        ts = now_ist().isoformat()
        chat_dict = {
            "id": new_chat_id, 
            "title": "New Chat", 
            "messages": [], 
            "created_at": ts, 
            "updated_at": ts
        }
        
        chats_dict[new_chat_id] = chat_dict
        user.chats = chats_dict
        db.commit()
        
        return {"chat_id": new_chat_id, "chat": chat_dict}
    finally:
        db.close()


@app.post("/api/chats/{user_id}/{chat_id}/message")
def send_message(user_id: int, chat_id: str, request: QueryRequest, x_gemini_api_key: Optional[str] = Header(None)):
    db = SessionLocal()
    try:
        user = db.query(EcommerceAccount).filter(EcommerceAccount.id == user_id).first()
        if not user:
            raise HTTPException(status_code=404, detail="User not found")
            
        chats_dict = copy.deepcopy(user.chats) if user.chats else {}
        if chat_id not in chats_dict:
            raise HTTPException(status_code=404, detail="Chat not found")
            
        current_chat = chats_dict[chat_id]
        
        # Agent inference loop with optional API key override
        optimized_query = optimize_query(request.query, request.history)
        if optimized_query != request.query:
            print(f"Original Query: {request.query} -> Optimized Query: {optimized_query}")
            
        response_text = run_agent(optimized_query, api_key=x_gemini_api_key)
        
        # Update chat state
        current_chat["messages"].append({"role": "user", "content": request.query})
        current_chat["messages"].append({"role": "assistant", "content": response_text})
        
        if current_chat.get("title") == "New Chat" or current_chat.get("title") == "":
            new_title = request.query[:25] + ("..." if len(request.query) > 25 else "")
            current_chat["title"] = new_title
            
        current_chat["updated_at"] = now_ist().isoformat()
        
        chats_dict[chat_id] = current_chat
        user.chats = chats_dict
        flag_modified(user, 'chats')  # Tell SQLAlchemy the JSON column changed
        db.commit()
        
        return {"response": response_text, "chat": current_chat}
    except Exception as e:
        print(f"Error: {e}")
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        db.close()

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
