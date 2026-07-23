"""FastAPI application for ScanHound backend API."""
from __future__ import annotations

import asyncio
import logging
import threading
import time
from contextlib import asynccontextmanager
from typing import Any, Dict, Optional

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from backend.api.dependencies import (
    ServiceRegistry, ScannerAppBridge, registry,
    auth_enabled as _auth_enabled,
    token_authorized as _token_authorized,
    has_any_credential as _has_any_credential,
    allow_open as _allow_open,
)

logger = logging.getLogger(__name__)

__version__ = "2.0.0-dev"


def _should_auto_connect_plex(config: Dict[str, Any]) -> bool:
    """Whether startup should auto-connect to Plex given the current config.

    Direct mode needs a server URL + token; account mode (plex.tv) only needs
    a username + password — the URL and token are discovered after sign-in.
    Mirror the gate that ``PlexService.connect()`` applies so account mode is
    not silently skipped at startup.
    """
    if not config.get("auto_connect_plex"):
        return False
    if config.get("plex_connection_mode", "direct") == "account":
        return bool(config.get("plex_username") and config.get("plex_password"))
    return bool(config.get("plex_url") and config.get("plex_token"))


# Every object in this tuple belongs to exactly one FastAPI lifespan.  Keeping
# any of them across TestClient/app lifespans lets a new startup reach a service
# whose AppService/DatabaseManager was already shut down.
_REGISTRY_LIFESPAN_FIELDS = (
    "backend",
    "db",
    "bridge",
    "_scanner_service",
    "_plex_service",
    "_download_service",
    "_download_queue_service",
    "_auto_grab_service",
    "_notification_bridge",
    "_watchlist_manager",
    "_analytics_dashboard",
    "_background_scanner",
    "_rename_service",
    "_plex_metadata_scan_job",
)


def _clear_registry_lifespan_state(reg: ServiceRegistry) -> None:
    """Drop every reference owned by a completed or abandoned lifespan."""
    for field_name in _REGISTRY_LIFESPAN_FIELDS:
        setattr(reg, field_name, None)
    reg.config = {}


def _prepare_registry_for_startup(reg: ServiceRegistry) -> int:
    """Make a reused registry fresh and return its new ownership generation."""
    # Advancing the generation before clearing references means an old worker
    # remains stale even after begin_lifespan() clears the shared shutdown event.
    generation = reg.begin_lifespan()
    _clear_registry_lifespan_state(reg)
    return generation


