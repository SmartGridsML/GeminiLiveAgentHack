"""
PitchMirror — FastAPI backend with ADK multi-agent architecture.

Session lifecycle:
  1. Browser connects via WebSocket /ws
  2. LiveCoachAgent (ADK LlmAgent) starts via runner.run_live()
     - Browser sends binary frames: 0x01=audio PCM, 0x02=webcam JPEG, 0x04=screen JPEG, 0x05=slide JPEG, 0x03=stop
     - Gemini calls flag_issue() tool → metric updates pushed to browser as JSON
     - Gemini coach audio → binary frame 0x10 to browser (no base64)
  3. Browser sends 0x03 → live session ends
  4. Post-session agents run in parallel (delivery/content/research) then synthesis
  5. Final scorecard + written report sent to browser, persisted to Firestore

Binary frame protocol (client→server):
  byte 0: 0x01 = audio PCM 16-bit 16kHz  |  0x02 = webcam JPEG
          0x04 = screen-share JPEG        |  0x05 = uploaded-slide JPEG
          0x03 = stop
  bytes 1..N: payload

Binary frame protocol (server→client):
  byte 0: 0x10 = coach audio PCM 24kHz
  bytes 1..N: payload
  (all other server→client messages are JSON text frames)
"""
import asyncio
import base64
import os
import ipaddress
import json
import hashlib
import logging
import re
import time
import uuid
from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager

from dotenv import load_dotenv

load_dotenv()

from fastapi import FastAPI, File, HTTPException, Query, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from google.adk.agents.live_request_queue import LiveRequestQueue
from google.adk.agents.run_config import RunConfig, StreamingMode
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

