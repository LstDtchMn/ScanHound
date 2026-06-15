# ScanHound v2.0 Phase 1 Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Rebuild ScanHound's frontend in Tauri v2 + Svelte 5 with a FastAPI backend API layer wrapping existing Python services.

**Architecture:** Python backend services remain unchanged. A new FastAPI layer in `backend/api/` exposes them via REST + WebSocket. A Svelte 5 + Tailwind CSS frontend in `frontend/` replaces QML. Tauri v2 manages the Python sidecar process, system tray, and desktop notifications.

**Tech Stack:** Python 3.10+, FastAPI, uvicorn, Tauri v2, Svelte 5, TypeScript, Tailwind CSS, Rust

**Spec:** `docs/superpowers/specs/2026-03-10-scanhound-v2-design.md`

---

## Chunk 1: Backend API Foundation

Sets up the FastAPI app, service dependency injection, health endpoint, and WebSocket hub. After this chunk, `uvicorn backend.api.main:app --port 9721` starts and serves `/health` and `/ws`.

### Task 1.1: FastAPI App Skeleton + Health Endpoint

**Files:**
- Create: `backend/api/__init__.py`
- Create: `backend/api/main.py`
- Create: `backend/api/dependencies.py`
- Create: `backend/api/routes/__init__.py`
- Create: `backend/api/routes/system.py`
- Modify: `backend/requirements.txt` (add fastapi, uvicorn, websockets)
- Create: `tests/test_api_system.py`

- [ ] **Step 1: Add API dependencies to requirements.txt**

Add to `backend/requirements.txt`:
```
fastapi>=0.115.0
uvicorn[standard]>=0.30.0
websockets>=12.0
```

- [ ] **Step 2: Write failing test for health endpoint**

```python
# tests/test_api_system.py
import pytest
from fastapi.testclient import TestClient
from backend.api.main import create_app

@pytest.fixture
def client(tmp_path):
    app = create_app(config_override={"plex_url": "", "plex_token": ""}, db_path=str(tmp_path / "test.db"))
    return TestClient(app)

def test_health_returns_ok(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert "version" in data

def test_shutdown_returns_accepted(client):
    resp = client.post("/shutdown")
    assert resp.status_code == 202
```

- [ ] **Step 3: Run test to verify it fails**

Run: `pytest tests/test_api_system.py -v`
Expected: FAIL (ModuleNotFoundError: backend.api.main)

- [ ] **Step 4: Create backend/api/__init__.py**

```python
# backend/api/__init__.py
```

- [ ] **Step 5: Create dependencies.py with service registry**

```python
# backend/api/dependencies.py
"""Service dependency injection for FastAPI."""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from backend.app_service import AppService
from backend.database import DatabaseManager
from backend.notification_bridge import NotificationBridge


@dataclass
class ServiceRegistry:
    """Holds all initialized backend service singletons."""

    config: Dict[str, Any] = field(default_factory=dict)
    backend: Optional[AppService] = None
    db: Optional[DatabaseManager] = None
    notifications: Optional[NotificationBridge] = None
    _scanner_service: Optional[Any] = None
    _plex_service: Optional[Any] = None
    _download_service: Optional[Any] = None
    _auto_grab_service: Optional[Any] = None
    _shutdown_event: threading.Event = field(default_factory=threading.Event)

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

    def request_shutdown(self):
        self._shutdown_event.set()

    @property
    def shutdown_requested(self) -> bool:
        return self._shutdown_event.is_set()


# Module-level singleton — populated during app lifespan
registry = ServiceRegistry()


def get_registry() -> ServiceRegistry:
    return registry
```

- [ ] **Step 6: Create main.py with lifespan and app factory**

```python
# backend/api/main.py
"""FastAPI application for ScanHound backend."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Any, Dict, Optional

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from backend.api.dependencies import ServiceRegistry, registry
from backend.app_service import AppService
from backend.database import DatabaseManager
from backend.notification_bridge import NotificationBridge

logger = logging.getLogger(__name__)

__version__ = "2.0.0-dev"


def _init_services(
    reg: ServiceRegistry,
    config_override: Optional[Dict[str, Any]] = None,
    db_path: Optional[str] = None,
) -> None:
    """Initialize all backend services into the registry."""
    from backend.config import get_default_config

    backend = AppService()
    startup_warnings = backend.startup()
    for w in startup_warnings:
        logger.warning("Startup warning: %s", w)

    if config_override:
        backend.config.update(config_override)

    reg.backend = backend
    reg.config = backend.config
    reg.db = backend.db

    notif = NotificationBridge()
    notif.configure(backend.config)
    reg.notifications = notif

    # Import heavy services lazily to keep startup visible
    from backend.matching import MatchingEngine
    from backend.metadata_enricher import MetadataEnricher
    from backend.plex_service import PlexService
    from backend.scanner_service import ScannerService
    from backend.download_service import DownloadService
    from backend.auto_grab_service import AutoGrabService
    from backend.scrapers import WebScrapers

    scrapers = WebScrapers(backend.config)
    matching = MatchingEngine(backend.config)
    plex_svc = PlexService(backend.config, backend.db, backend.plex_manager)

    scanner_svc = ScannerService(
        config=backend.config,
        db=backend.db,
        scrapers=scrapers,
        matching=matching,
        plex_service=plex_svc,
        tmdb_cache=getattr(backend, "_tmdb_cache", None),
        omdb_cache=getattr(backend, "_omdb_cache", None),
    )

    download_svc = DownloadService(backend.config, backend.db)
    auto_grab_svc = AutoGrabService(backend.config, download_svc)

    reg._scanner_service = scanner_svc
    reg._plex_service = plex_svc
    reg._download_service = download_svc
    reg._auto_grab_service = auto_grab_svc


def _teardown_services(reg: ServiceRegistry) -> None:
    """Gracefully shut down all services."""
    if reg.notifications:
        try:
            reg.notifications.shutdown()
        except Exception:
            pass
    if reg.backend:
        try:
            reg.backend.shutdown()
        except Exception:
            pass


def create_app(
    config_override: Optional[Dict[str, Any]] = None,
    db_path: Optional[str] = None,
) -> FastAPI:
    """Create and configure the FastAPI application."""

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        logger.info("Starting ScanHound API v%s", __version__)
        _init_services(registry, config_override=config_override, db_path=db_path)
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
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Register routes
    from backend.api.routes import system
    app.include_router(system.router)

    return app


# Default app instance for `uvicorn backend.api.main:app`
app = create_app()
```

- [ ] **Step 7: Create system routes (health + shutdown)**

```python
# backend/api/routes/__init__.py
```

```python
# backend/api/routes/system.py
"""System endpoints: health check, version, shutdown."""
from fastapi import APIRouter, Depends

from backend.api.dependencies import ServiceRegistry, get_registry
from backend.api.main import __version__

router = APIRouter(tags=["system"])


@router.get("/health")
def health(reg: ServiceRegistry = Depends(get_registry)):
    return {
        "status": "ok",
        "version": __version__,
        "plex_connected": bool(
            reg.plex and getattr(reg.plex, "plex_movies", None)
        ),
    }


@router.post("/shutdown", status_code=202)
def shutdown(reg: ServiceRegistry = Depends(get_registry)):
    reg.request_shutdown()
    return {"status": "shutting_down"}
```

- [ ] **Step 8: Run tests to verify they pass**

Run: `pytest tests/test_api_system.py -v`
Expected: 2 PASSED

- [ ] **Step 9: Verify manual startup**

Run: `cd backend && python -m uvicorn api.main:app --port 9721`
Visit: `http://localhost:9721/health`
Expected: `{"status":"ok","version":"2.0.0-dev",...}`

- [ ] **Step 10: Commit**

```bash
git add backend/api/ backend/requirements.txt tests/test_api_system.py
git commit -m "feat(api): FastAPI app skeleton with health and shutdown endpoints"
```

---

### Task 1.2: WebSocket Hub

**Files:**
- Create: `backend/api/ws.py`
- Modify: `backend/api/main.py` (register WS route)
- Create: `tests/test_api_ws.py`

- [ ] **Step 1: Write failing test for WebSocket connection**

```python
# tests/test_api_ws.py
import pytest
from fastapi.testclient import TestClient
from backend.api.main import create_app
from backend.api.ws import ws_manager

@pytest.fixture
def client(tmp_path):
    app = create_app(config_override={"plex_url": "", "plex_token": ""})
    return TestClient(app)

def test_ws_connect_and_receive_welcome(client):
    with client.websocket_connect("/ws") as ws:
        data = ws.receive_json()
        assert data["type"] == "connected"
        assert "version" in data["data"]

def test_ws_broadcast_reaches_client(client):
    with client.websocket_connect("/ws") as ws:
        _ = ws.receive_json()  # welcome
        ws_manager.broadcast_sync({"type": "test", "data": {"msg": "hello"}})
        data = ws.receive_json()
        assert data["type"] == "test"
        assert data["data"]["msg"] == "hello"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_api_ws.py -v`
Expected: FAIL (ImportError)

- [ ] **Step 3: Create ws.py with connection manager**

```python
# backend/api/ws.py
"""WebSocket hub for real-time communication."""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Dict, List

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

logger = logging.getLogger(__name__)
router = APIRouter()

__version__ = "2.0.0-dev"


class ConnectionManager:
    """Manages active WebSocket connections and broadcasts."""

    def __init__(self):
        self._connections: List[WebSocket] = []
        self._lock = asyncio.Lock()

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        async with self._lock:
            self._connections.append(ws)
        logger.info("WebSocket client connected (%d total)", len(self._connections))
        await ws.send_json({"type": "connected", "data": {"version": __version__}})

    async def disconnect(self, ws: WebSocket) -> None:
        async with self._lock:
            if ws in self._connections:
                self._connections.remove(ws)
        logger.info("WebSocket client disconnected (%d remaining)", len(self._connections))

    async def broadcast(self, message: Dict[str, Any]) -> None:
        async with self._lock:
            stale = []
            for ws in self._connections:
                try:
                    await ws.send_json(message)
                except Exception:
                    stale.append(ws)
            for ws in stale:
                self._connections.remove(ws)

    def broadcast_sync(self, message: Dict[str, Any]) -> None:
        """Thread-safe broadcast from sync code (e.g., scanner callbacks)."""
        try:
            loop = asyncio.get_running_loop()
            loop.call_soon_threadsafe(
                lambda: asyncio.ensure_future(self.broadcast(message))
            )
        except RuntimeError:
            # No running loop — skip (happens in tests without async context)
            pass


ws_manager = ConnectionManager()


@router.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws_manager.connect(ws)
    try:
        while True:
            raw = await ws.receive_text()
            try:
                msg = json.loads(raw)
                msg_type = msg.get("type", "")
                logger.debug("WS received: %s", msg_type)
                # Client→Server messages handled here in future tasks
            except json.JSONDecodeError:
                await ws.send_json({"type": "error", "data": {"message": "Invalid JSON"}})
    except WebSocketDisconnect:
        pass
    finally:
        await ws_manager.disconnect(ws)
```

- [ ] **Step 4: Register WS router in main.py**

Add to `create_app()` in `backend/api/main.py`, after the system router import:

```python
    from backend.api import ws
    app.include_router(ws.router)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/test_api_ws.py -v`
Expected: 2 PASSED

- [ ] **Step 6: Commit**

