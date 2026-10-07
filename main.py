import asyncio
import json
import logging
import threading
import time
import uuid
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from typing import Deque, Dict, Optional

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from auth import create_access_token, get_current_user, hash_password, verify_password
from core.config import Config
from database import engine, get_db
from models import Base, User
from schemas import AskRequest, TokenResponse, UserLoginSchema, UserSignupSchema

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("meritech.api")

# 1. Automatically create the database tables if they don't exist
Base.metadata.create_all(bind=engine)


# ── Engine lifecycle ──────────────────────────────────────────────────────────
# Loading BGE-large + the reranker takes tens of seconds. It used to run at
# import time, so uvicorn accepted no connections (and /api/status could not
# even answer "loading") until it finished. Now the server is up immediately,
# models load in a background thread, and /api/status reports real progress.
class EngineState:
    def __init__(self) -> None:
        self.engine = None
        self.error: Optional[str] = None
        self.ready = threading.Event()
        self.components: Dict[str, str] = {
            "database": "loading", "embedder": "loading", "reranker": "loading", "visual": "loading",
        }
        self.stats: Dict[str, int] = {}


state = EngineState()


def _boot_engine() -> None:
    try:
        from rag.embedding import Embedder
        from rag.engine import RagEngine
        from rag.reranker import load_reranker
        from rag.store import Store
        from rag.visual import VisualRetriever

        store = Store(Config.DATABASE_URL, Config.EMBED_DIM)
        store.ensure_schema()
        state.stats = store.stats()
        state.components["database"] = "ok"
        if not state.stats.get("units"):
            raise RuntimeError(
                "The knowledge base is empty. Run: python -m ingestion.pipeline ingest data/raw/company_policy.pdf"
            )

        embedder = Embedder()
        state.components["embedder"] = "ok"

        reranker = load_reranker() if Config.RERANK_ENABLED else None
        state.components["reranker"] = "ok" if reranker else "unavailable (falling back to hybrid scores)" if Config.RERANK_ENABLED else "disabled (not needed on this corpus)"

        visual = VisualRetriever()
        if Config.VISUAL_ENABLED:
            visual.store.load()
            state.components["visual"] = (
                f"ok ({len(visual.store)} pages)" if len(visual.store) else "no page index (text-only)"
            )
        else:
            state.components["visual"] = "disabled"

        state.engine = RagEngine(store, embedder, reranker, visual)
        state.ready.set()
        logger.info("RAG engine ready: %s", state.components)
    except Exception as exc:  # surfaced to the UI via /api/status
        logger.exception("engine failed to start")
        state.error = str(exc)
        state.ready.set()


@asynccontextmanager
async def lifespan(_: FastAPI):
    threading.Thread(target=_boot_engine, name="engine-boot", daemon=True).start()
    yield


is_prod = Config.ENVIRONMENT == "production"
app = FastAPI(
    title="Meritech Enterprise API",
    lifespan=lifespan,
    docs_url=None if is_prod else "/docs",
    redoc_url=None,
    openapi_url=None if is_prod else "/openapi.json",
)

# Mount static files here so FastAPI knows how to serve HTML/CSS/JS
app.mount("/frontend", StaticFiles(directory="frontend"), name="frontend")