def _init_services(
    reg: ServiceRegistry,
    config_override: Optional[Dict[str, Any]] = None,
) -> None:
    """Initialize all backend services into the registry.

    Mirrors the initialization pattern from main.py and
    ScannerController._ensure_services().
    """
    from backend.app_service import AppService
    from backend.notification_bridge import NotificationBridge

    # A module-level registry is intentionally reused by create_app(), including
    # repeated TestClient lifespans.  Clear the previous lifespan *before*
    # AppService.startup() runs its synchronous maintenance pass, otherwise that
    # pass can reach a stale RenameService whose DB was already closed.
    _prepare_registry_for_startup(reg)

    # Publish the new backend object immediately.  AppService.startup() creates
    # its DatabaseManager internally, so reg.db cannot truthfully be assigned
    # before startup; the critical safety property is that no old service or DB
    # reference remains reachable while startup maintenance executes.
    backend = AppService()
    reg.backend = backend
    startup_warnings = backend.startup()
    for w in startup_warnings:
        logger.warning("Startup warning: %s", w)

    if config_override:
        backend.config.update(config_override)

    reg.backend = backend
    reg.config = backend.config
    reg.db = backend.db

    # Notification bridge
    notif = NotificationBridge()
    notif.configure(backend.config)
    reg._notification_bridge = notif

    # Bridge adapter (provides interface MatchingEngine/WebScrapers expect)
    bridge = ScannerAppBridge(backend)
    reg.bridge = bridge

    # Load download history into bridge
    if backend.db:
        try:
            bridge.download_history = backend.load_download_history()
        except Exception:
            bridge.download_history = set()

    # Lazy-import heavy services
    from backend.matching import MatchingEngine
    from backend.plex_service import PlexService
    from backend.scanner_service import ScannerService
    from backend.download_service import DownloadService
    from backend.auto_grab_service import AutoGrabService
    from backend.scrapers import WebScrapers

    # Plex
    plex_svc = PlexService(backend.config, backend.db, backend.plex_manager)
    reg._plex_service = plex_svc

    # Scanner
    scrapers = WebScrapers(bridge)
    matching = MatchingEngine(bridge)
    scanner_svc = ScannerService(
        config=backend.config,
        db=backend.db,
        scrapers=scrapers,
        matching=matching,
        plex_service=plex_svc,
        tmdb_cache=backend.tmdb_cache,
        omdb_cache=backend.omdb_cache,
    )
    reg._scanner_service = scanner_svc

    # Downloads
    download_svc = DownloadService(backend.config, backend.db, server_mode=True)
    reg._download_service = download_svc
    download_svc.driver_preflight()  # log browser version; warn on drift early

    # Durable download queue: owns staggered batches and verification retries.
    from backend.download_queue import DownloadQueueService
    from backend.api.ws import ws_manager as _download_ws_manager

    def _on_queue_delivery() -> None:
        # Preserve the existing batch route's post-delivery cache annotation.
        try:
            scanner_svc.rematch_cache()
        except Exception:
            logger.debug("queued post-grab cache re-match skipped", exc_info=True)

    queue_svc = DownloadQueueService(
        backend.config,
        backend.db,
        download_svc,
        broadcast=_download_ws_manager.broadcast_sync,
        broadcast_flush=_download_ws_manager.broadcast_sync_wait,
        on_delivery=_on_queue_delivery,
        claim_lease_seconds=backend.config.get(
            "download_queue_claim_lease_seconds", 600
        ),
    )
    reg._download_queue_service = queue_svc
    queue_svc.start()

    # Auto-grab
    auto_grab_svc = AutoGrabService(backend.config, download_svc)
    reg._auto_grab_service = auto_grab_svc

    # Watchlist
    if backend.watchlist_manager:
        reg._watchlist_manager = backend.watchlist_manager

    # Analytics — pass the shared DatabaseManager so reads go through its
    # locked connection (Wave B1) instead of opening a second sqlite connection.
    from backend.analytics import StatsDashboard
    reg._analytics_dashboard = StatsDashboard(
        backend.db.db_path if backend.db else None, db_manager=backend.db)

    logger.info("All services initialized successfully")

    # Background poller: track download + extraction outcomes from JDownloader.
    _start_results_poller(reg)

    # Auto-connect to Plex on startup if configured (direct or account mode).
    if _should_auto_connect_plex(reg.config):
        # (threading is imported at module level — a local import here would
        # shadow it for the whole function and break earlier uses.)
        def _auto_connect_plex():
            from backend.api.ws import ws_manager
            try:
                plex_svc.connect()
                if plex_svc.plex_manager.is_connected:
                    # Broadcast connected immediately so the UI shows green right away
                    ws_manager.broadcast_sync({
                        "type": "plex:status",
                        "data": {
                            "connected": True,
                            "server": reg.config.get("plex_server_name", ""),
                            "movie_count": 0,
                            "tv_count": 0,
                        },
                    })
                    plex_svc.load_libraries(use_cache=True)
                    # Broadcast again with final library counts
                    ws_manager.broadcast_sync({
                        "type": "plex:status",
                        "data": {
                            "connected": True,
                            "server": reg.config.get("plex_server_name", ""),
                            "movie_count": len(plex_svc.plex_movies) if plex_svc.plex_movies else 0,
                            "tv_count": len(plex_svc.plex_tv) if plex_svc.plex_tv else 0,
                        },
                    })
                else:
                    ws_manager.broadcast_sync({
                        "type": "plex:status",
                        "data": {"connected": False, "server": "", "movie_count": 0, "tv_count": 0},
                    })
            except Exception as e:
                # Log the detail server-side, but don't broadcast the raw
                # exception text to every connected client — it can carry the
                # Plex URL / token depending on the underlying client.
                logger.warning("Auto-connect to Plex failed: %s", e)
                ws_manager.broadcast_sync({
                    "type": "plex:status",
                    "data": {"connected": False, "server": "", "movie_count": 0, "tv_count": 0},
                })
        threading.Thread(target=_auto_connect_plex, daemon=True, name="plex-auto-connect").start()

    # Backfill resolution/size/HDR/DV onto older download-history rows that
    # were grabbed before the metadata was captured (e.g. via batch grabs that
    # only sent url/title). Matched by URL against the scan cache, so it's the
    # exact release. Idempotent and cheap — runs once per startup, self-healing.
    if reg.db:
        try:
            reg.db.enrich_downloads_from_cache()
        except Exception:
            logger.warning("Download-history enrichment skipped")

    # Background pre-cache scanner — always created so /background/scan-now and
    # runtime toggling work; the loop self-gates on background_scan_enabled and
    # just sleeps while the feature is off (the default).
    from backend.background_scanner import BackgroundScanner
    reg._background_scanner = BackgroundScanner(reg)
    reg._background_scanner.start()

    # Auto-rename service — created so the JD poller hook and /rename endpoints
    # work; it self-gates on auto_rename_enabled (off by default).
    from backend.rename.service import RenameService
    reg._rename_service = RenameService(reg)

    # Plex library metadata scan job — constructed eagerly (like every other
    # service above) rather than lazily on first property access. The lazy
    # form raced: two concurrent sync-route requests hitting the property for
    # the very first time could both pass the "is None" check before either
    # assignment landed, yielding two independent job instances (each with
    # its own bounded ThreadPoolExecutor) and defeating both the max-2-worker
    # concurrency cap and the job's own start() re-entrancy lock.
    from backend.plex_metadata_scan import PlexMetadataScanJob
    from backend.api.ws import ws_manager as _ws_manager

    def _broadcast_metadata_scan_progress(status_dict):
        _ws_manager.broadcast_sync({
            "type": "plex:metadata_scan_progress",
            "data": status_dict,
        })

    reg._plex_metadata_scan_job = PlexMetadataScanJob(
        reg.db, progress_cb=_broadcast_metadata_scan_progress)

    # Crash recovery: any job left in the transient 'applying' state (process
    # died mid-move) is reset to 'matched' so it can be retried. The move is
    # crash-safe, so this never risks a half-applied file.
    if backend.db is not None:
        try:
            n = backend.db.reset_applying_rename_jobs()
            if n:
                logger.info("Recovered %d rename job(s) stuck in 'applying' "
                            "after an unclean shutdown", n)
        except Exception:
            logger.warning("applying-job recovery failed (non-fatal)", exc_info=True)

    # One-shot poster backfill for jobs created before poster capture existed
    # (they render as "No poster" otherwise). Delayed + threaded so startup
    # never blocks on TMDB; idempotent (only touches empty poster_path rows).
    def _poster_backfill():
        time.sleep(30)  # let the app settle first
        try:
            reg._rename_service.backfill_posters()
        except Exception:
            logger.debug("poster backfill failed (non-fatal)", exc_info=True)
    threading.Thread(target=_poster_backfill, name="poster-backfill",
                     daemon=True).start()

    # Surface a DB corruption quarantine (if init_db() hit one) now that the
    # notification bridge actually exists — DatabaseManager._notify_corruption
    # fires during init_db(), before this bridge is wired up, so it's a
    # best-effort bonus channel; this is the reliable, once-per-incident alert.
    if backend.db is not None:
        try:
            from backend.database import notify_db_corruption_once
            notify_db_corruption_once(backend.db.db_path, reg._notification_bridge)
        except Exception:
            logger.warning("DB corruption startup check failed (non-fatal)", exc_info=True)