```bash
git add backend/api/ws.py backend/api/main.py tests/test_api_ws.py
git commit -m "feat(api): WebSocket hub with connection manager and broadcast"
```

---

## Chunk 2: Backend API Routes — Scanner, Results, Plex

### Task 2.1: Settings Routes

**Files:**
- Create: `backend/api/routes/settings.py`
- Modify: `backend/api/main.py` (register router)
- Create: `tests/test_api_settings.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_api_settings.py
import pytest
from fastapi.testclient import TestClient
from backend.api.main import create_app

@pytest.fixture
def client(tmp_path):
    app = create_app(config_override={"plex_url": "http://localhost:32400", "plex_token": "abc"})
    return TestClient(app)

def test_get_settings(client):
    resp = client.get("/settings")
    assert resp.status_code == 200
    data = resp.json()
    assert "plex_url" in data
    # Sensitive fields should be masked
    assert data["plex_token"] != "abc"

def test_put_settings_partial_update(client):
    resp = client.put("/settings", json={"theme_mode": "light"})
    assert resp.status_code == 200
    # Verify it was applied
    resp2 = client.get("/settings")
    assert resp2.json()["theme_mode"] == "light"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_api_settings.py -v`
Expected: FAIL

- [ ] **Step 3: Create settings routes**

```python
# backend/api/routes/settings.py
"""Settings endpoints: get/update configuration."""
from typing import Any, Dict

from fastapi import APIRouter, Depends

from backend.api.dependencies import ServiceRegistry, get_registry

router = APIRouter(prefix="/settings", tags=["settings"])

SENSITIVE_KEYS = {"plex_token", "tmdb_api_key", "omdb_api_key", "cuty_password",
                  "adithd_password", "discord_webhook_url", "smtp_password"}


def _mask_config(config: Dict[str, Any]) -> Dict[str, Any]:
    """Return config with sensitive values masked."""
    masked = dict(config)
    for key in SENSITIVE_KEYS:
        if key in masked and masked[key]:
            masked[key] = "••••••••"
    return masked


@router.get("")
def get_settings(reg: ServiceRegistry = Depends(get_registry)):
    return _mask_config(reg.config)


@router.put("")
def update_settings(
    updates: Dict[str, Any],
    reg: ServiceRegistry = Depends(get_registry),
):
    # Filter out masked values (user didn't change them)
    real_updates = {k: v for k, v in updates.items() if v != "••••••••"}
    reg.config.update(real_updates)
    if reg.backend:
        reg.backend.save_config()
    return {"status": "ok", "updated_keys": list(real_updates.keys())}
```

- [ ] **Step 4: Register router in main.py**

```python
    from backend.api.routes import settings
    app.include_router(settings.router)
```

- [ ] **Step 5: Run tests**

Run: `pytest tests/test_api_settings.py -v`
Expected: 2 PASSED

- [ ] **Step 6: Commit**

```bash
git add backend/api/routes/settings.py backend/api/main.py tests/test_api_settings.py
git commit -m "feat(api): settings GET/PUT endpoints with sensitive field masking"
```

---

### Task 2.2: Sources Routes

**Files:**
- Create: `backend/api/routes/sources.py`
- Modify: `backend/api/main.py`
- Create: `tests/test_api_sources.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_api_sources.py
import pytest
from fastapi.testclient import TestClient
from backend.api.main import create_app

@pytest.fixture
def client(tmp_path):
    app = create_app(config_override={"plex_url": "", "plex_token": ""})
    return TestClient(app)

def test_list_sources(client):
    resp = client.get("/sources")
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, list)
    assert any(s["id"] == "hdencode" for s in data)

def test_toggle_source(client):
    resp = client.put("/sources/hdencode", json={"enabled": False})
    assert resp.status_code == 200
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_api_sources.py -v`
Expected: FAIL

- [ ] **Step 3: Create sources routes**

```python
# backend/api/routes/sources.py
"""Source plugin endpoints."""
from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException

from backend.api.dependencies import ServiceRegistry, get_registry
from backend.sources.registry import SourceRegistry

router = APIRouter(prefix="/sources", tags=["sources"])


@router.get("")
def list_sources(reg: ServiceRegistry = Depends(get_registry)):
    source_reg = SourceRegistry()
    sources = []
    for name, cls in source_reg.get_all().items():
        config_key = f"{name}_enabled"
        sources.append({
            "id": name,
            "name": cls.display_name if hasattr(cls, "display_name") else name,
            "enabled": reg.config.get(config_key, True),
            "capabilities": [c.value if hasattr(c, "value") else str(c)
                             for c in getattr(cls, "capabilities", [])],
        })
    return sources


@router.put("/{source_id}")
def update_source(
    source_id: str,
    body: Dict[str, Any],
    reg: ServiceRegistry = Depends(get_registry),
):
    config_key = f"{source_id}_enabled"
    if "enabled" in body:
        reg.config[config_key] = body["enabled"]
        if reg.backend:
            reg.backend.save_config()
    return {"status": "ok", "source": source_id}
```

- [ ] **Step 4: Register router in main.py**

```python
    from backend.api.routes import sources
    app.include_router(sources.router)
```

- [ ] **Step 5: Run tests**

Run: `pytest tests/test_api_sources.py -v`
Expected: 2 PASSED

- [ ] **Step 6: Commit**

```bash
git add backend/api/routes/sources.py backend/api/main.py tests/test_api_sources.py
git commit -m "feat(api): sources list and toggle endpoints"
```

---

### Task 2.3: Plex Routes

**Files:**
- Create: `backend/api/routes/plex.py`
- Modify: `backend/api/main.py`
- Create: `tests/test_api_plex.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_api_plex.py
import pytest
from unittest.mock import MagicMock, patch
from fastapi.testclient import TestClient
from backend.api.main import create_app
from backend.api.dependencies import registry

@pytest.fixture
def client(tmp_path):
    app = create_app(config_override={"plex_url": "http://localhost:32400", "plex_token": "test"})
    return TestClient(app)

def test_plex_status_disconnected(client):
    resp = client.get("/plex/status")
    assert resp.status_code == 200
    data = resp.json()
    assert "connected" in data

def test_plex_connect_returns_result(client):
    # Mock the plex service connect method
    if registry.plex:
        registry.plex.connect = MagicMock(return_value=(True, "Connected to TestServer"))
    resp = client.post("/plex/connect")
    assert resp.status_code == 200

def test_plex_libraries(client):
    resp = client.get("/plex/libraries")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_api_plex.py -v`
Expected: FAIL

- [ ] **Step 3: Create plex routes**

```python
# backend/api/routes/plex.py
"""Plex integration endpoints."""
from __future__ import annotations

import threading
import logging
from typing import Any, Dict, List

from fastapi import APIRouter, Depends, BackgroundTasks

from backend.api.dependencies import ServiceRegistry, get_registry
from backend.api.ws import ws_manager

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/plex", tags=["plex"])


@router.get("/status")
def plex_status(reg: ServiceRegistry = Depends(get_registry)):
    plex = reg.plex
    connected = bool(plex and getattr(plex, "plex_movies", None))
    server_name = reg.config.get("plex_server_name", "")
    return {
        "connected": connected,
        "server": server_name,
        "movie_count": len(plex.plex_movies) if plex else 0,
        "tv_count": len(plex.plex_tv) if plex else 0,
    }


@router.post("/connect")
def plex_connect(
    background_tasks: BackgroundTasks,
    reg: ServiceRegistry = Depends(get_registry),
):
    plex = reg.plex
    if not plex:
        return {"success": False, "message": "Plex service not initialized"}

    def _connect():
        success, message = plex.connect()
        ws_manager.broadcast_sync({
            "type": "plex:status",
            "data": {"connected": success, "server": message},
        })
        if success:
            plex.load_libraries()

    background_tasks.add_task(_connect)
    return {"status": "connecting"}


@router.get("/libraries")
def plex_libraries(reg: ServiceRegistry = Depends(get_registry)):
    plex = reg.plex
    if not plex:
        return []
    # Return library names from config
    selected = reg.config.get("plex_libraries", [])
    return [{"name": lib, "selected": lib in selected} for lib in selected]


@router.get("/stats")
def plex_stats(reg: ServiceRegistry = Depends(get_registry)):
    plex = reg.plex
    if not plex:
        return {}
    return getattr(plex, "stats", {})
```

- [ ] **Step 4: Register in main.py**

```python
    from backend.api.routes import plex
    app.include_router(plex.router)
```

- [ ] **Step 5: Run tests**

Run: `pytest tests/test_api_plex.py -v`
Expected: 3 PASSED

- [ ] **Step 6: Commit**

```bash
git add backend/api/routes/plex.py backend/api/main.py tests/test_api_plex.py
git commit -m "feat(api): Plex connection, status, libraries, and stats endpoints"
```

---

### Task 2.4: Scanner Routes

**Files:**
- Create: `backend/api/routes/scanner.py`
- Modify: `backend/api/main.py`
- Create: `tests/test_api_scanner.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_api_scanner.py
import pytest
from unittest.mock import MagicMock, patch
from fastapi.testclient import TestClient
from backend.api.main import create_app
from backend.api.dependencies import registry

@pytest.fixture
def client(tmp_path):
    app = create_app(config_override={"plex_url": "", "plex_token": ""})
    return TestClient(app)

def test_scan_status_idle(client):
    resp = client.get("/scan/status")
    assert resp.status_code == 200
    data = resp.json()
    assert data["state"] == "idle"

def test_scan_start(client):
    if registry.scanner:
        registry.scanner.run_scan = MagicMock(return_value=[])
    resp = client.post("/scan/start", json={"type": "deep"})
    assert resp.status_code == 200
    assert "status" in resp.json()

def test_scan_stop(client):
    resp = client.post("/scan/stop")
    assert resp.status_code == 200
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_api_scanner.py -v`
Expected: FAIL

- [ ] **Step 3: Create scanner routes**

