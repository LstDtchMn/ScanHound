"""Scanner endpoints: start, stop, status."""
from __future__ import annotations

import logging
import threading
import time
from enum import Enum
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from backend.api.dependencies import ServiceRegistry, get_registry
from backend.api.ws import ws_manager

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/scan", tags=["scanner"])
TMDB_IMAGE_BASE = "https://image.tmdb.org/t/p/w500"

# Scan state tracking
_scan_thread: Optional[threading.Thread] = None
_scan_state: Dict[str, Any] = {
    "state": "idle",
    "progress": 0.0,
    "phase": "",
    "scanned": 0,
    "total": 0,
}
_scan_lock = threading.Lock()

# Last scan results (shared with results route)
_last_scan_items: List[Any] = []
_items_lock = threading.Lock()


class ScanRequest(BaseModel):
    type: str = "deep"
    source: str = "HDEncode"
    sources: Optional[List[str]] = None
    search_query: str = ""
    pages: int = 1
    flags: Optional[Dict[str, bool]] = None


# Map frontend shorthand to backend scan_type values
_SCAN_TYPE_MAP = {
    "deep": "Deep Scan",
    "incremental": "Incremental",
    "loaded": "Loaded Scan",
    "search": "Site Search",
}

# Normalize source names (case-insensitive input → canonical form)
_SOURCE_NAME_MAP = {
    "hdencode": "HDEncode",
    "ddlbase": "DDLBase",
    "adit-hd": "Adit-HD",
    "adithd": "Adit-HD",
}


def get_last_scan_items() -> List[Any]:
    """Access last scan items from the results route."""
    with _items_lock:
        return list(_last_scan_items)


def _progress_callback(progress: float, phase: str) -> None:
    """Called by ScannerService during scan — broadcasts via WebSocket."""
    with _scan_lock:
        _scan_state["progress"] = progress
        _scan_state["phase"] = phase
    ws_manager.broadcast_sync({
        "type": "scan:progress",
        "data": {"progress": progress, "phase": phase},
    })


def _log_callback(message: str, level: str = "info") -> None:
    """Forward scanner log messages to WebSocket."""
    ws_manager.broadcast_sync({
        "type": "log",
        "data": {"level": level, "message": message, "timestamp": time.strftime("%H:%M:%S")},
    })


