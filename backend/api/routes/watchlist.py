"""Watchlist endpoints: CRUD, import/export, stats."""
import logging
from typing import Literal, Optional

import requests
from fastapi import APIRouter, Body, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from backend.api.dependencies import ServiceRegistry, get_registry

logger = logging.getLogger(__name__)
from backend.watchlist import (
    WatchlistItem,
    WatchlistItemStatus,
    WatchlistItemType,
    WatchlistManager,
)

router = APIRouter(prefix="/watchlist", tags=["watchlist"])

VALID_TYPES = Literal["movie", "tv_show", "tv_season"]
VALID_STATUSES = Literal["wanted", "found", "downloaded", "in_library"]


class WatchlistAddRequest(BaseModel):
    title: str
    year: Optional[int] = None
    imdb_id: Optional[str] = None
    tmdb_id: Optional[str] = None
    item_type: VALID_TYPES = "movie"
    season: Optional[int] = Field(None, ge=1)
    min_resolution: Optional[str] = None
    prefer_dovi: bool = False
    notes: str = ""
    priority: int = Field(1, ge=1, le=3)


class WatchlistUpdateRequest(BaseModel):
    title: Optional[str] = None
    year: Optional[int] = None
    imdb_id: Optional[str] = None
    tmdb_id: Optional[str] = None
    item_type: Optional[VALID_TYPES] = None
    status: Optional[VALID_STATUSES] = None
    season: Optional[int] = Field(None, ge=1)
    min_resolution: Optional[str] = None
    prefer_dovi: Optional[bool] = None
    notes: Optional[str] = None
    priority: Optional[int] = Field(None, ge=1, le=3)


def _get_manager(reg: ServiceRegistry) -> WatchlistManager:
    mgr = reg.watchlist
    if mgr is None:
        raise HTTPException(status_code=503, detail="Watchlist service unavailable")
    return mgr


def _parse_status(value: Optional[str]) -> Optional[WatchlistItemStatus]:
    if not value:
        return None
    try:
        return WatchlistItemStatus(value)
    except ValueError:
        raise HTTPException(status_code=422, detail=f"Invalid status: {value}")


def _parse_type(value: Optional[str]) -> Optional[WatchlistItemType]:
    if not value:
        return None
    try:
        return WatchlistItemType(value)
    except ValueError:
        raise HTTPException(status_code=422, detail=f"Invalid item_type: {value}")


@router.get("")
def list_items(
    status: Optional[str] = Query(None),
    item_type: Optional[str] = Query(None),
    reg: ServiceRegistry = Depends(get_registry),
):
    """List all watchlist items, optionally filtered."""
    mgr = _get_manager(reg)
    items = mgr.get_all(status=_parse_status(status), item_type=_parse_type(item_type))
    return [item.to_dict() for item in items]


@router.get("/stats")
def watchlist_stats(reg: ServiceRegistry = Depends(get_registry)):
    """Watchlist statistics."""
    return _get_manager(reg).get_stats()


@router.get("/search")
def search_items(
    q: str = Query(..., min_length=1),
    reg: ServiceRegistry = Depends(get_registry),
):
    """Search watchlist by title."""
    items = _get_manager(reg).search(q)
    return [item.to_dict() for item in items]


@router.get("/{item_id}")
def get_item(item_id: int, reg: ServiceRegistry = Depends(get_registry)):
    """Get a single watchlist item."""
    item = _get_manager(reg).get(item_id)
    if not item:
        raise HTTPException(status_code=404, detail="Watchlist item not found")
    return item.to_dict()


@router.post("")
def add_item(
    req: WatchlistAddRequest,
    reg: ServiceRegistry = Depends(get_registry),
):
    """Add item to watchlist."""
    if req.item_type == "tv_season" and req.season is None:
        raise HTTPException(status_code=422, detail="season is required for tv_season items")

    item = WatchlistItem(
        title=req.title,
        year=req.year,
        imdb_id=req.imdb_id,
        tmdb_id=req.tmdb_id,
        item_type=WatchlistItemType(req.item_type),
        season=req.season,
        min_resolution=req.min_resolution,
        prefer_dovi=req.prefer_dovi,
        notes=req.notes,
        priority=req.priority,
    )
    item_id = _get_manager(reg).add(item)
    return {"id": item_id, "status": "added"}


@router.put("/{item_id}")
def update_item(
    item_id: int,
    req: WatchlistUpdateRequest,
    reg: ServiceRegistry = Depends(get_registry),
):
    """Update a watchlist item."""
    mgr = _get_manager(reg)
    item = mgr.get(item_id)
    if not item:
        raise HTTPException(status_code=404, detail="Watchlist item not found")

    updates = req.model_dump(exclude_unset=True)
    if "item_type" in updates:
        updates["item_type"] = WatchlistItemType(updates["item_type"])
    if "status" in updates:
        updates["status"] = WatchlistItemStatus(updates["status"])

    # Cross-field validation: tv_season requires season
    final_type = updates.get("item_type", item.item_type)
    final_season = updates.get("season", item.season)
    if final_type == WatchlistItemType.TV_SEASON and final_season is None:
        raise HTTPException(status_code=422, detail="season is required for tv_season items")

    for k, v in updates.items():
        setattr(item, k, v)

    mgr.update(item)
    return {"status": "updated"}


