"""
PitchMirror — Firestore client.
Single module-level instance shared across all sessions — no per-session init churn.
"""
from collections import deque
from copy import deepcopy
import logging
import os
import time
from typing import Optional

logger = logging.getLogger(__name__)

FIRESTORE_COLLECTION = os.getenv("FIRESTORE_COLLECTION", "pitchmirror_sessions")


def _int_env(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        logger.warning("Invalid %s=%r; using default=%d", name, raw, default)
        return default


RECENT_SESSION_CACHE_SIZE = max(10, _int_env("RECENT_SESSION_CACHE_SIZE", 200))


class FirestoreClient:
    def __init__(self):
        self._client = None
        self._firestore = None
        self._enabled = False
        self._memory_store: dict[str, dict] = {}
        self._recent_ids: deque[str] = deque()
        self._init()

    def _init(self):
        try:
            from google.cloud import firestore
            project = os.getenv("GOOGLE_CLOUD_PROJECT")
            if project:
                self._client = firestore.AsyncClient(project=project)
                self._firestore = firestore
                self._enabled = True
                logger.info("Firestore client initialized")
            else:
                logger.warning("GOOGLE_CLOUD_PROJECT not set — Firestore disabled")
        except ImportError:
            logger.warning("google-cloud-firestore not installed — Firestore disabled")
        except Exception as e:
            logger.warning(f"Firestore init failed: {e} — running in-memory only")

    def _cache_session(self, session_id: str, payload: dict) -> None:
        self._memory_store[session_id] = deepcopy(payload)
        try:
            self._recent_ids.remove(session_id)
        except ValueError:
            pass
        self._recent_ids.append(session_id)

        while len(self._recent_ids) > RECENT_SESSION_CACHE_SIZE:
            evicted = self._recent_ids.popleft()
            self._memory_store.pop(evicted, None)

    @staticmethod
    def _sanitize_for_persistence(payload: dict) -> dict:
        """
        Keep scorecard documents small and Firestore-friendly.

        Post-session visuals are returned live to the browser already; storing
        base64 binaries in Firestore inflates document size and can break writes.
        """
        sanitized = deepcopy(payload)
        assets = sanitized.get("generated_assets")
        if isinstance(assets, list):
            compact_assets: list[dict] = []
            for asset in assets[:2]:
                if not isinstance(asset, dict):
                    continue
                item = dict(asset)
                if "data_base64" in item:
                    item.pop("data_base64", None)
                    item["data_omitted"] = True
                compact_assets.append(item)
            sanitized["generated_assets"] = compact_assets
        return sanitized

    async def save_session(self, session_id: str, scorecard: dict) -> bool:
        payload = dict(scorecard)
        payload.setdefault("session_id", session_id)
        payload.setdefault("created_at", int(time.time()))
        payload_to_store = self._sanitize_for_persistence(payload)

        self._cache_session(session_id, payload_to_store)

        if not self._enabled or self._client is None:
            logger.debug(
                "[in-memory] session %s score=%s (Firestore disabled — not durable across restarts)",
                session_id, payload_to_store.get("overall_score"),
            )
            return False
        try:
            doc_ref = self._client.collection(FIRESTORE_COLLECTION).document(session_id)
            await doc_ref.set(payload_to_store)
            logger.info(f"Saved session {session_id} to Firestore")
            return True
        except Exception as e:
            logger.error(f"Failed to save session {session_id}: {e}")
            return False

    async def get_session(self, session_id: str) -> Optional[dict]:
        if self._enabled and self._client is not None:
            try:
                doc_ref = self._client.collection(FIRESTORE_COLLECTION).document(session_id)
                doc = await doc_ref.get()
                if doc.exists:
                    payload = doc.to_dict() or {}
                    self._cache_session(session_id, payload)
                    return payload
            except Exception as e:
                logger.error(f"Failed to fetch session {session_id}: {e}")

        cached = self._memory_store.get(session_id)
        return deepcopy(cached) if cached else None

    async def list_recent(self, limit: int = 20, user_id: Optional[str] = None) -> list[dict]:
        limit = max(1, min(limit, 100))

        if self._enabled and self._client is not None and self._firestore is not None:
            try:
                collection = self._client.collection(FIRESTORE_COLLECTION)
                if user_id:
                    query = (
                        collection.where("user_id", "==", user_id)
                        .order_by("created_at", direction=self._firestore.Query.DESCENDING)
                        .limit(limit)
                    )
                else:
                    query = (
                        collection.order_by("created_at", direction=self._firestore.Query.DESCENDING)
                        .limit(limit)
                    )
                docs = [doc async for doc in query.stream()]
                rows = [doc.to_dict() or {} for doc in docs]
                for row in rows:
                    sid = row.get("session_id")
                    if sid:
                        self._cache_session(sid, row)
                return rows
            except Exception as e:
                logger.error(f"Failed to list recent sessions from Firestore: {e}")

        recent: list[dict] = []
        for sid in reversed(self._recent_ids):
            row = self._memory_store.get(sid)
            if row and (not user_id or row.get("user_id") == user_id):
                recent.append(deepcopy(row))
            if len(recent) >= limit:
                break
        return recent


# Module-level singleton — initialized once at import time, reused across all sessions
_db = FirestoreClient()


def get_db() -> FirestoreClient:
    return _db