def _rename_dedup_key(r: Dict[str, Any]) -> str:
    """Durable dedup key for the auto-rename hand-off set.

    Keyed by the package's ``package_uuid`` so two extracted packages that
    happen to share a display ``name`` are each handed to auto-rename rather
    than the second being silently skipped as a "duplicate". Legacy rows
    without a uuid (pre-migration) fall back to ``name``.
    """
    return r.get("package_uuid") or r.get("name") or ""


def _start_results_poller(reg: ServiceRegistry, interval: float = 8.0) -> None:
    """Background thread that tracks JDownloader download + extraction outcomes.

    Every ``interval`` seconds (only while the MyJDownloader API integration is
    enabled) it polls JDownloader, persists each package's download/extraction
    state to the DB, and broadcasts changes over the WebSocket so the Downloads
    page updates live. Stops when the registry signals shutdown.
    """
    import threading
    import time as _time
    from backend.api.ws import ws_manager

    def _loop():
        last_sig = None
        handed_to_rename: set = set()  # packages already sent to auto-rename
        while not reg.shutdown_requested:
            try:
                cfg = reg.config or {}
                dl = reg.download
                if dl and cfg.get("jd_enabled") and cfg.get("jd_method") == "api":
                    results = dl.poll_results(record=True)
                    sig = tuple(
                        (r["name"], r["state"], r["bytes_loaded"], r["extraction"])
                        for r in results
                    )
                    if sig != last_sig:
                        last_sig = sig
                        ws_manager.broadcast_sync({
                            "type": "download:results",
                            "data": {"results": results},
                        })
                    # Empirical package-name capture: persist JD's own reported
                    # name for any grab still awaiting confirmation, so pipeline
                    # matching is immune to JD's punctuation sanitization.
                    # Cheap no-op once every recent grab is captured.
                    if results and reg.db:
                        try:
                            reg.db.capture_jd_confirmed_names([r["name"] for r in results])
                        except Exception:
                            logger.debug("jd_confirmed_name capture failed", exc_info=True)
                    # Auto-rename hook: hand each newly-extracted package's output
                    # folder to the rename service (it self-gates on the setting
                    # and dedups by package). Runs off-thread so the poller never
                    # blocks on filesystem walks / TMDB lookups.
                    if cfg.get("auto_rename_enabled") and reg._rename_service:
                        for r in results:
                            key = _rename_dedup_key(r)
                            if (r.get("state") == "extracted" and r.get("save_to")
                                    and key not in handed_to_rename):
                                handed_to_rename.add(key)
                                threading.Thread(
                                    target=reg._rename_service.process_package,
                                    args=(r.get("name"), r.get("save_to")),
                                    name="auto-rename", daemon=True,
                                ).start()
                    # Prune stale keys so the set doesn't grow unbounded. Only
                    # when this poll actually returned rows — poll_results()
                    # returns [] on a transient JD failure, and clearing the
                    # set on an empty poll would re-hand every still-live
                    # package to auto-rename on the next good poll.
                    if results:
                        live_keys = {_rename_dedup_key(r) for r in results}
                        handed_to_rename &= live_keys
            except Exception as e:
                logger.debug("results poller error: %s", e)
            # Sleep in short slices so shutdown stays responsive.
            waited = 0.0
            while waited < interval and not reg.shutdown_requested:
                _time.sleep(0.5)
                waited += 0.5

    threading.Thread(target=_loop, daemon=True, name="jd-results-poller").start()
    logger.info("Download results poller started")