```python
# backend/api/routes/scanner.py
"""Scanner endpoints: start, stop, status."""
from __future__ import annotations

import logging
import threading
import time
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from backend.api.dependencies import ServiceRegistry, get_registry
from backend.api.ws import ws_manager

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/scan", tags=["scanner"])

# Scan state tracking
_scan_thread: Optional[threading.Thread] = None
_scan_state = {"state": "idle", "progress": 0.0, "phase": "", "scanned": 0, "total": 0}
_scan_lock = threading.Lock()


class ScanRequest(BaseModel):
    type: str = "deep"  # deep, incremental, loaded, search
    sources: Optional[List[str]] = None
    search_query: str = ""
    pages: int = 1


def _progress_callback(progress: float, phase: str) -> None:
    """Called by ScannerService during scan — broadcasts via WebSocket."""
    _scan_state["progress"] = progress
    _scan_state["phase"] = phase
    ws_manager.broadcast_sync({
        "type": "scan:progress",
        "data": {"progress": progress, "phase": phase},
    })


def _run_scan(reg: ServiceRegistry, req: ScanRequest) -> None:
    """Execute scan in background thread."""
    global _scan_thread

    scanner = reg.scanner
    if not scanner:
        return

    with _scan_lock:
        _scan_state["state"] = "running"
        _scan_state["progress"] = 0.0
        _scan_state["phase"] = "starting"

    scanner.set_progress_callback(_progress_callback)
    start_time = time.time()

    try:
        source_type = "all"
        if req.sources and len(req.sources) == 1:
            source_type = req.sources[0]

        items = scanner.run_scan(
            scan_type=req.type,
            source_type=source_type,
            pages=req.pages,
            search_query=req.search_query,
        )

        duration = time.time() - start_time

        # Broadcast each result
        for item in (items or []):
            item_dict = item.__dict__ if hasattr(item, "__dict__") else item
            ws_manager.broadcast_sync({"type": "scan:result", "data": item_dict})

        # Stats
        stats = {
            "total": len(items) if items else 0,
            "missing": sum(1 for i in (items or []) if getattr(i, "status", "") == "missing"),
            "upgrades": sum(1 for i in (items or []) if "upgrade" in getattr(i, "status", "")),
        }

        ws_manager.broadcast_sync({
            "type": "scan:complete",
            "data": {"stats": stats, "duration": round(duration, 1)},
        })

        # Auto-grab
        if reg.auto_grab and reg.auto_grab.enabled and items:
            reg.auto_grab.process_items(items)

    except Exception as e:
        logger.exception("Scan failed")
        ws_manager.broadcast_sync({
            "type": "scan:error",
            "data": {"message": str(e)},
        })
    finally:
        with _scan_lock:
            _scan_state["state"] = "idle"
            _scan_state["progress"] = 0.0
            _scan_state["phase"] = ""


@router.get("/status")
def scan_status():
    return dict(_scan_state)


@router.post("/start")
def scan_start(
    req: ScanRequest,
    reg: ServiceRegistry = Depends(get_registry),
):
    global _scan_thread

    with _scan_lock:
        if _scan_state["state"] == "running":
            return {"status": "already_running"}

    _scan_thread = threading.Thread(target=_run_scan, args=(reg, req), daemon=True)
    _scan_thread.start()
    return {"status": "started", "type": req.type}


@router.post("/stop")
def scan_stop(reg: ServiceRegistry = Depends(get_registry)):
    scanner = reg.scanner
    if scanner:
        scanner.stop_scan_flag = True
    with _scan_lock:
        _scan_state["state"] = "stopping"
    return {"status": "stopping"}
```

- [ ] **Step 4: Register in main.py**

```python
    from backend.api.routes import scanner
    app.include_router(scanner.router)
```

- [ ] **Step 5: Run tests**

Run: `pytest tests/test_api_scanner.py -v`
Expected: 3 PASSED

- [ ] **Step 6: Commit**

```bash
git add backend/api/routes/scanner.py backend/api/main.py tests/test_api_scanner.py
git commit -m "feat(api): scanner start/stop/status with WebSocket progress"
```

---

### Task 2.5: Results Routes

**Files:**
- Create: `backend/api/routes/results.py`
- Modify: `backend/api/main.py`
- Create: `tests/test_api_results.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_api_results.py
import pytest
from fastapi.testclient import TestClient
from backend.api.main import create_app

@pytest.fixture
def client(tmp_path):
    app = create_app(config_override={"plex_url": "", "plex_token": ""})
    return TestClient(app)

def test_get_results_empty(client):
    resp = client.get("/results")
    assert resp.status_code == 200
    data = resp.json()
    assert data["items"] == []
    assert data["total"] == 0

def test_select_items(client):
    resp = client.post("/results/select", json={"group_keys": ["test|S0"], "selected": True})
    assert resp.status_code == 200
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_api_results.py -v`
Expected: FAIL

- [ ] **Step 3: Create results routes**

```python
# backend/api/routes/results.py
"""Results endpoints: list, filter, select, export."""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from backend.api.dependencies import ServiceRegistry, get_registry

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/results", tags=["results"])

# In-memory results store (populated during scans via WebSocket)
_results: List[Dict[str, Any]] = []
_selected: set = set()


def set_results(items: List[Any]) -> None:
    """Called by scanner route after scan completes."""
    global _results
    _results = [i.__dict__ if hasattr(i, "__dict__") else i for i in items]


def clear_results() -> None:
    global _results, _selected
    _results = []
    _selected = set()


class SelectRequest(BaseModel):
    group_keys: List[str]
    selected: bool = True


@router.get("")
def get_results(
    filter: Optional[str] = Query(None, description="Status filter: missing, upgrade, library, new"),
    search: Optional[str] = Query(None),
    sort: str = Query("title", description="Sort field"),
    order: str = Query("asc"),
    page: int = Query(1, ge=1),
    per_page: int = Query(100, ge=1, le=500),
    reg: ServiceRegistry = Depends(get_registry),
):
    items = list(_results)

    # Filter by status
    if filter:
        items = [i for i in items if i.get("status") == filter or filter in i.get("status", "")]

    # Search by title
    if search:
        search_lower = search.lower()
        items = [i for i in items if search_lower in i.get("title", "").lower()]

    # Sort
    reverse = order == "desc"
    items.sort(key=lambda x: x.get(sort, ""), reverse=reverse)

    total = len(items)
    start = (page - 1) * per_page
    end = start + per_page
    page_items = items[start:end]

    # Annotate selection state
    for item in page_items:
        item["selected"] = item.get("group_key", "") in _selected

    return {
        "items": page_items,
        "total": total,
        "page": page,
        "per_page": per_page,
        "stats": {
            "missing": sum(1 for i in _results if i.get("status") == "missing"),
            "upgrade": sum(1 for i in _results if "upgrade" in i.get("status", "")),
            "library": sum(1 for i in _results if i.get("status") == "in_library"),
            "total": len(_results),
        },
    }


@router.post("/select")
def select_items(req: SelectRequest):
    if req.selected:
        _selected.update(req.group_keys)
    else:
        _selected.difference_update(req.group_keys)
    return {"status": "ok", "selected_count": len(_selected)}


@router.post("/select-all")
def select_all():
    for item in _results:
        gk = item.get("group_key", "")
        if gk:
            _selected.add(gk)
    return {"status": "ok", "selected_count": len(_selected)}


@router.post("/deselect-all")
def deselect_all():
    _selected.clear()
    return {"status": "ok", "selected_count": 0}


@router.post("/export")
def export_csv(reg: ServiceRegistry = Depends(get_registry)):
    download = reg.download
    if not download:
        return {"error": "Download service not available"}
    filepath = download.export_results_csv(_results)
    return {"status": "ok", "filepath": filepath}
```

- [ ] **Step 4: Register in main.py**

```python
    from backend.api.routes import results
    app.include_router(results.router)
```

- [ ] **Step 5: Run tests**

Run: `pytest tests/test_api_results.py -v`
Expected: 2 PASSED

- [ ] **Step 6: Commit**

```bash
git add backend/api/routes/results.py backend/api/main.py tests/test_api_results.py
git commit -m "feat(api): results list, filter, select, and export endpoints"
```

---

### Task 2.6: Download Routes

**Files:**
- Create: `backend/api/routes/downloads.py`
- Modify: `backend/api/main.py`
- Create: `tests/test_api_downloads.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_api_downloads.py
import pytest
from fastapi.testclient import TestClient
from backend.api.main import create_app

@pytest.fixture
def client(tmp_path):
    app = create_app(config_override={"plex_url": "", "plex_token": ""})
    return TestClient(app)

def test_download_history_empty(client):
    resp = client.get("/download/history")
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, list)

def test_download_requires_url(client):
    resp = client.post("/download", json={})
    assert resp.status_code == 422  # validation error
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_api_downloads.py -v`
Expected: FAIL

- [ ] **Step 3: Create download routes**

```python
# backend/api/routes/downloads.py
"""Download endpoints: send to JDownloader, history, open in Plex."""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, BackgroundTasks
from pydantic import BaseModel

from backend.api.dependencies import ServiceRegistry, get_registry
from backend.api.ws import ws_manager

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/download", tags=["downloads"])


class DownloadRequest(BaseModel):
    url: str
    title: str = ""
    season: Optional[int] = None
    resolution: str = ""
    size: str = ""


class BatchDownloadRequest(BaseModel):
    items: List[DownloadRequest]


class OpenPlexRequest(BaseModel):
    title: str
    year: Optional[int] = None
    season: Optional[int] = None
    imdb_id: Optional[str] = None
    plex_rating_key: Optional[str] = None


@router.post("")
def download_item(
    req: DownloadRequest,
    background_tasks: BackgroundTasks,
    reg: ServiceRegistry = Depends(get_registry),
):
    dl = reg.download
    if not dl:
        return {"error": "Download service not available"}

    def _do_download():
        result = dl.download_item(
            url=req.url, title=req.title, season=req.season,
            resolution=req.resolution, size=req.size,
        )
        ws_manager.broadcast_sync({
            "type": "notification",
            "data": {"title": "Download", "body": f"Sent: {req.title}", "priority": "normal"},
        })

    background_tasks.add_task(_do_download)
    return {"status": "started", "title": req.title}


@router.post("/batch")
def download_batch(
    req: BatchDownloadRequest,
    background_tasks: BackgroundTasks,
    reg: ServiceRegistry = Depends(get_registry),
):
    dl = reg.download
    if not dl:
        return {"error": "Download service not available"}

    links = [item.url for item in req.items]
    package_name = req.items[0].title if req.items else "ScanHound Batch"

    def _do_batch():
        dl.send_to_jdownloader(links, package_name)
        for item in req.items:
            dl.save_to_history(item.url, item.title, item.season, item.resolution, item.size)
        ws_manager.broadcast_sync({
            "type": "notification",
            "data": {"title": "Batch Download", "body": f"Sent {len(links)} items", "priority": "normal"},
        })

    background_tasks.add_task(_do_batch)
    return {"status": "started", "count": len(links)}


@router.post("/open-plex")
def open_in_plex(
    req: OpenPlexRequest,
    reg: ServiceRegistry = Depends(get_registry),
):
    dl = reg.download
    plex = reg.plex
    if not dl or not plex:
        return {"url": None}
    url = dl.open_in_plex(
        title=req.title,
        plex_movies=plex.plex_movies,
        plex_tv=plex.plex_tv,
        year=req.year,
        season=req.season,
        imdb_id=req.imdb_id,
        plex_rating_key=req.plex_rating_key,
    )
    return {"url": url}


@router.get("/history")
def download_history(
    limit: int = 100,
    reg: ServiceRegistry = Depends(get_registry),
):
    if reg.db:
        return reg.db.get_download_history(limit=limit)
    return []
```

- [ ] **Step 4: Register in main.py**

```python
    from backend.api.routes import downloads
    app.include_router(downloads.router)
```

- [ ] **Step 5: Run tests**

Run: `pytest tests/test_api_downloads.py -v`
Expected: 2 PASSED

- [ ] **Step 6: Commit**

```bash
git add backend/api/routes/downloads.py backend/api/main.py tests/test_api_downloads.py
git commit -m "feat(api): download, batch download, open-in-plex, and history endpoints"
```

---

## Chunk 3: Frontend Scaffolding — Tauri + Svelte + Tailwind

### Task 3.1: Initialize Tauri v2 + SvelteKit Project

**Files:**
- Create: `frontend/` (entire scaffolded project)

- [ ] **Step 1: Create SvelteKit project**

```bash
cd c:/Users/NLSur/OneDrive/Documents/MediaScout
npm create svelte@latest frontend -- --template skeleton --types typescript
cd frontend
npm install
```

- [ ] **Step 2: Add Tailwind CSS**

```bash
cd frontend
npm install -D tailwindcss @tailwindcss/vite
```

