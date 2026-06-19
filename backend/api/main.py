"""FastAPI application for ScanHound backend API."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Any, Dict, Optional

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from backend.api.dependencies import ServiceRegistry, ScannerAppBridge, registry

logger = logging.getLogger(__name__)

__version__ = "2.0.0-dev"


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

    # Core backend — config, DB, plex manager, optional subsystems
    backend = AppService()
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
    download_svc = DownloadService(backend.config, backend.db)
    reg._download_service = download_svc
    download_svc.driver_preflight()  # log browser version; warn on drift early

    # Auto-grab
    auto_grab_svc = AutoGrabService(backend.config, download_svc)
    reg._auto_grab_service = auto_grab_svc

    # Watchlist
    if backend.watchlist_manager:
        reg._watchlist_manager = backend.watchlist_manager

    # Analytics
    from backend.analytics import StatsDashboard
    reg._analytics_dashboard = StatsDashboard(backend.db.db_path if backend.db else None)

    logger.info("All services initialized successfully")

    # Background poller: track download + extraction outcomes from JDownloader.
    _start_results_poller(reg)

    # Auto-connect to Plex on startup if configured
    if reg.config.get("auto_connect_plex") and reg.config.get("plex_url") and reg.config.get("plex_token"):
        import threading
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
                logger.warning("Auto-connect to Plex failed: %s", e)
                ws_manager.broadcast_sync({
                    "type": "plex:status",
                    "data": {"connected": False, "server": str(e), "movie_count": 0, "tv_count": 0},
                })
        threading.Thread(target=_auto_connect_plex, daemon=True, name="plex-auto-connect").start()


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
            except Exception as e:
                logger.debug("results poller error: %s", e)
            # Sleep in short slices so shutdown stays responsive.
            waited = 0.0
            while waited < interval and not reg.shutdown_requested:
                _time.sleep(0.5)
                waited += 0.5

    threading.Thread(target=_loop, daemon=True, name="jd-results-poller").start()
    logger.info("Download results poller started")


def _teardown_services(reg: ServiceRegistry) -> None:
    """Gracefully shut down all services."""
    reg.request_shutdown()  # stop the background results poller
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


def create_app(
    config_override: Optional[Dict[str, Any]] = None,
) -> FastAPI:
    """Create and configure the FastAPI application."""

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        logger.info("Starting ScanHound API v%s", __version__)
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
            "https://tauri.localhost", # Tauri production webview
            "tauri://localhost",       # Tauri custom protocol
        ],
        # Allow any localhost/127.0.0.1 port — Vite picks a free port at dev time
        allow_origin_regex=r"http://(localhost|127\.0\.0\.1)(:\d+)?",
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Auth nonce middleware — protects all HTTP endpoints.
    # When auth_nonce is empty (dev mode / no env var), auth is disabled.
    @app.middleware("http")
    async def auth_middleware(request: Request, call_next):
        nonce = registry.auth_nonce
        if nonce:
            # Skip auth for health check (needed for readiness probes)
            if request.url.path == "/health":
                return await call_next(request)
            auth_header = request.headers.get("authorization", "")
            if auth_header != f"Bearer {nonce}":
                return JSONResponse(
                    status_code=401,
                    content={"detail": "Unauthorized"},
                )
        return await call_next(request)

    # Register route modules
    from backend.api.routes import system, settings, sources, plex, scanner, results, downloads, analytics, watchlist, scheduler
    from backend.api import ws

    app.include_router(system.router)
    app.include_router(ws.router)
    app.include_router(settings.router)
    app.include_router(sources.router)
    app.include_router(plex.router)
    app.include_router(scanner.router)
    app.include_router(results.router)
    app.include_router(downloads.router)
    app.include_router(analytics.router)
    app.include_router(watchlist.router)
    app.include_router(scheduler.router)

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

        @app.get("/{full_path:path}")
        async def _serve_spa(full_path: str):
            candidate = _os.path.normpath(_os.path.join(frontend_dir, full_path))
            if candidate.startswith(_os.path.normpath(frontend_dir)) and _os.path.isfile(candidate):
                return FileResponse(candidate)
            return FileResponse(index_file)  # SPA fallback for client-side routes

        logger.info("Serving frontend from %s", frontend_dir)

    return app


# Default app instance for `uvicorn backend.api.main:app`
app = create_app()
