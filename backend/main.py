import os
import sys
from pathlib import Path
from dotenv import load_dotenv

# CRITICAL: Load .env BEFORE importing app modules so DATABASE_URL is set
# when database.py initializes the SQLAlchemy engine
backend_root = Path(__file__).resolve().parent
env_path = backend_root / "app" / ".env"
load_dotenv(dotenv_path=env_path)

from fastapi import FastAPI, HTTPException, Depends, Header, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import List, Optional
from datetime import datetime, timezone, timedelta
import uuid
import bcrypt
import copy

# JWT
from jose import jwt, JWTError

# Rate limiting
from slowapi import Limiter
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from fastapi.responses import JSONResponse

# Add the backend directory to sys.path so 'app.xyz' imports work
sys.path.append(str(backend_root))

from app.db.database import engine, Base, SessionLocal
from app.db.models import EcommerceAccount
from app.agent import run_agent
from app.memory import optimize_query
from sqlalchemy.orm.attributes import flag_modified

# --- JWT Config ---
JWT_SECRET = os.getenv("JWT_SECRET", "change-me-in-production")
JWT_ALGORITHM = "HS256"
JWT_EXPIRY_HOURS = 1

def create_token(user_id: int, username: str) -> str:
    payload = {
        "sub": str(user_id),
        "username": username,
        "exp": datetime.now(timezone.utc) + timedelta(hours=JWT_EXPIRY_HOURS),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)

def get_current_user(authorization: str = Header(...)) -> dict:
    """Extract and verify JWT from Authorization header. Returns {"user_id": int, "username": str}."""
    token = authorization.removeprefix("Bearer ").strip()
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        return {"user_id": int(payload["sub"]), "username": payload["username"]}
    except JWTError:
        raise HTTPException(status_code=401, detail="Invalid or expired token.")

# Initialize DB
Base.metadata.create_all(bind=engine)

# --- Rate Limiter ---
limiter = Limiter(key_func=get_remote_address)

app = FastAPI(title="E-commerce Agent API")
app.state.limiter = limiter

@app.exception_handler(RateLimitExceeded)
async def rate_limit_handler(request: Request, exc: RateLimitExceeded):
    return JSONResponse(status_code=429, content={"detail": "Too many requests. Please slow down."})

app.add_middleware(SlowAPIMiddleware)

# Enable CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://ecommerce-agent-frontend-kihh.onrender.com",
        "http://localhost:5173"
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- Pydantic Models ---
class AuthRequest(BaseModel):
    username: str
    password: str

class ChatMessage(BaseModel):
    role: str
    content: str

class QueryRequest(BaseModel):
    query: str
    history: List[dict]

class QueryResponse(BaseModel):
    response: str

# --- Password Hashing ---
def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")

def verify_password(password: str, hashed: str) -> bool:
    """Verify against bcrypt hash, with fallback for legacy SHA-256 hashes."""
    if hashed.startswith("$2b$") or hashed.startswith("$2a$"):
        return bcrypt.checkpw(password.encode("utf-8"), hashed.encode("utf-8"))
    # Legacy SHA-256 fallback
    import hashlib
    return hashlib.sha256(password.encode("utf-8")).hexdigest() == hashed

# Dependency to get DB session
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

IST = timezone(timedelta(hours=5, minutes=30))
def now_ist():
    return datetime.now(IST)

# --- Auth Endpoints ---
@app.post("/api/auth/signup")
@limiter.limit("5/minute")
def signup(request: AuthRequest, req: Request):
    db = SessionLocal()
    try:
        existing_user = db.query(EcommerceAccount).filter(EcommerceAccount.username == request.username).first()
        if existing_user:
            raise HTTPException(status_code=400, detail="Username already exists.")

        hashed_password = hash_password(request.password)
        new_user = EcommerceAccount(username=request.username, hashed_password=hashed_password, chats={})
        db.add(new_user)
        db.commit()
        db.refresh(new_user)

        token = create_token(new_user.id, new_user.username)
        return {"token": token, "user_id": new_user.id, "username": new_user.username, "message": "Signup successful"}
    finally:
        db.close()

@app.post("/api/auth/login")
@limiter.limit("10/minute")
def login(request: AuthRequest, req: Request):
    db = SessionLocal()
    try:
        user = db.query(EcommerceAccount).filter(EcommerceAccount.username == request.username).first()
        if not user or not verify_password(request.password, user.hashed_password):
            raise HTTPException(status_code=401, detail="Invalid username or password.")

        # Auto-migrate legacy SHA-256 hashes to bcrypt on successful login
        if not user.hashed_password.startswith("$2b$"):
            user.hashed_password = hash_password(request.password)
            db.commit()

        token = create_token(user.id, user.username)
        return {"token": token, "user_id": user.id, "username": user.username, "message": "Login successful"}
    finally:
        db.close()

# --- Chat Endpoints (JWT-protected) ---
@app.get("/api/chats")
def get_chats(current_user: dict = Depends(get_current_user)):
    db = SessionLocal()
    try:
        user = db.query(EcommerceAccount).filter(EcommerceAccount.id == current_user["user_id"]).first()
        if not user:
            raise HTTPException(status_code=404, detail="User not found")
        return {"chats": user.chats if user.chats else {}}
    finally:
        db.close()

@app.post("/api/chats/new")
def create_new_chat(current_user: dict = Depends(get_current_user)):
    db = SessionLocal()
    try:
        user = db.query(EcommerceAccount).filter(EcommerceAccount.id == current_user["user_id"]).first()
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


@app.post("/api/chats/{chat_id}/message")
@limiter.limit("20/minute")
def send_message(
    chat_id: str,
    request: QueryRequest,
    req: Request,
    x_gemini_api_key: Optional[str] = Header(None),
    current_user: dict = Depends(get_current_user),
):
    db = SessionLocal()
    try:
        user = db.query(EcommerceAccount).filter(EcommerceAccount.id == current_user["user_id"]).first()
        if not user:
            raise HTTPException(status_code=404, detail="User not found")

        chats_dict = copy.deepcopy(user.chats) if user.chats else {}
        if chat_id not in chats_dict:
            raise HTTPException(status_code=404, detail="Chat not found")

        current_chat = chats_dict[chat_id]

        # Agent inference loop with optional API key override
        optimized_query = optimize_query(request.query, request.history, api_key=x_gemini_api_key)
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
    except HTTPException:
        raise
    except Exception as e:
        print(f"Error: {e}")
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        db.close()

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
