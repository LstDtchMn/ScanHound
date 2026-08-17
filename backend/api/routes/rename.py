"""Auto-rename endpoints: track jobs, apply/undo/rematch, Ollama test."""
import logging
import os
import threading
from typing import List, Literal, Optional

from fastapi import APIRouter, Body, Depends, HTTPException
from pydantic import BaseModel, field_validator

from backend.api.dependencies import ServiceRegistry, get_registry
from backend.api.public_errors import capture_public_exception
from backend.api.routes.scanner import TMDB_IMAGE_BASE  # same base+size as Scan posters (w500)
from backend.api.ws import ws_manager
from backend.rename import dv_detect, dv_labeler, fileops, llm_identify
from backend.rename.conflict_analyzer import analyze_job_conflict, has_active_duplicate
from backend.rename.conflicts import conflict_annotations, find_library_duplicate
from backend.rename.dv_import import (
    DvHostReadError, import_dv_host_db, import_dv_rows)


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

# Jobs currently being background-analyzed — prevents the list route (polled
# frequently) from spawning a redundant analysis thread for the same job on
# every request while one's already running.
_analyzing_job_ids: set = set()
_analyzing_lock = threading.Lock()


# ── Path confinement (A3) ────────────────────────────────────────────────
# process_folder / bulk_set_destination / dv_import all take a caller-supplied
# filesystem path. Without a check, a client (or a compromised frontend) could
# point them anywhere the container's filesystem permissions allow — e.g.
# ``../../etc`` or another mounted drive entirely. Confine each to an
# allowlist of roots derived from what the app is actually configured to use,
# reusing main.py's ``_within`` containment rule (real prefix check, not a
# bare ``startswith`` that a sibling directory name could slip past).

def _library_roots(reg: ServiceRegistry) -> list:
    """Configured auto-rename library roots (movie/4K-movie/TV), non-empty only."""
    cfg = reg.config or {}
    roots = [
        cfg.get("auto_rename_movie_library"),
        cfg.get("auto_rename_movie_library_4k"),
        cfg.get("auto_rename_tv_library"),
    ]
    return [os.path.normpath(r) for r in roots if r and str(r).strip()]


def _require_within_roots(path: str, roots: list, what: str) -> str:
    """Return ``path`` normalized if it falls under one of ``roots``, else 422.

    No configured roots at all means nothing has been set up yet — reject
    rather than silently allow-all, since an empty allowlist is not the same
    as "anywhere is fine".
    """
    # Deferred import: backend.api.main imports this module's router (inside
    # create_app(), lazily) as part of its own top-to-bottom execution, so a
    # module-level `from backend.api.main import _within` here would recreate
    # exactly the circular-import trap that laziness avoids — it only happens
    # to work when main.py is imported first (pytest/uvicorn's normal order)
    # and breaks if anything ever imports this module directly, first.
    from backend.api.main import _within
    candidate = os.path.normpath((path or "").strip())
    if not candidate or not roots or not any(_within(candidate, r) for r in roots):
        raise HTTPException(
            status_code=422,
            detail=f"{what} must be inside a configured library root",
        )
    return candidate


# Directory the DV host-detector handoff file lives in — see _DEFAULT_DV_HOST_DB
# below (same env var, so this stays in sync with whatever path it resolves to).
def _dv_host_db_root() -> str:
    return os.path.normpath(os.path.dirname(_DEFAULT_DV_HOST_DB))


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


class DvImportRequest(BaseModel):
    host_db_path: Optional[str] = None


class DvHostRow(BaseModel):
    path: str
    dv_layer: Optional[str] = None
    sig_mtime: Optional[float] = None
    sig_size: Optional[int] = None
    title: Optional[str] = None

    @field_validator("path")
    @classmethod
    def _path_not_blank(cls, v: str) -> str:
        # A blank/whitespace path is not an importable row. Reject at the request
        # boundary (422) so the server's success invariant — every accepted row is
        # processed exactly once — holds without a skipped-row fudge (re-review F2).
        if not (v or "").strip():
            raise ValueError("path must not be blank")
        return v


