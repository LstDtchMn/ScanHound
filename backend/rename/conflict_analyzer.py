"""Background duplicate-quality analysis — fills rename_jobs.conflict_analysis
for every active duplicate (same-destination-path collision or a library-wide
match at a different path), so the Renames row can show a real quality diff
without the user opening the Compare modal.

A plain module of functions, not a class — mirrors pipeline_service's
reconcile_batch(db, ...) shape so both the route layer (RenameService has a
db) and AppService's maintenance loop (no RenameService reference at all) can
call analyze_pending_conflicts(db) directly.
"""
from __future__ import annotations

import logging
import os
from typing import Optional

from backend.rename import dv_detect as _dv
from backend.rename.conflicts import (
    conflict_annotations, find_library_duplicate, needs_dv_layer_scan, rank_conflict,
)
from backend.rename.mediainfo import probe_specs
from backend.rename.path_translation import translate_plex_path

logger = logging.getLogger(__name__)

# The full not-present FileSpec shape probe_specs() itself returns for a
# missing file — reused here for the "no path to probe at all" case so every
# conflict_analysis.existing/incoming is ALWAYS the same full shape (every
# key present, unknowns as null) regardless of which branch produced it.
# Never abbreviate this dict — the frontend's FileSpec type expects every key.
_ABSENT_SPEC_FIELDS = ("size_bytes", "container", "duration_min", "bitrate",
                       "resolution", "video_codec", "hdr", "dv_layer", "audio",
                       "audio_profile")


def _absent_spec(path: Optional[str]) -> dict:
    return {"present": False, "path": path, **{k: None for k in _ABSENT_SPEC_FIELDS}}


def _now_iso() -> str:
    import datetime
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _job_dest(job: dict) -> Optional[str]:
    dest = (job.get("destination_path") or "").rstrip("/\\")
    name = job.get("new_filename") or os.path.basename(job.get("original_path") or "")
    if not dest or not name:
        return None
    return os.path.join(dest, name)


def has_active_duplicate(job: dict, annotations: dict, plex_cache_rows: list) -> bool:
    """Whether *job* has an active duplicate worth analyzing — either an
    exact-destination-path collision (from conflict_annotations, computed
    over the whole active job list) or a library-wide match at a different
    path. Shared by analyze_pending_conflicts' pre-filter and (indirectly,
    via the route's own equivalent check) the list route's trigger — kept
    here so both use the identical definition of "active duplicate"."""
    ann = annotations.get(job.get("id")) or {}
    if ann.get("destination_conflict"):
        return True
    return find_library_duplicate(job, plex_cache_rows) is not None


