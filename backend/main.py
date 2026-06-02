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
from typing import List
import re
from datetime import datetime, timezone, timedelta
import uuid
import bcrypt
import json
import asyncio
from collections import defaultdict

# JWT
from jose import jwt, JWTError

# Rate limiting
from slowapi import Limiter
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
from fastapi.responses import JSONResponse, StreamingResponse

logger = logging.getLogger(__name__)

# Add the backend directory to sys.path so 'app.xyz' imports work
sys.path.append(str(backend_root))

# Structured JSON logging with request_id correlation. Configure BEFORE the app
# modules below emit any import-time logs. (Replaces logging.basicConfig.)
from app.logging_setup import configure_logging, request_context
configure_logging()

from sqlalchemy import text
from app.db.database import engine, Base, SessionLocal
from app.db.models import EcommerceAccount, LoginFailure, Chat, Message
from app.agent import route_query
from app.memory import optimize_query
from app.faq import faq_chain_stream_async
from app.sql import sql_chain_stream_async
from app.compare import compare_saved_stream_async
from app.observability import (
    init_observability, trace_message, set_output, flush as trace_flush,
    init_http_tracing, init_metrics, record_message,
)

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

# Observability — all no-ops unless their env vars are set (see observability.py):
init_observability()      # LLM pipeline -> Langfuse (LANGFUSE_*)
init_http_tracing(app)    # HTTP-layer spans -> Grafana Cloud (GRAFANA_OTLP_*)
init_metrics()            # chat_messages_total counter -> Grafana Cloud


@app.middleware("http")
async def add_request_id(request: Request, call_next):
    """Bind one request_id to every log line for this request, and return it as
    X-Request-ID so a user can quote it in a support request."""
    request_id = uuid.uuid4().hex[:12]
    with request_context(request_id):
        response = await call_next(request)
    response.headers["X-Request-ID"] = request_id
    return response

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

