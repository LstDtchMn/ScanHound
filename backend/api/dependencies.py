"""Service dependency injection for FastAPI."""
from __future__ import annotations

import logging
import os
import secrets
import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Set

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
    _auto_grab_service: Any = None
    _notification_bridge: Any = None
    _watchlist_manager: Any = None
    _analytics_dashboard: Any = None
    _background_scanner: Any = None
    _rename_service: Any = None
    _shutdown_event: threading.Event = field(default_factory=threading.Event)
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

    def request_shutdown(self):
        self._shutdown_event.set()

    @property
    def shutdown_requested(self) -> bool:
        return self._shutdown_event.is_set()


# Module-level singleton — populated during app lifespan
registry = ServiceRegistry()


def get_registry() -> ServiceRegistry:
    return registry


def auth_enabled() -> bool:
    """Auth is active when a nonce is configured or a password has been set.

    Canonical home so both the HTTP middleware (backend.api.main) and the
    WebSocket endpoint (backend.api.ws) gate on the exact same rule.
    """
    if registry.auth_nonce:
        return True
    db = registry.db
    return bool(db and db.has_password())


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