# CORS used to be "*" together with allow_credentials=True. Origins are now an explicit allow-list.
app.add_middleware(
    CORSMiddleware,
    allow_origins=Config.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    request_id = request.headers.get("x-request-id") or uuid.uuid4().hex[:12]
    response = await call_next(request)
    response.headers["X-Request-ID"] = request_id
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "same-origin"
    return response


@app.exception_handler(RequestValidationError)
async def validation_handler(_: Request, exc: RequestValidationError):
    """Flatten pydantic's list-of-dicts into a single human sentence.

    The UI does `throw new Error(data.detail)`; with a list that rendered as
    "[object Object]".
    """
    messages = []
    for err in exc.errors():
        field = ".".join(str(p) for p in err["loc"] if p != "body")
        msg = err["msg"].removeprefix("Value error, ")
        messages.append(f"{field}: {msg}" if field else msg)
    return JSONResponse(status_code=422, content={"detail": "; ".join(messages) or "Invalid request"})


# ── Per-user rate limiting (sliding window) ───────────────────────────────────
# /api/ask had no authentication at all, so anyone who could reach the port could
# spend the Groq quota. It is now authenticated AND limited per user.
_rate_lock = threading.Lock()
_rate_hits: Dict[str, Deque[float]] = defaultdict(deque)


def check_rate_limit(key: str, limit: int = Config.ASK_RATE_LIMIT_PER_MIN, window: float = 60.0) -> Optional[int]:
    """Return None if allowed, else the number of seconds until the next slot."""
    now = time.monotonic()
    with _rate_lock:
        hits = _rate_hits[key]
        while hits and now - hits[0] > window:
            hits.popleft()
        if len(hits) >= limit:
            return max(1, int(window - (now - hits[0])) + 1)
        hits.append(now)
    return None


# ── Root & health ─────────────────────────────────────────────────────────────

@app.get("/", include_in_schema=False)
def root():
    return RedirectResponse("/frontend/login.html")


@app.get("/healthz", include_in_schema=False)
def healthz():
    """Liveness: the process is up (does not depend on model loading)."""
    return {"ok": True}


@app.get("/api/status")
def get_status():
    """Readiness + per-component state for the UI and for operators."""
    if state.error:
        return {"ready": False, "error": state.error, "components": state.components}
    return {
        "ready": state.ready.is_set(),
        "components": state.components,
        "knowledge": state.stats,
    }


# ── AUTHENTICATION ENDPOINTS ──────────────────────────────────────────────────

def _find_user(db: Session, email: str) -> Optional[User]:
    # Case-insensitive so accounts created before email folding still resolve.
    return db.query(User).filter(func.lower(User.email) == email.lower()).first()


def _issue_token(user: User) -> TokenResponse:
    full_name = f"{user.first_name} {user.last_name}".strip()
    token = create_access_token(
        data={"sub": user.email.lower(), "name": full_name, "first_name": user.first_name, "last_name": user.last_name}
    )
    return TokenResponse(access_token=token, user_name=full_name)


@app.post("/signup", response_model=TokenResponse, status_code=status.HTTP_201_CREATED)
def signup(payload: UserSignupSchema, db: Session = Depends(get_db)):
    if len(payload.email_address) > 120:
        raise HTTPException(status_code=422, detail="email_address: must be at most 120 characters")
    if _find_user(db, payload.email_address):
        raise HTTPException(status_code=400, detail="Email already registered")

    new_user = User(
        first_name=payload.first_name,
        last_name=payload.last_name,
        email=payload.email_address,
        password_hash=hash_password(payload.password),
    )
    db.add(new_user)
    try:
        db.commit()
    except IntegrityError:
        # Two simultaneous signups can both pass the check above; the unique index decides.
        db.rollback()
        raise HTTPException(status_code=400, detail="Email already registered")
    db.refresh(new_user)
    return _issue_token(new_user)


# Hash verified against when the account does not exist, so "unknown email" and
# "wrong password" take the same time (no account-enumeration timing oracle).
_DUMMY_HASH = hash_password("timing-equaliser-not-a-real-password")


@app.post("/login", response_model=TokenResponse)
def login(payload: UserLoginSchema, request: Request, db: Session = Depends(get_db)):
    client = request.client.host if request.client else "unknown"
    wait = check_rate_limit(f"login:{client}", limit=10, window=60.0)
    if wait:
        raise HTTPException(
            status_code=429, detail="Too many login attempts. Please wait a moment.", headers={"Retry-After": str(wait)}
        )

    user = _find_user(db, payload.email_address)
    ok = verify_password(payload.password, user.password_hash if user else _DUMMY_HASH)
    if not user or not ok:
        raise HTTPException(status_code=401, detail="Invalid email or password")
    return _issue_token(user)


# ── RAG CHAT ENDPOINT ─────────────────────────────────────────────────────────

def _sse(event: dict) -> str:
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"


@app.post("/api/ask")
def ask_question(payload: AskRequest, user: User = Depends(get_current_user)):
    wait = check_rate_limit(f"ask:{user.id}")
    if wait:
        raise HTTPException(
            status_code=429,
            detail=f"You're asking too quickly. Try again in {wait}s.",
            headers={"Retry-After": str(wait)},
        )

    history = [(t.role, t.content) for t in payload.history]

    def event_stream():
        try:
            if not state.ready.is_set():
                yield _sse({"type": "status", "content": "The assistant is still starting up…"})
                state.ready.wait(timeout=120)
            if state.error or state.engine is None:
                yield _sse({"type": "error", "content": state.error or "The assistant is not available yet."})
                return
            for event in state.engine.stream_answer(payload.query, history):
                yield _sse(event)
        except Exception:
            logger.exception("unhandled error while streaming an answer")
            yield _sse({"type": "error", "content": "Something went wrong while answering. Please try again."})

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        # X-Accel-Buffering: stop nginx-style proxies from buffering tokens
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
    )


@app.get("/api/pages/{doc_id}/{page}")
def page_image(doc_id: str, page: int, user: User = Depends(get_current_user)):
    """Page preview for citations. Authenticated; never accepts a filesystem path."""
    from rag.render import render_page

    if state.engine is None:
        raise HTTPException(status_code=503, detail="Not ready")
    doc = state.engine.store.get_document(doc_id)
    if not doc:
        raise HTTPException(status_code=404, detail="Unknown document")
    path = render_page(doc["doc_id"], doc["source_path"], page, doc["n_pages"])
    if path is None:
        raise HTTPException(status_code=404, detail="Page not available")
    return FileResponse(path, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=86400"})