#: The only row schema this container understands. The detector's
#: DV_ROWS_SCHEMA_VERSION must match; a bump on either side without the other is
#: rejected at the request boundary rather than silently mis-parsed.
DV_ROWS_SCHEMA_VERSION = 1


class DvHostRowsRequest(BaseModel):
    """The detector POSTs its dv_host rows here. `source_rows` is the count the
    detector believes it sent; the server rejects a mismatch so a truncated or
    duplicated body cannot pass as success."""
    rows: List[DvHostRow]
    source_rows: int
    #: Forward-compat: the producer's row schema version. Bump on a shape change.
    schema_version: int = 1

    @field_validator("schema_version")
    @classmethod
    def _schema_supported(cls, v: int) -> int:
        # Reject an unrecognised producer version at the boundary (422) so a
        # future shape change can never be half-applied (re-review cleanup).
        if v != DV_ROWS_SCHEMA_VERSION:
            raise ValueError(
                f"unsupported schema_version {v}; expected {DV_ROWS_SCHEMA_VERSION}")
        return v


class DvSyncRequest(BaseModel):
    dry_run: bool = False
    #: Default TRUE, which INVERTS the previous behaviour of this endpoint.
    #:
    #: sync_labels' own default is additive_only=False, and this endpoint used
    #: to accept it by omission — so pressing the manual button ran a full
    #: reconciliation over every movie in the configured Plex libraries, and
    #: reconcile_movie grants removal on an unmatched title (`may_remove =
    #: authoritative or not additive_only`). A title whose row had not yet been
    #: imported was therefore not merely skipped: its managed DV label was
    #: REMOVED, and Kometa's overlays key off those labels.
    #:
    #: Measured 2026-08-10 against the live library: 444 titles carry a managed
    #: DV label and all 444 currently match an authoritative dv_scan row, so
    #: today's exposure is zero. But that is arithmetic that happens to hold,
    #: not a property anything enforces — it survives only while labels and
    #: scan rows stay in step, and the whole point of the import backlog work
    #: is that they had already drifted for two weeks.
    #:
    #: Destructive reconciliation is still reachable, by asking for it.
    additive_only: bool = True


class BulkIdsRequest(BaseModel):
    ids: list[int] = []


class BulkSetDestRequest(BaseModel):
    ids: list[int] = []
    destination_root: str = ""


class TrashRestoreRequest(BaseModel):
    bucket: str
    name: str


class ApplyConfidentRequest(BaseModel):
    ids: Optional[list[int]] = None


class ApplyRequest(BaseModel):
    # 'replace_library_dup' trashes an existing library file at a DIFFERENT
    # path (movies-only library duplicate) then places the download. 'keep_plex'
    # is deliberately NOT accepted here — it is not a placement and has its own
    # /keep-plex route; routing it through apply() would place the file instead
    # of trashing the redundant download.
    conflict_strategy: Optional[
        Literal["overwrite", "keep_both", "skip", "replace_library_dup"]] = None


