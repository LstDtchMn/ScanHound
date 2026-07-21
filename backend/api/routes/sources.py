"""Source plugin endpoints."""
import logging
from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException

from backend.api.dependencies import ServiceRegistry, get_registry
from backend.sources.registry import get_registry as get_source_registry
from backend.source_health import effective_health_state
from backend.hdencode_coordinator import get_hdencode_coordinator

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/sources", tags=["sources"])


@router.get("")
def list_sources(reg: ServiceRegistry = Depends(get_registry)):
    source_reg = get_source_registry()
    source_reg.sync_from_config(reg.config)
    sources = source_reg.list_sources()
    try:
        health_by_source = reg.db.get_source_health() if reg.db else {}
    except Exception:
        # Health is advisory. A locked/corrupt/unavailable snapshot must not
        # make the source-settings endpoint unusable.
        logger.warning("Source health snapshot unavailable", exc_info=True)
        health_by_source = {}
    for source in sources:
        health = health_by_source.get(source["name"], {})
        source["health_state"] = effective_health_state(health)
        source["health_reason_code"] = health.get("reason_code")
        source["health_updated_at"] = health.get("updated_at")
        source["last_success_at"] = health.get("last_success_at")
        source["last_failure_at"] = health.get("last_failure_at")
        source["cooldown_until"] = health.get("cooldown_until")
        if source["name"] == "hdencode":
            source["traffic"] = get_hdencode_coordinator().snapshot()
    return sources


@router.put("/{source_id}")
def update_source(
    source_id: str,
    body: Dict[str, Any],
    reg: ServiceRegistry = Depends(get_registry),
):
    source_reg = get_source_registry()
    if source_reg.get_config(source_id) is None:
        raise HTTPException(status_code=404, detail=f"Unknown source: {source_id}")

    config_key = f"{source_id}_enabled"
    if "enabled" in body:
        enabled = body["enabled"]
        if not isinstance(enabled, bool):
            raise HTTPException(status_code=422, detail="enabled must be a boolean")
        reg.config[config_key] = enabled
        source_reg.enable_source(source_id, enabled)
        if reg.backend:
            reg.backend.save_config()
    return {"status": "ok", "source": source_id}
