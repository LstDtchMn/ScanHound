"""Background pre-cache scanner endpoints: status + manual trigger."""
import logging
import threading
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException

from backend.api.dependencies import ServiceRegistry, get_registry

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/background", tags=["background"])


def _iso(ts: Optional[float]) -> Optional[str]:
    if not ts:
        return None
    try:
        return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
    except (TypeError, ValueError, OSError):
        return None


@router.get("/status")
def background_status(reg: ServiceRegistry = Depends(get_registry)):
    """Report background-scan config + state for the settings card / banner."""
    cfg = reg.config or {}
    scanner = reg._background_scanner
    return {
        "enabled": bool(cfg.get("background_scan_enabled")),
        "interval_hours": cfg.get("background_scan_interval_hours", 6),
        "pages": cfg.get("background_scan_pages", 3),
        "sources": cfg.get("background_scan_sources", []),
        "retain_days": cfg.get("background_scan_retain_days", 7),
        "last_run_at": _iso(cfg.get("background_scan_last_run")),
        "next_run_at": _iso(scanner.next_run_at()) if scanner else None,
        "cached_count": reg.db.count_background_cache() if reg.db else 0,
        "running": bool(scanner and scanner.is_scanning),
    }


@router.post("/scan-now")
def background_scan_now(reg: ServiceRegistry = Depends(get_registry)):
    """Trigger an immediate background scan regardless of schedule."""
    scanner = reg._background_scanner
    if scanner is None:
        raise HTTPException(status_code=503, detail="Background scanner not initialized")
    if scanner.is_scanning:
        raise HTTPException(status_code=409, detail="A background scan is already running")
    threading.Thread(
        target=scanner.scan_once, name="background-scan-now", daemon=True).start()
    return {"status": "triggered"}