@router.get("/jobs")
def list_jobs(status: Optional[str] = None, limit: int = 200, archived: bool = False,
              reg: ServiceRegistry = Depends(get_registry)):
    """List tracked rename jobs (optionally filtered by status) + status counts.

    ``archived`` defaults to False (active jobs only). apply() now archives a
    job the instant it succeeds, so callers that need to see applied/archived
    jobs must pass ``archived=true`` explicitly — this is the minimal slice of
    Task 2's route change pulled forward into Task 1's fix commit, since
    without it a just-applied job is invisible to every consumer of this
    endpoint the moment archiving landed (see Task 1 review finding).

    Each job is annotated with ``destination_conflict`` (another job targets the
    same destination file), ``library_duplicate`` (a same-title/year movie
    already exists in Plex at a DIFFERENT path) and, for the best release in a
    duplicate group, ``keep_recommended`` + ``keep_reason`` — so the UI can flag
    the duplicate and suggest which copy to keep before either is applied.

    A job newly flagged by either signal, with no conflict_analysis yet, gets a
    background analysis thread fired (fire-and-forget, de-duplicated by
    _analyzing_job_ids so rapid repeat polls don't pile up redundant threads)."""
    if reg.db is None:
        return {"jobs": [], "counts": {}}
    limit = max(1, min(int(limit), 2000))  # clamp: never let a client OOM the box
    jobs = reg.db.list_rename_jobs(status=status, limit=limit, archived=archived)
    # Conflict/duplicate annotations are deliberately scoped to ACTIVE jobs only
    # (archived=False default, unaffected by the `archived` param above) — an
    # applied-and-archived job shouldn't flag as a live duplicate/conflict.
    all_active_jobs = reg.db.list_rename_jobs(limit=100000) or []
    # Annotations are computed over ALL active jobs (not just this filtered page),
    # so a duplicate is still flagged when the two halves land on different pages
    # or under a status filter.
    annotations = conflict_annotations(all_active_jobs)
    plex_movie_rows = reg.db.list_plex_cache_movies()
    paths = [j.get("original_path") for j in jobs if j.get("original_path")]
    dv_map = reg.db.get_dv_scans_by_paths(paths)
    to_analyze = []
    for j in jobs:
        ann = annotations.get(j.get("id")) or {}
        j["destination_conflict"] = ann.get("destination_conflict", False)
        j["keep_recommended"] = ann.get("keep_recommended", False)
        j["keep_reason"] = ann.get("keep_reason")
        j["poster_url"] = _poster_url(j.get("poster_path"))
        dv = dv_map.get(j.get("original_path"))
        j["dv_layer"] = (dv or {}).get("dv_layer")
        lib_dup = find_library_duplicate(j, plex_movie_rows) is not None
        j["library_duplicate"] = lib_dup
        # has_active_duplicate re-derives destination_conflict from
        # `annotations` itself rather than reusing j["destination_conflict"]
        # above — same source, just kept as the ONE shared definition of
        # "active duplicate" also used by the maintenance-loop sweep
        # (Task 6's analyze_pending_conflicts), so the two never drift.
        if has_active_duplicate(j, annotations, plex_movie_rows) and not j.get("conflict_analysis"):
            to_analyze.append(j["id"])

    if to_analyze:
        with _analyzing_lock:
            fresh = [jid for jid in to_analyze if jid not in _analyzing_job_ids]
            _analyzing_job_ids.update(fresh)
        if fresh:
            def _run(job_ids):
                try:
                    for jid in job_ids:
                        try:
                            job = reg.db.get_rename_job(jid)
                            if job:
                                analyze_job_conflict(
                                    reg.db, job, plex_cache_rows=plex_movie_rows,
                                    path_mappings=reg.config.get("plex_library_path_mappings"))
                        except Exception:
                            logger.exception("list_jobs: background analysis failed for job %s", jid)
                finally:
                    with _analyzing_lock:
                        _analyzing_job_ids.difference_update(job_ids)
            try:
                threading.Thread(target=_run, args=(fresh,), name="conflict-analyze", daemon=True).start()
            except RuntimeError:
                # Thread creation itself failed (e.g. OS thread exhaustion). The
                # _run() finally that releases these ids never runs, so release
                # them here or they'd stay pinned "in flight" (never re-analyzed)
                # until process restart.
                logger.exception("list_jobs: failed to start conflict-analyze thread")
                with _analyzing_lock:
                    _analyzing_job_ids.difference_update(fresh)

    return {
        "jobs": jobs,
        "counts": reg.db.count_rename_jobs_by_status(),
    }


@router.get("/status")
def rename_status(reg: ServiceRegistry = Depends(get_registry)):
    """Config + counts for the Renames tab / settings card."""
    cfg = reg.config or {}
    counts = reg.db.count_rename_jobs_by_status() if reg.db else {}
    archived_count = len(reg.db.list_rename_jobs(archived=True, limit=100000)) if reg.db else 0
    return {
        "enabled": bool(cfg.get("auto_rename_enabled")),
        "require_confirmation": bool(cfg.get("auto_rename_require_confirmation", True)),
        "confidence_threshold": cfg.get("auto_rename_confidence_threshold", 70),
        "move_method": cfg.get("auto_rename_move_method", "hardlink"),
        "llm_enabled": bool(cfg.get("auto_rename_llm_enabled")),
        "counts": counts,
        "needs_review": counts.get("needs_review", 0),
        "archived": archived_count,
    }


