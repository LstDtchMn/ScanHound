"""Service dependency injection for FastAPI."""
from __future__ import annotations

import logging
import os
import secrets
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from backend.app_service import (
    AppService, clean_string,
    STATUS_MISSING, STATUS_DOWNLOADED, STATUS_IN_LIBRARY, STATUS_IN_LIBRARY_CHECK,
    STATUS_UPGRADE_4K, STATUS_UPGRADE_SIZE, STATUS_UPGRADE_SIZE_DV, STATUS_DV_UPGRADE,
    COLOR_MISSING, COLOR_DOWNLOADED, COLOR_IN_LIBRARY, COLOR_UPGRADE, COLOR_DV_UPGRADE,
    RESOLUTION_ORDER,
)
from backend.database import DatabaseManager

logger = logging.getLogger(__name__)

# Emoji constants expected by MatchingEngine
EMOJI_4K = "[4K]"
EMOJI_DV = "[DV]"
EMOJI_INFO = "\u2139\ufe0f"
EMOJI_WARNING = "\u26a0\ufe0f"


class ScannerAppBridge:
    """Adapter providing the interface MatchingEngine/WebScrapers expect from parent_app.

    Mirrors _ScannerAppBridge from ui/controllers/scanner_controller.py so the
    same backend services can be used without any Qt/QML dependencies.
    """

    def __init__(self, backend: AppService):
        self._backend = backend
        self.tmdb_cache = backend.tmdb_cache
        self.omdb_cache = backend.omdb_cache
        self.download_history: set = set()

        # Constants expected by MatchingEngine
        self.STATUS_MISSING = STATUS_MISSING
        self.STATUS_DOWNLOADED = STATUS_DOWNLOADED
        self.STATUS_IN_LIBRARY = STATUS_IN_LIBRARY
        self.STATUS_IN_LIBRARY_CHECK = STATUS_IN_LIBRARY_CHECK
        self.STATUS_UPGRADE_4K = STATUS_UPGRADE_4K
        self.STATUS_UPGRADE_SIZE = STATUS_UPGRADE_SIZE
        self.STATUS_UPGRADE_SIZE_DV = STATUS_UPGRADE_SIZE_DV
        self.STATUS_DV_UPGRADE = STATUS_DV_UPGRADE
        self.COLOR_MISSING = COLOR_MISSING
        self.COLOR_DOWNLOADED = COLOR_DOWNLOADED
        self.COLOR_IN_LIBRARY = COLOR_IN_LIBRARY
        self.COLOR_UPGRADE = COLOR_UPGRADE
        self.COLOR_DV_UPGRADE = COLOR_DV_UPGRADE
        self.RESOLUTION_ORDER = RESOLUTION_ORDER
        self.EMOJI_4K = EMOJI_4K
        self.EMOJI_DV = EMOJI_DV
        self.EMOJI_INFO = EMOJI_INFO
        self.EMOJI_WARNING = EMOJI_WARNING

    @property
    def config(self):
        return self._backend.config

    def clean_string(self, s: str) -> str:
        return clean_string(s)

    def safe_log(self, message: str, level: str = "info"):
        getattr(logger, level if level != "success" else "info", logger.info)(message)

    def log(self, message: str, level: str = "info"):
        self.safe_log(message, level)

    @staticmethod
    def parse_size(size_str: str) -> float:
        """Parse size string to GB (float)."""
        if not size_str:
            return 0.0
        size_str = size_str.strip().upper()
        try:
            if "GB" in size_str:
                return float(size_str.replace("GB", "").strip())
            elif "MB" in size_str:
                return float(size_str.replace("MB", "").strip()) / 1024
            elif "TB" in size_str:
                return float(size_str.replace("TB", "").strip()) * 1024
            return float(size_str)
        except (ValueError, TypeError):
            return 0.0