Create `frontend/src/app.css`:
```css
@import "tailwindcss";

:root {
  --bg-primary: #0f1117;
  --bg-secondary: #1a1d27;
  --bg-tertiary: #242736;
  --text-primary: #e4e4e7;
  --text-secondary: #a1a1aa;
  --accent: #06b6d4;
  --accent-hover: #22d3ee;
  --success: #22c55e;
  --warning: #f59e0b;
  --error: #ef4444;
  --border: #2e3144;
}

[data-theme="light"] {
  --bg-primary: #ffffff;
  --bg-secondary: #f4f4f5;
  --bg-tertiary: #e4e4e7;
  --text-primary: #18181b;
  --text-secondary: #52525b;
  --accent: #0891b2;
  --accent-hover: #06b6d4;
  --border: #d4d4d8;
}

body {
  background-color: var(--bg-primary);
  color: var(--text-primary);
  font-family: system-ui, -apple-system, sans-serif;
}
```

Add to `frontend/vite.config.ts`:
```typescript
import tailwindcss from "@tailwindcss/vite";
// Add tailwindcss() to plugins array
```

- [ ] **Step 3: Initialize Tauri v2**

```bash
cd frontend
npm install -D @tauri-apps/cli@latest
npx tauri init
```

When prompted:
- App name: ScanHound
- Window title: ScanHound
- Dev server URL: http://localhost:5173
- Build dir: ../build

- [ ] **Step 4: Install Tauri JS API packages**

```bash
cd frontend
npm install @tauri-apps/api @tauri-apps/plugin-shell @tauri-apps/plugin-notification
```

- [ ] **Step 5: Configure tauri.conf.json**

Update `frontend/src-tauri/tauri.conf.json`:
```jsonc
{
  "productName": "ScanHound",
  "identifier": "com.scanhound.app",
  "build": {
    "frontendDist": "../build",
    "devUrl": "http://localhost:5173",
    "beforeDevCommand": "npm run dev",
    "beforeBuildCommand": "npm run build"
  },
  "app": {
    "withGlobalTauri": true,
    "windows": [
      {
        "title": "ScanHound",
        "width": 1600,
        "height": 950,
        "minWidth": 1000,
        "minHeight": 600
      }
    ]
  },
  "bundle": {
    "active": true,
    "icon": [
      "icons/32x32.png",
      "icons/128x128.png",
      "icons/icon.ico"
    ]
  }
}
```

- [ ] **Step 6: Verify dev server starts**

```bash
cd frontend && npm run dev
```
Expected: SvelteKit dev server running on http://localhost:5173

- [ ] **Step 7: Commit**

```bash
git add frontend/
git commit -m "feat(frontend): scaffold Tauri v2 + SvelteKit + Tailwind project"
```

---

### Task 3.2: API Client + Connection Store

**Files:**
- Create: `frontend/src/lib/api/client.ts`
- Create: `frontend/src/lib/api/types.ts`
- Create: `frontend/src/lib/stores/connection.ts`

- [ ] **Step 1: Create shared TypeScript types**

```typescript
// frontend/src/lib/api/types.ts

export interface ScanResult {
  title: string;
  year: number | null;
  season: number | null;
  resolution: string;
  size: string;
  status: string;
  color: string;
  url: string;
  group_key: string;
  rating: number | null;
  votes: number | null;
  rt_critics: number | null;
  rt_audience: number | null;
  genres: string[];
  language: string;
  poster_url: string;
  imdb_id: string | null;
  tmdb_id: number | null;
  description: string;
  hdr: boolean;
  dovi: boolean;
  selected: boolean;
  plex_rating_key: string | null;
}

export interface ScanStats {
  total: number;
  missing: number;
  upgrade: number;
  library: number;
}

export interface ResultsResponse {
  items: ScanResult[];
  total: number;
  page: number;
  per_page: number;
  stats: ScanStats;
}

export interface WsMessage {
  type: string;
  data: Record<string, unknown>;
}

export interface PlexStatus {
  connected: boolean;
  server: string;
  movie_count: number;
  tv_count: number;
}

export interface HealthResponse {
  status: string;
  version: string;
  plex_connected: boolean;
}
```

- [ ] **Step 2: Create API client**

```typescript
// frontend/src/lib/api/client.ts

const API_BASE = "http://localhost:9721";

async function request<T>(path: string, options?: RequestInit): Promise<T> {
  const resp = await fetch(`${API_BASE}${path}`, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  if (!resp.ok) {
    throw new Error(`API error: ${resp.status} ${resp.statusText}`);
  }
  return resp.json();
}

export const api = {
  // System
  health: () => request<{ status: string; version: string }>("/health"),
  shutdown: () => request<{ status: string }>("/shutdown", { method: "POST" }),

  // Scanner
  scanStart: (type = "deep", searchQuery = "", pages = 1) =>
    request("/scan/start", {
      method: "POST",
      body: JSON.stringify({ type, search_query: searchQuery, pages }),
    }),
  scanStop: () => request("/scan/stop", { method: "POST" }),
  scanStatus: () => request<{ state: string; progress: number; phase: string }>("/scan/status"),

  // Results
  getResults: (params?: Record<string, string>) => {
    const qs = params ? "?" + new URLSearchParams(params).toString() : "";
    return request<import("./types").ResultsResponse>(`/results${qs}`);
  },
  selectItems: (groupKeys: string[], selected: boolean) =>
    request("/results/select", {
      method: "POST",
      body: JSON.stringify({ group_keys: groupKeys, selected }),
    }),
  selectAll: () => request("/results/select-all", { method: "POST" }),
  deselectAll: () => request("/results/deselect-all", { method: "POST" }),
  exportCsv: () => request<{ filepath: string }>("/results/export", { method: "POST" }),

  // Plex
  plexConnect: () => request("/plex/connect", { method: "POST" }),
  plexStatus: () => request<import("./types").PlexStatus>("/plex/status"),
  plexLibraries: () => request<{ name: string; selected: boolean }[]>("/plex/libraries"),
  plexStats: () => request<Record<string, number>>("/plex/stats"),

  // Downloads
  download: (url: string, title: string) =>
    request("/download", { method: "POST", body: JSON.stringify({ url, title }) }),
  downloadBatch: (items: { url: string; title: string }[]) =>
    request("/download/batch", { method: "POST", body: JSON.stringify({ items }) }),
  openInPlex: (title: string, imdbId?: string, plexRatingKey?: string) =>
    request("/download/open-plex", {
      method: "POST",
      body: JSON.stringify({ title, imdb_id: imdbId, plex_rating_key: plexRatingKey }),
    }),
  downloadHistory: (limit = 100) => request<Record<string, unknown>[]>(`/download/history?limit=${limit}`),

  // Settings
  getSettings: () => request<Record<string, unknown>>("/settings"),
  updateSettings: (updates: Record<string, unknown>) =>
    request("/settings", { method: "PUT", body: JSON.stringify(updates) }),

  // Sources
  getSources: () => request<{ id: string; name: string; enabled: boolean }[]>("/sources"),
  toggleSource: (id: string, enabled: boolean) =>
    request(`/sources/${id}`, { method: "PUT", body: JSON.stringify({ enabled }) }),
};
```

- [ ] **Step 3: Create connection store with WebSocket**

```typescript
// frontend/src/lib/stores/connection.ts
import { writable, derived } from "svelte/store";
import type { WsMessage } from "$lib/api/types";

const WS_URL = "ws://localhost:9721/ws";
const RECONNECT_DELAY = 2000;
const MAX_RECONNECT_DELAY = 30000;

type ConnectionState = "connecting" | "connected" | "disconnected";

function createConnection() {
  const state = writable<ConnectionState>("disconnected");
  const version = writable<string>("");
  const handlers = new Map<string, Set<(data: Record<string, unknown>) => void>>();

  let ws: WebSocket | null = null;
  let reconnectDelay = RECONNECT_DELAY;
  let reconnectTimer: ReturnType<typeof setTimeout> | null = null;

  function on(type: string, handler: (data: Record<string, unknown>) => void) {
    if (!handlers.has(type)) handlers.set(type, new Set());
    handlers.get(type)!.add(handler);
    return () => handlers.get(type)?.delete(handler);
  }

  function dispatch(msg: WsMessage) {
    const fns = handlers.get(msg.type);
    if (fns) fns.forEach((fn) => fn(msg.data));
    // Also dispatch to wildcard handlers
    const wild = handlers.get("*");
    if (wild) wild.forEach((fn) => fn({ type: msg.type, ...msg.data }));
  }

  function connect() {
    if (ws?.readyState === WebSocket.OPEN) return;
    state.set("connecting");

    ws = new WebSocket(WS_URL);

    ws.onopen = () => {
      reconnectDelay = RECONNECT_DELAY;
    };

    ws.onmessage = (event) => {
      try {
        const msg: WsMessage = JSON.parse(event.data);
        if (msg.type === "connected") {
          state.set("connected");
          version.set((msg.data.version as string) || "");
        }
        dispatch(msg);
      } catch {
        console.error("Failed to parse WS message", event.data);
      }
    };

    ws.onclose = () => {
      state.set("disconnected");
      ws = null;
      scheduleReconnect();
    };

    ws.onerror = () => {
      ws?.close();
    };
  }

  function scheduleReconnect() {
    if (reconnectTimer) clearTimeout(reconnectTimer);
    reconnectTimer = setTimeout(() => {
      connect();
      reconnectDelay = Math.min(reconnectDelay * 1.5, MAX_RECONNECT_DELAY);
    }, reconnectDelay);
  }

  function disconnect() {
    if (reconnectTimer) clearTimeout(reconnectTimer);
    ws?.close();
    ws = null;
    state.set("disconnected");
  }

  function send(msg: WsMessage) {
    if (ws?.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify(msg));
    }
  }

  return { state, version, connect, disconnect, send, on };
}

export const connection = createConnection();
```

- [ ] **Step 4: Commit**

```bash
git add frontend/src/lib/
git commit -m "feat(frontend): API client, TypeScript types, and WebSocket connection store"
```

---

### Task 3.3: Core Svelte Stores

**Files:**
- Create: `frontend/src/lib/stores/scanner.ts`
- Create: `frontend/src/lib/stores/results.ts`
- Create: `frontend/src/lib/stores/notifications.ts`
- Create: `frontend/src/lib/stores/plex.ts`
- Create: `frontend/src/lib/stores/settings.ts`
- Create: `frontend/src/lib/stores/logs.ts`

- [ ] **Step 1: Create scanner store**

```typescript
// frontend/src/lib/stores/scanner.ts
import { writable, derived } from "svelte/store";
import { api } from "$lib/api/client";
import { connection } from "./connection";

export type ScanState = "idle" | "running" | "stopping";
export type ScanType = "deep" | "incremental" | "loaded" | "search";

export const scanState = writable<ScanState>("idle");
export const scanType = writable<ScanType>("deep");
export const scanProgress = writable<number>(0);
export const scanPhase = writable<string>("");
export const searchQuery = writable<string>("");

// Wire up WebSocket events
connection.on("scan:progress", (data) => {
  scanProgress.set(data.progress as number);
  scanPhase.set(data.phase as string);
});

connection.on("scan:complete", () => {
  scanState.set("idle");
  scanProgress.set(0);
  scanPhase.set("");
});

connection.on("scan:error", () => {
  scanState.set("idle");
});

export async function startScan(type: ScanType, query = "", pages = 1) {
  scanState.set("running");
  scanType.set(type);
  scanProgress.set(0);
  await api.scanStart(type, query, pages);
}

export async function stopScan() {
  scanState.set("stopping");
  await api.scanStop();
}
```

