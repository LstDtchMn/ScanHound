"""Plex integration endpoints."""
from __future__ import annotations

import csv
import io
import logging
from typing import List, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Response
from pydantic import BaseModel

from backend.api.dependencies import ServiceRegistry, get_registry
from backend.api.ws import ws_manager
from backend.rename.path_translation import translate_plex_path

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/plex", tags=["plex"])


def _unique_movie_count(movies: list) -> int:
    """Count unique movies by rating_key (ignores multiple media versions)."""
    if not movies:
        return 0
    seen = {m.get("rating_key") for m in movies if m.get("rating_key")}
    return len(seen)


@router.get("/status")
def plex_status(reg: ServiceRegistry = Depends(get_registry)):
    plex = reg.plex
    if not plex:
        return {
            "connected": False,
            "server": "",
            "movie_count": 0,
            "tv_count": 0,
        }
    connected = bool(getattr(plex, "plex_manager", None) and plex.plex_manager.is_connected)
    server_name = reg.config.get("plex_server_name", "")
    if not server_name:
        try:
            server_info = plex.plex_manager.get_server_info() if getattr(plex, "plex_manager", None) else None
            server_name = server_info.get("name", "") if server_info else ""
        except Exception:
            server_name = ""
    return {
        "connected": connected,
        "server": server_name,
        "movie_count": _unique_movie_count(plex.plex_movies),
        "tv_count": len(plex.plex_tv) if plex.plex_tv else 0,
    }


@router.post("/connect")
def plex_connect(
    background_tasks: BackgroundTasks,
    reg: ServiceRegistry = Depends(get_registry),
):
    plex = reg.plex
    if not plex:
        raise HTTPException(status_code=503, detail="Plex service not initialized")

    def _connect():
        try:
            success, message = plex.connect()
            ws_manager.broadcast_sync({
                "type": "plex:status",
                "data": {"connected": success, "server": message},
            })
            if success:
                plex.load_libraries()
                server_name = reg.config.get("plex_server_name", "") or message.replace("Connected to ", "").replace(" (via plex.tv)", "")
                ws_manager.broadcast_sync({
                    "type": "plex:status",
                    "data": {
                        "connected": True,
                        "server": server_name,
                        "movie_count": _unique_movie_count(plex.plex_movies),
                        "tv_count": len(plex.plex_tv) if plex.plex_tv else 0,
                    },
                })
        except Exception as e:
            logger.exception("Plex connection failed")
            ws_manager.broadcast_sync({
                "type": "plex:status",
                "data": {"connected": False, "server": str(e)},
            })

    background_tasks.add_task(_connect)
    return {"status": "connecting"}


@router.get("/libraries")
def plex_libraries(reg: ServiceRegistry = Depends(get_registry)):
    movie_libs = reg.config.get("movie_libs", [])
    tv_libs = reg.config.get("tv_libs", [])
    known = reg.config.get("known_libraries", [])
    return {
        "movie_libraries": movie_libs,
        "tv_libraries": tv_libs,
        "known_libraries": known,
    }


class UpdatePlexLibraries(BaseModel):
    movie_libraries: Optional[List[str]] = None
    tv_libraries: Optional[List[str]] = None


@router.put("/libraries")
def update_plex_libraries(
    body: UpdatePlexLibraries,
    reg: ServiceRegistry = Depends(get_registry),
):
    """Update movie/tv library assignments."""
    if body.movie_libraries is not None:
        reg.config["movie_libs"] = body.movie_libraries
    if body.tv_libraries is not None:
        reg.config["tv_libs"] = body.tv_libraries
    if reg.backend:
        reg.backend.save_config()
    return {"status": "ok"}


@router.post("/refresh")
def plex_refresh(
    background_tasks: BackgroundTasks,
    reg: ServiceRegistry = Depends(get_registry),
):
    """Refresh Plex library cache."""
    plex = reg.plex
    if not plex:
        raise HTTPException(status_code=503, detail="Plex service not initialized")

    def _refresh():
        try:
            ws_manager.broadcast_sync({
                "type": "plex:refreshing",
                "data": {"status": "loading"},
            })
            plex.load_libraries()
            ws_manager.broadcast_sync({
                "type": "plex:status",
                "data": {
                    "connected": True,
                    "server": reg.config.get("plex_server_name", ""),
                    "movie_count": _unique_movie_count(plex.plex_movies),
                    "tv_count": len(plex.plex_tv) if plex.plex_tv else 0,
                },
            })
        except Exception as e:
            logger.exception("Plex refresh failed")
            ws_manager.broadcast_sync({
                "type": "plex:status",
                "data": {"connected": False, "server": str(e)},
            })

    background_tasks.add_task(_refresh)
    return {"status": "refreshing"}


@router.get("/stats")
def plex_stats(reg: ServiceRegistry = Depends(get_registry)):
    plex = reg.plex
    if not plex:
        return {}
    return getattr(plex, "stats", {})


