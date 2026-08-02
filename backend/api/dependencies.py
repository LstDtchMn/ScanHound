"""Service dependency injection for FastAPI."""
from __future__ import annotations

import logging
import os
import secrets
import threading
import time
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

# Total wall-clock a lifespan teardown may spend waiting for registry-owned
# threads. It is a BUDGET SHARED BY ALL of them, not a per-thread timeout: a
# handful of workers wedged in a long network call must not multiply into a
# minutes-long shutdown. Whatever is still alive when it expires is logged by
# name and abandoned.
#
# Abandonment is only safe because these are PLAIN daemon threads:
# measured 2026-08-02, a wedged plain daemon thread does not stop the
# interpreter exiting (exit 0). Do NOT generalise that to a wedged
# ThreadPoolExecutor worker — concurrent.futures.thread registers every
# worker and its _python_exit hook JOINS them at interpreter shutdown,
# daemon flag or not, so one of those blocks process exit outright. Any
# future worker that owns an executor needs a real bound on the callable,
# not this budget.
LIFESPAN_JOIN_BUDGET_SECONDS = 5.0


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
    # Background threads this lifespan started and must join before it ends.
    # Services that own a thread AND their own stop() (download queue,
    # background scanner, notification bridge, AppService) keep joining it
    # themselves; this list is for the loose threads nobody else owns.
    _lifespan_threads: List[threading.Thread] = field(default_factory=list)
    _lifespan_threads_lock: threading.Lock = field(default_factory=threading.Lock)
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

                    self._plex_metadata_scan_job = PlexMetadataScanJob(
                        self.db, progress_cb=_broadcast, registry=self)
        return self._plex_metadata_scan_job

    def begin_lifespan(self) -> int:
        """Advance ownership and clear cancellation for one new app lifespan."""
        with self._lifespan_generation_lock:
            self._lifespan_generation += 1
            generation = self._lifespan_generation
        # A fresh lifespan owns no threads yet. Normally join_lifespan_threads()
        # has already emptied this during teardown; clearing again matters for
        # the ABANDONED-lifespan path (startup raised, so teardown never ran) —
        # otherwise the next shutdown would try to join a previous generation's
        # workers, which are no longer this lifespan's problem.
        with self._lifespan_threads_lock:
            self._lifespan_threads = []
        self._shutdown_event.clear()
        return generation

    def spawn_lifespan_thread(
        self,
        target: Callable,
        *,
        name: str,
        args: tuple = (),
        kwargs: Optional[Dict[str, Any]] = None,
    ) -> threading.Thread:
        """Start a daemon thread whose lifetime is bounded by this lifespan.

        Use this instead of a bare ``threading.Thread(...).start()`` for any
        background work started from the lifespan (or from a route, on the
        lifespan's behalf). The thread is joined by ``join_lifespan_threads``
        during teardown, so it cannot survive into the next lifespan and reach
        a service whose DB was already closed.

        The target is still responsible for NOTICING cancellation — poll
        ``shutdown_requested`` or block on ``wait_for_shutdown`` rather than
        ``time.sleep`` — the join only bounds how long teardown waits for it.
        """
        thread = threading.Thread(
            target=target, name=name, args=args, kwargs=kwargs or {}, daemon=True)
        # Tracking and start() happen under ONE lock hold, which is what makes
        # this safe against a concurrent spawn or teardown. A thread that has
        # been constructed but not started reports is_alive() == False exactly
        # like a finished one, so a racing register() would reap it as dead and
        # a racing join() would call join() on an unstarted thread (RuntimeError,
        # aborting the rest of the shutdown). Holding the lock across start()
        # closes both windows; the new thread never needs this lock, so it
        # cannot deadlock against us. (A target that itself spawns simply
        # blocks until start() returns, which does not wait on the child.)
        with self._lifespan_threads_lock:
            self._track_locked(thread)
            thread.start()
        return thread

    def register_lifespan_thread(self, thread: threading.Thread) -> None:
        """Track an ALREADY-STARTED thread as owned by this lifespan.

        Prefer ``spawn_lifespan_thread``; this exists for threads constructed
        elsewhere. Pass a started thread — an unstarted one is indistinguishable
        from a finished one here, and ``join_lifespan_threads`` would raise on
        it rather than wait for it.
        """
        with self._lifespan_threads_lock:
            self._track_locked(thread)

    def _track_locked(self, thread: threading.Thread) -> None:
        """Append ``thread`` to the tracked list. Caller holds the lock."""
        # Drop finished entries as we go: the real app runs one lifespan for
        # weeks, and per-scan/per-package threads would otherwise pile up
        # dead Thread objects for its whole life.
        self._lifespan_threads = [
            t for t in self._lifespan_threads if t.is_alive()]
        self._lifespan_threads.append(thread)

    def wait_for_shutdown(self, seconds: float) -> bool:
        """Sleep up to ``seconds``, waking immediately if shutdown is requested.

        The interruptible replacement for ``time.sleep`` in background workers:
        a plain sleep makes the thread unjoinable for its full duration, which
        is what turns a 30-second settle delay into a 30-second shutdown stall.

        Returns True if shutdown was requested (i.e. the caller should return).
        """
        return self._shutdown_event.wait(seconds)

    def join_lifespan_threads(
        self, timeout: float = LIFESPAN_JOIN_BUDGET_SECONDS
    ) -> List[str]:
        """Join every registry-owned thread within one shared time budget.

        ``timeout`` is the TOTAL wall clock spent here, not per thread, so N
        wedged workers cost the same as one. Returns the names of the threads
        still alive when the budget ran out (empty on a clean shutdown) for the
        caller to log. Abandoning them is safe for PLAIN daemon threads,
        which the interpreter does not wait for; it is NOT safe for a
        wedged ThreadPoolExecutor worker, which blocks interpreter exit
        regardless of its daemon flag (see the module constant above).
        """
        with self._lifespan_threads_lock:
            threads = list(self._lifespan_threads)
            self._lifespan_threads = []
        current = threading.current_thread()
        deadline = time.monotonic() + max(0.0, timeout)
        for thread in threads:
            # A worker that triggered shutdown itself (e.g. the /shutdown route
            # handler's thread) would deadlock for the whole budget joining
            # itself.
            if thread is current:
                continue
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            thread.join(timeout=remaining)
        return sorted({
            t.name for t in threads if t is not current and t.is_alive()})

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
