"""Scheduler endpoints: status, config, trigger."""
import threading
import time
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from typing import Optional

from backend.api.dependencies import ServiceRegistry, get_registry
from backend.api.routes.scanner import ScanRequest, _run_scan, _scan_state, _scan_lock

router = APIRouter(prefix="/scheduler", tags=["scheduler"])


@router.get("/status")
def scheduler_status(reg: ServiceRegistry = Depends(get_registry)):
    """Get scheduler status."""
    config = reg.config or {}
    backend = reg.backend

    enabled = config.get("scheduler_enabled", False)
    interval = config.get("scheduler_interval", 24)
    idle_only = config.get("scheduler_only_when_idle", False)
    last_scan = config.get("last_scan_time", 0)

    # Calculate next run time
    next_run = None
    if enabled and last_scan:
        next_ts = last_scan + (interval * 3600)
        if next_ts > time.time():
            from datetime import datetime, timezone
            next_run = datetime.fromtimestamp(next_ts, tz=timezone.utc).isoformat()

    last_run_str = None
    if last_scan:
        from datetime import datetime, timezone
        last_run_str = datetime.fromtimestamp(last_scan, tz=timezone.utc).isoformat()

    return {
        "enabled": enabled,
        "interval_hours": interval,
        "idle_only": idle_only,
        "last_run": last_run_str,
        "next_run": next_run,
        "scheduler_active": bool(
            backend and
            getattr(backend, '_scheduler_thread', None) and
            backend._scheduler_thread.is_alive()
        ),
    }


class SchedulerConfig(BaseModel):
    enabled: Optional[bool] = None
    interval_hours: Optional[int] = None
    idle_only: Optional[bool] = None


@router.put("/config")
def scheduler_config(body: SchedulerConfig, reg: ServiceRegistry = Depends(get_registry)):
    """Update scheduler configuration."""
    config = reg.config
    if config is None:
        raise HTTPException(status_code=503, detail="Backend not initialized")

    updated = {}
    if body.enabled is not None:
        config["scheduler_enabled"] = body.enabled
        updated["scheduler_enabled"] = body.enabled
    if body.interval_hours is not None:
        clamped = max(1, min(168, body.interval_hours))
        config["scheduler_interval"] = clamped
        updated["scheduler_interval"] = clamped
    if body.idle_only is not None:
        config["scheduler_only_when_idle"] = body.idle_only
        updated["scheduler_only_when_idle"] = body.idle_only

    # Persist config
    if reg.backend:
        reg.backend.save_config()

    return {"status": "updated", "updated": updated}


@router.post("/trigger")
def scheduler_trigger(reg: ServiceRegistry = Depends(get_registry)):
    """Manually trigger an immediate scan."""
    if not reg._scanner_service:
        raise HTTPException(status_code=503, detail="Scanner not initialized")

    # Use the same lock and state as /scan/start to prevent concurrent scans
    req = ScanRequest(type="incremental")
    with _scan_lock:
        if _scan_state["state"] == "running":
            raise HTTPException(status_code=409, detail="Scan already in progress")
        _scan_state["state"] = "running"
        threading.Thread(
            target=_run_scan, args=(reg, req), name="scheduled-scan", daemon=True
        ).start()

    return {"status": "triggered"}