- [ ] **Step 2: Create results store**

```typescript
// frontend/src/lib/stores/results.ts
import { writable, derived } from "svelte/store";
import { api } from "$lib/api/client";
import { connection } from "./connection";
import type { ScanResult, ScanStats } from "$lib/api/types";

export type StatusFilter = "all" | "missing" | "upgrade" | "library" | "new";
export type ViewMode = "grid" | "list";

export const results = writable<ScanResult[]>([]);
export const statusFilter = writable<StatusFilter>("all");
export const searchFilter = writable<string>("");
export const viewMode = writable<ViewMode>("grid");
export const stats = writable<ScanStats>({ total: 0, missing: 0, upgrade: 0, library: 0 });
export const selectedKeys = writable<Set<string>>(new Set());

// Live result streaming from WebSocket
connection.on("scan:result", (data) => {
  results.update((items) => [...items, data as unknown as ScanResult]);
});

connection.on("scan:complete", (data) => {
  const s = data.stats as ScanStats;
  if (s) stats.set(s);
});

// Derived filtered results
export const filteredResults = derived(
  [results, statusFilter, searchFilter],
  ([$results, $filter, $search]) => {
    let items = $results;
    if ($filter !== "all") {
      items = items.filter((i) => i.status === $filter || i.status.includes($filter));
    }
    if ($search) {
      const q = $search.toLowerCase();
      items = items.filter((i) => i.title.toLowerCase().includes(q));
    }
    return items;
  }
);

export function clearResults() {
  results.set([]);
  stats.set({ total: 0, missing: 0, upgrade: 0, library: 0 });
  selectedKeys.set(new Set());
}

export function toggleSelect(groupKey: string) {
  selectedKeys.update((s) => {
    const next = new Set(s);
    if (next.has(groupKey)) next.delete(groupKey);
    else next.add(groupKey);
    return next;
  });
}

export async function selectAll() {
  await api.selectAll();
  results.update((items) => {
    const keys = new Set(items.map((i) => i.group_key));
    selectedKeys.set(keys);
    return items;
  });
}

export async function deselectAll() {
  await api.deselectAll();
  selectedKeys.set(new Set());
}
```

- [ ] **Step 3: Create notifications store**

```typescript
// frontend/src/lib/stores/notifications.ts
import { writable } from "svelte/store";
import { connection } from "./connection";

export interface Toast {
  id: string;
  title: string;
  body: string;
  priority: string;
  timestamp: number;
}

const MAX_TOASTS = 5;
const TOAST_DURATION = 5000;

export const toasts = writable<Toast[]>([]);

connection.on("notification", (data) => {
  addToast(data.title as string, data.body as string, data.priority as string);
});

export function addToast(title: string, body: string, priority = "normal") {
  const id = crypto.randomUUID();
  const toast: Toast = { id, title, body, priority, timestamp: Date.now() };

  toasts.update((t) => {
    const next = [toast, ...t].slice(0, MAX_TOASTS);
    return next;
  });

  setTimeout(() => {
    toasts.update((t) => t.filter((x) => x.id !== id));
  }, TOAST_DURATION);
}

export function dismissToast(id: string) {
  toasts.update((t) => t.filter((x) => x.id !== id));
}
```

- [ ] **Step 4: Create plex store**

```typescript
// frontend/src/lib/stores/plex.ts
import { writable } from "svelte/store";
import { api } from "$lib/api/client";
import { connection } from "./connection";

export const plexConnected = writable(false);
export const plexServer = writable("");
export const plexMovieCount = writable(0);
export const plexTvCount = writable(0);

connection.on("plex:status", (data) => {
  plexConnected.set(data.connected as boolean);
  plexServer.set(data.server as string);
});

export async function connectPlex() {
  await api.plexConnect();
}

export async function refreshPlexStatus() {
  const status = await api.plexStatus();
  plexConnected.set(status.connected);
  plexServer.set(status.server);
  plexMovieCount.set(status.movie_count);
  plexTvCount.set(status.tv_count);
}
```

- [ ] **Step 5: Create settings store**

```typescript
// frontend/src/lib/stores/settings.ts
import { writable, derived } from "svelte/store";
import { api } from "$lib/api/client";

export const settings = writable<Record<string, unknown>>({});
export const settingsLoaded = writable(false);
const originalSettings = writable<Record<string, unknown>>({});

export const isDirty = derived(
  [settings, originalSettings],
  ([$settings, $original]) => JSON.stringify($settings) !== JSON.stringify($original)
);

export async function loadSettings() {
  const config = await api.getSettings();
  settings.set(config);
  originalSettings.set(structuredClone(config));
  settingsLoaded.set(true);
}

export async function saveSettings() {
  let current: Record<string, unknown> = {};
  settings.subscribe((s) => (current = s))();
  await api.updateSettings(current);
  originalSettings.set(structuredClone(current));
}

export function resetSettings() {
  let original: Record<string, unknown> = {};
  originalSettings.subscribe((s) => (original = s))();
  settings.set(structuredClone(original));
}
```

- [ ] **Step 6: Create logs store**

```typescript
// frontend/src/lib/stores/logs.ts
import { writable, derived } from "svelte/store";
import { connection } from "./connection";

export interface LogEntry {
  level: string;
  message: string;
  timestamp: string;
}

const MAX_LOGS = 500;

export const logs = writable<LogEntry[]>([]);
export const logLevelFilter = writable<string>("all");
export const logPanelOpen = writable(false);

connection.on("log", (data) => {
  const entry: LogEntry = {
    level: data.level as string,
    message: data.message as string,
    timestamp: data.timestamp as string,
  };
  logs.update((l) => {
    const next = [...l, entry];
    return next.length > MAX_LOGS ? next.slice(-MAX_LOGS) : next;
  });
});

export const filteredLogs = derived([logs, logLevelFilter], ([$logs, $filter]) => {
  if ($filter === "all") return $logs;
  return $logs.filter((l) => l.level === $filter);
});

export function clearLogs() {
  logs.set([]);
}
```

- [ ] **Step 7: Commit**

```bash
git add frontend/src/lib/stores/
git commit -m "feat(frontend): Svelte stores for scanner, results, plex, settings, logs, notifications"
```

---

## Chunk 4: Frontend UI — Layout + Scanner Page

### Task 4.1: App Shell Layout

**Files:**
- Create: `frontend/src/routes/+layout.svelte`
- Create: `frontend/src/lib/components/Sidebar.svelte`
- Create: `frontend/src/lib/components/Snackbar.svelte`
- Modify: `frontend/src/routes/+layout.ts` (disable SSR)

- [ ] **Step 1: Disable SSR for Tauri**

```typescript
// frontend/src/routes/+layout.ts
export const ssr = false;
export const prerender = false;
```

- [ ] **Step 2: Create Sidebar component**

```svelte
<!-- frontend/src/lib/components/Sidebar.svelte -->
<script lang="ts">
  import { page } from "$app/stores";

  const navItems = [
    { href: "/", label: "Scan", icon: "search" },
    { href: "/downloads", label: "Downloads", icon: "download" },
    { href: "/settings", label: "Settings", icon: "settings" },
  ];
</script>

<nav class="flex flex-col w-16 h-full bg-[var(--bg-secondary)] border-r border-[var(--border)]">
  <div class="flex items-center justify-center h-14 border-b border-[var(--border)]">
    <span class="text-[var(--accent)] font-bold text-lg">SH</span>
  </div>

  <div class="flex flex-col gap-1 p-2 flex-1">
    {#each navItems as item}
      <a
        href={item.href}
        class="flex flex-col items-center gap-1 p-2 rounded-lg text-xs transition-colors
          {$page.url.pathname === item.href
            ? 'bg-[var(--accent)]/10 text-[var(--accent)]'
            : 'text-[var(--text-secondary)] hover:text-[var(--text-primary)] hover:bg-[var(--bg-tertiary)]'}"
      >
        <span class="text-base">{item.icon === 'search' ? '\u{1F50D}' : item.icon === 'download' ? '\u{2B07}' : '\u{2699}'}</span>
        <span>{item.label}</span>
      </a>
    {/each}
  </div>
</nav>
```

- [ ] **Step 3: Create Snackbar component**

```svelte
<!-- frontend/src/lib/components/Snackbar.svelte -->
<script lang="ts">
  import { toasts, dismissToast } from "$lib/stores/notifications";
  import { fly } from "svelte/transition";
</script>

<div class="fixed bottom-4 right-4 flex flex-col gap-2 z-50">
  {#each $toasts as toast (toast.id)}
    <div
      transition:fly={{ y: 20, duration: 200 }}
      class="px-4 py-3 rounded-lg shadow-lg max-w-sm border border-[var(--border)]
        {toast.priority === 'high' ? 'bg-red-900/90' : 'bg-[var(--bg-tertiary)]'}"
    >
      <div class="flex justify-between items-start gap-3">
        <div>
          <p class="font-medium text-sm">{toast.title}</p>
          <p class="text-xs text-[var(--text-secondary)] mt-0.5">{toast.body}</p>
        </div>
        <button
          class="text-[var(--text-secondary)] hover:text-[var(--text-primary)] text-sm"
          onclick={() => dismissToast(toast.id)}
        >&times;</button>
      </div>
    </div>
  {/each}
</div>
```

- [ ] **Step 4: Create root layout**

```svelte
<!-- frontend/src/routes/+layout.svelte -->
<script lang="ts">
  import "../app.css";
  import Sidebar from "$lib/components/Sidebar.svelte";
  import Snackbar from "$lib/components/Snackbar.svelte";
  import { connection } from "$lib/stores/connection";
  import { onMount } from "svelte";

  let { children } = $props();

  onMount(() => {
    connection.connect();
    return () => connection.disconnect();
  });
</script>

<div class="flex h-screen overflow-hidden">
  <Sidebar />
  <main class="flex-1 flex flex-col overflow-hidden">
    {@render children()}
  </main>
</div>

<Snackbar />
```

- [ ] **Step 5: Commit**

```bash
git add frontend/src/routes/ frontend/src/lib/components/
git commit -m "feat(frontend): app shell layout with sidebar navigation and snackbar"
```

---

### Task 4.2: Scanner Page — Controls + Filter Bar

**Files:**
- Create: `frontend/src/lib/components/ScanControls.svelte`
- Create: `frontend/src/lib/components/FilterBar.svelte`
- Create: `frontend/src/lib/components/StatusBar.svelte`
- Modify: `frontend/src/routes/+page.svelte`

- [ ] **Step 1: Create ScanControls**

