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
from pydantic import BaseModel, ConfigDict, Field, field_validator
from typing import List
import re
from datetime import datetime, timezone, timedelta
import uuid
import bcrypt
import json
import asyncio
from contextlib import asynccontextmanager
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
from sqlalchemy.exc import IntegrityError
from app.db.database import engine, Base, SessionLocal
from app.db.models import EcommerceAccount, LoginFailure, Chat, Message
from app import jobs
from app.observability import init_observability, init_http_tracing, init_metrics
# Tracing of the agent run itself moved to the worker, which is where the run
# now happens (trace_message / set_output / record_message live there).

# --- JWT Config ---
JWT_SECRET = os.getenv("JWT_SECRET")
if not JWT_SECRET:
    raise ValueError("JWT_SECRET environment variable is not set.")
JWT_ALGORITHM = "HS256"
# Must match SESSION_MS in frontend App.jsx — the shorter of the two wins.
JWT_EXPIRY_HOURS = 12

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
def _identity_key(request: Request) -> str:
    """Rate-limit key: the authenticated user when there is one, else the IP.

    Keying purely on IP got both halves wrong — a NAT'd office or campus shares
    one bucket, while one user on mobile data rotates through many. Signup and
    login have no token yet, so they legitimately fall back to IP.
    """
    auth = request.headers.get("authorization", "")
    if auth.startswith("Bearer "):
        try:
            payload = jwt.decode(auth[7:].strip(), JWT_SECRET, algorithms=[JWT_ALGORITHM])
            return f"user:{payload['sub']}"
        except JWTError:
            pass
    return f"ip:{get_remote_address(request)}"


limiter = Limiter(key_func=_identity_key)

# RUN_WORKER_IN_PROCESS: on a free tier there is no separate worker instance to
# pay for, so the single consumer runs inside the API process. Set it to 0 and
# run `python -m app.worker` as its own service to get real isolation — the code
# is identical either way, only the entrypoint differs.
RUN_WORKER_IN_PROCESS = os.getenv("RUN_WORKER_IN_PROCESS", "1") == "1"


@asynccontextmanager
async def lifespan(_app: FastAPI):
    task, stop = None, asyncio.Event()
    if RUN_WORKER_IN_PROCESS:
        from app.worker import worker_loop
        task = asyncio.create_task(worker_loop(stop))
        logger.info("Job worker started in-process.")
    try:
        yield
    finally:
        stop.set()
        if task:
            try:
                from app.worker import SHUTDOWN_GRACE_S
                await asyncio.wait_for(task, timeout=SHUTDOWN_GRACE_S)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                task.cancel()
                logger.warning("Worker did not stop in time; cancelled.")


app = FastAPI(title="E-commerce Agent API", lifespan=lifespan)
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

def error_response(status_code: int, error: str, detail: str, headers: dict | None = None):
    return JSONResponse(status_code=status_code, headers=headers,
                        content={"status": "error", "error": error, "detail": detail})


def _seconds_to_ist_midnight() -> int:
    now = now_ist()
    tomorrow = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return max(1, int((tomorrow - now).total_seconds()))


@app.exception_handler(RateLimitExceeded)
async def rate_limit_handler(request: Request, exc: RateLimitExceeded):
    # Retry-After turns "come back later" into a number, so a client can back
    # off correctly instead of guessing or hammering.
    return error_response(429, "rate_limit_exceeded", "Too many requests. Please slow down.",
                          headers={"Retry-After": "60"})

@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    return error_response(exc.status_code, "http_error", exc.detail,
                          headers=getattr(exc, "headers", None))

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
# History is client-supplied and billable (it feeds the rewrite prompt).
MAX_HISTORY_ITEMS = 50
MAX_HISTORY_ITEM_CHARS = 4000
MAX_USERNAME_LENGTH = 30
MIN_PASSWORD_LENGTH = 8
MAX_CHAT_TITLE_LENGTH = 60

# DB-backed login lockout (see leads-dashboard approach): N failures in the window
# locks the username, and it holds across instances because it lives in the DB.
MAX_LOGIN_FAILURES = 5
LOGIN_LOCKOUT_MINUTES = 15

