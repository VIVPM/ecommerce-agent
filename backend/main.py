import os
import sys
import logging
from pathlib import Path
from dotenv import load_dotenv

# CRITICAL: Load .env BEFORE importing app modules so DATABASE_URL is set
# when database.py initializes the SQLAlchemy engine
backend_root = Path(__file__).resolve().parent
env_path = backend_root / "app" / ".env"
load_dotenv(dotenv_path=env_path)

from fastapi import FastAPI, HTTPException, Depends, Header, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, field_validator
from typing import List, Optional
import re
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
from fastapi.responses import JSONResponse

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(name)s | %(levelname)s | %(message)s",
)
logger = logging.getLogger(__name__)

# Add the backend directory to sys.path so 'app.xyz' imports work
sys.path.append(str(backend_root))

from app.db.database import engine, Base, SessionLocal
from app.db.models import EcommerceAccount
from app.agent import run_agent
from app.memory import optimize_query
from sqlalchemy.orm.attributes import flag_modified

# --- JWT Config ---
JWT_SECRET = os.getenv("JWT_SECRET")
if not JWT_SECRET:
    raise ValueError("JWT_SECRET environment variable is not set.")
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

def error_response(status_code: int, error: str, detail: str):
    return JSONResponse(status_code=status_code, content={"status": "error", "error": error, "detail": detail})

@app.exception_handler(RateLimitExceeded)
async def rate_limit_handler(request: Request, exc: RateLimitExceeded):
    return error_response(429, "rate_limit_exceeded", "Too many requests. Please slow down.")

@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    return error_response(exc.status_code, "http_error", exc.detail)

@app.exception_handler(Exception)
async def generic_exception_handler(request: Request, exc: Exception):
    return error_response(500, "internal_server_error", "An unexpected error occurred. Please try again later.")

from fastapi.exceptions import RequestValidationError

@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    errors = exc.errors()
    messages = [e.get("msg", "").replace("Value error, ", "") for e in errors]
    return error_response(422, "validation_error", messages[0] if len(messages) == 1 else "; ".join(messages))

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

# --- Health Check ---
@app.get("/api/health")
def health_check():
    return {"status": "ok", "service": "ecommerce-agent-api", "version": "1.0.0"}

# --- Pydantic Models ---
MAX_QUERY_LENGTH = 500
MAX_USERNAME_LENGTH = 30
MIN_PASSWORD_LENGTH = 8

class LoginRequest(BaseModel):
    username: str
    password: str

class SignupRequest(BaseModel):
    username: str
    password: str

    @field_validator("username")
    @classmethod
    def validate_username(cls, v):
        v = v.strip()
        if not v or len(v) < 3:
            raise ValueError("Username must be at least 3 characters.")
        if len(v) > MAX_USERNAME_LENGTH:
            raise ValueError(f"Username must be at most {MAX_USERNAME_LENGTH} characters.")
        if not re.match(r'^[a-zA-Z0-9_]+$', v):
            raise ValueError("Username can only contain letters, numbers, and underscores.")
        return v

    @field_validator("password")
    @classmethod
    def validate_password(cls, v):
        if len(v) < MIN_PASSWORD_LENGTH:
            raise ValueError(f"Password must be at least {MIN_PASSWORD_LENGTH} characters.")
        if not re.search(r'[A-Z]', v):
            raise ValueError("Password must contain at least one uppercase letter.")
        if not re.search(r'[a-z]', v):
            raise ValueError("Password must contain at least one lowercase letter.")
        if not re.search(r'[0-9]', v):
            raise ValueError("Password must contain at least one digit.")
        return v

class ChatMessage(BaseModel):
    role: str
    content: str

class QueryRequest(BaseModel):
    query: str
    history: List[dict]
    gemini_api_key: Optional[str] = None

    @field_validator("query")
    @classmethod
    def validate_query(cls, v):
        v = v.strip()
        if not v:
            raise ValueError("Query cannot be empty.")
        if len(v) > MAX_QUERY_LENGTH:
            raise ValueError(f"Query must be at most {MAX_QUERY_LENGTH} characters.")
        return v

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
def signup(body: SignupRequest, request: Request):
    db = SessionLocal()
    try:
        existing_user = db.query(EcommerceAccount).filter(EcommerceAccount.username == body.username).first()
        if existing_user:
            raise HTTPException(status_code=400, detail="Username already exists.")

        hashed_password = hash_password(body.password)
        new_user = EcommerceAccount(username=body.username, hashed_password=hashed_password, chats={})
        db.add(new_user)
        db.commit()
        db.refresh(new_user)

        token = create_token(new_user.id, new_user.username)
        return {"token": token, "user_id": new_user.id, "username": new_user.username, "message": "Signup successful"}
    finally:
        db.close()

@app.post("/api/auth/login")
@limiter.limit("10/minute")
def login(body: LoginRequest, request: Request):
    db = SessionLocal()
    try:
        user = db.query(EcommerceAccount).filter(EcommerceAccount.username == body.username).first()
        if not user or not verify_password(body.password, user.hashed_password):
            raise HTTPException(status_code=401, detail="Invalid username or password.")

        # Auto-migrate legacy SHA-256 hashes to bcrypt on successful login
        if not user.hashed_password.startswith("$2b$"):
            user.hashed_password = hash_password(body.password)
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
    body: QueryRequest,
    request: Request,
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
        optimized_query = optimize_query(body.query, body.history, api_key=body.gemini_api_key)
        if optimized_query != body.query:
            logger.info("Original Query: %s -> Optimized Query: %s", body.query, optimized_query)

        response_text = run_agent(optimized_query, api_key=body.gemini_api_key)

        # Update chat state
        current_chat["messages"].append({"role": "user", "content": body.query})
        current_chat["messages"].append({"role": "assistant", "content": response_text})

        if current_chat.get("title") == "New Chat" or current_chat.get("title") == "":
            new_title = body.query[:25] + ("..." if len(body.query) > 25 else "")
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
        logger.error("Error: %s", e)
        db.rollback()
        raise HTTPException(status_code=500, detail="Something went wrong while processing your request.")
    finally:
        db.close()


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