```svelte
<!-- frontend/src/lib/components/ScanControls.svelte -->
<script lang="ts">
  import { scanState, scanType, scanProgress, scanPhase, searchQuery, startScan, stopScan } from "$lib/stores/scanner";
  import type { ScanType } from "$lib/stores/scanner";

  const scanTypes: { value: ScanType; label: string }[] = [
    { value: "deep", label: "Deep Scan" },
    { value: "incremental", label: "Incremental" },
    { value: "loaded", label: "Load Cache" },
    { value: "search", label: "Site Search" },
  ];

  let selectedType = $state<ScanType>("deep");
  let query = $state("");

  function handleStart() {
    startScan(selectedType, query);
  }
</script>

<div class="flex items-center gap-3 p-4 border-b border-[var(--border)]">
  <select
    bind:value={selectedType}
    disabled={$scanState !== "idle"}
    class="bg-[var(--bg-tertiary)] text-[var(--text-primary)] px-3 py-2 rounded-lg border border-[var(--border)] text-sm"
  >
    {#each scanTypes as t}
      <option value={t.value}>{t.label}</option>
    {/each}
  </select>

  {#if selectedType === "search"}
    <input
      type="text"
      bind:value={query}
      placeholder="Search title..."
      class="bg-[var(--bg-tertiary)] text-[var(--text-primary)] px-3 py-2 rounded-lg border border-[var(--border)] text-sm flex-1 max-w-xs"
    />
  {/if}

  {#if $scanState === "idle"}
    <button
      onclick={handleStart}
      class="px-4 py-2 bg-[var(--accent)] hover:bg-[var(--accent-hover)] text-white rounded-lg text-sm font-medium transition-colors"
    >
      Start Scan
    </button>
  {:else}
    <button
      onclick={() => stopScan()}
      disabled={$scanState === "stopping"}
      class="px-4 py-2 bg-[var(--error)] hover:bg-red-600 text-white rounded-lg text-sm font-medium transition-colors disabled:opacity-50"
    >
      {$scanState === "stopping" ? "Stopping..." : "Stop"}
    </button>
  {/if}

  {#if $scanState === "running"}
    <div class="flex-1 max-w-md">
      <div class="flex justify-between text-xs text-[var(--text-secondary)] mb-1">
        <span>{$scanPhase}</span>
        <span>{Math.round($scanProgress * 100)}%</span>
      </div>
      <div class="h-1.5 bg-[var(--bg-tertiary)] rounded-full overflow-hidden">
        <div
          class="h-full bg-[var(--accent)] transition-all duration-300 rounded-full"
          style="width: {$scanProgress * 100}%"
        ></div>
      </div>
    </div>
  {/if}
</div>
```

- [ ] **Step 2: Create FilterBar**

```svelte
<!-- frontend/src/lib/components/FilterBar.svelte -->
<script lang="ts">
  import { statusFilter, searchFilter, viewMode, stats } from "$lib/stores/results";
  import type { StatusFilter, ViewMode } from "$lib/stores/results";

  const filters: { value: StatusFilter; label: string }[] = [
    { value: "all", label: "All" },
    { value: "missing", label: "Missing" },
    { value: "upgrade", label: "Upgrades" },
    { value: "library", label: "In Library" },
  ];

  let search = $state("");

  function onSearch() {
    searchFilter.set(search);
  }
</script>

<div class="flex items-center gap-3 px-4 py-2 border-b border-[var(--border)]">
  <div class="flex gap-1">
    {#each filters as f}
      <button
        onclick={() => statusFilter.set(f.value)}
        class="px-3 py-1.5 rounded-md text-xs font-medium transition-colors
          {$statusFilter === f.value
            ? 'bg-[var(--accent)] text-white'
            : 'text-[var(--text-secondary)] hover:bg-[var(--bg-tertiary)]'}"
      >
        {f.label}
        {#if f.value === "missing" && $stats.missing > 0}
          <span class="ml-1 opacity-70">{$stats.missing}</span>
        {:else if f.value === "upgrade" && $stats.upgrade > 0}
          <span class="ml-1 opacity-70">{$stats.upgrade}</span>
        {/if}
      </button>
    {/each}
  </div>

  <div class="flex-1"></div>

  <input
    type="text"
    bind:value={search}
    oninput={onSearch}
    placeholder="Filter results..."
    class="bg-[var(--bg-tertiary)] text-[var(--text-primary)] px-3 py-1.5 rounded-lg border border-[var(--border)] text-sm w-48"
  />

  <div class="flex gap-1">
    <button
      onclick={() => viewMode.set("grid")}
      class="p-1.5 rounded {$viewMode === 'grid' ? 'text-[var(--accent)]' : 'text-[var(--text-secondary)]'}"
    >Grid</button>
    <button
      onclick={() => viewMode.set("list")}
      class="p-1.5 rounded {$viewMode === 'list' ? 'text-[var(--accent)]' : 'text-[var(--text-secondary)]'}"
    >List</button>
  </div>
</div>
```

- [ ] **Step 3: Create StatusBar**

```svelte
<!-- frontend/src/lib/components/StatusBar.svelte -->
<script lang="ts">
  import { stats } from "$lib/stores/results";
</script>

<div class="flex items-center gap-4 px-4 py-2 border-t border-[var(--border)] text-xs text-[var(--text-secondary)]">
  <span>Total: <strong class="text-[var(--text-primary)]">{$stats.total}</strong></span>
  <span>Missing: <strong class="text-[var(--error)]">{$stats.missing}</strong></span>
  <span>Upgrades: <strong class="text-[var(--warning)]">{$stats.upgrade}</strong></span>
  <span>In Library: <strong class="text-[var(--success)]">{$stats.library}</strong></span>
</div>
```

- [ ] **Step 4: Wire up the scanner page**

```svelte
<!-- frontend/src/routes/+page.svelte -->
<script lang="ts">
  import ScanControls from "$lib/components/ScanControls.svelte";
  import FilterBar from "$lib/components/FilterBar.svelte";
  import StatusBar from "$lib/components/StatusBar.svelte";
  import { filteredResults, viewMode } from "$lib/stores/results";
</script>

<div class="flex flex-col h-full">
  <ScanControls />
  <FilterBar />

  <div class="flex-1 overflow-auto p-4">
    {#if $filteredResults.length === 0}
      <div class="flex items-center justify-center h-full text-[var(--text-secondary)]">
        <p>No results. Start a scan to find media.</p>
      </div>
    {:else if $viewMode === "grid"}
      <div class="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-4 xl:grid-cols-5 2xl:grid-cols-6 gap-4">
        {#each $filteredResults as item (item.group_key)}
          <div class="bg-[var(--bg-secondary)] rounded-lg overflow-hidden border border-[var(--border)]">
            <div class="aspect-[2/3] bg-[var(--bg-tertiary)]">
              {#if item.poster_url}
                <img src={item.poster_url} alt={item.title} class="w-full h-full object-cover" />
              {/if}
            </div>
            <div class="p-2">
              <p class="text-sm font-medium truncate">{item.title}</p>
              <p class="text-xs text-[var(--text-secondary)]">{item.year} &middot; {item.resolution}</p>
            </div>
          </div>
        {/each}
      </div>
    {:else}
      <p class="text-[var(--text-secondary)] text-sm">List view — Task 4.3</p>
    {/if}
  </div>

  <StatusBar />
</div>
```

- [ ] **Step 5: Commit**

```bash
git add frontend/src/
git commit -m "feat(frontend): scanner page with controls, filter bar, results grid, and status bar"
```

---

### Task 4.3: Result Tile + Result Row Components

**Files:**
- Create: `frontend/src/lib/components/ResultTile.svelte`
- Create: `frontend/src/lib/components/ResultRow.svelte`
- Create: `frontend/src/lib/components/Badge.svelte`
- Modify: `frontend/src/routes/+page.svelte` (use components)

- [ ] **Step 1: Create Badge component**

```svelte
<!-- frontend/src/lib/components/Badge.svelte -->
<script lang="ts">
  let { label, variant = "default" }: { label: string; variant?: "default" | "success" | "warning" | "error" | "accent" } = $props();

  const colors = {
    default: "bg-[var(--bg-tertiary)] text-[var(--text-secondary)]",
    success: "bg-green-900/50 text-green-400",
    warning: "bg-amber-900/50 text-amber-400",
    error: "bg-red-900/50 text-red-400",
    accent: "bg-cyan-900/50 text-cyan-400",
  };
</script>

<span class="inline-flex px-1.5 py-0.5 rounded text-[10px] font-medium {colors[variant]}">
  {label}
</span>
```

- [ ] **Step 2: Create ResultTile component**

```svelte
<!-- frontend/src/lib/components/ResultTile.svelte -->
<script lang="ts">
  import Badge from "./Badge.svelte";
  import { toggleSelect, selectedKeys } from "$lib/stores/results";
  import type { ScanResult } from "$lib/api/types";
  import { fly } from "svelte/transition";

  let { item }: { item: ScanResult } = $props();

  const statusVariant = {
    missing: "error",
    upgrade: "warning",
    dv_upgrade: "accent",
    in_library: "success",
  } as const;

  $: selected = $selectedKeys.has(item.group_key);
</script>

<div
  transition:fly={{ y: 10, duration: 200 }}
  class="bg-[var(--bg-secondary)] rounded-lg overflow-hidden border transition-colors cursor-pointer
    {selected ? 'border-[var(--accent)]' : 'border-[var(--border)] hover:border-[var(--text-secondary)]'}"
  onclick={() => toggleSelect(item.group_key)}
>
  <div class="aspect-[2/3] bg-[var(--bg-tertiary)] relative">
    {#if item.poster_url}
      <img src={item.poster_url} alt={item.title} class="w-full h-full object-cover" loading="lazy" />
    {:else}
      <div class="flex items-center justify-center h-full text-[var(--text-secondary)] text-xs">No poster</div>
    {/if}

    <!-- Status badge overlay -->
    <div class="absolute top-1.5 right-1.5">
      <Badge label={item.status.replace("_", " ")} variant={statusVariant[item.status] ?? "default"} />
    </div>

    <!-- Resolution + HDR badges -->
    <div class="absolute bottom-1.5 left-1.5 flex gap-1">
      <Badge label={item.resolution} />
      {#if item.dovi}<Badge label="DV" variant="accent" />{/if}
      {#if item.hdr && !item.dovi}<Badge label="HDR" variant="warning" />{/if}
    </div>

    <!-- Selection indicator -->
    {#if selected}
      <div class="absolute top-1.5 left-1.5 w-5 h-5 bg-[var(--accent)] rounded-full flex items-center justify-center text-white text-xs">
        &#10003;
      </div>
    {/if}
  </div>

  <div class="p-2.5">
    <p class="text-sm font-medium truncate" title={item.title}>{item.title}</p>
    <div class="flex items-center gap-2 mt-1 text-xs text-[var(--text-secondary)]">
      {#if item.year}<span>{item.year}</span>{/if}
      {#if item.size}<span>&middot; {item.size}</span>{/if}
      {#if item.rating}<span>&middot; {item.rating.toFixed(1)}</span>{/if}
    </div>
  </div>
</div>
```

- [ ] **Step 3: Create ResultRow component**

