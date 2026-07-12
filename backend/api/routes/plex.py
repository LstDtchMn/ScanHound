"""Plex integration endpoints."""
from __future__ import annotations

import logging
from typing import List, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel

from backend.api.dependencies import ServiceRegistry, get_registry
from backend.api.ws import ws_manager

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
    dicts, movies only, skipping rows with no known file_path."""
    movies = reg.db.list_plex_cache_movies() if reg.db else []
    if scope == "selected":
        wanted = set(ids or [])
        movies = [m for m in movies if m.get("key") in wanted]
    targets = []
    for m in movies:
        path = m.get("file_path")
        if not path:
            continue
        targets.append({
            "path": path,
            "title": m.get("title"),
            "rating_key": m.get("rating_key"),
            "imdb_id": m.get("imdb_id"),
        })
    return targets


class ScanMetadataRequest(BaseModel):
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