# Paths reachable without a token, e.g. readiness probes and the login flow
# itself (you have no token yet when logging in). Extend this set rather than
# bolting another bespoke comparison onto auth_middleware.
_AUTH_EXEMPT_PATHS = frozenset({"/health", "/auth/login", "/auth/status"})

# Additionally reachable without a token, but ONLY while no credential exists
# yet (no nonce, no password) — the first-run bootstrap surface. Once a
# password is set this path is protected again by the normal middleware rule
# (auth_enabled() is then True), matching the route's own "current password
# required to change an existing one" check in backend/api/routes/auth.py.
_BOOTSTRAP_EXEMPT_PATHS = frozenset({"/auth/set-password"})


def _is_auth_exempt(request: Request) -> bool:
    """Whether this request should pass the auth middleware without a token."""
    if request.method == "OPTIONS":
        # CORS preflight: browsers send OPTIONS with no credentials by design,
        # so auth-ing it would 401 the preflight and strip the CORS headers
        # CORSMiddleware needs to attach — blocking a non-same-origin client
        # (the Android APK) before its real, authed request is ever made.
        # OPTIONS returns only CORS metadata, no data.
        return True
    return request.url.path in _AUTH_EXEMPT_PATHS


def _bearer_token(request: Request) -> str:
    """Extract the bearer token from the Authorization header, if any."""
    header = request.headers.get("authorization", "")
    return header[7:] if header.startswith("Bearer ") else ""


# _auth_enabled / _token_authorized are imported from dependencies (top of file)
# so the WebSocket handshake in backend.api.ws shares the exact same gate.


