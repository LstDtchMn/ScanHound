"""Auto-rename endpoints: track jobs, apply/undo/rematch, Ollama test."""
import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from backend.api.dependencies import ServiceRegistry, get_registry
from backend.rename import llm_identify

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/rename", tags=["rename"])


class RematchRequest(BaseModel):
    tmdb_id: int
    media_type: Optional[str] = None


@router.get("/jobs")
def list_jobs(status: Optional[str] = None, limit: int = 200,
              reg: ServiceRegistry = Depends(get_registry)):
    """List tracked rename jobs (optionally filtered by status) + status counts."""
    if reg.db is None:
        return {"jobs": [], "counts": {}}
    return {
        "jobs": reg.db.list_rename_jobs(status=status, limit=limit),
        "counts": reg.db.count_rename_jobs_by_status(),
    }


@router.get("/status")
def rename_status(reg: ServiceRegistry = Depends(get_registry)):
    """Config + counts for the Renames tab / settings card."""
    cfg = reg.config or {}
    counts = reg.db.count_rename_jobs_by_status() if reg.db else {}
    return {
        "enabled": bool(cfg.get("auto_rename_enabled")),
        "require_confirmation": bool(cfg.get("auto_rename_require_confirmation", True)),
        "confidence_threshold": cfg.get("auto_rename_confidence_threshold", 70),
        "move_method": cfg.get("auto_rename_move_method", "hardlink"),
        "llm_enabled": bool(cfg.get("auto_rename_llm_enabled")),
        "counts": counts,
        "needs_review": counts.get("needs_review", 0),
    }


def _service(reg: ServiceRegistry):
    if reg._rename_service is None:
        raise HTTPException(status_code=503, detail="Rename service not initialized")
    return reg._rename_service


@router.post("/jobs/{job_id}/apply")
def apply_job(job_id: int, reg: ServiceRegistry = Depends(get_registry)):
    out = _service(reg).apply(job_id)
    if not out.get("ok"):
        raise HTTPException(status_code=400, detail=out.get("error", "Apply failed"))
    return out


@router.post("/jobs/{job_id}/undo")
def undo_job(job_id: int, reg: ServiceRegistry = Depends(get_registry)):
    out = _service(reg).undo(job_id)
    if not out.get("ok"):
        raise HTTPException(status_code=400, detail=out.get("error", "Undo failed"))
    return out


@router.post("/jobs/{job_id}/rematch")
def rematch_job(job_id: int, body: RematchRequest,
                reg: ServiceRegistry = Depends(get_registry)):
    out = _service(reg).rematch(job_id, body.tmdb_id, body.media_type)
    if not out.get("ok"):
        raise HTTPException(status_code=400, detail=out.get("error", "Rematch failed"))
    return out


@router.delete("/jobs/{job_id}")
def delete_job(job_id: int, reg: ServiceRegistry = Depends(get_registry)):
    if reg.db is None:
        raise HTTPException(status_code=503, detail="Database unavailable")
    reg.db.delete_rename_job(job_id)
    return {"ok": True}


@router.get("/llm/test")
def llm_test(reg: ServiceRegistry = Depends(get_registry)):
    """Probe the configured Ollama endpoint (lists installed models)."""
    cfg = reg.config or {}
    return llm_identify.test_connection(cfg.get("ollama_base_url", ""))