def _service(reg: ServiceRegistry):
    if reg._rename_service is None:
        raise HTTPException(status_code=503, detail="Rename service not initialized")
    return reg._rename_service


# ── Bulk endpoints (must be registered before /{job_id}/… routes so the static
#    /bulk/… path segment isn't swallowed by the int-typed path parameter) ────

@router.post("/jobs/apply-confident")
def apply_confident(body: ApplyConfidentRequest,
                    reg: ServiceRegistry = Depends(get_registry)):
    # Queued: applying moves files (cross-device copies can take minutes) —
    # never inside the HTTP request. Progress arrives per-job over the WS.
    return _service(reg).queue_apply(body.ids, confident_only=True)


@router.post("/jobs/bulk/apply")
def bulk_apply(body: BulkIdsRequest, reg: ServiceRegistry = Depends(get_registry)):
    # Queued (see apply-confident): returns {queued, skipped} immediately.
    return _service(reg).queue_apply(body.ids)


@router.post("/apply/cancel")
def cancel_apply(reg: ServiceRegistry = Depends(get_registry)):
    """Gracefully halt a running bulk apply ("Stop applying") — queue_apply
    or apply-confident — after its in-flight file finishes. Jobs that hadn't
    started yet revert to their prior status instead of being left stuck
    'applying'; the per-job WS broadcast brings the UI back in sync live."""
    return _service(reg).cancel_apply()


@router.post("/jobs/bulk/reidentify")
def bulk_reidentify(body: BulkIdsRequest,
                    reg: ServiceRegistry = Depends(get_registry)):
    return _service(reg).bulk_reidentify(body.ids)


@router.post("/jobs/bulk/delete")
def bulk_delete(body: BulkIdsRequest, reg: ServiceRegistry = Depends(get_registry)):
    if reg.db is None:
        raise HTTPException(status_code=503, detail="Database unavailable")
    return _service(reg).bulk_delete(body.ids)


@router.post("/jobs/bulk/archive")
def bulk_archive(body: BulkIdsRequest, reg: ServiceRegistry = Depends(get_registry)):
    if reg.db is None:
        raise HTTPException(status_code=503, detail="Database unavailable")
    return _service(reg).bulk_archive(body.ids)


@router.post("/jobs/bulk/unarchive")
def bulk_unarchive(body: BulkIdsRequest, reg: ServiceRegistry = Depends(get_registry)):
    if reg.db is None:
        raise HTTPException(status_code=503, detail="Database unavailable")
    return _service(reg).bulk_unarchive(body.ids)


@router.post("/jobs/bulk/set-destination")
def bulk_set_destination(body: BulkSetDestRequest,
                         reg: ServiceRegistry = Depends(get_registry)):
    root = (body.destination_root or "").strip()
    # An empty root is a legitimate "clear the destination" request the
    # service already handles (marks jobs needs_review) — only confine
    # non-empty roots to the configured library allowlist.
    if root:
        root = _require_within_roots(root, _library_roots(reg), "destination_root")
    return _service(reg).bulk_set_destination(body.ids, root)


@router.post("/jobs/{job_id}/apply")
def apply_job(job_id: int, body: ApplyRequest = Body(default=ApplyRequest()),
              reg: ServiceRegistry = Depends(get_registry)):
    # Queued (see apply-confident): the actual move runs on a worker thread and
    # reports back over the WS, so a slow cross-device copy can't time out the
    # request. queued=0 means the job wasn't eligible (already applied/applying).
    # body is optional — a bodyless POST (existing clients) applies with no
    # conflict_strategy, i.e. today's hold-for-review-on-collision behavior.
    out = _service(reg).queue_apply([job_id], conflict_strategy=body.conflict_strategy)
    if not out.get("ok"):
        raise HTTPException(status_code=400, detail=out.get("error", "Apply failed"))
    if not out.get("queued"):
        raise HTTPException(status_code=400,
                            detail="Job is not applicable (already applied or in progress)")
    return out