def _movie_targets_for_scope(reg: ServiceRegistry, scope: str, ids: Optional[List[str]]) -> list:
    """Resolve a scan scope into a list of {path, title, rating_key, imdb_id}
    dicts, movies only, skipping rows with no known file_path. Each path is
    translated from Plex's own reported form into the container-local path
    the docker-compose mounts actually expose."""
    movies = reg.db.list_plex_cache_movies() if reg.db else []
    if scope == "selected":
        wanted = {str(value) for value in (ids or [])}
        movies = [
            m for m in movies
            if str(m.get("rating_key") or "") in wanted
            or str(m.get("key") or "") in wanted
        ]
    mappings = reg.config.get("plex_library_path_mappings") if reg.config else None
    targets = []
    for m in movies:
        path = m.get("file_path")
        if not path:
            continue
        targets.append({
            "path": translate_plex_path(path, mappings),
            "title": m.get("title"),
            "rating_key": m.get("rating_key"),
            "imdb_id": m.get("imdb_id"),
            "library_name": m.get("library_name"),
            "resolution": m.get("res"),
        })
    return targets


class ScanMetadataRequest(BaseModel):
    scope: str
    ids: Optional[List[str]] = None


class DurableMetadataScanRequest(BaseModel):
    """Explicit, read-only metadata-inventory scan request.

    ``pilot`` and ``targeted`` require caller-selected Plex keys. ``full`` is
    intentionally limited to the cached 4K movie set; it never scans TV or
    starts from a background scheduler.
    """
    scope: str
    ids: Optional[List[str]] = None


@router.post("/scan-metadata")
def plex_scan_metadata(
    body: ScanMetadataRequest,
    reg: ServiceRegistry = Depends(get_registry),
):
    if body.scope not in ("all", "selected"):
        raise HTTPException(status_code=400, detail="scope must be 'all' or 'selected'")
    if body.scope == "selected" and not body.ids:
        raise HTTPException(status_code=400, detail="ids required when scope is 'selected'")

    targets = _movie_targets_for_scope(reg, body.scope, body.ids)
    job = reg.plex_metadata_scan_job
    started = job.start(targets)
    if not started:
        # job.status_dict() carries its own "status" key (e.g. "running") —
        # it must be spread first so the "already_running" sentinel below
        # wins in the merged dict, letting callers tell "this POST was a
        # no-op" apart from a normal in-progress status poll.
        return {**job.status_dict(), "status": "already_running"}
    return {"status": "starting", "total": len(targets)}


@router.post("/scan-metadata/cancel")
def plex_scan_metadata_cancel(reg: ServiceRegistry = Depends(get_registry)):
    reg.plex_metadata_scan_job.cancel()
    return {"status": "cancelling"}


@router.get("/scan-metadata/status")
def plex_scan_metadata_status(reg: ServiceRegistry = Depends(get_registry)):
    return reg.plex_metadata_scan_job.status_dict()


@router.post("/metadata-scans")
def plex_start_durable_metadata_scan(
    body: DurableMetadataScanRequest,
    reg: ServiceRegistry = Depends(get_registry),
):
    if body.scope not in ("pilot", "full", "targeted"):
        raise HTTPException(status_code=400, detail="scope must be 'pilot', 'full', or 'targeted'")
    if body.scope in ("pilot", "targeted") and not body.ids:
        raise HTTPException(status_code=400, detail="ids are required for pilot or targeted scans")

    targets = _movie_targets_for_scope(
        reg, "selected" if body.scope in ("pilot", "targeted") else "all", body.ids
    )
    targets = [
        target for target in targets
        if str(target.get("resolution") or "").lower() in {"2160p", "4k", "uhd"}
    ]
    if not targets:
        raise HTTPException(status_code=400, detail="no eligible 4K movie files were found")
    try:
        return reg.plex_metadata_scan_job.start_run(body.scope, targets)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/metadata-scans/{run_uuid}")
def plex_durable_metadata_scan_status(run_uuid: str, reg: ServiceRegistry = Depends(get_registry)):
    if not reg.db:
        raise HTTPException(status_code=503, detail="Database service not initialized")
    run = reg.db.get_metadata_scan_run(run_uuid)
    if not run:
        raise HTTPException(status_code=404, detail="metadata scan run not found")
    return run


@router.get("/metadata-scans/{run_uuid}/items")
def plex_durable_metadata_scan_items(
    run_uuid: str, status: Optional[str] = None,
    reg: ServiceRegistry = Depends(get_registry),
):
    if not reg.db or not reg.db.get_metadata_scan_run(run_uuid):
        raise HTTPException(status_code=404, detail="metadata scan run not found")
    return {"items": reg.db.list_metadata_scan_items(run_uuid, status=status)}


def _run_control(operation, run_uuid: str):
    try:
        return operation(run_uuid)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/metadata-scans/{run_uuid}/pause")
def plex_pause_metadata_scan(run_uuid: str, reg: ServiceRegistry = Depends(get_registry)):
    return _run_control(reg.plex_metadata_scan_job.pause, run_uuid)