```svelte
<!-- frontend/src/lib/components/ResultRow.svelte -->
<script lang="ts">
  import Badge from "./Badge.svelte";
  import { toggleSelect, selectedKeys } from "$lib/stores/results";
  import type { ScanResult } from "$lib/api/types";

  let { item }: { item: ScanResult } = $props();

  const statusVariant = {
    missing: "error",
    upgrade: "warning",
    dv_upgrade: "accent",
    in_library: "success",
  } as const;

  $: selected = $selectedKeys.has(item.group_key);
</script>

<tr
  class="border-b border-[var(--border)] hover:bg-[var(--bg-tertiary)] transition-colors cursor-pointer
    {selected ? 'bg-[var(--accent)]/5' : ''}"
  onclick={() => toggleSelect(item.group_key)}
>
  <td class="p-2 w-8">
    <input type="checkbox" checked={selected} class="accent-[var(--accent)]" />
  </td>
  <td class="p-2 text-sm font-medium max-w-xs truncate">{item.title}</td>
  <td class="p-2 text-sm text-[var(--text-secondary)]">{item.year ?? ""}</td>
  <td class="p-2"><Badge label={item.resolution} /></td>
  <td class="p-2 text-sm text-[var(--text-secondary)]">{item.size}</td>
  <td class="p-2 text-sm text-[var(--text-secondary)]">{item.rating?.toFixed(1) ?? "-"}</td>
  <td class="p-2">
    <Badge label={item.status.replace("_", " ")} variant={statusVariant[item.status] ?? "default"} />
  </td>
</tr>
```

- [ ] **Step 4: Update +page.svelte to use components**

Replace the inline grid/list markup in `frontend/src/routes/+page.svelte` with:

```svelte
<!-- frontend/src/routes/+page.svelte -->
<script lang="ts">
  import ScanControls from "$lib/components/ScanControls.svelte";
  import FilterBar from "$lib/components/FilterBar.svelte";
  import StatusBar from "$lib/components/StatusBar.svelte";
  import ResultTile from "$lib/components/ResultTile.svelte";
  import ResultRow from "$lib/components/ResultRow.svelte";
  import { filteredResults, viewMode } from "$lib/stores/results";
</script>

<div class="flex flex-col h-full">
  <ScanControls />
  <FilterBar />

  <div class="flex-1 overflow-auto p-4">
    {#if $filteredResults.length === 0}
      <div class="flex items-center justify-center h-full text-[var(--text-secondary)]">
        <p>No results. Start a scan to find media.</p>
      </div>
    {:else if $viewMode === "grid"}
      <div class="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-4 xl:grid-cols-5 2xl:grid-cols-6 gap-4">
        {#each $filteredResults as item (item.group_key)}
          <ResultTile {item} />
        {/each}
      </div>
    {:else}
      <table class="w-full">
        <thead>
          <tr class="text-left text-xs text-[var(--text-secondary)] border-b border-[var(--border)]">
            <th class="p-2 w-8"></th>
            <th class="p-2">Title</th>
            <th class="p-2">Year</th>
            <th class="p-2">Res</th>
            <th class="p-2">Size</th>
            <th class="p-2">Rating</th>
            <th class="p-2">Status</th>
          </tr>
        </thead>
        <tbody>
          {#each $filteredResults as item (item.group_key)}
            <ResultRow {item} />
          {/each}
        </tbody>
      </table>
    {/if}
  </div>

  <StatusBar />
</div>
```

- [ ] **Step 5: Commit**

```bash
git add frontend/src/
git commit -m "feat(frontend): ResultTile, ResultRow, Badge components with selection and animations"
```

---

## Chunk 5: Frontend — Settings + Downloads Pages

### Task 5.1: Settings Page

**Files:**
- Create: `frontend/src/routes/settings/+page.svelte`
- Create: `frontend/src/lib/components/SettingsSection.svelte`

- [ ] **Step 1: Create SettingsSection component**

```svelte
<!-- frontend/src/lib/components/SettingsSection.svelte -->
<script lang="ts">
  let { title, children }: { title: string; children: any } = $props();
</script>

<div class="mb-6">
  <h3 class="text-sm font-semibold text-[var(--text-primary)] mb-3 pb-2 border-b border-[var(--border)]">{title}</h3>
  <div class="space-y-3">
    {@render children()}
  </div>
</div>
```

- [ ] **Step 2: Create Settings page**

```svelte
<!-- frontend/src/routes/settings/+page.svelte -->
<script lang="ts">
  import { onMount } from "svelte";
  import { settings, settingsLoaded, loadSettings, saveSettings, resetSettings, isDirty } from "$lib/stores/settings";
  import SettingsSection from "$lib/components/SettingsSection.svelte";
  import { addToast } from "$lib/stores/notifications";

  let activeCategory = $state("plex");

  const categories = [
    { id: "plex", label: "Plex" },
    { id: "api", label: "APIs" },
    { id: "sources", label: "Sources" },
    { id: "rules", label: "Rules" },
    { id: "downloads", label: "Downloads" },
    { id: "notifications", label: "Notifications" },
    { id: "appearance", label: "Appearance" },
  ];

  onMount(() => {
    if (!$settingsLoaded) loadSettings();
  });

  async function handleSave() {
    await saveSettings();
    addToast("Settings", "Settings saved successfully");
  }
</script>

<div class="flex h-full">
  <!-- Category sidebar -->
  <div class="w-48 border-r border-[var(--border)] p-3">
    <h2 class="text-lg font-bold mb-4">Settings</h2>
    {#each categories as cat}
      <button
        onclick={() => activeCategory = cat.id}
        class="block w-full text-left px-3 py-2 rounded-lg text-sm mb-1 transition-colors
          {activeCategory === cat.id
            ? 'bg-[var(--accent)]/10 text-[var(--accent)]'
            : 'text-[var(--text-secondary)] hover:bg-[var(--bg-tertiary)]'}"
      >
        {cat.label}
      </button>
    {/each}
  </div>

  <!-- Settings content -->
  <div class="flex-1 overflow-auto p-6">
    {#if !$settingsLoaded}
      <p class="text-[var(--text-secondary)]">Loading settings...</p>
    {:else}
      {#if activeCategory === "plex"}
        <SettingsSection title="Plex Server">
          <label class="block text-sm">
            <span class="text-[var(--text-secondary)]">Plex URL</span>
            <input type="text" bind:value={$settings.plex_url}
              class="mt-1 block w-full bg-[var(--bg-tertiary)] border border-[var(--border)] rounded-lg px-3 py-2 text-sm" />
          </label>
          <label class="block text-sm">
            <span class="text-[var(--text-secondary)]">Plex Token</span>
            <input type="password" bind:value={$settings.plex_token}
              class="mt-1 block w-full bg-[var(--bg-tertiary)] border border-[var(--border)] rounded-lg px-3 py-2 text-sm" />
          </label>
          <label class="block text-sm">
            <span class="text-[var(--text-secondary)]">Connection Mode</span>
            <select bind:value={$settings.plex_connection_mode}
              class="mt-1 block w-full bg-[var(--bg-tertiary)] border border-[var(--border)] rounded-lg px-3 py-2 text-sm">
              <option value="direct">Direct</option>
              <option value="account">Account</option>
            </select>
          </label>
        </SettingsSection>
      {:else if activeCategory === "api"}
        <SettingsSection title="API Keys">
          <label class="block text-sm">
            <span class="text-[var(--text-secondary)]">TMDB API Key</span>
            <input type="password" bind:value={$settings.tmdb_api_key}
              class="mt-1 block w-full bg-[var(--bg-tertiary)] border border-[var(--border)] rounded-lg px-3 py-2 text-sm" />
          </label>
          <label class="block text-sm">
            <span class="text-[var(--text-secondary)]">OMDb API Key</span>
            <input type="password" bind:value={$settings.omdb_api_key}
              class="mt-1 block w-full bg-[var(--bg-tertiary)] border border-[var(--border)] rounded-lg px-3 py-2 text-sm" />
          </label>
          <label class="flex items-center gap-2 text-sm">
            <input type="checkbox" bind:checked={$settings.use_tmdb} class="accent-[var(--accent)]" />
            <span>Use TMDB for metadata</span>
          </label>
        </SettingsSection>
      {:else if activeCategory === "rules"}
        <SettingsSection title="Upgrade Rules">
          <label class="flex items-center gap-2 text-sm">
            <input type="checkbox" bind:checked={$settings.rule_1080_4k} class="accent-[var(--accent)]" />
            <span>1080p to 4K upgrades</span>
          </label>
          <label class="flex items-center gap-2 text-sm">
            <input type="checkbox" bind:checked={$settings.rule_dv} class="accent-[var(--accent)]" />
            <span>Dolby Vision upgrades</span>
          </label>
          <label class="flex items-center gap-2 text-sm">
            <input type="checkbox" bind:checked={$settings.strict_resolution} class="accent-[var(--accent)]" />
            <span>Strict resolution matching</span>
          </label>
        </SettingsSection>
        <SettingsSection title="Matching Thresholds">
          <label class="block text-sm">
            <span class="text-[var(--text-secondary)]">Movie match threshold (%)</span>
            <input type="number" bind:value={$settings.movie_match_threshold} min="50" max="100"
              class="mt-1 block w-32 bg-[var(--bg-tertiary)] border border-[var(--border)] rounded-lg px-3 py-2 text-sm" />
          </label>
          <label class="block text-sm">
            <span class="text-[var(--text-secondary)]">TV match threshold (%)</span>
            <input type="number" bind:value={$settings.tv_match_threshold} min="50" max="100"
              class="mt-1 block w-32 bg-[var(--bg-tertiary)] border border-[var(--border)] rounded-lg px-3 py-2 text-sm" />
          </label>
        </SettingsSection>
      {:else if activeCategory === "appearance"}
        <SettingsSection title="Theme">
          <label class="block text-sm">
            <span class="text-[var(--text-secondary)]">Theme Mode</span>
            <select bind:value={$settings.theme_mode}
              class="mt-1 block w-full bg-[var(--bg-tertiary)] border border-[var(--border)] rounded-lg px-3 py-2 text-sm">
              <option value="dark">Dark</option>
              <option value="light">Light</option>
              <option value="system">System</option>
            </select>
          </label>
        </SettingsSection>
      {:else}
        <p class="text-[var(--text-secondary)] text-sm">Section: {activeCategory} — coming soon</p>
      {/if}

      <!-- Save/Reset bar -->
      {#if $isDirty}
        <div class="fixed bottom-0 left-64 right-0 p-4 bg-[var(--bg-secondary)] border-t border-[var(--border)] flex gap-3 justify-end">
          <button onclick={resetSettings} class="px-4 py-2 text-sm text-[var(--text-secondary)] hover:text-[var(--text-primary)]">Reset</button>
          <button onclick={handleSave} class="px-4 py-2 bg-[var(--accent)] text-white rounded-lg text-sm font-medium">Save</button>
        </div>
      {/if}
    {/if}
  </div>
</div>
```

- [ ] **Step 3: Commit**

```bash
git add frontend/src/routes/settings/ frontend/src/lib/components/SettingsSection.svelte
git commit -m "feat(frontend): settings page with category navigation and save/reset"
```

---

### Task 5.2: Downloads Page

**Files:**
- Create: `frontend/src/routes/downloads/+page.svelte`

- [ ] **Step 1: Create Downloads page**