# Daily chat credits: 1 credit = 1 message (user question + AI answer). Value in
# .env so it's tunable without a redeploy; caps operator LLM spend per user.
DAILY_MESSAGE_CAP = int(os.getenv("DAILY_MESSAGE_CAP", "5"))
# With a single worker, one user queueing many messages blocks everyone else.
MAX_ACTIVE_JOBS = int(os.getenv("MAX_ACTIVE_JOBS", "2"))
# The real spend bound. 0 disables it. Sits alongside DAILY_MESSAGE_CAP rather
# than replacing it: the message cap limits how OFTEN, this limits how MUCH.
DAILY_TOKEN_CAP = int(os.getenv("DAILY_TOKEN_CAP", "200000"))
# How often a listening client checks the durable event log, and how long a
# quiet stream waits before sending a comment so a proxy doesn't close it.
JOB_EVENT_POLL_S = float(os.getenv("JOB_EVENT_POLL_S", "0.3"))
SSE_KEEPALIVE_S = float(os.getenv("SSE_KEEPALIVE_S", "15"))

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
    # extra="forbid": an unknown field is a client bug or a probe, not something
    # to silently accept and carry into a prompt.
    model_config = ConfigDict(extra="forbid")

    query: str
    history: List[dict] = Field(default_factory=list, max_length=MAX_HISTORY_ITEMS)

    @field_validator("query")
    @classmethod
    def validate_query(cls, v):
        v = v.strip()
        if not v:
            raise ValueError("Query cannot be empty.")
        if len(v) > MAX_QUERY_LENGTH:
            raise ValueError(f"Query must be at most {MAX_QUERY_LENGTH} characters.")
        return v

    @field_validator("history")
    @classmethod
    def validate_history(cls, v):
        """History is echoed back by the client and fed to the rewrite prompt, so
        it is billable input the caller controls. Bound it. memory.py only reads
        the last MAX_HISTORY_MESSAGES turns, so the cap is generous by design."""
        for msg in v:
            content = msg.get("content")
            if isinstance(content, str) and len(content) > MAX_HISTORY_ITEM_CHARS:
                raise ValueError(
                    f"Each history message must be at most {MAX_HISTORY_ITEM_CHARS} characters."
                )
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


def _ist_midnight():
    return now_ist().replace(hour=0, minute=0, second=0, microsecond=0)


def _messages_used_today(user_id: int) -> int:
    """User messages sent since IST midnight — this IS the credit meter:
    remaining = cap - this. No credits table and no reset job; at midnight the
    window moves and the count is 0 again."""
    start = _ist_midnight()
    db = SessionLocal()
    try:
        return (
            db.query(Message)
            .filter(
                Message.user_id == user_id,
                Message.role == "user",
                Message.created_at >= start,
            )
            .count()
        )
    finally:
        db.close()


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


@app.get("/api/account/credits")
def get_credits(current_user: dict = Depends(get_current_user)):
    """Daily message credits: 1 credit = 1 message (your question + the AI's reply).
    Cap per IST day, auto-resets at midnight — no reset job."""
    used = _messages_used_today(current_user["user_id"])
    tokens = jobs.tokens_used_today(current_user["user_id"], _ist_midnight())
    return {"cap": DAILY_MESSAGE_CAP, "used": used, "remaining": max(0, DAILY_MESSAGE_CAP - used),
            "token_cap": DAILY_TOKEN_CAP, "tokens_used": tokens}


def _sse(event_type: str, data, seq: int | None = None) -> str:
    """Format one Server-Sent Event. `seq` is the client's resume cursor: it
    reconnects with ?after=<last seq> and picks up exactly where it left off."""
    payload = {"type": event_type, "data": data}
    if seq is not None:
        payload["seq"] = seq
    return f"data: {json.dumps(payload)}\n\n"


@app.post("/api/chats/{chat_id}/message", status_code=202)
@limiter.limit("30/minute")
async def send_message(
    chat_id: str,
    body: QueryRequest,
    request: Request,
    current_user: dict = Depends(get_current_user),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
):
    """Accept the message and hand back a job id. The agent runs in the worker.

    202, not 200: the answer does not exist yet. Observe it with
    GET /api/jobs/{job_id}/events (stream) or GET /api/jobs/{job_id} (poll).
    Because the job outlives this request, closing the tab no longer destroys
    the answer — it is waiting on reconnect.
    """
    user_id = current_user["user_id"]

    # Idempotency: a retried submit — flaky network, impatient double-tap —
    # returns the ORIGINAL job instead of running the agent (and billing) twice.
    # Checked before every gate, so a retry can't be refused by a limit the
    # first attempt already passed.
    if idempotency_key:
        existing = await asyncio.to_thread(jobs.find_by_idempotency_key, user_id, idempotency_key)
        if existing:
            return {"job_id": existing, "status": "queued", "idempotent_replay": True}

    midnight_in = _seconds_to_ist_midnight()

    # Credit gate before any work. NOTE: the question is now saved at submit, so
    # a run that later fails still counts against the cap. That is deliberate —
    # a failed run has usually already paid the model, and the cap exists to
    # bound spend, not to bill only for successes.
    if await asyncio.to_thread(_messages_used_today, user_id) >= DAILY_MESSAGE_CAP:
        raise HTTPException(
            status_code=429,
            detail=f"Daily limit reached — {DAILY_MESSAGE_CAP} messages a day. Resets at midnight IST.",
            headers={"Retry-After": str(midnight_in)},
        )

    # Token budget. This is the gate that actually bounds spend: a message count
    # charges a 50-token question and a 40k-token one alike, and only tokens
    # track what the provider bills.
    if DAILY_TOKEN_CAP > 0:
        used = await asyncio.to_thread(jobs.tokens_used_today, user_id, _ist_midnight())
        if used >= DAILY_TOKEN_CAP:
            raise HTTPException(
                status_code=429,
                detail="Daily usage limit reached. Resets at midnight IST.",
                headers={"Retry-After": str(midnight_in)},
            )

    # Backpressure. With one worker, a user queueing twenty messages would make
    # everyone else wait behind them; this is the queue's fairness for now.
    if await asyncio.to_thread(jobs.active_job_count, user_id) >= MAX_ACTIVE_JOBS:
        raise HTTPException(
            status_code=429,
            detail=f"You already have {MAX_ACTIVE_JOBS} messages in progress. Wait for one to finish.",
            headers={"Retry-After": "10"},
        )

    def _accept():
        """Save the question and create the job in ONE transaction, so the
        transcript can never show a question with no job behind it."""
        db = SessionLocal()
        try:
            chat = db.query(Chat).filter(Chat.id == chat_id, Chat.user_id == user_id).first()
            if chat is None:
                return None
            db.add(Message(chat_id=chat_id, user_id=user_id, role="user", content=body.query))
            if chat.title in ("New Chat", "", None):
                chat.title = body.query[:25] + ("..." if len(body.query) > 25 else "")
            chat.updated_at = now_ist()
            job_id = jobs.create_job(user_id, chat_id, body.query, body.history,
                                     idempotency_key=idempotency_key, db=db)
            db.commit()
            return job_id
        finally:
            db.close()

    try:
        job_id = await asyncio.to_thread(_accept)
    except IntegrityError:
        # Two concurrent submits raced on the same key; the unique index caught
        # the loser. The winner's job is the answer to both.
        existing = await asyncio.to_thread(jobs.find_by_idempotency_key, user_id, idempotency_key)
        if existing:
            return {"job_id": existing, "status": "queued", "idempotent_replay": True}
        raise
    if job_id is None:
        raise HTTPException(status_code=404, detail="Chat not found.")
    return {"job_id": job_id, "status": "queued"}


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str, current_user: dict = Depends(get_current_user)):
    """Poll one job. Scoped by user — a job id alone never reveals someone
    else's answer."""
    job = jobs.get_job(job_id, current_user["user_id"])
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found.")
    return {
        "job_id": job["id"],
        "status": job["status"],
        "tool": job["tool"],
        "result": job["result"],
        "error": job["error"],
        "created_at": _iso(job["created_at"]),
        "finished_at": _iso(job["finished_at"]),
    }