def _run_scan(reg: ServiceRegistry, req: ScanRequest) -> None:
    """Execute scan in background thread."""
    global _last_scan_items

    scanner = reg.scanner
    if not scanner:
        return

    # Claim the global scan slot so we never run concurrently with a background
    # pre-cache scan (they share one ScannerService and would corrupt each
    # other's in-memory state). Foreground-vs-foreground is already serialized
    # by _scan_state; this guards foreground-vs-background.
    if not scanner.try_acquire_scan():
        logger.info("Scan aborted: a background pre-cache scan is currently running")
        ws_manager.broadcast_sync({
            "type": "scan:error",
            "data": {"message": "A background pre-cache scan is running; please retry in a moment."},
        })
        with _scan_lock:
            _scan_state["state"] = "idle"
            _scan_state["progress"] = 0.0
            _scan_state["phase"] = ""
        return

    with _scan_lock:
        _scan_state["state"] = "running"
        _scan_state["progress"] = 0.0
        _scan_state["phase"] = "starting"

    # Clear stale selections from previous scans
    from backend.api.routes.results import _selected, _selected_lock
    with _selected_lock:
        _selected.clear()

    scanner.set_progress_callback(_progress_callback)
    scanner.set_log_callback(_log_callback)
    start_time = time.time()

    try:
        source_type = req.source
        if req.sources and len(req.sources) == 1:
            source_type = req.sources[0]
        # Normalize source name (case-insensitive)
        source_type = _SOURCE_NAME_MAP.get(source_type.lower(), source_type)

        scan_type = _SCAN_TYPE_MAP.get(req.type, req.type)
        items = scanner.run_scan(
            scan_type=scan_type,
            source_type=source_type,
            pages=req.pages,
            resolution_flags=req.flags,
            search_query=req.search_query,
        )

        duration = time.time() - start_time

        # Store results
        with _items_lock:
            _last_scan_items = list(items) if items else []

        # Broadcast each result
        for item in (items or []):
            item_dict = _media_item_to_dict(item)
            ws_manager.broadcast_sync({"type": "scan:result", "data": item_dict})

        # Stats
        stats = _compute_stats(items)
        ws_manager.broadcast_sync({
            "type": "scan:complete",
            "data": {
                "stats": stats,
                "total": stats["total"],
                "duration": round(duration, 1),
            },
        })

        # Stamp "Last scan" at COMPLETION. Previously only the scheduler set
        # last_scan_time, and it did so when the scan was *triggered* (start),
        # so the UI showed "Just now" the moment a scan began. Manual scans
        # never updated it at all. Stamp it here so it reflects the real last
        # completed scan.
        try:
            reg.config["last_scan_time"] = time.time()
            if reg.backend:
                reg.backend.save_config()
        except Exception:
            logger.warning("Failed to update last_scan_time")

        # Notify
        if reg.notifications:
            reg.notifications.notify_scan_complete(
                total=stats["total"],
                missing=stats["missing"],
                upgrades=stats["upgrade"],
            )

        # Auto-grab
        if reg.auto_grab and reg.auto_grab.enabled and items:
            try:
                ws_manager.broadcast_sync({
                    "type": "autograb:started",
                    "data": {"count": len(items)},
                })
                grabbed = reg.auto_grab.process_items(items)
                ws_manager.broadcast_sync({
                    "type": "autograb:complete",
                    "data": {"grabbed": grabbed if isinstance(grabbed, int) else 0, "total": len(items)},
                })
            except Exception as e:
                logger.warning("Auto-grab failed: %s", e)
                ws_manager.broadcast_sync({
                    "type": "autograb:complete",
                    "data": {"grabbed": 0, "total": len(items), "error": str(e)},
                })

    except Exception as e:
        logger.exception("Scan failed")
        ws_manager.broadcast_sync({
            "type": "scan:error",
            "data": {"message": str(e)},
        })
    finally:
        scanner.release_scan()
        with _scan_lock:
            _scan_state["state"] = "idle"
            _scan_state["progress"] = 0.0
            _scan_state["phase"] = ""


def _media_item_to_dict(item: Any) -> Dict[str, Any]:
    """Convert a MediaItem to a JSON-serializable dict."""
    if hasattr(item, "__dict__"):
        d = {}
        for k, v in item.__dict__.items():
            if k.startswith("_"):
                continue
            if isinstance(v, set):
                d[k] = list(v)
            elif isinstance(v, Enum):
                d[k] = v.value
            else:
                d[k] = v
        # Construct full poster URL from TMDB path fragment
        poster_path = d.get("poster_path") or d.get("poster")
        if poster_path and isinstance(poster_path, str) and poster_path.startswith("/"):
            d["poster_url"] = f"{TMDB_IMAGE_BASE}{poster_path}"
        elif not d.get("poster_url"):
            d["poster_url"] = ""
        return d
    return item if isinstance(item, dict) else {}


def _compute_stats(items: Optional[List[Any]]) -> Dict[str, int]:
    """Compute result statistics."""
    if not items:
        return {"total": 0, "missing": 0, "upgrade": 0, "library": 0}

    missing = 0
    upgrade = 0
    library = 0
    for i in items:
        status = getattr(i, "status", "")
        # status may be an enum — convert to string
        status_str = str(status).lower() if status else ""
        if "missing" in status_str:
            missing += 1
        elif "upgrade" in status_str:
            upgrade += 1
        elif "library" in status_str:
            library += 1

    return {
        "total": len(items),
        "missing": missing,
        "upgrade": upgrade,
        "library": library,
    }


@router.get("/status")
def scan_status():
    with _scan_lock:
        return dict(_scan_state)


@router.post("/start")
def scan_start(
    req: ScanRequest,
    reg: ServiceRegistry = Depends(get_registry),
):
    global _scan_thread

    scanner = reg.scanner
    if scanner and scanner.scan_in_progress:
        raise HTTPException(
            status_code=409,
            detail="A background pre-cache scan is running; please retry in a moment")

    with _scan_lock:
        if _scan_state["state"] == "running":
            raise HTTPException(status_code=409, detail="Scan already running")
        _scan_state["state"] = "running"
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
