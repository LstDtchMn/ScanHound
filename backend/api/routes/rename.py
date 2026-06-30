"""Auto-rename endpoints: track jobs, apply/undo/rematch, Ollama test."""
import logging
import os
import threading
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from backend.api.dependencies import ServiceRegistry, get_registry
from backend.api.routes.scanner import TMDB_IMAGE_BASE  # same base+size as Scan posters (w500)
from backend.api.ws import ws_manager
from backend.rename import dv_detect, llm_identify
from backend.rename.service import conflict_annotations


def _poster_url(poster_path):
    """Build a TMDB poster URL from a stored path, fail-safe."""
    try:
        if poster_path and str(poster_path).startswith("/"):
            return f"{TMDB_IMAGE_BASE}{poster_path}"
    except Exception:
        pass
    return None

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/rename", tags=["rename"])


class RematchRequest(BaseModel):
    tmdb_id: int
    media_type: Optional[str] = None
    season: Optional[int] = None
    episode: Optional[int] = None


RematchPreviewRequest = RematchRequest


class ProcessFolderRequest(BaseModel):
    folder: str
    dry_run: bool = False


class DvScanRequest(BaseModel):
    folder: str
    force: bool = False


@router.get("/jobs")
def list_jobs(status: Optional[str] = None, limit: int = 200,
              reg: ServiceRegistry = Depends(get_registry)):
    """List tracked rename jobs (optionally filtered by status) + status counts.

    Each job is annotated with ``destination_conflict`` (another job targets the
    same destination file) and, for the best release in a duplicate group,
    ``keep_recommended`` + ``keep_reason`` — so the UI can flag the duplicate and
    suggest which copy to keep before either is applied."""
    if reg.db is None:
        return {"jobs": [], "counts": {}}
    limit = max(1, min(int(limit), 2000))  # clamp: never let a client OOM the box
    jobs = reg.db.list_rename_jobs(status=status, limit=limit)
    # Annotations are computed over ALL active jobs (not just this filtered page),
    # so a duplicate is still flagged when the two halves land on different pages
    # or under a status filter.
    annotations = conflict_annotations(reg.db.list_rename_jobs(limit=100000) or [])
    paths = [j.get("original_path") for j in jobs if j.get("original_path")]
    dv_map = reg.db.get_dv_scans_by_paths(paths)
    for j in jobs:
        ann = annotations.get(j.get("id")) or {}
        j["destination_conflict"] = ann.get("destination_conflict", False)
        j["keep_recommended"] = ann.get("keep_recommended", False)
        j["keep_reason"] = ann.get("keep_reason")
        j["poster_url"] = _poster_url(j.get("poster_path"))
        dv = dv_map.get(j.get("original_path"))
        j["dv_layer"] = (dv or {}).get("dv_layer")
    return {
        "jobs": jobs,
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
    out = _service(reg).rematch(job_id, body.tmdb_id, body.media_type,
                                season=body.season, episode=body.episode)
    if not out.get("ok"):
        raise HTTPException(status_code=400, detail=out.get("error", "Rematch failed"))
    return out


@router.post("/jobs/{job_id}/rematch-preview")
def rematch_preview(job_id: int, body: RematchPreviewRequest,
                    reg: ServiceRegistry = Depends(get_registry)):
    return _service(reg).rematch_preview(
        job_id, body.tmdb_id, body.media_type,
        season=body.season, episode=body.episode)


@router.post("/jobs/{job_id}/accept-combined")
def accept_combined(job_id: int, reg: ServiceRegistry = Depends(get_registry)):
    """Accept a combined-episode detection proposal — promotes job to matched."""
    out = _service(reg).accept_combined(job_id)
    if not out.get("ok"):
        raise HTTPException(status_code=400, detail=out.get("error", "Accept failed"))
    return out


@router.post("/jobs/{job_id}/accept-correction")
def accept_correction(job_id: int, reg: ServiceRegistry = Depends(get_registry)):
    """Accept an episode correction proposal — updates S/E, promotes job to matched."""
    out = _service(reg).accept_correction(job_id)
    if not out.get("ok"):
        raise HTTPException(status_code=400, detail=out.get("error", "Accept failed"))
    return out


@router.delete("/jobs/{job_id}")
def delete_job(job_id: int, reg: ServiceRegistry = Depends(get_registry)):
    if reg.db is None:
        raise HTTPException(status_code=503, detail="Database unavailable")
    reg.db.delete_rename_job(job_id)
    return {"ok": True}


@router.post("/jobs/{job_id}/reidentify")
def reidentify_job(job_id: int, reg: ServiceRegistry = Depends(get_registry)):
    """Re-run identification for one job with the current matcher."""
    return _service(reg).reidentify(job_id)


@router.post("/reidentify-all")
def reidentify_all(reg: ServiceRegistry = Depends(get_registry)):
    """Re-identify every not-yet-applied job, in the background."""
    svc = _service(reg)

    def _run():
        try:
            result = svc.reidentify_all()
            ws_manager.broadcast_sync({"type": "rename:reidentified", "data": result})
            ws_manager.broadcast_sync({"type": "notification", "data": {
                "title": "Re-identify", "body": f"Re-ran {result.get('reidentified', 0)} job(s)",
                "priority": "normal"}})
        except Exception as e:
            logger.exception("reidentify-all failed")
            ws_manager.broadcast_sync({"type": "notification", "data": {
                "title": "Re-identify failed", "body": str(e), "priority": "high"}})

    threading.Thread(target=_run, name="rename-reidentify-all", daemon=True).start()
    return {"status": "started"}


@router.get("/llm/test")
def llm_test(reg: ServiceRegistry = Depends(get_registry)):
    """Probe the configured Ollama endpoint (lists installed models)."""
    cfg = reg.config or {}
    return llm_identify.test_connection(cfg.get("ollama_base_url", ""))


@router.get("/health")
def rename_health(reg: ServiceRegistry = Depends(get_registry)):
    """Report which rename fallback capabilities are actually available — the
    external binaries (ffmpeg/ffprobe/tesseract) and the Ollama model — so a
    silently-broken dependency (e.g. a rebuild without tesseract, a model that
    isn't pulled) is visible instead of just degrading quietly."""
    cfg = reg.config or {}
    bins = {**llm_identify.dependency_status(), **dv_detect.dependency_status()}
    url = cfg.get("ollama_base_url", "")
    model = cfg.get("ollama_model", "")
    ollama = (llm_identify.test_connection(url) if url
              else {"ok": False, "error": "not configured"})
    return {
        "binaries": bins,
        "capabilities": {
            "runtime_check": bins["ffprobe"],
            "subtitles": bins["ffmpeg"],
            "ocr_credits": bins["ffmpeg"] and bins["tesseract"],
            "vision": bins["ffmpeg"],
            "dv_detection": bins.get("dovi_tool", False),
        },
        "ollama": {
            "ok": bool(ollama.get("ok")),
            "model": model,
            "model_available": bool(model) and model in (ollama.get("models") or []),
            "error": ollama.get("error"),
        },
        "llm_enabled": bool(cfg.get("auto_rename_llm_enabled")),
    }


@router.post("/process-folder")
def process_folder(req: ProcessFolderRequest, reg: ServiceRegistry = Depends(get_registry)):
    """Scan a folder for video files and create rename jobs for each — for
    renaming an existing download backlog (no JDownloader involved). Runs in the
    background (a TMDB lookup per file is slow) and reports the result over the
    WebSocket; jobs appear in the Renames list as they're created."""
    svc = _service(reg)
    folder = (req.folder or "").strip()
    dry_run = bool(req.dry_run)
    if not folder:
        raise HTTPException(status_code=400, detail="No folder provided")

    def _run():
        try:
            result = svc.process_folder(folder, dry_run=dry_run)
            if dry_run:
                ws_manager.broadcast_sync({"type": "rename:folder_preview", "data": result})
                body = (result["error"] if result.get("error")
                        else f"Preview: {result.get('would_match', 0)} of "
                             f"{result.get('found', 0)} file(s) would match")
                prio = "high" if result.get("error") else "normal"
            else:
                ws_manager.broadcast_sync({"type": "rename:folder_done", "data": result})
                if result.get("error"):
                    body, prio = result["error"], "high"
                else:
                    body = (f"{result.get('created', 0)} new rename job(s) from "
                            f"{result.get('found', 0)} file(s)"
                            f"{f', {result['skipped']} already tracked' if result.get('skipped') else ''}")
                    prio = "normal"
            ws_manager.broadcast_sync({"type": "notification", "data": {
                "title": "Process folder preview" if dry_run else "Process folder",
                "body": body, "priority": prio}})
        except Exception as e:
            logger.exception("process-folder failed")
            ws_manager.broadcast_sync({"type": "notification", "data": {
                "title": "Process folder failed", "body": str(e), "priority": "high"}})

    threading.Thread(target=_run, name="rename-process-folder", daemon=True).start()
    return {"status": "started", "folder": folder, "dry_run": dry_run}


@router.post("/dv-scan-folder")
def dv_scan_folder(req: DvScanRequest, reg: ServiceRegistry = Depends(get_registry)):
    """Manually scan a folder for Dolby Vision FEL/MEL and record each file in the
    DV inventory. Detection-only (no labeling/tagging). Slow (an RPU walk per
    file), so it runs in the background and streams progress over the WebSocket."""
    svc = _service(reg)
    folder = (req.folder or "").strip()
    force = bool(req.force)
    if not folder:
        raise HTTPException(status_code=400, detail="No folder provided")

    def _run():
        def _progress(done, total, path, layer):
            ws_manager.broadcast_sync({"type": "dv:scan_progress", "data": {
                "done": done, "total": total,
                "file": os.path.basename(path), "layer": layer}})
        try:
            result = svc.scan_folder_dv(folder, force=force, progress_cb=_progress)
            ws_manager.broadcast_sync({"type": "dv:scan_done", "data": result})
            if result.get("error"):
                body, prio = result["error"], "high"
            else:
                fel = (result.get("by_layer") or {}).get("fel", 0)
                body = (f"Scanned {result.get('scanned', 0)} of {result.get('found', 0)} "
                        f"file(s) — {fel} FEL"
                        f"{f', {result['skipped']} unchanged' if result.get('skipped') else ''}")
                prio = "normal"
            ws_manager.broadcast_sync({"type": "notification", "data": {
                "title": "Dolby Vision scan", "body": body, "priority": prio}})
        except Exception as e:
            logger.exception("dv-scan-folder failed")
            ws_manager.broadcast_sync({"type": "notification", "data": {
                "title": "Dolby Vision scan failed", "body": str(e), "priority": "high"}})

    threading.Thread(target=_run, name="dv-scan-folder", daemon=True).start()
    return {"status": "started", "folder": folder, "force": force}


@router.get("/search-tmdb")
def search_tmdb(query: str = "", media_type: str = "movie",
                reg: ServiceRegistry = Depends(get_registry)):
    """TMDB search for the rematch picker; serializes poster_url."""
    results = _service(reg).search_tmdb_public(query, media_type)
    for r in results:
        r["poster_url"] = _poster_url(r.pop("poster_path", None))
    return {"results": results}


@router.get("/dv-scans")
def dv_scans(layer: Optional[str] = None, limit: int = 500,
             reg: ServiceRegistry = Depends(get_registry)):
    """The Dolby Vision inventory: scanned files + per-layer counts."""
    if reg.db is None:
        return {"scans": [], "counts": {}}
    limit = max(1, min(int(limit), 2000))  # clamp: never let a client OOM the box
    return {
        "scans": reg.db.get_dv_scans(dv_layer=layer, limit=limit),
        "counts": reg.db.count_dv_scans_by_layer(),
    }