@dataclass
class ServiceRegistry:
    """Holds all initialized backend service singletons."""

    config: Dict[str, Any] = field(default_factory=dict)
    backend: Optional[AppService] = None
    db: Optional[DatabaseManager] = None
    bridge: Optional[ScannerAppBridge] = None
    _scanner_service: Any = None
    _plex_service: Any = None
    _download_service: Any = None
    _download_queue_service: Any = None
    _auto_grab_service: Any = None
    _notification_bridge: Any = None
    _watchlist_manager: Any = None
    _analytics_dashboard: Any = None
    _background_scanner: Any = None
    _rename_service: Any = None
    _plex_metadata_scan_job: Any = None
    _plex_metadata_scan_job_lock: threading.Lock = field(default_factory=threading.Lock)
    _shutdown_event: threading.Event = field(default_factory=threading.Event)
    _lifespan_generation: int = 0
    _lifespan_generation_lock: threading.Lock = field(default_factory=threading.Lock)
    # Auth nonce — generated on startup, validated by middleware.
    # If SCANHOUND_AUTH_NONCE env var is set, use that (Tauri passes it).
    # If empty string, auth is disabled (dev mode).
    auth_nonce: str = field(default_factory=lambda: os.environ.get("SCANHOUND_AUTH_NONCE", ""))

    @property
    def scanner(self):
        return self._scanner_service

    @property
    def plex(self):
        return self._plex_service

    @property
    def download(self):
        return self._download_service

    @property
    def download_queue(self):
        return self._download_queue_service

    @property
    def auto_grab(self):
        return self._auto_grab_service

    @property
    def notifications(self):
        return self._notification_bridge

    @property
    def watchlist(self):
        return self._watchlist_manager

    @property
    def analytics(self):
        return self._analytics_dashboard

    @property
    def background_scanner(self):
        return self._background_scanner

    @property
    def rename_service(self):
        return self._rename_service

    @property
    def plex_metadata_scan_job(self):
        """Normally constructed eagerly in ``_init_services`` (backend.api.main),
        alongside every sibling service, precisely so this property never has
        to build one on the fly under concurrent request threads. The
        lock-guarded lazy fallback below only matters for callers that build a
        ``ServiceRegistry`` without going through ``_init_services`` (e.g.
        ad-hoc scripts/tests) — it double-checks under a dedicated lock so two
        threads racing a true first-ever access can't each construct their own
        instance (which would defeat both the max-2-worker concurrency cap and
        the job's own start() re-entrancy lock, since those live per-instance).
        """
        if self._plex_metadata_scan_job is None:
            with self._plex_metadata_scan_job_lock:
                if self._plex_metadata_scan_job is None:
                    from backend.plex_metadata_scan import PlexMetadataScanJob
                    from backend.api.ws import ws_manager

                    def _broadcast(status_dict):
                        ws_manager.broadcast_sync({
                            "type": "plex:metadata_scan_progress",
                            "data": status_dict,
                        })

                    self._plex_metadata_scan_job = PlexMetadataScanJob(self.db, progress_cb=_broadcast)
        return self._plex_metadata_scan_job

    def begin_lifespan(self) -> int:
        """Advance ownership and clear cancellation for one new app lifespan."""
        with self._lifespan_generation_lock:
            self._lifespan_generation += 1
            generation = self._lifespan_generation
        self._shutdown_event.clear()
        return generation

    @property
    def lifespan_generation(self) -> int:
        with self._lifespan_generation_lock:
            return self._lifespan_generation

    def owns_lifespan(self, generation: int) -> bool:
        """Whether work created by ``generation`` may still publish state."""
        with self._lifespan_generation_lock:
            current = self._lifespan_generation
        return generation == current and not self._shutdown_event.is_set()

    def request_shutdown(self):
        self._shutdown_event.set()

    @property
    def shutdown_requested(self) -> bool:
        return self._shutdown_event.is_set()


# Module-level singleton — populated during app lifespan
registry = ServiceRegistry()


def get_registry() -> ServiceRegistry:
    return registry


_CREDENTIAL_STATES = ("present", "absent", "unknown")


def credential_state(db: Any = None) -> str:
    """Three-state read of the stored admin password.

    ``"present"``, ``"absent"``, or ``"unknown"`` when the credential could not
    be read at all. Security decisions must treat "unknown" as credentialed and
    fail CLOSED, because the alternative — reading an unreadable database as
    "no password configured" — un-gates the /auth/set-password bootstrap path.

    The detection itself lives in ``DatabaseManager.credential_state``; only
    that layer can tell a failed read from an empty one. Here we ask for it and
    ACCEPT ONLY one of the three known strings.

    Anything else falls back to ``has_password()``, which stays the interface
    every caller and test double speaks. That fallback is load-bearing, not
    defensive: a ``MagicMock`` answers every attribute, so
    ``db.credential_state()`` returns a Mock rather than a string. Treating
    that as "unknown" would fail every mock-backed request closed and 401 a
    large part of the existing suite — which is exactly what happened before
    this was rewritten.

    ``db is None`` maps to ``"absent"`` to preserve the historical
    ``auth_enabled()`` answer for a registry with no database at all; that case
    self-mitigates because ``token_authorized`` cannot resolve a session either
    and ``/auth/set-password`` already 503s on it.
    """
    db = registry.db if db is None else db
    if db is None:
        return "absent"
    native = getattr(db, "credential_state", None)
    if callable(native):
        try:
            state = native()
        except Exception:
            return "unknown"
        if state in _CREDENTIAL_STATES:
            return state
        # Not a real implementation (a stand-in that answers everything).
        # Fall through to the boolean interface rather than guessing.
    try:
        return "present" if db.has_password() else "absent"
    except Exception:
        # The boolean interface itself raised: a genuine read failure.
        return "unknown"