@router.post("/metadata-scans/{run_uuid}/resume")
def plex_resume_metadata_scan(run_uuid: str, reg: ServiceRegistry = Depends(get_registry)):
    return _run_control(reg.plex_metadata_scan_job.resume, run_uuid)


@router.post("/metadata-scans/{run_uuid}/cancel")
def plex_cancel_durable_metadata_scan(
    run_uuid: str, reg: ServiceRegistry = Depends(get_registry)
):
    return _run_control(reg.plex_metadata_scan_job.cancel, run_uuid)


@router.post("/metadata-scans/{run_uuid}/retry-failures")
def plex_retry_metadata_scan_failures(
    run_uuid: str, reg: ServiceRegistry = Depends(get_registry)
):
    return _run_control(reg.plex_metadata_scan_job.retry_failures, run_uuid)


@router.get("/media-inventory")
def plex_media_inventory(
    q: Optional[str] = None,
    library: Optional[str] = None,
    resolution: Optional[str] = None,
    hdr: Optional[str] = None,
    hdr10plus_state: Optional[str] = None,
    dv_layer: Optional[str] = None,
    dv_profile: Optional[str] = None,
    scan_state: Optional[str] = None,
    discrepancy: Optional[str] = None,
    page: int = 1,
    page_size: int = 100,
    sort: str = "title",
    reg: ServiceRegistry = Depends(get_registry),
):
    """Search the read-only local-file metadata inventory.

    This route intentionally exposes only projected technical facts and never
    raw detector stderr or media-file contents.
    """
    if not reg.db:
        raise HTTPException(status_code=503, detail="Database service not initialized")
    return reg.db.search_media_inventory(
        q=q, library=library, resolution=resolution, hdr=hdr,
        hdr10plus_state=hdr10plus_state, dv_layer=dv_layer, dv_profile=dv_profile,
        scan_state=scan_state, discrepancy=discrepancy,
        page=page, page_size=page_size, sort=sort,
    )


@router.get("/media-inventory/facets")
def plex_media_inventory_facets(reg: ServiceRegistry = Depends(get_registry)):
    if not reg.db:
        raise HTTPException(status_code=503, detail="Database service not initialized")
    return reg.db.media_inventory_facets()


def _csv_cell(value) -> str:
    text = "" if value is None else str(value)
    if text.lstrip().startswith(("=", "+", "-", "@")):
        return "'" + text
    return text


@router.get("/media-inventory/export.csv")
def plex_media_inventory_export(
    q: Optional[str] = None,
    library: Optional[str] = None,
    resolution: Optional[str] = None,
    hdr: Optional[str] = None,
    hdr10plus_state: Optional[str] = None,
    dv_layer: Optional[str] = None,
    dv_profile: Optional[str] = None,
    scan_state: Optional[str] = None,
    discrepancy: Optional[str] = None,
    sort: str = "title",
    reg: ServiceRegistry = Depends(get_registry),
):
    """Export the filtered authenticated inventory with spreadsheet-safe cells."""
    if not reg.db:
        raise HTTPException(status_code=503, detail="Database service not initialized")
    filters = dict(
        q=q, library=library, resolution=resolution, hdr=hdr,
        hdr10plus_state=hdr10plus_state, dv_layer=dv_layer, dv_profile=dv_profile,
        scan_state=scan_state, discrepancy=discrepancy, sort=sort,
    )
    first = reg.db.search_media_inventory(**filters, page=1, page_size=500)
    rows = list(first["items"])
    pages = (first["total"] + 499) // 500
    for page in range(2, pages + 1):
        rows.extend(reg.db.search_media_inventory(**filters, page=page, page_size=500)["items"])

    columns = [
        "title", "year", "library_name", "resolution", "hdr", "hdr10plus_state",
        "dv_layer", "dv_profile", "seed_layer", "scan_layer", "discrepancy",
        "scan_state", "last_scanned_at", "rating_key", "path",
    ]
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\r\n")
    writer.writerow(columns)
    for row in rows:
        writer.writerow([_csv_cell(row.get(column)) for column in columns])
    return Response(
        content=output.getvalue(), media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=media-inventory.csv"},
    )


@router.get("/metadata-scans/{run_uuid}/discrepancies")
def plex_metadata_scan_discrepancies(
    run_uuid: str, reg: ServiceRegistry = Depends(get_registry)
):
    if not reg.db or not reg.db.get_metadata_scan_run(run_uuid):
        raise HTTPException(status_code=404, detail="metadata scan run not found")
    return {"items": reg.db.list_metadata_discrepancies(run_uuid)}


@router.get("/unmapped-paths")
def unmapped_plex_paths(reg: ServiceRegistry = Depends(get_registry)):
    """Distinct plex_cache path prefixes with no configured
    plex_library_path_mappings entry -- surfaces a gap before it silently
    means those files never get probed."""
    from backend.rename.path_translation import find_unmapped_plex_path_prefixes
    movies = reg.db.list_plex_cache_movies() if reg.db else []
    mappings = reg.config.get("plex_library_path_mappings") if reg.config else None
    return {"prefixes": find_unmapped_plex_path_prefixes(movies, mappings)}