@router.delete("/{item_id}")
def remove_item(item_id: int, reg: ServiceRegistry = Depends(get_registry)):
    """Remove item from watchlist."""
    mgr = _get_manager(reg)
    item = mgr.get(item_id)
    if not item:
        raise HTTPException(status_code=404, detail="Watchlist item not found")
    mgr.remove(item_id)
    return {"status": "removed"}


@router.post("/import/json")
def import_json(
    data: str = Body(..., media_type="text/plain"),
    reg: ServiceRegistry = Depends(get_registry),
):
    """Import watchlist from JSON."""
    import json
    try:
        json.loads(data)
    except (json.JSONDecodeError, TypeError) as e:
        raise HTTPException(status_code=400, detail=f"Invalid JSON: {e}")
    count = _get_manager(reg).import_from_json(data)
    return {"imported": count}


@router.post("/import/imdb")
def import_imdb(
    data: str = Body(..., media_type="text/plain"),
    reg: ServiceRegistry = Depends(get_registry),
):
    """Import from IMDb CSV export."""
    if not data.strip():
        raise HTTPException(status_code=400, detail="Empty CSV data")
    count = _get_manager(reg).import_from_imdb_list(data)
    return {"imported": count}


@router.post("/import/letterboxd")
def import_letterboxd(
    data: str = Body(..., media_type="text/plain"),
    reg: ServiceRegistry = Depends(get_registry),
):
    """Import from Letterboxd CSV export."""
    if not data.strip():
        raise HTTPException(status_code=400, detail="Empty CSV data")
    count = _get_manager(reg).import_from_letterboxd(data)
    return {"imported": count}


class TraktImportRequest(BaseModel):
    username: str
    list_type: str = "watchlist"  # watchlist, collection, or a custom list slug


@router.post("/import/trakt")
def import_trakt(
    req: TraktImportRequest,
    reg: ServiceRegistry = Depends(get_registry),
):
    """Import from a Trakt user's public watchlist or list.

    Requires a Trakt API client_id in settings (trakt_client_id).
    Falls back to TMDB for metadata enrichment if Trakt returns minimal data.
    """
    client_id = reg.config.get("trakt_client_id", "")
    if not client_id:
        raise HTTPException(
            status_code=400,
            detail="Trakt client ID not configured. Add it in Settings > Sources."
        )

    username = req.username.strip()
    if not username:
        raise HTTPException(status_code=400, detail="Username is required")

    # Build Trakt API URL
    if req.list_type == "watchlist":
        url = f"https://api.trakt.tv/users/{username}/watchlist/movies"
    elif req.list_type == "collection":
        url = f"https://api.trakt.tv/users/{username}/collection/movies"
    else:
        url = f"https://api.trakt.tv/users/{username}/lists/{req.list_type}/items/movies"

    headers = {
        "Content-Type": "application/json",
        "trakt-api-version": "2",
        "trakt-api-key": client_id,
    }

    try:
        resp = requests.get(url, headers=headers, timeout=15)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Failed to reach Trakt API: {e}")

    if resp.status_code == 404:
        raise HTTPException(status_code=404, detail=f"Trakt user '{username}' or list not found")
    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail=f"Trakt API returned {resp.status_code}")

    items = resp.json()
    if not isinstance(items, list):
        raise HTTPException(status_code=502, detail="Unexpected Trakt response format")

    mgr = _get_manager(reg)
    count = 0
    for entry in items:
        movie = entry.get("movie", entry)
        title = movie.get("title", "")
        year = movie.get("year")
        imdb_id = (movie.get("ids") or {}).get("imdb")
        tmdb_id = (movie.get("ids") or {}).get("tmdb")

        if not title:
            continue

        try:
            item = WatchlistItem(
                title=title,
                year=year,
                imdb_id=imdb_id,
                tmdb_id=str(tmdb_id) if tmdb_id else None,
                item_type=WatchlistItemType.MOVIE,
                status=WatchlistItemStatus.WANTED,
                notes=f"Imported from Trakt ({username})",
            )
            mgr.add_item(item)
            count += 1
        except Exception as e:
            logger.warning("Failed to import Trakt item '%s': %s", title, e)

    return {"imported": count, "total_in_list": len(items)}


@router.get("/export/json")
def export_json(reg: ServiceRegistry = Depends(get_registry)):
    """Export watchlist as JSON."""
    import json
    from fastapi.responses import JSONResponse
    data = json.loads(_get_manager(reg).export_to_json())
    return JSONResponse(content=data)


@router.delete("")
def clear_watchlist(
    status: Optional[str] = Query(None),
    reg: ServiceRegistry = Depends(get_registry),
):
    """Clear watchlist, optionally only items with specific status."""
    _get_manager(reg).clear(_parse_status(status))
    return {"status": "cleared"}