def _compute_protected_segments(routers) -> frozenset:
    """Top-level path segments owned by API routers — what the middleware guards.

    Derived from the routers' own prefixes/paths (read directly off the router
    objects, which is stable; FastAPI wraps included routers opaquely in
    ``app.routes``), so new routers are covered automatically. The SPA shell and
    static assets have no API router, so they stay open for the login page.
    """
    segments = set()
    for router in routers:
        prefix = (getattr(router, "prefix", "") or "").lstrip("/")
        if prefix:
            segments.add(prefix.split("/", 1)[0])
            continue
        # Prefix-less router (e.g. system: /health, /discover, /shutdown).
        for route in getattr(router, "routes", []):
            segment = getattr(route, "path", "").lstrip("/").split("/", 1)[0]
            if segment and not segment.startswith("{"):
                segments.add(segment)
    return frozenset(segments)


def _request_requires_auth(request: Request) -> bool:
    """Whether this request must present a valid token to proceed.

    Fail-CLOSED posture: when no credential exists at all (no nonce, no
    password — e.g. a fresh install, or a corrupted/reset DB that silently
    dropped the auth_credentials row) protected routes are now DENIED rather
    than served openly, unless the explicit ``SCANHOUND_ALLOW_OPEN=1`` escape
    hatch is set. The bootstrap surface (``/auth/set-password`` plus the
    always-exempt health/login/status/OPTIONS) stays reachable so the first
    password can be set without a chicken-and-egg lockout.
    """
    if _is_auth_exempt(request):
        return False
    protected = getattr(request.app.state, "protected_segments", frozenset())
    segment = request.url.path.lstrip("/").split("/", 1)[0]
    if segment not in protected:
        return False  # SPA shell / static asset — served openly
    if _auth_enabled():
        return True  # normal case: a credential exists — gate on it
    if _allow_open():
        return False  # explicit opt-in to the old fully-open behavior
    if request.url.path in _BOOTSTRAP_EXEMPT_PATHS:
        return False  # let the first password be set
    return True  # fail CLOSED: no credential, no escape hatch — deny


def _within(path: str, base: str) -> bool:
    """Real path-containment check: is ``path`` equal to or inside ``base``?

    A bare ``startswith`` would let a sibling like ``.../build-evil`` or
    ``.../immutable-x`` pass a prefix test against ``.../build`` /
    ``.../immutable``. Both callers pass already-``os.path.normpath``-ed
    inputs. Shared by the SPA static-file guard below and the path-confinement
    checks in backend/api/routes/rename.py (A3) — one containment rule for
    every "does this resolved path stay inside its allowed root" check.
    """
    import os as _os
    return path == base or path.startswith(base + _os.sep)


def _teardown_services(reg: ServiceRegistry) -> None:
    """Gracefully shut down one lifespan, then erase its complete object graph."""
    reg.request_shutdown()  # stop the background results poller
    try:
        if reg._download_queue_service:
            try:
                reg._download_queue_service.stop()
            except Exception:
                pass
        if reg._background_scanner:
            try:
                reg._background_scanner.stop()
            except Exception:
                pass
        if reg._notification_bridge:
            try:
                reg._notification_bridge.shutdown()
            except Exception:
                pass
        if reg._watchlist_manager:
            try:
                reg._watchlist_manager.close()
            except Exception:
                pass
        if reg.backend:
            try:
                reg.backend.shutdown()
            except Exception:
                pass
    finally:
        # Even a failing shutdown hook must not leak a closed DB/service into the
        # next lifespan.  Leave the shutdown event set until the next startup so
        # late old threads still observe cancellation.
        _clear_registry_lifespan_state(reg)