@router.post("/jobs/{job_id}/undo")
def undo_job(job_id: int, reg: ServiceRegistry = Depends(get_registry)):
    out = _service(reg).undo(job_id)
    if not out.get("ok"):
        raise HTTPException(status_code=400, detail=out.get("error", "Undo failed"))
    return out


@router.post("/jobs/{job_id}/keep-plex")
def keep_plex_job(job_id: int, reg: ServiceRegistry = Depends(get_registry)):
    # Resolve a duplicate conflict by keeping the copy already in Plex: archive
    # the job and move the redundant download to recoverable trash. Not queued
    # (no file placement), so it returns synchronously.
    out = _service(reg).resolve_keep_plex(job_id)
    if not out.get("ok"):
        raise HTTPException(status_code=400, detail=out.get("error", "Keep-Plex failed"))
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


@router.post("/jobs/{job_id}/conflict-preview")
def conflict_preview(job_id: int, reg: ServiceRegistry = Depends(get_registry)):
    """Two-file spec comparison (existing on-disk file vs incoming) for a
    destination conflict, with a recommendation. No body, no persistence —
    mirrors rematch-preview's read-only pattern."""
    return _service(reg).conflict_preview(job_id)


@router.post("/jobs/{job_id}/scan-dv-conflict")
def scan_dv_conflict(job_id: int, reg: ServiceRegistry = Depends(get_registry)):
    """On-demand Dolby Vision FEL/MEL scan of a conflict's two files (incoming
    source + existing destination). Slow (an RPU walk per file), so it runs in
    the background and broadcasts the result over the WebSocket — mirrors
    dv-scan-folder's fire-and-forget pattern, scoped to one job's two files.

    Broadcasts its own ``dv:conflict_scan_done`` event rather than reusing
    dv-scan-folder's ``dv:scan_done`` — that event is already bound to the
    full-library DV-scan panel's state on the frontend, and reusing it here
    would corrupt that panel's progress/result state."""
    svc = _service(reg)

    def _run():
        try:
            result = svc.scan_conflict_dv(job_id)
            ws_manager.broadcast_sync({"type": "dv:conflict_scan_done", "data": result})
        except Exception:
            logger.exception("scan-dv-conflict failed")

    threading.Thread(target=_run, name="scan-dv-conflict", daemon=True).start()
    return {"status": "scanning", "job_id": job_id}


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
            public = capture_public_exception(
                logger, e, code="reidentify_failed",
                message="Re-identification could not be completed.",
                context="reidentify-all failed",
            )
            ws_manager.broadcast_sync({"type": "notification",
                                       "data": public.notification_data(
                                           title="Re-identify failed")})

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
    isn't pulled) is visible instead of just degrading quietly.

    Also surfaces two otherwise-invisible failure signals: how many files were
    dropped by the most recent process_package() run due to a genuine DB
    error (failed_db_last_package), and whether an un-acknowledged database
    corruption quarantine flag is currently on disk (db_corruption_flag)."""
    cfg = reg.config or {}
    bins = {**llm_identify.dependency_status(), **dv_detect.dependency_status()}
    url = cfg.get("ollama_base_url", "")
    model = cfg.get("ollama_model", "")
    ollama = (llm_identify.test_connection(url) if url
              else {"ok": False, "error": "not configured"})

    failed_db_last_package = getattr(reg._rename_service, "last_package_failed_db", 0)
    db_corruption_flag = False
    if reg.db is not None:
        from backend.database import db_corruption_flag_present
        db_corruption_flag = db_corruption_flag_present(reg.db.db_path)

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
        "failed_db_last_package": failed_db_last_package,
        "db_corruption_flag": db_corruption_flag,
    }


@router.get("/trash")
def list_trash(reg: ServiceRegistry = Depends(get_registry)):
    """List every trashed file across both trash locations (see
    fileops.all_trash_roots).

    Each entry reports {bucket, name, size, trashed_at, original_path,
    restorable}; original_path is null when the bucket has no manifest record
    for that file (e.g. it predates the manifest feature)."""
    return {"entries": fileops.list_trash_entries(fileops.all_trash_roots())}


@router.post("/trash/restore")
def restore_trash(body: TrashRestoreRequest, reg: ServiceRegistry = Depends(get_registry)):
    """Restore a manifest-backed trash entry to its original_path.

    Refuses (409) if the destination is occupied or the entry/manifest can't
    be found; rejects (400) bucket/name values that look like path traversal."""
    bucket, name = body.bucket, body.name
    if not fileops._is_safe_component(bucket) or not fileops._is_safe_component(name):
        raise HTTPException(status_code=400, detail="Invalid bucket or name")
    result = fileops.restore_trash_entry(bucket, name, fileops.all_trash_roots())
    if not result.get("ok"):
        error = result.get("error", "Restore failed")
        status = 409 if "already exists" in error.lower() else 404
        raise HTTPException(status_code=status, detail=error)
    return result


@router.post("/trash/delete")
def delete_trash(body: TrashRestoreRequest, reg: ServiceRegistry = Depends(get_registry)):
    """Permanently delete one trash entry — the counterpart to /trash/restore.

    Unlike restore this accepts manifest-less entries (there's nowhere to put
    them back, which is precisely when a user wants them gone). Rejects (400)
    bucket/name values that look like path traversal; 404 if not found."""
    bucket, name = body.bucket, body.name
    if not fileops._is_safe_component(bucket) or not fileops._is_safe_component(name):
        raise HTTPException(status_code=400, detail="Invalid bucket or name")
    result = fileops.delete_trash_entry(bucket, name, fileops.all_trash_roots())
    if not result.get("ok"):
        raise HTTPException(status_code=404, detail=result.get("error", "Delete failed"))
    return result


@router.post("/trash/empty")
def empty_trash(reg: ServiceRegistry = Depends(get_registry)):
    """Permanently delete every trashed file now, ignoring trash_retention_days.

    Irreversible — the frontend confirms before calling. Returns the same
    summary shape as the retention sweep."""
    return fileops.empty_trash(fileops.all_trash_roots())


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
    # The UI accepts host-style paths (e.g. F:\Downloads) and relies on
    # RenameService._translate_path to map them into the container's mounted
    # view — confining the *raw* input against container roots would break
    # that documented flow. Translate first, then confine the resolved,
    # container-side path to the configured library roots; reject anything
    # that lands outside them (a mapping miss, `..` escape, or another mount
    # entirely) with 422 instead of silently walking whatever it resolved to.
    resolved_folder = svc._translate_path(folder)
    _require_within_roots(resolved_folder, _library_roots(reg), "folder")

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
                    skipped = result.get("skipped", 0)
                    skipped_suffix = (
                        f", {skipped} already tracked" if skipped else ""
                    )
                    body = (
                        f"{result.get('created', 0)} new rename job(s) from "
                        f"{result.get('found', 0)} file(s)"
                        f"{skipped_suffix}"
                    )
                    prio = "normal"
            ws_manager.broadcast_sync({"type": "notification", "data": {
                "title": "Process folder preview" if dry_run else "Process folder",
                "body": body, "priority": prio}})
        except Exception as e:
            public = capture_public_exception(
                logger, e, code="process_folder_failed",
                message="The folder could not be processed.",
                context="process-folder failed",
            )
            ws_manager.broadcast_sync({"type": "notification",
                                       "data": public.notification_data(
                                           title="Process folder failed")})

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
                skipped = result.get("skipped", 0)
                skipped_suffix = (
                    f", {skipped} unchanged" if skipped else ""
                )
                body = (
                    f"Scanned {result.get('scanned', 0)} of "
                    f"{result.get('found', 0)} file(s) — {fel} FEL"
                    f"{skipped_suffix}"
                )
                prio = "normal"
            ws_manager.broadcast_sync({"type": "notification", "data": {
                "title": "Dolby Vision scan", "body": body, "priority": prio}})
        except Exception as e:
            public = capture_public_exception(
                logger, e, code="dv_scan_failed",
                message="The Dolby Vision scan could not be completed.",
                context="dv-scan-folder failed",
            )
            ws_manager.broadcast_sync({"type": "notification",
                                       "data": public.notification_data(
                                           title="Dolby Vision scan failed")})

    threading.Thread(target=_run, name="dv-scan-folder", daemon=True).start()
    return {"status": "started", "folder": folder, "force": force}


# default host store path inside the container's bind-mounted data dir
_DEFAULT_DV_HOST_DB = os.environ.get(
    "SCANHOUND_DV_HOST_DB", "/data/dv_host.db")


@router.post("/dv-host-rows")
def dv_host_rows(
    req: DvHostRowsRequest, reg: ServiceRegistry = Depends(get_registry)
):
    """Ingest DV rows POSTed by the host detector — the durable transport.

    The container never reads the host file, so there is no cross-OS SQLite /
    WAL / bind-mount handoff to get wrong. The response is mechanically
    checkable: the detector accepts it only when ``ok`` is true, ``processed``
    equals ``source_rows``, and ``failed`` is zero (round-4 review). Any
    shortfall is a NON-2xx so it can never read as success.
    """
    if reg.db is None:
        raise HTTPException(status_code=503, detail="DB not initialized")
    rows = [r.model_dump() for r in req.rows]
    if req.source_rows != len(rows):
        # A truncated or duplicated body. Refuse rather than import a partial set.
        raise HTTPException(
            status_code=422,
            detail={
                "ok": False,
                "error": "source_rows_mismatch",
                "source_rows": req.source_rows,
                "received_rows": len(rows),
            },
        )
    return _import_response(import_dv_rows(reg.db, rows))


def _import_response(result: dict) -> dict:
    """Validate an import_dv_rows result into a response body, or raise 500.

    ONE invariant, shared by both endpoints (re-review F2/F3): every source row
    must be processed exactly once with zero failures. `processed == source_rows`
    (not `processed + skipped`) — blank-path rows are rejected at validation, so a
    processed shortfall now means a real failure, and a partial or failed import
    can never read as an HTTP-200 success.
    """
    ok = result["failed"] == 0 and result["processed"] == result["source_rows"]
    body = {"ok": ok, **result}
    if not ok:
        raise HTTPException(status_code=500, detail=body)
    return body


@router.post("/dv-import")
def dv_import(req: DvImportRequest, reg: ServiceRegistry = Depends(get_registry)):
    """Ingest the host detector's dv_host.db into dv_scan (source='scan').

    LEGACY file path (superseded by /dv-host-rows). Retained for compatibility,
    but a read/open failure is now a 503 rather than a silent zero-row success —
    an unreadable host DB and a successfully-read empty one are different states
    (round-4 finding 2).
    """
    if reg.db is None:
        raise HTTPException(status_code=503, detail="DB not initialized")
    path = (req.host_db_path or _DEFAULT_DV_HOST_DB)
    # Confine to the expected data/ handoff dir (dirname of _DEFAULT_DV_HOST_DB,
    # i.e. SCANHOUND_DV_HOST_DB's directory, /data by default) — the only place
    # the host detector is ever configured to drop dv_host.db. Rejects an
    # explicit host_db_path pointed anywhere else (`..` escape, another mount).
    path = _require_within_roots(path, [_dv_host_db_root()], "host_db_path")
    try:
        result = import_dv_host_db(reg.db, path)
    except DvHostReadError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    # Same validator as /dv-host-rows: a partial destination upsert is a 500, not
    # a silent 200 (re-review F3).
    return _import_response(result)


def wire_safe_sync_result(result):
    """A copy of the sync summary that is safe to put on the WebSocket.

    ``layer_conflict_paths`` is unbounded in principle — capped only by the
    number of colliding keys — and the completion event goes to every connected
    client on every manual sync, dry runs included. The EXACT set is wanted
    server-side, because the unattended alert dedups on it, so it stays in the
    returned summary and is trimmed here instead: at the boundary that actually
    transmits it (peer review round 2, L1).

    ``layer_conflicts`` remains the exact count either way, so a client can
    always tell how many there are even when it only receives a sample.
    """
    if not isinstance(result, dict):
        return result
    paths = result.get("layer_conflict_paths")
    if not isinstance(paths, list):
        return result
    trimmed = dict(result)
    trimmed["layer_conflict_paths"] = paths[:dv_labeler.CONFLICT_SAMPLE]
    trimmed["layer_conflict_paths_truncated"] = (
        len(paths) > dv_labeler.CONFLICT_SAMPLE)
    return trimmed


def dv_sync_summary_body(result, dry_run):
    """The one-line summary the manual DV sync reports when it finishes.

    Module-level and pure so it can be tested without driving the route's
    background thread — the counts are the only thing worth asserting and a
    threaded end-to-end test for a display string would be flaky for no gain.

    Mentions conflicts because this summary is the ONLY place a manual run
    reports them. A file whose two scan rows claim different DV layers is left
    strictly alone, so it moves none of matched/added/removed: without this,
    a sync that skipped titles is indistinguishable from one that had nothing
    to do. The scheduled sync has its own alert; a manual run has only this.
    """
    conflicts = result.get("layer_conflicts", 0)
    return (f"{result['matched']} matched, {result['added']} added, "
            f"{result['removed']} removed"
            + (f", {conflicts} file(s) skipped — contradicting scan records"
               if conflicts else "")
            + (" (dry run)" if dry_run else ""))


@router.post("/dv-sync-labels")
def dv_sync_labels(req: DvSyncRequest, reg: ServiceRegistry = Depends(get_registry)):
    """Reconcile managed DV labels on every movie against dv_scan (source='scan').
    Runs in the background; streams dv:sync_progress and ALWAYS emits dv:sync_done."""
    dry_run = bool(req.dry_run)
    additive_only = bool(req.additive_only)
    if reg.db is None:
        raise HTTPException(status_code=503, detail="DB not initialized")
    plex_manager = getattr(reg._plex_service, "plex_manager", None) if reg._plex_service else None
    if plex_manager is None:
        raise HTTPException(status_code=503, detail="Plex not initialized")

    def _run():
        result = None
        try:
            def _progress(done, total):
                ws_manager.broadcast_sync({"type": "dv:sync_progress", "data": {
                    "done": done, "total": total}})
            result = dv_labeler.sync_labels(
                reg.db, plex_manager, reg.config,
                dry_run=dry_run, progress_cb=_progress,
                additive_only=additive_only)
            ws_manager.broadcast_sync({"type": "notification", "data": {
                "title": "Dolby Vision label sync",
                "body": dv_sync_summary_body(result, dry_run),
                "priority": ("high" if result.get("layer_conflicts")
                             else "normal")}})
        except Exception as e:
            public = capture_public_exception(
                logger, e, code="dv_label_sync_failed",
                message="Dolby Vision labels could not be synchronized.",
                context="dv-sync-labels failed",
            )
            ws_manager.broadcast_sync({"type": "notification",
                                       "data": public.notification_data(
                                           title="Dolby Vision label sync failed")})
            result = {"error": public.message, **public.as_detail()}
        finally:
            ws_manager.broadcast_sync({"type": "dv:sync_done",
                                       "data": wire_safe_sync_result(result)})

    threading.Thread(target=_run, name="dv-sync-labels", daemon=True).start()
    return {"status": "started", "dry_run": dry_run}


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
    """The Dolby Vision inventory: scanned files, per-layer counts, conflicts.

    ``conflicts`` is CURRENT STATE, recomputed from the rows on every call, not
    a record of whether anyone was ever told. The unattended conflict alert
    broadcasts to whoever is connected at that instant and raises nothing if
    that is nobody, so a conflict that appeared while no client was open would
    be marked "seen" and never re-announced. Deriving it here means an
    unresolved conflict is discoverable whenever someone next opens the panel,
    regardless of what happened to the notification (peer review round 2, M1).
    """
    if reg.db is None:
        return {"scans": [], "counts": {}, "conflicts": {"count": 0, "sample": [],
                                                         "truncated": False}}
    limit = max(1, min(int(limit), 2000))  # clamp: never let a client OOM the box
    # Separate read from the paged `scans` above: the conflict set is a property
    # of ALL scan rows, so a page of 500 cannot answer it.
    all_rows = reg.db.get_dv_scans(limit=1000000, source="scan")
    return {
        "scans": reg.db.get_dv_scans(dv_layer=layer, limit=limit, source="scan"),
        "counts": reg.db.count_dv_scans_by_layer(source="scan"),
        "conflicts": dv_labeler.current_conflicts(all_rows),
    }
