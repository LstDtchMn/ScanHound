"""Source plugin endpoints."""
import logging
from typing import Any, Dict

from fastapi import APIRouter, Depends

from backend.api.dependencies import ServiceRegistry, get_registry
from backend.sources.registry import SourceRegistry

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/sources", tags=["sources"])


@router.get("")
def list_sources(reg: ServiceRegistry = Depends(get_registry)):
    source_reg = SourceRegistry()
    return source_reg.list_sources()


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