```svelte
<!-- frontend/src/routes/downloads/+page.svelte -->
<script lang="ts">
  import { onMount } from "svelte";
  import { api } from "$lib/api/client";

  let history = $state<Record<string, unknown>[]>([]);
  let loading = $state(true);

  onMount(async () => {
    history = await api.downloadHistory();
    loading = false;
  });
</script>

<div class="flex flex-col h-full">
  <div class="p-4 border-b border-[var(--border)]">
    <h2 class="text-lg font-bold">Download History</h2>
  </div>

  <div class="flex-1 overflow-auto p-4">
    {#if loading}
      <p class="text-[var(--text-secondary)]">Loading...</p>
    {:else if history.length === 0}
      <div class="flex items-center justify-center h-full text-[var(--text-secondary)]">
        <p>No download history yet.</p>
      </div>
    {:else}
      <table class="w-full">
        <thead>
          <tr class="text-left text-xs text-[var(--text-secondary)] border-b border-[var(--border)]">
            <th class="p-2">Title</th>
            <th class="p-2">Resolution</th>
            <th class="p-2">Size</th>
            <th class="p-2">Date</th>
          </tr>
        </thead>
        <tbody>
          {#each history as item}
            <tr class="border-b border-[var(--border)] hover:bg-[var(--bg-tertiary)]">
              <td class="p-2 text-sm">{item.title ?? "Unknown"}</td>
              <td class="p-2 text-sm text-[var(--text-secondary)]">{item.resolution ?? ""}</td>
              <td class="p-2 text-sm text-[var(--text-secondary)]">{item.size ?? ""}</td>
              <td class="p-2 text-sm text-[var(--text-secondary)]">{item.date_added ?? ""}</td>
            </tr>
          {/each}
        </tbody>
      </table>
    {/if}
  </div>
</div>
```

- [ ] **Step 2: Commit**

```bash
git add frontend/src/routes/downloads/
git commit -m "feat(frontend): download history page with sortable table"
```

---

## Chunk 6: Tauri Shell — Sidecar + System Tray

### Task 6.1: Sidecar Process Management

**Files:**
- Modify: `frontend/src-tauri/src/main.rs`
- Modify: `frontend/src-tauri/tauri.conf.json`
- Modify: `frontend/src-tauri/Cargo.toml`

- [ ] **Step 1: Add Tauri shell plugin to Cargo.toml**

Add to `[dependencies]` in `frontend/src-tauri/Cargo.toml`:
```toml
tauri-plugin-shell = "2"
tauri-plugin-notification = "2"
reqwest = { version = "0.12", features = ["json"] }
tokio = { version = "1", features = ["time"] }
```

- [ ] **Step 2: Configure sidecar in tauri.conf.json**

Add to the config:
```jsonc
{
  "plugins": {
    "shell": {
      "sidecar": true,
      "scope": [
        { "name": "binaries/scanhound-api", "sidecar": true }
      ]
    }
  }
}
```

- [ ] **Step 3: Implement main.rs with sidecar lifecycle**

```rust
// frontend/src-tauri/src/main.rs
use std::time::Duration;
use tauri::Manager;
use tauri_plugin_shell::ShellExt;

const API_PORT: u16 = 9721;
const HEALTH_URL: &str = "http://localhost:9721/health";
const SHUTDOWN_URL: &str = "http://localhost:9721/shutdown";
const MAX_STARTUP_WAIT: Duration = Duration::from_secs(15);
const POLL_INTERVAL: Duration = Duration::from_millis(500);

async fn wait_for_backend() -> bool {
    let client = reqwest::Client::new();
    let start = std::time::Instant::now();
    while start.elapsed() < MAX_STARTUP_WAIT {
        if let Ok(resp) = client.get(HEALTH_URL).send().await {
            if resp.status().is_success() {
                return true;
            }
        }
        tokio::time::sleep(POLL_INTERVAL).await;
    }
    false
}

async fn shutdown_backend() {
    let client = reqwest::Client::new();
    let _ = client.post(SHUTDOWN_URL).send().await;
    tokio::time::sleep(Duration::from_secs(2)).await;
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_shell::init())
        .plugin(tauri_plugin_notification::init())
        .setup(|app| {
            let shell = app.shell();

            // Spawn Python sidecar
            let (mut _rx, _child) = shell
                .sidecar("scanhound-api")
                .expect("failed to find sidecar binary")
                .args(["--port", &API_PORT.to_string()])
                .spawn()
                .expect("failed to spawn sidecar");

            // Wait for backend in background
            let app_handle = app.handle().clone();
            tauri::async_runtime::spawn(async move {
                if wait_for_backend().await {
                    let _ = app_handle.emit("backend-ready", ());
                } else {
                    let _ = app_handle.emit("backend-failed", ());
                }
            });

            Ok(())
        })
        .on_window_event(|window, event| {
            if let tauri::WindowEvent::CloseRequested { .. } = event {
                tauri::async_runtime::block_on(shutdown_backend());
            }
        })
        .run(tauri::generate_context!())
        .expect("error running ScanHound");
}
```

- [ ] **Step 4: Commit**

```bash
git add frontend/src-tauri/
git commit -m "feat(tauri): sidecar process management with health polling and graceful shutdown"
```

---

### Task 6.2: System Tray

**Files:**
- Modify: `frontend/src-tauri/src/main.rs`
- Modify: `frontend/src-tauri/Cargo.toml`

- [ ] **Step 1: Add tray plugin to Cargo.toml**

```toml
tauri-plugin-tray = "2"
```

- [ ] **Step 2: Add tray setup to main.rs**

Add after `.plugin(tauri_plugin_notification::init())`:

```rust
use tauri::tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent};
use tauri::menu::{Menu, MenuItem};

// Inside .setup(|app| { ... })
let quit = MenuItem::with_id(app, "quit", "Quit", true, None::<&str>)?;
let show = MenuItem::with_id(app, "show", "Show", true, None::<&str>)?;
let menu = Menu::with_items(app, &[&show, &quit])?;

let _tray = TrayIconBuilder::new()
    .menu(&menu)
    .tooltip("ScanHound")
    .on_menu_event(move |app, event| match event.id.as_ref() {
        "quit" => {
            tauri::async_runtime::block_on(shutdown_backend());
            app.exit(0);
        }
        "show" => {
            if let Some(window) = app.get_webview_window("main") {
                let _ = window.show();
                let _ = window.set_focus();
            }
        }
        _ => {}
    })
    .on_tray_icon_event(|tray, event| {
        if let TrayIconEvent::Click { button: MouseButton::Left, button_state: MouseButtonState::Up, .. } = event {
            let app = tray.app_handle();
            if let Some(window) = app.get_webview_window("main") {
                let _ = window.show();
                let _ = window.set_focus();
            }
        }
    })
    .build(app)?;
```

- [ ] **Step 3: Commit**

```bash
git add frontend/src-tauri/
git commit -m "feat(tauri): system tray with show/quit menu and click-to-focus"
```

---

## Chunk 7: Packaging + Build Pipeline

### Task 7.1: PyInstaller Spec for Backend

**Files:**
- Create: `backend/scanhound-api.spec`
- Create: `backend/api/__main__.py`

- [ ] **Step 1: Create API entry point for PyInstaller**

```python
# backend/api/__main__.py
"""Entry point for packaged backend API."""
import argparse
import uvicorn

def main():
    parser = argparse.ArgumentParser(description="ScanHound API Server")
    parser.add_argument("--port", type=int, default=9721)
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args()

    from backend.api.main import create_app
    app = create_app()
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")

if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Create PyInstaller spec**

```python
# backend/scanhound-api.spec
# -*- mode: python ; coding: utf-8 -*-

a = Analysis(
    ['api/__main__.py'],
    pathex=['.'],
    binaries=[],
    datas=[],
    hiddenimports=[
        'uvicorn.logging',
        'uvicorn.loops',
        'uvicorn.loops.auto',
        'uvicorn.protocols',
        'uvicorn.protocols.http',
        'uvicorn.protocols.http.auto',
        'uvicorn.protocols.websockets',
        'uvicorn.protocols.websockets.auto',
        'uvicorn.lifespan',
        'uvicorn.lifespan.on',
        'backend.api',
        'backend.api.main',
        'backend.api.routes',
        'backend.api.routes.system',
        'backend.api.routes.scanner',
        'backend.api.routes.results',
        'backend.api.routes.plex',
        'backend.api.routes.downloads',
        'backend.api.routes.settings',
        'backend.api.routes.sources',
        'backend.app_service',
        'backend.scanner_service',
        'backend.plex_service',
        'backend.download_service',
        'backend.database',
        'backend.matching',
        'backend.sources',
    ],
    hookspath=[],
    runtime_hooks=[],
    excludes=['PySide6', 'PyQt5', 'PyQt6', 'tkinter'],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='scanhound-api',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,
)
```

- [ ] **Step 3: Test PyInstaller build**

Run: `cd backend && pyinstaller scanhound-api.spec --noconfirm`
Expected: `dist/scanhound-api.exe` created

- [ ] **Step 4: Commit**

```bash
git add backend/api/__main__.py backend/scanhound-api.spec
git commit -m "feat(build): PyInstaller spec and API entry point for sidecar packaging"
```

---

### Task 7.2: Build Script

**Files:**
- Create: `scripts/build.sh`

- [ ] **Step 1: Create build script**

```bash
#!/usr/bin/env bash
# scripts/build.sh — Build ScanHound v2.0 for distribution
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT_DIR="$(dirname "$SCRIPT_DIR")"

echo "=== Building ScanHound v2.0 ==="

# Step 1: Build Python sidecar
echo "[1/3] Building Python backend sidecar..."
cd "$ROOT_DIR/backend"
pyinstaller scanhound-api.spec --noconfirm
echo "  -> dist/scanhound-api built"

# Step 2: Copy sidecar to Tauri binaries
echo "[2/3] Copying sidecar to Tauri..."
TAURI_BIN="$ROOT_DIR/frontend/src-tauri/binaries"
mkdir -p "$TAURI_BIN"
if [[ "$OSTYPE" == "msys" || "$OSTYPE" == "cygwin" || "$OSTYPE" == "win32" ]]; then
    cp "$ROOT_DIR/backend/dist/scanhound-api.exe" "$TAURI_BIN/scanhound-api-x86_64-pc-windows-msvc.exe"
else
    cp "$ROOT_DIR/backend/dist/scanhound-api" "$TAURI_BIN/scanhound-api-$(rustc -vV | grep host | awk '{print $2}')"
fi
echo "  -> Sidecar copied"

# Step 3: Build Tauri app
echo "[3/3] Building Tauri application..."
cd "$ROOT_DIR/frontend"
npm run tauri build
echo "  -> Tauri build complete"

echo ""
echo "=== Build complete ==="
echo "Output: frontend/src-tauri/target/release/bundle/"
```

- [ ] **Step 2: Make executable and commit**

```bash
chmod +x scripts/build.sh
git add scripts/build.sh
git commit -m "feat(build): end-to-end build script for PyInstaller + Tauri packaging"
```

---

## Chunk Summary

| Chunk | Tasks | What it delivers |
|---|---|---|
| **1: API Foundation** | 1.1, 1.2 | FastAPI app with health endpoint + WebSocket hub |
| **2: API Routes** | 2.1–2.6 | All REST endpoints: settings, sources, plex, scanner, results, downloads |
| **3: Frontend Scaffolding** | 3.1–3.3 | Tauri + SvelteKit project, API client, all Svelte stores |
| **4: Scanner UI** | 4.1–4.3 | App shell, sidebar, scan controls, filter bar, result grid/list, tiles |
| **5: Settings + Downloads** | 5.1–5.2 | Settings page with categories, downloads history page |
| **6: Tauri Shell** | 6.1–6.2 | Sidecar process management, system tray |
| **7: Packaging** | 7.1–7.2 | PyInstaller spec, build script, end-to-end packaging |

**Execution order:** Chunks 1→2 (backend), then 3→4→5 (frontend), then 6→7 (shell + packaging). Chunks 1-2 and 3-5 can be parallelized since backend and frontend are independent.