def analyze_job_conflict(db, job: dict, plex_cache_rows: Optional[list] = None,
                          path_mappings: Optional[str] = None) -> Optional[dict]:
    """Analyze one job's active duplicate (if any) and persist the result.

    Resolution order: an exact-destination-path collision (a file already on
    disk at the job's would-be destination) takes priority over a
    library-wide match — they're mutually exclusive by construction
    (find_library_duplicate excludes same-path matches). Returns the written
    conflict_analysis dict, or None if this job has no active duplicate to
    analyze (nothing is written in that case)."""
    incoming_path = job.get("original_path")
    dest = _job_dest(job)
    kind = None
    existing_path = None

    if dest and os.path.lexists(dest):
        kind = "same_path"
        existing_path = dest
    else:
        rows = plex_cache_rows if plex_cache_rows is not None else db.list_plex_cache_movies()
        match = find_library_duplicate(job, rows)
        if match and match.get("file_path"):
            kind = "library_duplicate"
            existing_path = translate_plex_path(match["file_path"], path_mappings)

    if kind is None:
        return None

    # probe_specs() already returns a full not-present dict for a missing
    # file (checked internally via os.path.exists) — never pre-check
    # existence here, or the fallback shape drifts from probe_specs' own.
    # It returns None only for a genuine ffprobe FAILURE (missing binary,
    # timeout, bad output) — THAT is what "degraded" means.
    existing = probe_specs(existing_path, db=db) if existing_path else _absent_spec(existing_path)
    incoming = probe_specs(incoming_path, db=db) if incoming_path else _absent_spec(incoming_path)

    degraded = existing is None or incoming is None
    existing = existing or _absent_spec(existing_path)
    incoming = incoming or _absent_spec(incoming_path)

    # Attach the filename-borne signals `_quality_score` reads but probe_specs
    # can't know (source/audio/edition tiers live in the release name, not the
    # container). conflict_preview() does the same for the Compare modal
    # (service.py) — without this the row's ★ and the modal's ★ score the same
    # pair differently and can disagree whenever only those tags differ.
    existing = {**existing,
                "original_filename": os.path.basename(existing_path) if existing_path else None}
    incoming = {**incoming,
                "original_filename": job.get("original_filename"),
                "resolution": incoming.get("resolution") or job.get("resolution")}

    # A genuinely not-present incoming (source file vanished between detection
    # and analysis) must not yield a recommendation. rank_conflict() returns
    # "incoming" whenever `existing` is absent, WITHOUT verifying `incoming`
    # exists, so a both-files-vanished TOCTOU race would otherwise render a
    # misleading "keep Incoming ★" for a file that isn't there. Treat it as
    # degraded so advice is suppressed (the analysis blob is still written).
    if not incoming.get("present"):
        degraded = True

    if not degraded and existing.get("present") and incoming.get("present") \
            and _dv.available() and needs_dv_layer_scan(existing, incoming):
        try:
            e_layer = _dv.detect_layer(existing_path).get("layer")
            i_layer = _dv.detect_layer(incoming_path).get("layer")
            if e_layer and e_layer != _dv.LAYER_UNKNOWN:
                existing = {**existing, "dv_layer": e_layer}
            if i_layer and i_layer != _dv.LAYER_UNKNOWN:
                incoming = {**incoming, "dv_layer": i_layer}
        except Exception:
            logger.exception("conflict_analyzer: DV layer scan failed for job %s", job.get("id"))

    if degraded:
        rec = {"recommended": None, "reason": None}
    else:
        rec = rank_conflict(existing if existing.get("present") else None,
                            {**incoming, "id": job.get("id")})

    analysis = {
        "kind": kind,
        "existing": existing,
        "incoming": incoming,
        "recommended": rec["recommended"],
        "reason": rec["reason"],
        "degraded": degraded,
        "analyzed_at": _now_iso(),
    }
    try:
        db.update_rename_job(job["id"], conflict_analysis=analysis)
    except Exception:
        logger.exception("conflict_analyzer: could not write analysis for job %s", job.get("id"))
    return analysis


def analyze_pending_conflicts(db, limit: int = 50, path_mappings: Optional[str] = None) -> int:
    """Maintenance-loop sweep: find active jobs that ACTUALLY have a duplicate
    (has_active_duplicate) with missing/stale conflict_analysis (older than
    detected_at — a duplicate that only became detectable after the job's
    own creation), analyze up to *limit* of them.

    The has_active_duplicate pre-filter matters: without it, *limit* would
    apply to every matched/needs_review job (the vast majority of which have
    no duplicate at all), starving genuine duplicates of the expensive
    ffprobe/dovi_tool budget behind a wall of cheap non-duplicate jobs ahead
    of them in list order. Per-job try/except — one bad job never stops the
    sweep. Returns the count actually processed."""
    if db is None:
        return 0
    jobs = db.list_rename_jobs(limit=100000) or []
    active = [j for j in jobs if j.get("status") in ("matched", "needs_review")]
    # conflict_annotations() needs the WHOLE active-job list (incl. applied)
    # to correctly group same-destination collisions — same as the route.
    annotations = conflict_annotations(jobs)
    plex_rows = db.list_plex_cache_movies()
    candidates = [
        j for j in active
        if has_active_duplicate(j, annotations, plex_rows)
        and (j.get("conflict_analysis") is None
             or (j.get("conflict_analysis") or {}).get("analyzed_at", "") < (j.get("detected_at") or ""))
    ][:limit]
    n = 0
    for job in candidates:
        try:
            analyze_job_conflict(db, job, plex_cache_rows=plex_rows, path_mappings=path_mappings)
        except Exception:
            logger.exception("analyze_pending_conflicts: job %s failed", job.get("id"))
        n += 1
    return n