def auth_enabled() -> bool:
    """Auth is active when a nonce is configured or a password has been set.

    Canonical home so both the HTTP middleware (backend.api.main) and the
    WebSocket endpoint (backend.api.ws) gate on the exact same rule.
    """
    if registry.auth_nonce:
        return True
    db = registry.db
    if db is None:
        return False
    # "unknown" counts as credentialed so a DB read failure keeps both gates
    # SHUT instead of dropping through to the no-credential bootstrap path.
    return credential_state(db) in ("present", "unknown")


def has_any_credential() -> bool:
    """Whether a nonce is configured or a password has been persisted.

    Same predicate as ``auth_enabled`` today, named for its other use: telling
    the fail-closed bootstrap gate (backend.api.main) whether any credential
    exists at all, independent of the open-mode escape hatch below.
    """
    return auth_enabled()


def allow_open() -> bool:
    """Explicit escape hatch restoring the old fully-open behavior.

    Historically, "no nonce and no password" meant the whole API was served
    without auth — including after a DB reset/corruption silently wiped the
    ``auth_credentials`` row. That fail-OPEN posture is now opt-in only: set
    ``SCANHOUND_ALLOW_OPEN=1`` for intentional headless/dev use. Left unset
    (the default), a missing credential fails CLOSED instead — see
    ``backend.api.main._request_requires_auth``.
    """
    return os.environ.get("SCANHOUND_ALLOW_OPEN", "") == "1"


def token_authorized(token: str) -> bool:
    """Whether a bearer token is the desktop nonce or an unexpired session token.

    Used by both the HTTP middleware and the WebSocket handshake so a
    password-login session is honoured on the socket too — without this the
    socket would accept any (or no) token whenever the nonce is unset.
    """
    if not token:
        return False
    nonce = registry.auth_nonce
    # Constant-time compare so the nonce can't be recovered by timing.
    if nonce and secrets.compare_digest(token, nonce):
        return True
    db = registry.db
    if db:
        from backend import auth_service
        expires_at = db.get_session_expiry(auth_service.hash_token(token))
        if expires_at and not auth_service.is_expired(expires_at):
            return True
    return False


# ── Short-lived WebSocket tickets ─────────────────────────────────────
# A browser cannot set an Authorization header on a WebSocket handshake, so the
# 30-day session token travels as ``?token=…`` — which NPM/nginx, Cloudflare
# and uvicorn's own access log all record verbatim, turning any log reader into
# an admin. A ticket is minted by an already-authorized HTTP request, dies in
# seconds and on first use, so a logged one is worthless. It resolves back to
# the credential that minted it so the socket can keep re-checking that
# session's revocation (see backend.api.ws).
_WS_TICKET_TTL_S = 30.0
_ws_tickets: Dict[str, Tuple[str, float]] = {}
_ws_tickets_lock = threading.Lock()


def ws_ticket_ttl_seconds() -> float:
    """Lifetime of a WebSocket ticket, so the client can time its handshake."""
    return _WS_TICKET_TTL_S


def _purge_ws_tickets(now: float) -> None:
    """Drop expired tickets. Caller must hold ``_ws_tickets_lock``."""
    for key in [k for k, (_, exp) in _ws_tickets.items() if exp <= now]:
        _ws_tickets.pop(key, None)


def issue_ws_ticket(credential: str) -> str:
    """Mint a single-use ticket standing in for ``credential`` on the socket."""
    from backend import auth_service
    ticket = secrets.token_urlsafe(32)
    now = time.monotonic()
    with _ws_tickets_lock:
        _purge_ws_tickets(now)
        _ws_tickets[auth_service.hash_token(ticket)] = (
            credential, now + _WS_TICKET_TTL_S)
    return ticket


def consume_ws_ticket(ticket: str) -> Optional[str]:
    """Redeem a ticket ONCE, returning the credential it was minted for.

    Removed on the first lookup whether or not it had expired, so a replay of
    the same ticket never resolves.
    """
    if not ticket:
        return None
    from backend import auth_service
    now = time.monotonic()
    with _ws_tickets_lock:
        _purge_ws_tickets(now)
        entry = _ws_tickets.pop(auth_service.hash_token(ticket), None)
    if entry is None:
        return None
    credential, expires_at = entry
    return credential if expires_at > now else None
