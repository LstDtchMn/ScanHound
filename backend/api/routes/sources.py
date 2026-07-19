"""Source plugin endpoints."""
import logging
from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException

from backend.api.dependencies import ServiceRegistry, get_registry
from backend.sources.registry import get_registry as get_source_registry

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/sources", tags=["sources"])


@router.get("")
def list_sources(reg: ServiceRegistry = Depends(get_registry)):
    source_reg = get_source_registry()
    source_reg.sync_from_config(reg.config)
    return source_reg.list_sources()


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