@app.post("/api/jobs/{job_id}/cancel")
def cancel_job(job_id: str, current_user: dict = Depends(get_current_user)):
    """Request cancellation. A queued job stops immediately; a running one ends
    at the worker's next heartbeat, which also aborts the in-flight model call."""
    if not jobs.request_cancel(job_id, current_user["user_id"]):
        raise HTTPException(status_code=404, detail="No cancellable job with that id.")
    return {"job_id": job_id, "cancel_requested": True}


@app.get("/api/jobs/{job_id}/events")
async def job_events(
    job_id: str,
    request: Request,
    after: int = 0,
    current_user: dict = Depends(get_current_user),
):
    """Replay-then-tail the job's event log as SSE.

    `after` is the client's cursor: reconnecting with the last seq it saw
    replays nothing it already has and then follows the rest. The worker writes
    these events whether or not anyone is listening, so a dropped connection
    costs nothing but the reconnect.
    """
    user_id = current_user["user_id"]
    if jobs.get_job(job_id, user_id) is None:
        raise HTTPException(status_code=404, detail="Job not found.")

    async def event_stream():
        cursor, idle = after, 0.0
        while True:
            if await request.is_disconnected():
                return
            events = await asyncio.to_thread(jobs.read_events, job_id, cursor)
            for ev in events:
                cursor = ev["seq"]
                if ev["type"] in ("done", "error"):
                    payload = json.loads(ev["data"])
                    if ev["type"] == "done":
                        # The client's follow-up chips key off `tool`; it also
                        # wants the saved chat, which only exists now.
                        payload["chat"] = await asyncio.to_thread(_chat_payload, chat_of(job_id, user_id), user_id)
                    yield _sse(ev["type"], payload, cursor)
                    return
                yield _sse(ev["type"], ev["data"], cursor)
            if events:
                idle = 0.0
                continue
            await asyncio.sleep(JOB_EVENT_POLL_S)
            idle += JOB_EVENT_POLL_S
            if idle >= SSE_KEEPALIVE_S:
                idle = 0.0
                yield ": keepalive\n\n"   # stops an idle proxy closing the stream

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        # Without this a buffering proxy holds the whole stream and the
        # token-by-token feel — the entire point — is destroyed.
        headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"},
    )


def chat_of(job_id: str, user_id: int):
    db = SessionLocal()
    try:
        return db.execute(
            text("SELECT chat_id FROM jobs WHERE id = :id AND user_id = :uid"),
            {"id": job_id, "uid": user_id},
        ).scalar()
    finally:
        db.close()


def _chat_payload(chat_id: str, user_id: int):
    db = SessionLocal()
    try:
        chat = db.query(Chat).filter(Chat.id == chat_id, Chat.user_id == user_id).first()
        if chat is None:
            return None
        msgs = db.query(Message).filter(Message.chat_id == chat_id).order_by(Message.id).all()
        return _chat_to_dict(chat, msgs)
    finally:
        db.close()


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