def create_app(
    config_override: Optional[Dict[str, Any]] = None,
) -> FastAPI:
    """Create and configure the FastAPI application."""

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        logger.info("Starting ScanHound API v%s", __version__)
        # Capture the running loop BEFORE starting background services so their
        # early broadcasts (results poller, plex auto-connect) aren't dropped
        # while no WebSocket client has connected yet.
        from backend.api.ws import ws_manager
        ws_manager.set_loop(asyncio.get_running_loop())
        _init_services(registry, config_override=config_override)
        yield
        logger.info("Shutting down ScanHound API")
        _teardown_services(registry)

    app = FastAPI(
        title="ScanHound API",
        version=__version__,
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            "https://tauri.localhost", # Tauri production webview (useHttpsScheme)
            "http://tauri.localhost",  # Tauri >=2.x default scheme on Windows + Android
            "tauri://localhost",       # Tauri custom protocol (Linux/macOS)
        ],
        # A5: was `(:\d+)?` — any port on localhost/127.0.0.1, unbounded. Tightened
        # to the actual dev surface: tauri.conf.json pins devUrl to :5173, and Vite
        # increments by one (5174, 5175, …) if that port is already taken locally
        # (e.g. a second dev instance) rather than picking an arbitrary port — so a
        # small fixed window covers real usage without leaving every port on the
        # loopback interface as a trusted, credentialed CORS origin. Widen this
        # window (not the unbounded regex) if a dev setup needs more concurrent
        # instances than it covers.
        allow_origin_regex=r"http://(localhost|127\.0\.0\.1):517[0-9]",
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Bearer-token middleware — guards the API routes. A request is authorized
    # by either a valid login-session token or the desktop nonce. When neither
    # is configured (fresh install, or a reset/corrupted DB) protected routes
    # fail CLOSED — only the bootstrap surface (set-password/login/status/
    # health) stays reachable — unless SCANHOUND_ALLOW_OPEN=1 is set. The SPA
    # shell and static assets are always served openly either way.
    @app.middleware("http")
    async def auth_middleware(request: Request, call_next):
        if _request_requires_auth(request):
            if not _token_authorized(_bearer_token(request)):
                return JSONResponse(
                    status_code=401,
                    content={"detail": "Unauthorized"},
                )
        return await call_next(request)

    # Register route modules
    from backend.api.routes import system, settings, sources, plex, scanner, results, downloads, analytics, watchlist, scheduler, auth, background, rename, pipeline, rss
    from backend.api import ws

    api_routers = [
        system.router, auth.router, ws.router, settings.router, sources.router,
        plex.router, scanner.router, results.router, downloads.router,
        analytics.router, watchlist.router, scheduler.router, background.router,
        rename.router, pipeline.router, rss.router,
    ]
    for router in api_routers:
        app.include_router(router)

    # Snapshot which top-level path segments the bearer middleware guards. The
    # SPA shell + static assets have no API router and stay open (so the login
    # page can load before the user holds a token).
    app.state.protected_segments = _compute_protected_segments(api_routers)

    # ── Serve the built frontend (production / Docker) ──────────────────
    # When the SvelteKit static build is present, serve it from the API so the
    # whole app runs on a single origin (works behind a reverse proxy /
    # Cloudflare tunnel). API routes above are registered first and take
    # precedence; this catch-all handles the SPA + its assets.
    import os as _os
    from pathlib import Path as _Path
    frontend_dir = _os.environ.get("SCANHOUND_FRONTEND_DIR") or str(
        _Path(__file__).resolve().parents[2] / "frontend" / "build"
    )
    index_file = _os.path.join(frontend_dir, "index.html")
    if _os.path.isfile(index_file):
        from fastapi.responses import FileResponse

        _root = _os.path.normpath(frontend_dir)
        _immutable = _os.path.normpath(_os.path.join(frontend_dir, "_app", "immutable"))

        # _within is now module-level (see above) so rename.py's path-confinement
        # checks (A3) can share the exact same containment rule.

        @app.get("/{full_path:path}")
        async def _serve_spa(full_path: str):
            candidate = _os.path.normpath(_os.path.join(frontend_dir, full_path))
            if _within(candidate, _root) and _os.path.isfile(candidate):
                # Vite hashes immutable assets — safe to cache forever at CDN + browser.
                if _within(candidate, _immutable):
                    hdrs = {"Cache-Control": "public, max-age=31536000, immutable"}
                else:
                    hdrs = {"Cache-Control": "no-cache"}
                return FileResponse(candidate, headers=hdrs)
            # SPA fallback — index.html must never be cached so deploys take effect immediately.
            return FileResponse(index_file, headers={"Cache-Control": "no-cache"})

        logger.info("Serving frontend from %s", frontend_dir)

    return app


# Default app instance for `uvicorn backend.api.main:app`
app = create_app()