# Enable CORS — origins from env (comma-separated); default keeps current behavior.
_DEFAULT_ORIGINS = "https://ecommerce-agent-frontend-kihh.onrender.com,http://localhost:5173"
ALLOWED_ORIGINS = [o.strip() for o in os.getenv("ALLOWED_ORIGINS", _DEFAULT_ORIGINS).split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- Health Check ---
@app.get("/")
def root():
    return {"status": "ok", "service": "ecommerce-agent-api", "version": "1.0.0"}

@app.get("/api/health")
def health_check():
    return {"status": "ok", "service": "ecommerce-agent-api", "version": "1.0.0"}

# --- Pydantic Models ---
MAX_QUERY_LENGTH = 500
MAX_USERNAME_LENGTH = 30
MIN_PASSWORD_LENGTH = 8
MAX_CHAT_TITLE_LENGTH = 60

# DB-backed login lockout (see leads-dashboard approach): N failures in the window
# locks the username, and it holds across instances because it lives in the DB.
MAX_LOGIN_FAILURES = 5
LOGIN_LOCKOUT_MINUTES = 15

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

class QueryRequest(BaseModel):
    query: str
    history: List[dict]

    @field_validator("query")
    @classmethod
    def validate_query(cls, v):
        v = v.strip()
        if not v:
            raise ValueError("Query cannot be empty.")
        if len(v) > MAX_QUERY_LENGTH:
            raise ValueError(f"Query must be at most {MAX_QUERY_LENGTH} characters.")
        return v

class RenameChatRequest(BaseModel):
    title: str

    @field_validator("title")
    @classmethod
    def validate_title(cls, v):
        v = v.strip()
        if not v:
            raise ValueError("Title cannot be empty.")
        if len(v) > MAX_CHAT_TITLE_LENGTH:
            raise ValueError(f"Title must be at most {MAX_CHAT_TITLE_LENGTH} characters.")
        return v

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

IST = timezone(timedelta(hours=5, minutes=30))
def now_ist():
    return datetime.now(IST)


def _iso(dt):
    return dt.isoformat() if dt else None


def _chat_to_dict(chat, messages):
    """Assemble the exact API shape the frontend already expects, from table rows."""
    return {
        "id": chat.id,
        "title": chat.title,
        "messages": [{"role": m.role, "content": m.content} for m in messages],
        "created_at": _iso(chat.created_at),
        "updated_at": _iso(chat.updated_at),
    }



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
        new_user = EcommerceAccount(username=body.username, hashed_password=hashed_password)
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
        # DB-backed lockout: too many recent failures for this username = locked,
        # independent of IP and holding across API instances.
        cutoff = now_ist() - timedelta(minutes=LOGIN_LOCKOUT_MINUTES)
        recent_failures = db.query(LoginFailure).filter(
            LoginFailure.username == body.username,
            LoginFailure.created_at >= cutoff,
        ).count()
        if recent_failures >= MAX_LOGIN_FAILURES:
            raise HTTPException(
                status_code=429,
                detail=f"Too many failed login attempts. Try again in {LOGIN_LOCKOUT_MINUTES} minutes.",
            )

        user = db.query(EcommerceAccount).filter(EcommerceAccount.username == body.username).first()
        if not user or not verify_password(body.password, user.hashed_password):
            db.add(LoginFailure(username=body.username))
            db.commit()
            raise HTTPException(status_code=401, detail="Invalid username or password.")

        # Successful login: clear this username's failure history.
        db.query(LoginFailure).filter(LoginFailure.username == body.username).delete()

        # Auto-migrate legacy SHA-256 hashes to bcrypt on successful login
        if not user.hashed_password.startswith("$2b$"):
            user.hashed_password = hash_password(body.password)
        db.commit()

        token = create_token(user.id, user.username)
        return {"token": token, "user_id": user.id, "username": user.username, "message": "Login successful"}
    finally:
        db.close()

# --- Chat Endpoints (JWT-protected) — backed by the chats/messages tables ---
@app.get("/api/chats")
def get_chats(current_user: dict = Depends(get_current_user)):
    db = SessionLocal()
    try:
        uid = current_user["user_id"]
        chats = db.query(Chat).filter(Chat.user_id == uid).order_by(Chat.updated_at.desc()).all()
        msgs = db.query(Message).filter(Message.user_id == uid).order_by(Message.id).all()
        by_chat = defaultdict(list)
        for m in msgs:
            by_chat[m.chat_id].append(m)
        return {"chats": {c.id: _chat_to_dict(c, by_chat[c.id]) for c in chats}}
    finally:
        db.close()

@app.post("/api/chats/new")
def create_new_chat(current_user: dict = Depends(get_current_user)):
    db = SessionLocal()
    try:
        uid = current_user["user_id"]
        # Reuse an existing empty "New Chat" so repeated + clicks don't pile up blanks.
        for c in db.query(Chat).filter(Chat.user_id == uid, Chat.title == "New Chat").all():
            if db.query(Message).filter(Message.chat_id == c.id).count() == 0:
                return {"chat_id": c.id, "chat": _chat_to_dict(c, [])}

        new_id = str(uuid.uuid4())
        now = now_ist()
        chat = Chat(id=new_id, user_id=uid, title="New Chat", created_at=now, updated_at=now)
        db.add(chat)
        db.commit()
        return {"chat_id": new_id, "chat": _chat_to_dict(chat, [])}
    finally:
        db.close()


def _sse(event_type: str, data) -> str:
    """Format one Server-Sent Event line."""
    return f"data: {json.dumps({'type': event_type, 'data': data})}\n\n"


@app.post("/api/chats/{chat_id}/message")
@limiter.limit("30/minute")
async def send_message(
    chat_id: str,
    body: QueryRequest,
    request: Request,
    current_user: dict = Depends(get_current_user),
):
    """Streams the agent's answer as Server-Sent Events:
      status -> progress text, token -> answer chunks, done -> saved chat, error -> message.
    Async: the LLM answer streams on the event loop; sync DB/prefix work runs in a thread."""
    user_id = current_user["user_id"]

    def _chat_exists():
        db = SessionLocal()
        try:
            return db.query(Chat).filter(Chat.id == chat_id, Chat.user_id == user_id).first() is not None
        finally:
            db.close()

    def _save(response_text):
        db = SessionLocal()
        try:
            chat = db.query(Chat).filter(Chat.id == chat_id, Chat.user_id == user_id).first()
            if chat is None:
                return None  # deleted mid-stream
            db.add(Message(chat_id=chat_id, user_id=user_id, role="user", content=body.query))
            db.add(Message(chat_id=chat_id, user_id=user_id, role="assistant", content=response_text))
            if chat.title in ("New Chat", "", None):
                chat.title = body.query[:25] + ("..." if len(body.query) > 25 else "")
            chat.updated_at = now_ist()
            db.commit()
            msgs = db.query(Message).filter(Message.chat_id == chat_id).order_by(Message.id).all()
            return _chat_to_dict(chat, msgs)
        finally:
            db.close()

    async def event_stream():
        # Phase 1: validate the chat exists
        if not await asyncio.to_thread(_chat_exists):
            yield _sse("error", "Chat not found.")
            return

        # Phase 2: run the agent and stream the answer. asyncio.to_thread keeps the
        # OTel context, so the threaded calls nest under this span too.
        tool_label, ok = "unknown", False
        try:
            with trace_message(body.query, user_id, chat_id) as span:
                yield _sse("status", "Understanding your query...")
                optimized_query = await asyncio.to_thread(optimize_query, body.query, body.history)
                if optimized_query != body.query:
                    logger.info("Original Query: %s -> Optimized Query: %s", body.query, optimized_query)

                yield _sse("status", "Routing to the right tool...")
                tool, arg = await asyncio.to_thread(route_query, optimized_query)
                tool_label = tool or "unknown"

                if tool == "search_product_database":
                    yield _sse("status", "Searching products...")
                    agen = sql_chain_stream_async(arg)
                elif tool == "compare_saved_products":
                    yield _sse("status", "Reviewing your saved products...")
                    # user-scoped, so it needs user_id and is never cached
                    agen = compare_saved_stream_async(arg, user_id)
                else:
                    yield _sse("status", "Searching the knowledge base...")
                    agen = faq_chain_stream_async(arg)

                parts = []
                async for token in agen:
                    if token:
                        parts.append(token)
                        yield _sse("token", token)
                response_text = "".join(parts) or "I'm sorry, I couldn't generate a response."
                set_output(span, response_text)
                ok = True
        except Exception as e:
            logger.error("Agent streaming failed: %s", e)
            yield _sse("error", "Something went wrong while processing your request.")
            return
        finally:
            # Render can freeze the instance between requests; flush so traces aren't lost.
            trace_flush()
            # One counter point per message for Grafana alerting (rate + error rate + tool mix).
            record_message("ok" if ok else "error", tool_label)

        # Phase 3: persist. Each message is its own row, so there's no shared blob to race on.
        try:
            saved = await asyncio.to_thread(_save, response_text)
        except Exception as e:
            logger.error("Failed to save chat: %s", e)
            yield _sse("error", "Your answer was generated but could not be saved.")
            return
        if saved is None:
            yield _sse("error", "This chat no longer exists.")
            return
        # `tool` and `no_results` drive the client's follow-up chips. If the copy
        # below ever changes, the client falls back to the normal suggestions.
        no_results = response_text.startswith(("I couldn't find any products", "I can't search by"))
        yield _sse("done", {"chat": saved, "tool": tool_label, "no_results": no_results})

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@app.patch("/api/chats/{chat_id}")
def rename_chat(chat_id: str, body: RenameChatRequest, current_user: dict = Depends(get_current_user)):
    db = SessionLocal()
    try:
        chat = db.query(Chat).filter(Chat.id == chat_id, Chat.user_id == current_user["user_id"]).first()
        if not chat:
            raise HTTPException(status_code=404, detail="Chat not found")
        chat.title = body.title
        chat.updated_at = now_ist()
        db.commit()
        msgs = db.query(Message).filter(Message.chat_id == chat_id).order_by(Message.id).all()
        return {"chat": _chat_to_dict(chat, msgs)}
    finally:
        db.close()


# --- Saved products (shortlist) ---
class SaveProductRequest(BaseModel):
    pid: str

    @field_validator("pid")
    @classmethod
    def validate_pid(cls, v):
        v = v.strip().upper()
        if not re.match(r'^[A-Z0-9]{6,32}$', v):
            raise ValueError("Invalid product id.")
        return v


@app.get("/api/saved")
def list_saved(current_user: dict = Depends(get_current_user)):
    """Saved products joined to CURRENT catalog data, so the client can show
    price movement since save without any extra bookkeeping."""
    db = SessionLocal()
    try:
        rows = db.execute(text("""
            SELECT s.pid, s.saved_price, s.created_at,
                   p.title, p.brand, p.price, p.avg_rating, p.availability, p.product_link
              FROM saved_products s
              LEFT JOIN product p ON p.pid = s.pid
             WHERE s.user_id = :uid
             ORDER BY s.created_at DESC
        """), {"uid": current_user["user_id"]}).fetchall()

        saved = []
        for r in rows:
            m = r._mapping
            current, was = m["price"], m["saved_price"]
            saved.append({
                "pid": m["pid"],
                "title": m["title"],
                "brand": m["brand"],
                "price": current,
                "saved_price": was,
                # negative = cheaper than when you saved it
                "price_change": (current - was) if (current is not None and was is not None) else None,
                "avg_rating": m["avg_rating"],
                "availability": m["availability"],
                "product_link": m["product_link"],
                "saved_at": _iso(m["created_at"]),
            })
        return {"saved": saved}
    finally:
        db.close()


@app.post("/api/saved")
def save_product(body: SaveProductRequest, current_user: dict = Depends(get_current_user)):
    db = SessionLocal()
    try:
        product = db.execute(
            text("SELECT title, price FROM product WHERE pid = :pid"), {"pid": body.pid}
        ).fetchone()
        if not product:
            raise HTTPException(status_code=404, detail="Product not found.")

        # Idempotent: saving an already-saved product is a no-op, not an error.
        db.execute(text("""
            INSERT INTO saved_products (user_id, pid, saved_price, created_at)
            VALUES (:uid, :pid, :price, :now)
            ON CONFLICT (user_id, pid) DO NOTHING
        """), {"uid": current_user["user_id"], "pid": body.pid,
               "price": product._mapping["price"], "now": now_ist()})
        db.commit()
        return {"status": "ok", "pid": body.pid, "title": product._mapping["title"]}
    finally:
        db.close()


@app.delete("/api/saved/{pid}")
def unsave_product(pid: str, current_user: dict = Depends(get_current_user)):
    db = SessionLocal()
    try:
        res = db.execute(
            text("DELETE FROM saved_products WHERE user_id = :uid AND pid = :pid"),
            {"uid": current_user["user_id"], "pid": pid.strip().upper()},
        )
        db.commit()
        if not res.rowcount:
            raise HTTPException(status_code=404, detail="Not saved.")
        return {"status": "ok", "removed": pid}
    finally:
        db.close()


@app.delete("/api/chats/{chat_id}")
def delete_chat(chat_id: str, current_user: dict = Depends(get_current_user)):
    db = SessionLocal()
    try:
        chat = db.query(Chat).filter(Chat.id == chat_id, Chat.user_id == current_user["user_id"]).first()
        if not chat:
            raise HTTPException(status_code=404, detail="Chat not found")
        db.query(Message).filter(Message.chat_id == chat_id).delete()
        db.delete(chat)
        db.commit()
        return {"status": "ok", "deleted": chat_id}
    finally:
        db.close()


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