from backend.agents.live_coach import make_live_coach_agent
from backend.coach import (
    VALID_PERSONAS,
    normalize_coach_mode,
    normalize_delivery_context,
    normalize_primary_goal,
)
from backend.agents.post_session import (
    CONTENT_ANALYST,
    DELIVERY_ANALYST,
    RESEARCH_AGENT,
    SYNTHESIS_AGENT,
)
from backend.db import get_db
from backend.multimodal import extract_image_prompts, generate_session_assets
from backend.scorecard import build_scorecard
from backend.session_state import SessionState

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class _NormalLiveCloseFilter(logging.Filter):
    """
    Suppress noisy ADK "unexpected error" logs for normal WS close codes.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage().lower()
        if "an unexpected error occurred in live flow:" not in msg:
            return True
        return not any(token in msg for token in ("1000", "1001", "connectionclosedok"))


# Drop false-positive normal-close errors from ADK internals while preserving
# actual exception logs.
logging.getLogger("google_adk.google.adk.flows.llm_flows.base_llm_flow").addFilter(
    _NormalLiveCloseFilter()
)

APP_NAME = "pitchmirror"
POST_APP_NAME = "pitchmirror_post"

# Minimum gap between any two coach audio responses forwarded to the browser.
# This is the hard server-side gate — the tool-level cooldown in tools.py is a
# softer hint to the model, but native audio models can speak without calling
# the tool first, so we enforce silence here regardless.
#
# Set to 45s (midpoint between tools.py _GLOBAL_COOLDOWN_S=30 and
# _PER_TYPE_COOLDOWN_S=90) so spontaneous speech (bypass of flag_issue) can't
# refire before the per-type gate would have expired. Previous value of 28s
# was shorter than the global tool cooldown, allowing repeated identical
# feedback when the user paused for ≥30s between breaks.
COACH_SPEECH_COOLDOWN_S = 45.0

# Hard word-count cap per coach turn.  The system prompt says "4-10 words,
# never above 12" — this server-side gate enforces that even if the model
# ignores the instruction and produces a longer response.
MAX_COACH_WORDS_PER_TURN = 20

# ── Singletons (created once at startup) ─────────────────────────

# Firestore client — reused across all sessions
_db = get_db()

# ── Binary frame type IDs ─────────────────────────────────────────
_T_AUDIO_IN  = 0x01   # client→server: PCM 16-bit 16kHz
_T_VIDEO_IN  = 0x02   # client→server: JPEG frame
_T_STOP      = 0x03   # client→server: end session
_T_SCREEN_IN = 0x04   # client→server: screen-share JPEG frame
_T_SLIDE_IN  = 0x05   # client→server: uploaded slide JPEG frame
_T_AUDIO_OUT = 0x10   # server→client: coach PCM 24kHz


def _int_env(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        logger.warning("Invalid %s=%r; using default=%d", name, raw, default)
        return default


_SLIDE_MAX_UPLOAD_BYTES = _int_env("SLIDE_MAX_UPLOAD_BYTES", 20 * 1024 * 1024)
_SLIDE_MAX_PAGES = _int_env("SLIDE_MAX_PAGES", 20)
_SLIDE_MAX_WIDTH = _int_env("SLIDE_MAX_WIDTH", 1024)
_SLIDE_STORE_MAX_USERS = _int_env("SLIDE_STORE_MAX_USERS", 64)
_SLIDE_STORE_TTL_S = _int_env("SLIDE_STORE_TTL_S", 6 * 60 * 60)
_slides_store: dict[str, dict] = {}

# Thread pool for CPU-bound PDF rendering — keeps async event loop unblocked.
_pdf_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="pdf-conv")


def _bool_env(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _csv_env(name: str, default: str) -> list[str]:
    raw = os.getenv(name, default)
    values = [item.strip() for item in raw.split(",") if item.strip()]
    return values or [default]


def _client_ip(forwarded_for: str | None, fallback: str | None) -> str:
    if forwarded_for:
        # X-Forwarded-For may contain a spoofable chain. Cloud Run appends the
        # real connecting IP as the LAST entry — never trust the first entry,
        # which the client controls. Iterate from the right and return the first
        # valid IP we find (which is the last non-empty segment).
        for raw in reversed(forwarded_for.split(",")):
            candidate = raw.strip()
            try:
                ipaddress.ip_address(candidate)
                return candidate
            except ValueError:
                continue

    if fallback:
        try:
            ipaddress.ip_address(fallback)
            return fallback
        except ValueError:
            pass

    return "unknown"


def _bool_query(raw: str | None, default: bool = False) -> bool:
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _is_normal_live_close(exc: Exception) -> bool:
    """
    ADK/GenAI may surface graceful websocket termination as APIError(1000/1001).
    Treat these as normal end-of-session, not runtime failures.
    """
    status_code = getattr(exc, "status_code", None)
    code = getattr(exc, "code", None)
    if status_code in {1000, 1001} or code in {1000, 1001}:
        return True

    class_name = exc.__class__.__name__.lower()
    if "connectionclosedok" in class_name:
        return True

    err_str = str(exc).lower()
    return any(
        token in err_str
        for token in (
            "1000 none",
            "1001 none",
            "sent 1000 (ok)",
            "received 1000 (ok)",
            "connectionclosedok",
        )
    )


_USER_ID_RE = re.compile(r"^[A-Za-z0-9_.-]{3,64}$")
_CTRL_TOKEN_RE = re.compile(r"<ctrl\d+>", re.IGNORECASE)
_ASR_TAG_RE = re.compile(r"<(?:spoken_[^>]*|noise|inaudible|unk|unknown)>", re.IGNORECASE)
_CONTROL_CHAR_RE = re.compile(r"[\x00-\x08\x0B\x0C\x0E-\x1F\x7F]")
from backend.pipeline_utils import (
    extract_ai_scores as _extract_ai_scores,
    validate_synthesis as _validate_synthesis,
    missing_synthesis_sections as _missing_synthesis_sections,
)


def _hash_scope(value: str, prefix: str) -> str:
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]
    return f"{prefix}-{digest}"


def _normalize_user_id(raw_user_id: str | None) -> str | None:
    if not raw_user_id:
        return None
    candidate = raw_user_id.strip()
    if _USER_ID_RE.fullmatch(candidate):
        return candidate
    return None


def _resolve_user_scope(raw_user_id: str | None, *, client_ip: str) -> str:
    explicit = _normalize_user_id(raw_user_id)
    if explicit:
        return explicit
    return _hash_scope(client_ip or "unknown", "anon")


def _sanitize_transcript_text(text: str | None) -> str:
    """
    Remove low-level ASR/control artifacts before forwarding transcript text to UI.
    Examples: <ctrl46>, <spoken_no...>, non-printable control chars.
    """
    raw = (text or "").strip()
    if not raw:
        return ""
    clean = _CONTROL_CHAR_RE.sub(" ", raw)
    clean = _CTRL_TOKEN_RE.sub(" ", clean)
    clean = _ASR_TAG_RE.sub(" ", clean)
    clean = re.sub(r"\s+", " ", clean).strip()
    return clean


def _request_user_scope(request: Request) -> str:
    raw_user = request.headers.get("x-user-id") or request.query_params.get("user")
    client_ip = _client_ip(
        request.headers.get("x-forwarded-for"),
        request.client.host if request.client else None,
    )
    return _resolve_user_scope(raw_user, client_ip=client_ip)


def _filter_sessions_for_user(sessions: list[dict], user_id: str, limit: int | None = None) -> list[dict]:
    filtered = [s for s in sessions if isinstance(s, dict) and s.get("user_id") == user_id]
    if limit is None:
        return filtered
    return filtered[: max(0, limit)]


def _prune_slides_store(now: float | None = None) -> None:
    current = now or time.time()
    stale_users = [
        user
        for user, payload in _slides_store.items()
        if current - float(payload.get("created_at", 0.0)) > _SLIDE_STORE_TTL_S
    ]
    for user in stale_users:
        _slides_store.pop(user, None)

    if len(_slides_store) <= _SLIDE_STORE_MAX_USERS:
        return

    keep = sorted(
        _slides_store.items(),
        key=lambda kv: float(kv[1].get("created_at", 0.0)),
        reverse=True,
    )[:_SLIDE_STORE_MAX_USERS]
    _slides_store.clear()
    _slides_store.update(dict(keep))


def _slides_for_user(user_id: str) -> dict | None:
    _prune_slides_store()
    payload = _slides_store.get(user_id)
    if not payload:
        return None
    slides = payload.get("slides")
    if not isinstance(slides, list) or not slides:
        return None
    return payload


def _store_slides_for_user(user_id: str, file_name: str, slides: list[bytes]) -> dict:
    now = time.time()
    payload = {
        "deck_id": f"deck-{uuid.uuid4().hex[:12]}",
        "created_at": now,
        "file_name": file_name,
        "slides": slides,
    }
    _slides_store[user_id] = payload
    _prune_slides_store(now)
    return payload


def _convert_pdf_to_jpegs(pdf_bytes: bytes) -> list[bytes]:
    if not pdf_bytes:
        raise HTTPException(status_code=400, detail="Uploaded file is empty.")
    if len(pdf_bytes) > _SLIDE_MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"PDF too large. Max {_SLIDE_MAX_UPLOAD_BYTES // (1024 * 1024)}MB.",
        )

    try:
        import fitz  # PyMuPDF
    except ImportError as exc:
        raise HTTPException(status_code=500, detail="PyMuPDF not installed in backend image.") from exc

    try:
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Invalid PDF upload.") from exc

    if doc.page_count == 0:
        raise HTTPException(status_code=400, detail="PDF has no pages.")

    page_count = min(doc.page_count, _SLIDE_MAX_PAGES)
    if doc.page_count > _SLIDE_MAX_PAGES:
        logger.info("Slide upload truncated from %d to %d pages", doc.page_count, _SLIDE_MAX_PAGES)

    slides: list[bytes] = []
    for idx in range(page_count):
        page = doc.load_page(idx)
        width = max(1.0, float(page.rect.width))
        scale = max(1.0, min(2.5, _SLIDE_MAX_WIDTH / width))
        pix = page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)
        jpeg = pix.tobytes(output="jpeg", jpg_quality=82)
        if not jpeg:
            continue
        slides.append(jpeg)

    doc.close()
    if not slides:
        raise HTTPException(status_code=400, detail="Could not extract slide images from PDF.")
    return slides


class SlidingWindowLimiter:
    """Per-key sliding-window request limiter."""

    def __init__(self, limit: int, window_seconds: int):
        self.limit = max(1, limit)
        self.window_seconds = max(1, window_seconds)
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        hits = self._hits[key]
        cutoff = now - self.window_seconds
        while hits and hits[0] <= cutoff:
            hits.popleft()

        # Prune empty entries to bound memory when many distinct IPs have been seen.
        if len(self._hits) > 5_000:
            stale = [k for k, v in self._hits.items() if not v]
            for k in stale:
                del self._hits[k]

        if len(hits) >= self.limit:
            return False

        hits.append(now)
        return True


class ConcurrentSessionLimiter:
    """Per-key concurrent session cap."""

    def __init__(self, max_concurrent: int):
        self.max_concurrent = max(1, max_concurrent)
        self._active: dict[str, int] = defaultdict(int)

    def acquire(self, key: str) -> bool:
        if self._active[key] >= self.max_concurrent:
            return False
        self._active[key] += 1
        return True

    def release(self, key: str) -> None:
        current = self._active.get(key, 0)
        if current <= 1:
            self._active.pop(key, None)
        else:
            self._active[key] = current - 1


class PerSecondTrafficLimiter:
    """Simple per-session throughput guard for WebSocket frames."""

    def __init__(self, max_messages_per_sec: int, max_bytes_per_sec: int):
        self.max_messages_per_sec = max(1, max_messages_per_sec)
        self.max_bytes_per_sec = max(1, max_bytes_per_sec)
        self._window_start = time.monotonic()
        self._messages = 0
        self._bytes = 0

    def allow(self, frame_bytes: int) -> bool:
        now = time.monotonic()
        if now - self._window_start >= 1.0:
            self._window_start = now
            self._messages = 0
            self._bytes = 0

        self._messages += 1
        self._bytes += max(0, frame_bytes)
        return (
            self._messages <= self.max_messages_per_sec
            and self._bytes <= self.max_bytes_per_sec
        )


ENVIRONMENT = os.getenv("ENVIRONMENT", "development").lower()
CORS_ALLOWED_ORIGINS = _csv_env("CORS_ALLOWED_ORIGINS", "*")
_CORS_ALLOW_CREDENTIALS = CORS_ALLOWED_ORIGINS != ["*"]
API_BEARER_TOKEN = os.getenv("API_BEARER_TOKEN", "").strip()
AUTH_ENABLED = bool(API_BEARER_TOKEN)


def _extract_auth_token(
    authorization_header: str | None,
    api_key_header: str | None,
    query_token: str | None = None,
) -> str | None:
    if query_token:
        return query_token.strip()

    if api_key_header:
        return api_key_header.strip()

    if not authorization_header:
        return None

    parts = authorization_header.strip().split(" ", 1)
    if len(parts) == 2 and parts[0].lower() == "bearer":
        return parts[1].strip()
    return authorization_header.strip()


def _token_valid(token: str | None) -> bool:
    if not AUTH_ENABLED:
        return True
    return bool(token and token == API_BEARER_TOKEN)


def _auth_required_for_path(path: str) -> bool:
    return path.startswith("/api")


def _ws_origin_allowed(origin: str | None) -> bool:
    """
    Return True when the WebSocket upgrade Origin is acceptable.

    In development (CORS_ALLOWED_ORIGINS == ["*"]) every origin is allowed.
    In production the Origin header must be present and in the allow-list.
    """
    if CORS_ALLOWED_ORIGINS == ["*"]:
        return True
    if not origin:
        return False
    return origin in CORS_ALLOWED_ORIGINS

HTTP_MAX_REQUESTS_PER_MINUTE = _int_env("HTTP_MAX_REQUESTS_PER_MINUTE", 120)
WS_MAX_CONNECTIONS_PER_MINUTE = _int_env("WS_MAX_CONNECTIONS_PER_MINUTE", 20)
WS_MAX_CONCURRENT_PER_IP = _int_env("WS_MAX_CONCURRENT_PER_IP", 2)
WS_MAX_MESSAGES_PER_SECOND = _int_env("WS_MAX_MESSAGES_PER_SECOND", 30)
WS_MAX_BYTES_PER_SECOND = _int_env("WS_MAX_BYTES_PER_SECOND", 1_000_000)
WS_MAX_BINARY_FRAME_BYTES = _int_env("WS_MAX_BINARY_FRAME_BYTES", 600_000)
WS_MAX_TEXT_FRAME_BYTES = _int_env("WS_MAX_TEXT_FRAME_BYTES", 4_096)
ENABLE_SCREEN_SHARE = _bool_env("ENABLE_SCREEN_SHARE", True)
ENABLE_IMAGE_GENERATION = _bool_env("ENABLE_IMAGE_GENERATION", True)
IMAGE_GENERATION_TIMEOUT_S = _int_env("IMAGE_GENERATION_TIMEOUT_S", 24)
IMAGE_GENERATION_RETRIES = _int_env("IMAGE_GENERATION_RETRIES", 1)
DEMO_MODE_DEFAULT = _bool_env("DEMO_MODE_DEFAULT", False)

_http_rate_limiter = SlidingWindowLimiter(
    limit=HTTP_MAX_REQUESTS_PER_MINUTE,
    window_seconds=60,
)
_ws_connection_limiter = SlidingWindowLimiter(
    limit=WS_MAX_CONNECTIONS_PER_MINUTE,
    window_seconds=60,
)
_ws_concurrent_limiter = ConcurrentSessionLimiter(WS_MAX_CONCURRENT_PER_IP)


def _make_run_config() -> RunConfig:
    return RunConfig(
        streaming_mode=StreamingMode.BIDI,
        response_modalities=[types.Modality.AUDIO],
        input_audio_transcription=types.AudioTranscriptionConfig(),
        output_audio_transcription=types.AudioTranscriptionConfig(),
        session_resumption=types.SessionResumptionConfig(),
        context_window_compression=types.ContextWindowCompressionConfig(
            sliding_window=types.SlidingWindow(),
        ),
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("PitchMirror starting up")
    if not _db._enabled:
        logger.warning(
            "Firestore is DISABLED — session history will not persist across restarts. "
            "Set GOOGLE_CLOUD_PROJECT to enable durable storage."
        )
    yield
    logger.info("PitchMirror shutting down")
    # Cancel queued (not-yet-started) PDF jobs; running conversions finish naturally.
    _pdf_executor.shutdown(wait=False, cancel_futures=True)


app = FastAPI(title="PitchMirror", version="2.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ALLOWED_ORIGINS,
    allow_credentials=_CORS_ALLOW_CREDENTIALS,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount("/static", StaticFiles(directory="frontend"), name="static")


@app.middleware("http")
async def http_hardening_middleware(request: Request, call_next):
    if _auth_required_for_path(request.url.path):
        token = _extract_auth_token(
            request.headers.get("authorization"),
            request.headers.get("x-api-key"),
        )
        if not _token_valid(token):
            return JSONResponse(status_code=401, content={"detail": "Unauthorized"})

    client_ip = _client_ip(
        request.headers.get("x-forwarded-for"),
        request.client.host if request.client else None,
    )
    if not _http_rate_limiter.allow(client_ip):
        return JSONResponse(status_code=429, content={"detail": "Too Many Requests"})

    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault("Permissions-Policy", "camera=(self), microphone=(self)")
    if ENVIRONMENT == "production":
        response.headers.setdefault(
            "Strict-Transport-Security",
            "max-age=31536000; includeSubDomains",
        )
    return response


@app.get("/")
async def root():
    return FileResponse("frontend/landing.html")


@app.get("/app")
async def app_page():
    return FileResponse("frontend/index.html")


@app.get("/health")
async def health():
    return {"status": "ok", "service": "pitchmirror", "version": "2.0.0-adk"}


@app.get("/api/health")
async def api_health():
    return await health()


@app.post("/api/slides/upload")
async def api_upload_slides(request: Request, file: UploadFile = File(...)):
    user_id = _request_user_scope(request)
    filename = (file.filename or "slides.pdf").strip() or "slides.pdf"
    lower_name = filename.lower()
    content_type = (file.content_type or "").lower()
    if not lower_name.endswith(".pdf") and "pdf" not in content_type:
        raise HTTPException(status_code=400, detail="Only PDF uploads are supported.")

    pdf_bytes = await file.read()
    # Run CPU-heavy PDF rendering in a thread pool to avoid blocking the event loop.
    loop = asyncio.get_running_loop()
    slides = await loop.run_in_executor(_pdf_executor, _convert_pdf_to_jpegs, pdf_bytes)
    deck = _store_slides_for_user(user_id, filename, slides)

    # Return only the first slide eagerly; the rest are available via
    # GET /api/slides/{deck_id}/{index} and should be fetched lazily/in background.
    first_slide = deck["slides"][0] if deck["slides"] else None
    eager_slides = []
    if first_slide:
        eager_slides = [{
            "index": 0,
            "mime_type": "image/jpeg",
            "data_base64": base64.b64encode(first_slide).decode("ascii"),
        }]
    return {
        "deck_id": deck["deck_id"],
        "file_name": deck["file_name"],
        "total_slides": len(deck["slides"]),
        "current_slide_index": 0,
        "slides": eager_slides,
    }


@app.get("/api/slides/{deck_id}/{slide_index}")
async def api_get_slide(request: Request, deck_id: str, slide_index: int):
    """Return a single slide by index for lazy/background loading by the browser."""
    if not deck_id or len(deck_id) > 64:
        raise HTTPException(status_code=400, detail="Invalid deck_id")
    user_id = _request_user_scope(request)
    payload = _slides_for_user(user_id)
    if not payload or payload.get("deck_id") != deck_id:
        raise HTTPException(status_code=404, detail="Slide deck not found")
    slides = payload["slides"]
    if slide_index < 0 or slide_index >= len(slides):
        raise HTTPException(status_code=404, detail="Slide index out of range")
    return {
        "index": slide_index,
        "mime_type": "image/jpeg",
        "data_base64": base64.b64encode(slides[slide_index]).decode("ascii"),
    }


def _session_summary(payload: dict) -> dict:
    categories = payload.get("categories") or {}
    return {
        "session_id": payload.get("session_id"),
        "user_id": payload.get("user_id"),
        "created_at": payload.get("created_at"),
        "coach_mode": payload.get("coach_mode", "general"),
        "delivery_context": payload.get("delivery_context", "virtual"),
        "primary_goal": payload.get("primary_goal", "balanced"),
        "screen_enabled": bool(payload.get("screen_enabled")),
        "total_slides": int(payload.get("total_slides") or 0),
        "current_slide_index": int(payload.get("current_slide_index") or 0),
        "duration_seconds": payload.get("duration_seconds", 0),
        "overall_score": payload.get("overall_score", 0),
        "filler_score": (categories.get("filler_words") or {}).get("score"),
        "eye_score": (categories.get("eye_contact") or {}).get("score"),
        "pace_score": (categories.get("pace") or {}).get("score"),
        "clarity_score": (categories.get("clarity") or {}).get("score"),
        "visual_score": (categories.get("visual_delivery") or {}).get("score"),
    }


async def _fetch_user_history(user_id: str, limit: int = 1) -> str:
    """Fetch recent session data to use as cached context."""
    try:
        sessions = await _db.list_recent(limit=limit, user_id=user_id)
        if not sessions:
            return ""

        blocks = []
        for s in sessions:
            score = s.get("overall_score", 0)
            report = s.get("final_report", "")
            # Only use the top fixes / summary part of the report to keep it concise
            summary = report.split("**Delivery**")[0].strip() if "**Delivery**" in report else report[:300]
            blocks.append(f"- Previous session (Score: {score}/100):\n{summary}")
        return "\n\n".join(blocks)
    except Exception as e:
        logger.warning("Failed to fetch user history: %s", e)
        return ""


@app.get("/api/sessions/stats")
async def api_session_stats(request: Request, limit: int = Query(default=20, ge=1, le=100)):
    user_id = _request_user_scope(request)
    sessions = await _db.list_recent(limit=limit, user_id=user_id)
    if not sessions:
        return {
            "count": 0,
            "avg_overall_score": 0,
            "avg_duration_seconds": 0,
            "best_score": 0,
        }

    scores = [int(s.get("overall_score") or 0) for s in sessions]
    durations = [float(s.get("duration_seconds") or 0) for s in sessions]
    return {
        "count": len(sessions),
        "avg_overall_score": round(sum(scores) / len(scores), 1),
        "avg_duration_seconds": round(sum(durations) / len(durations), 1),
        "best_score": max(scores),
    }


@app.get("/api/sessions")
async def api_list_sessions(request: Request, limit: int = Query(default=20, ge=1, le=100)):
    user_id = _request_user_scope(request)
    sessions = await _db.list_recent(limit=limit, user_id=user_id)
    return {"sessions": [_session_summary(s) for s in sessions]}


@app.get("/api/sessions/{session_id}")
async def api_get_session(request: Request, session_id: str):
    sid = session_id.strip()
    if not sid or len(sid) > 128:
        raise HTTPException(status_code=400, detail="Invalid session_id")

    user_id = _request_user_scope(request)
    payload = await _db.get_session(sid)
    if not payload:
        raise HTTPException(status_code=404, detail="Session not found")
    if payload.get("user_id") != user_id:
        raise HTTPException(status_code=404, detail="Session not found")
    return payload


# ── WebSocket session handler ─────────────────────────────────────

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    client_ip = _client_ip(
        websocket.headers.get("x-forwarded-for"),
        websocket.client.host if websocket.client else None,
    )

    token = _extract_auth_token(
        websocket.headers.get("authorization"),
        websocket.headers.get("x-api-key"),
        websocket.query_params.get("token"),
    )
    if not _token_valid(token):
        logger.warning("WS auth rejected: ip=%s", client_ip)
        await websocket.close(code=1008, reason="Unauthorized")
        return

    origin = websocket.headers.get("origin")
    if not _ws_origin_allowed(origin):
        logger.warning("WS origin rejected: origin=%r ip=%s", origin, client_ip)
        await websocket.close(code=1008, reason="Origin not allowed")
        return

    if not _ws_connection_limiter.allow(client_ip):
        logger.warning("WS connection rate-limited: ip=%s", client_ip)
        await websocket.close(code=1008, reason="Connection rate limit exceeded")
        return

    connection_acquired = _ws_concurrent_limiter.acquire(client_ip)
    if not connection_acquired:
        logger.warning("WS concurrent limit exceeded: ip=%s", client_ip)
        await websocket.close(code=1008, reason="Too many concurrent sessions")
        return

    try:
        await websocket.accept()
    except Exception:
        _ws_concurrent_limiter.release(client_ip)
        raise
    session_id = str(uuid.uuid4())
    raw_user_id = websocket.query_params.get("user") or websocket.headers.get("x-user-id")
    user_id = _resolve_user_scope(raw_user_id, client_ip=client_ip)
    coach_mode = normalize_coach_mode(websocket.query_params.get("mode"))
    delivery_context = normalize_delivery_context(websocket.query_params.get("context"))
    primary_goal = normalize_primary_goal(websocket.query_params.get("goal"))
    raw_persona = websocket.query_params.get("persona")
    persona = raw_persona if raw_persona in VALID_PERSONAS else "coach"
    screen_enabled = ENABLE_SCREEN_SHARE and _bool_query(
        websocket.query_params.get("screen"),
        default=False,
    )
    demo_mode = _bool_query(
        websocket.query_params.get("demo"),
        default=DEMO_MODE_DEFAULT,
    )
    user_slides = _slides_for_user(user_id)
    total_slides = len(user_slides["slides"]) if user_slides else 0

    # ── Context Caching: fetch recent session summary ─────────────
    previous_summary = await _fetch_user_history(user_id, limit=1)

    logger.info("Session started: %s ip=%s user=%s persona=%s", session_id, client_ip, user_id, persona)

    state = SessionState(
        session_id=session_id,
        user_id=user_id,
        coach_mode=coach_mode,
        delivery_context=delivery_context,
        primary_goal=primary_goal,
        persona=persona,
        screen_enabled=screen_enabled,
        demo_mode=demo_mode,
        previous_summary=previous_summary,
        total_slides=total_slides,
        current_slide_index=0,
    )
    live_request_queue = LiveRequestQueue()

    try:
        live_agent = make_live_coach_agent(state)
        live_service = InMemorySessionService()
        live_runner = Runner(
            app_name=APP_NAME,
            agent=live_agent,
            session_service=live_service,
        )
        await live_service.create_session(
            app_name=APP_NAME, user_id=user_id, session_id=session_id
        )
        run_config = _make_run_config()

        await websocket.send_json({
            "type": "status",
            "state": "connected",
            "session_id": session_id,
            "user_id": state.user_id,
            "coach_mode": state.coach_mode,
            "delivery_context": state.delivery_context,
            "primary_goal": state.primary_goal,
            "screen_enabled": state.screen_enabled,
            "demo_mode": state.demo_mode,
            "total_slides": state.total_slides,
            "current_slide_index": state.current_slide_index,
            "has_uploaded_slides": state.total_slides > 0,
        })

        async with asyncio.TaskGroup() as tg:
            tg.create_task(
                _upstream(websocket, live_request_queue, state),
                name="upstream",
            )
            tg.create_task(
                _downstream(websocket, live_runner, user_id, session_id,
                             live_request_queue, run_config, state),
                name="downstream",
            )
            tg.create_task(
                _drain_tool_events(websocket, state),
                name="tool_events",
            )

    except WebSocketDisconnect:
        logger.info(f"Browser disconnected: {session_id}")
    except BaseException as exc:
        errors = exc.exceptions if isinstance(exc, ExceptionGroup) else [exc]
        for e in errors:
            if not isinstance(e, (WebSocketDisconnect, asyncio.CancelledError)):
                logger.error(f"Session {session_id}: {e}", exc_info=e)
    finally:
        state.is_active = False
        state.stop_drain()          # sentinel → drain task exits cleanly
        live_request_queue.close()
        _ws_concurrent_limiter.release(client_ip)

    # ── Post-session analysis ─────────────────────────────────────
    try:
        await _run_post_session(websocket, state)
    except Exception as e:
        logger.error(f"Post-session failed [{session_id}]: {e}", exc_info=True)
        state.research_tips = ""
        state.generated_assets = []
        await _safe_send(websocket, {
            "type": "analysis_complete",
            "report": "Analysis unavailable — see transcript for session details.",
            "research_tips": "",
            "generated_assets": [],
        })

    # ── Persist and return final scorecard ────────────────────────
    scorecard = build_scorecard(state)
    await _db.save_session(session_id, scorecard)
    await _safe_send(websocket, {"type": "scorecard", "data": scorecard})
    logger.info(f"Session {session_id} complete. Score: {scorecard['overall_score']}")


# ── Concurrent session tasks ──────────────────────────────────────

async def _upstream(
    websocket: WebSocket,
    live_request_queue: LiveRequestQueue,
    state: SessionState,
):
    """
    Receive binary frames from browser and route media into ADK LiveRequestQueue.

    Frame format: byte[0] = type, bytes[1..] = payload.
    No JSON parsing, no base64 decode on the hot path.
    """
    traffic_limiter = PerSecondTrafficLimiter(
        max_messages_per_sec=WS_MAX_MESSAGES_PER_SECOND,
        max_bytes_per_sec=WS_MAX_BYTES_PER_SECOND,
    )

    try:
        while True:
            frame = await websocket.receive()

            # Binary media frame
            if "bytes" in frame and frame["bytes"]:
                data = frame["bytes"]
                if len(data) > WS_MAX_BINARY_FRAME_BYTES:
                    logger.warning("WS frame too large session=%s bytes=%d", state.session_id, len(data))
                    await websocket.close(code=1009, reason="Binary frame too large")
                    break
                if not traffic_limiter.allow(len(data)):
                    logger.warning("WS traffic rate-limited session=%s", state.session_id)
                    await websocket.close(code=1008, reason="WebSocket rate limit exceeded")
                    break

                msg_type = data[0]
                payload = bytes(data[1:])

                if msg_type == _T_AUDIO_IN:
                    live_request_queue.send_realtime(
                        types.Blob(data=payload, mime_type="audio/pcm;rate=16000")
                    )
                elif msg_type == _T_VIDEO_IN:
                    live_request_queue.send_realtime(
                        types.Blob(data=payload, mime_type="image/jpeg")
                    )
                elif msg_type == _T_SCREEN_IN:
                    if not state.screen_enabled:
                        continue
                    live_request_queue.send_realtime(
                        types.Blob(data=payload, mime_type="image/jpeg")
                    )
                elif msg_type == _T_SLIDE_IN:
                    # Uploaded slide frames are streamed when the user/agent changes slides.
                    live_request_queue.send_realtime(
                        types.Blob(data=payload, mime_type="image/jpeg")
                    )
                elif msg_type == _T_STOP:
                    logger.info(f"Stop signal received: {state.session_id}")
                    break
                else:
                    logger.warning("Unsupported WS binary frame type=%s session=%s", msg_type, state.session_id)
                    await websocket.close(code=1003, reason="Unsupported frame type")
                    break

            # Graceful fallback: text "stop" message
            elif "text" in frame and frame["text"]:
                text = frame["text"]
                if len(text) > WS_MAX_TEXT_FRAME_BYTES:
                    logger.warning("WS text frame too large session=%s bytes=%d", state.session_id, len(text))
                    await websocket.close(code=1009, reason="Text frame too large")
                    break
                if not traffic_limiter.allow(len(text)):
                    logger.warning("WS traffic rate-limited session=%s", state.session_id)
                    await websocket.close(code=1008, reason="WebSocket rate limit exceeded")
                    break

                try:
                    payload_json = json.loads(text)
                    msg_type = payload_json.get("type")
                    if msg_type == "stop":
                        break
                    if msg_type == "slide_index":
                        total = int(payload_json.get("total_slides") or 0)
                        idx = int(payload_json.get("current_slide_index") or 0)
                        if total > 0:
                            state.total_slides = total
                            state.current_slide_index = max(0, min(total - 1, idx))
                except Exception:
                    pass

            elif frame.get("type") == "websocket.disconnect":
                break

    finally:
        live_request_queue.close()


async def _downstream(
    websocket: WebSocket,
    runner: Runner,
    user_id: str,
    session_id: str,
    live_request_queue: LiveRequestQueue,
    run_config: RunConfig,
    state: SessionState,
):
    """
    Consume ADK run_live() events.
    Coach audio → binary frames (no base64).
    Transcripts → JSON text frames (low frequency).
    Status → JSON text, emitted only on state transitions.

    Speech cooldown: native audio models can generate speech without first calling
    flag_issue (the audio is streamed as a side-effect of model generation).  We
    enforce COACH_SPEECH_COOLDOWN_S at the server level — suppressing both audio
    bytes and the coach transcript for any turn that starts within the cooldown
    window.  This is the hard gate; the tool-level cooldown in tools.py is a
    softer hint to the model.
    """
    current_status: str | None = None

    async def _emit_status(new_state: str):
        nonlocal current_status
        if new_state != current_status:
            current_status = new_state
            await websocket.send_json({"type": "status", "state": new_state})

    await _emit_status("listening")

    # Per-turn gate: set at the first audio chunk of each coach turn.
    _suppress_turn: bool = False      # True → drop audio + transcript for this turn
    _turn_committed: bool = False     # True → we already decided for this turn
    _in_audio_stream: bool = False    # True → currently receiving audio for an utterance
    _turn_index: int = 1
    _turn_word_count: int = 0         # Cumulative word count for current coach turn

    # Reply-level observability for docker logs.
    _reply_index: int = 0
    _reply_started_at: float = 0.0
    _reply_chunks: int = 0
    _reply_bytes: int = 0
    _reply_action: str = "unknown"  # allowed | suppressed
    _reply_reason: str = "unknown"
    _reply_preview_logged: bool = False

    def _start_reply(action: str, reason: str) -> None:
        nonlocal _reply_index, _reply_started_at, _reply_chunks, _reply_bytes
        nonlocal _reply_action, _reply_reason, _reply_preview_logged
        _reply_index += 1
        _reply_started_at = time.monotonic()
        _reply_chunks = 0
        _reply_bytes = 0
        _reply_action = action
        _reply_reason = reason
        _reply_preview_logged = False
        logger.info(
            "coach_reply_start session=%s turn=%d reply=%d action=%s reason=%s",
            state.session_id,
            _turn_index,
            _reply_index,
            _reply_action,
            _reply_reason,
        )

    def _end_reply(trigger: str) -> None:
        nonlocal _reply_started_at, _reply_chunks, _reply_bytes
        nonlocal _reply_action, _reply_reason
        if _reply_started_at <= 0:
            return
        duration_ms = int((time.monotonic() - _reply_started_at) * 1000)
        logger.info(
            "coach_reply_end session=%s turn=%d reply=%d action=%s reason=%s trigger=%s chunks=%d bytes=%d duration_ms=%d",
            state.session_id,
            _turn_index,
            _reply_index,
            _reply_action,
            _reply_reason,
            trigger,
            _reply_chunks,
            _reply_bytes,
            duration_ms,
        )
        _reply_started_at = 0.0
        _reply_chunks = 0
        _reply_bytes = 0
        _reply_action = "unknown"
        _reply_reason = "unknown"

    try:
        async for event in runner.run_live(
            user_id=user_id,
            session_id=session_id,
            live_request_queue=live_request_queue,
            run_config=run_config,
        ):
            if not state.is_active:
                break

            # ── Coach audio ────────────────────────────────────────────
            if event.content and event.content.parts:
                has_audio = any(
                    p.inline_data and p.inline_data.data for p in event.content.parts
                )

                if has_audio:
                    if not _in_audio_stream:
                        # Start of a new speech segment within this turn.
                        if not _turn_committed:
                            # First speech in this turn — evaluate cooldown.
                            now = time.monotonic()
                            elapsed = now - state.last_coach_speech_time
                            if elapsed >= COACH_SPEECH_COOLDOWN_S:
                                _suppress_turn = False
                                _turn_committed = True
                                state.last_coach_speech_time = now
                                _start_reply("allowed", "cooldown_pass")
                                logger.debug(
                                    "Coach speech allowed (%.1fs since last)", elapsed
                                )
                            else:
                                _suppress_turn = True
                                _turn_committed = True
                                _start_reply(
                                    "suppressed",
                                    f"cooldown_active_{int(max(0.0, COACH_SPEECH_COOLDOWN_S - elapsed))}s",
                                )
                                logger.info(
                                    "Coach speech suppressed — cooldown %.0fs remaining (session=%s)",
                                    COACH_SPEECH_COOLDOWN_S - elapsed,
                                    state.session_id,
                                )
                        else:
                            # Second+ speech segment in the same turn — always suppress.
                            # This prevents duplicate coach feedback when the model speaks
                            # both before and after a tool call (e.g. flag_issue → draw_overlay).
                            _suppress_turn = True
                            _start_reply("suppressed", "duplicate_segment_same_turn")
                            logger.info(
                                "Coach duplicate speech suppressed in same turn (session=%s)",
                                state.session_id,
                            )
                    _in_audio_stream = True

                    if not _suppress_turn:
                        for part in event.content.parts:
                            if part.inline_data and part.inline_data.data:
                                _reply_chunks += 1
                                _reply_bytes += len(part.inline_data.data)
                                frame = bytes([_T_AUDIO_OUT]) + part.inline_data.data
                                await websocket.send_bytes(frame)
                        await _emit_status("coaching")
                else:
                    # Non-audio content event (e.g. function_call) — marks a gap
                    # between utterances so the next audio is treated as a new segment.
                    if _in_audio_stream:
                        _end_reply("non_audio_gap")
                    _in_audio_stream = False

            # ── User speech transcription ──────────────────────────────
            if event.input_transcription and event.input_transcription.text:
                text = _sanitize_transcript_text(event.input_transcription.text)
                if text:
                    state.add_transcript("user", text)
                    await websocket.send_json({"type": "transcript", "speaker": "user", "text": text})

            # ── Coach speech transcription ─────────────────────────────
            # Only forward when we're actually sending audio for this turn.
            if event.output_transcription and event.output_transcription.text:
                text = _sanitize_transcript_text(event.output_transcription.text)
                if text:
                    # Hard word-count gate — enforce system-prompt "≤12 words" instruction
                    # even when the model ignores it.  We allow a small buffer (20 words)
                    # because transcription chunks may lag 1-2 events behind the audio.
                    if not _suppress_turn:
                        _turn_word_count += len(text.split())
                        if _turn_word_count > MAX_COACH_WORDS_PER_TURN:
                            _suppress_turn = True
                            logger.info(
                                "coach_speech_too_long session=%s words=%d limit=%d — suppressing remainder",
                                state.session_id, _turn_word_count, MAX_COACH_WORDS_PER_TURN,
                            )

                    if not _suppress_turn:
                        if not _reply_preview_logged:
                            preview = text if len(text) <= 160 else f"{text[:157]}..."
                            logger.info(
                                "coach_reply_text session=%s turn=%d reply=%d words=%d preview=%r",
                                state.session_id,
                                _turn_index,
                                _reply_index,
                                len(text.split()),
                                preview,
                            )
                            _reply_preview_logged = True
                        state.add_transcript("coach", text)
                        await websocket.send_json({"type": "transcript", "speaker": "coach", "text": text})
                    elif not _reply_preview_logged:
                        preview = text if len(text) <= 120 else f"{text[:117]}..."
                        logger.info(
                            "coach_reply_text_suppressed session=%s turn=%d reply=%d preview=%r",
                            state.session_id,
                            _turn_index,
                            _reply_index,
                            preview,
                        )
                        _reply_preview_logged = True

            # ── Turn complete — reset per-turn gate ────────────────────
            if event.turn_complete:
                if _in_audio_stream:
                    _end_reply("turn_complete")
                _suppress_turn = False
                _turn_committed = False
                _in_audio_stream = False
                _turn_word_count = 0
                logger.info("coach_turn_complete session=%s turn=%d", state.session_id, _turn_index)
                _turn_index += 1
                await _emit_status("listening")

    except Exception as exc:
        if _is_normal_live_close(exc):
            logger.info("Live session closed normally (session=%s)", state.session_id)
            state.is_active = False
            return

        # Quota exhaustion, context overflow, and transient API errors all surface
        # here as generic exceptions from the run_live generator.  Rather than
        # silently dying we surface a status message so the browser shows an
        # actionable error instead of hanging on "Listening".
        err_str = str(exc)
        if any(kw in err_str.lower() for kw in ("quota", "resource_exhausted", "context", "token")):
            logger.warning("Live session quota/context error (session=%s): %s", state.session_id, exc)
            await _safe_send(websocket, {"type": "status", "state": "error", "message": "API quota reached — session ended."})
        else:
            logger.error("Live session error (session=%s): %s", state.session_id, exc, exc_info=True)
            await _safe_send(websocket, {"type": "status", "state": "error", "message": "Session error — please try again."})
        state.is_active = False
    finally:
        # Signal _drain_tool_events to exit so the TaskGroup can complete and
        # post-session analysis can start.  Without this, _drain_tool_events
        # blocks forever on queue.get() — the TaskGroup never finishes — and
        # the scorecard + report are never generated.
        state.stop_drain()


async def _drain_tool_events(websocket: WebSocket, state: SessionState):
    """
    Drain the ws_event_queue populated by ADK tool calls.
    Uses blocking await instead of timeout polling — zero wakeups when queue is idle.
    Exits when it receives the None sentinel (put by state.stop_drain()).
    """
    while True:
        item = await state.ws_event_queue.get()
        if item is None:   # sentinel — session is done
            break
        await websocket.send_json(item)


# ── Post-session analysis pipeline ───────────────────────────────

_POST_AGENT_TIMEOUT_S = 90  # per-agent hard ceiling; google_search can be slow

async def _run_single_agent(agent, message: str) -> str:
    """
    Run one post-session LlmAgent in an isolated session and return its text output.

    Each call creates a fresh InMemorySessionService + Runner so concurrent
    agent runs (via asyncio.gather) don't share session state.
    A hard timeout prevents a hung agent (e.g. google_search) from blocking
    the entire post-session pipeline indefinitely.
    """
    async def _run() -> str:
        service = InMemorySessionService()
        runner = Runner(app_name=POST_APP_NAME, agent=agent, session_service=service)
        sid = uuid.uuid4().hex
        await service.create_session(app_name=POST_APP_NAME, user_id="presenter", session_id=sid)
        parts: list[str] = []
        async for event in runner.run_async(
            user_id="presenter",
            session_id=sid,
            new_message=types.Content(role="user", parts=[types.Part(text=message)]),
        ):
            if event.is_final_response() and event.content:
                for part in event.content.parts:
                    if part.text:
                        parts.append(part.text)
        return parts[-1] if parts else ""

    try:
        return await asyncio.wait_for(_run(), timeout=_POST_AGENT_TIMEOUT_S)
    except asyncio.TimeoutError:
        logger.warning("Post-session agent %s timed out after %ds", agent.name, _POST_AGENT_TIMEOUT_S)
        return ""
    except Exception as exc:
        logger.warning("Post-session agent %s failed: %s", agent.name, exc)
        return ""


async def _run_with_validation(
    agent,
    message: str,
    *,
    min_chars: int = 100,
    repair_suffix: str = "",
) -> str:
    """
    Run an agent and validate the output meets minimum quality requirements.
    If output is empty or too short, retry once with a repair prompt.
    """
    result = await _run_single_agent(agent, message)
    if len((result or "").strip()) >= min_chars:
        return result

    logger.warning(
        "Agent %s returned insufficient output (%d chars < %d min) — retrying with repair prompt",
        agent.name, len((result or "").strip()), min_chars,
    )
    repair_message = (
        f"{message}\n\n"
        "REPAIR: Your previous response was incomplete or empty. "
        "Provide a thorough, properly formatted response. Do not skip or truncate any sections."
        + (f"\n{repair_suffix}" if repair_suffix else "")
    )
    return await _run_single_agent(agent, repair_message)


async def _run_post_session(websocket: WebSocket, state: SessionState):
    if not state.transcript and not state.events:
        logger.info(f"No session data for {state.session_id} — skipping analysis")
        state.research_tips = ""
        state.generated_assets = []
        return

    await _safe_send(websocket, {"type": "status", "state": "analyzing"})
    context = _build_context_message(state)

    # ── Parallel phase: three agents run concurrently ─────────────
    # Each emits a pipeline_step event the moment it finishes so the browser
    # can mark that step done in real time rather than using fake timers.

    async def run_delivery() -> str:
        # min_chars=100 ensures the agent didn't return a fragment; retry once if so
        result = await _run_with_validation(DELIVERY_ANALYST, context, min_chars=100)
        await _safe_send(websocket, {"type": "pipeline_step", "step": "delivery_done"})
        logger.info("Post-session delivery analysis done: %s", state.session_id)
        return result

    async def run_content() -> str:
        result = await _run_with_validation(CONTENT_ANALYST, context, min_chars=100)
        await _safe_send(websocket, {"type": "pipeline_step", "step": "content_done"})
        logger.info("Post-session content analysis done: %s", state.session_id)
        return result

    async def run_research() -> str:
        result = await _run_with_validation(RESEARCH_AGENT, context, min_chars=60)
        await _safe_send(websocket, {"type": "pipeline_step", "step": "research_done"})
        logger.info("Post-session research done: %s", state.session_id)
        return result

    delivery_out, content_out, research_out = await asyncio.gather(
        run_delivery(), run_content(), run_research()
    )

    # Surface degraded-quality risk when agents silently returned empty strings.
    empty_agents = [
        name for name, out in (
            ("delivery", delivery_out),
            ("content", content_out),
            ("research", research_out),
        ) if not out
    ]
    if empty_agents:
        logger.warning(
            "Post-session agents returned empty output for %s: %s — report quality may be degraded",
            state.session_id, ", ".join(empty_agents),
        )

    # ── Synthesis phase: combine all three outputs ─────────────────
    synthesis_context = (
        f"{context}\n\n"
        f"DELIVERY ANALYSIS:\n{delivery_out or '(unavailable)'}\n\n"
        f"CONTENT ANALYSIS:\n{content_out or '(unavailable)'}\n\n"
        f"RESEARCH TIPS:\n{research_out or '(unavailable)'}"
    )
    final_report_raw = await _run_single_agent(SYNTHESIS_AGENT, synthesis_context)

    # Validate synthesis has all required section headers; retry once with
    # explicit repair prompt listing exactly which sections are missing.
    if final_report_raw and not _validate_synthesis(final_report_raw):
        missing = _missing_synthesis_sections(final_report_raw)
        logger.warning(
            "Synthesis missing sections for %s: %s — retrying",
            state.session_id, missing,
        )
        repair_suffix = (
            "MISSING SECTIONS (you must include all of them):\n"
            + "\n".join(f"  {s}" for s in missing)
        )
        repair_message = (
            f"{synthesis_context}\n\n"
            "REPAIR: Your previous response was missing required section headers. "
            "Rewrite the complete report including EVERY required section.\n"
            + repair_suffix
        )
        final_report_raw = await _run_single_agent(SYNTHESIS_AGENT, repair_message)

    if not final_report_raw:
        logger.error(
            "Post-session synthesis returned empty output for %s — using fallback report",
            state.session_id,
        )
        final_report_raw = (
            "**Analysis incomplete.** The AI synthesis step did not produce a report for this session. "
            "Check server logs for agent timeout or API quota errors."
        )
    final_report, image_prompts = extract_image_prompts(final_report_raw)
    state.ai_scores = _extract_ai_scores(final_report_raw)
    if len(state.ai_scores) == 5:
        logger.info("AI scores for %s: %s", state.session_id, state.ai_scores)
    else:
        logger.warning(
            "Incomplete AI scores for %s — got %d/5 (filler/pace/eye/clarity/visual); "
            "falling back to metric-based scoring for missing dimensions",
            state.session_id, len(state.ai_scores),
        )
    await _safe_send(websocket, {"type": "pipeline_step", "step": "synthesis_done"})
    logger.info("Post-session synthesis done: %s", state.session_id)

    generated_assets: list[dict] = []
    if ENABLE_IMAGE_GENERATION:
        await _safe_send(websocket, {"type": "pipeline_step", "step": "visuals_start"})
        generated_assets = await generate_session_assets(
            state,
            final_report,
            custom_prompts=image_prompts,
            timeout_s=IMAGE_GENERATION_TIMEOUT_S,
            retries=IMAGE_GENERATION_RETRIES,
        )
        await _safe_send(websocket, {"type": "pipeline_step", "step": "visuals_done"})

    state.final_report = final_report
    state.research_tips = research_out
    state.generated_assets = generated_assets
    await _safe_send(websocket, {
        "type": "analysis_complete",
        "report": final_report,
        "research_tips": research_out,   # raw output for citation card rendering
        "generated_assets": generated_assets,
    })
    logger.info(f"Post-session analysis complete: {state.session_id}")


def _build_context_message(state: SessionState) -> str:
    return (
        "You are analyzing a completed PitchMirror coaching session.\n\n"
        f"USER ID: {state.user_id}\n"
        f"SESSION MODE: {state.coach_mode}\n"
        f"DELIVERY CONTEXT: {state.delivery_context}\n"
        f"PRIMARY GOAL: {state.primary_goal}\n"
        f"SCREEN SHARE ENABLED: {state.screen_enabled}\n"
        f"SLIDES UPLOADED: {state.total_slides}\n"
        f"CURRENT SLIDE INDEX: {state.current_slide_index}\n"
        f"DEMO MODE: {state.demo_mode}\n\n"
        f"SESSION TRANSCRIPT:\n{state.transcript_text()}\n\n"
        f"COACHING EVENTS (tool calls during live session):\n{state.events_json()}\n\n"
        f"METRICS: fillers={state.filler_count} eye_drops={state.eye_contact_drops} "
        f"pace={state.pace_violations} contradictions={state.contradictions} "
        f"clarity={state.clarity_flags} slide_clarity={state.visual_flags} "
        f"slide_mismatch={state.mismatch_flags} duration={round(state.duration_seconds())}s\n\n"
        "Analyze this session and provide coaching as instructed."
    )


async def _safe_send(websocket: WebSocket, data: dict):
    try:
        await websocket.send_json(data)
    except Exception:
        pass
