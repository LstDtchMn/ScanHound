"""Results endpoints: list, filter, select, export."""
from __future__ import annotations

import logging
import threading
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from backend.api.dependencies import ServiceRegistry, get_registry
from backend.api.routes.scanner import get_last_scan_items, _media_item_to_dict

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/results", tags=["results"])

# Selection state
_selected: set = set()
_selected_lock = threading.Lock()


class SelectRequest(BaseModel):
    group_keys: List[str]
    selected: bool = True


class DismissRequest(BaseModel):
    urls: List[str]
    # Optional url -> title map, stored for display in the "dismissed" manager.
    titles: Optional[Dict[str, str]] = None
    # True = dismiss (skip), False = un-dismiss (restore).
    dismissed: bool = True


def _compute_status_counts(items: List[Dict[str, Any]]) -> Dict[str, int]:
    """Compute status counts from a list of item dicts."""
    return {
        "total": len(items),
        "missing": sum(1 for i in items if "missing" in str(i.get("status", "")).lower()),
        "upgrade": sum(1 for i in items if "upgrade" in str(i.get("status", "")).lower()),
        "library": sum(1 for i in items if "library" in str(i.get("status", "")).lower()),
    }


@router.get("")
def get_results(
    filter: Optional[str] = Query(None, description="Status filter: missing, upgrade, library"),
    search: Optional[str] = Query(None),
    sort: str = Query("title"),
    order: str = Query("asc"),
    page: int = Query(1, ge=1),
    per_page: int = Query(100, ge=1, le=500),
    include_dismissed: bool = Query(False, description="Include swiped-away items"),
    reg: ServiceRegistry = Depends(get_registry),
):
    raw_items = get_last_scan_items()
    items = [_media_item_to_dict(i) for i in raw_items]

    # Hide items the user swiped away on the deck (unless explicitly requested).
    if not include_dismissed and reg.db is not None:
        dismissed = reg.db.get_dismissed_urls()
        if dismissed:
            items = [i for i in items if i.get("url") not in dismissed]

    # Snapshot of all visible (non-dismissed) items for the overall stats,
    # before status/search filtering narrows them further.
    visible_items = list(items)

    # Filter by status
    if filter:
        filter_lower = filter.lower()
        items = [
            i for i in items
            if filter_lower in str(i.get("status", "")).lower()
        ]

    # Search by title
    if search:
        search_lower = search.lower()
        items = [i for i in items if search_lower in str(i.get("title", "")).lower()]

    # Sort
    reverse = order == "desc"
    items.sort(key=lambda x: str(x.get(sort, "")), reverse=reverse)

    total = len(items)
    start = (page - 1) * per_page
    end = start + per_page
    page_items = items[start:end]

    # Annotate selection state
    with _selected_lock:
        selected_snapshot = set(_selected)
    for item in page_items:
        item["selected"] = item.get("group_key", "") in selected_snapshot

    # Compute stats from all visible items (after dismissal, before filter/search)
    stats = _compute_status_counts(visible_items)

    # Compute filtered stats (from items after filter/search, before pagination)
    filtered_stats = _compute_status_counts(items)

    return {
        "items": page_items,
        "total": total,
        "page": page,
        "per_page": per_page,
        "stats": stats,
        "filtered_stats": filtered_stats,
    }


@router.post("/select")
def select_items(req: SelectRequest):
    with _selected_lock:
        if req.selected:
            _selected.update(req.group_keys)
        else:
            _selected.difference_update(req.group_keys)
        return {"status": "ok", "selected_count": len(_selected)}


@router.post("/select-all")
def select_all():
    raw_items = get_last_scan_items()
    with _selected_lock:
        for item in raw_items:
            gk = getattr(item, "group_key", None) or (item.get("group_key") if isinstance(item, dict) else None)
            if gk:
                _selected.add(gk)
        return {"status": "ok", "selected_count": len(_selected)}


@router.post("/deselect-all")
def deselect_all():
    with _selected_lock:
        _selected.clear()
    return {"status": "ok", "selected_count": 0}


@router.post("/dismiss")
def dismiss_items(req: DismissRequest, reg: ServiceRegistry = Depends(get_registry)):
    """Dismiss (or restore) swiped-away items so they stay hidden across scans."""
    db = reg.db
    if db is None:
        raise HTTPException(status_code=503, detail="Database not available")
    titles = req.titles or {}
    if req.dismissed:
        db.add_dismissed_items((url, titles.get(url)) for url in req.urls if url)
    else:
        db.remove_dismissed_items(req.urls)
    return {"status": "ok", "dismissed_count": db.get_dismissed_count()}


@router.get("/dismissed")
def list_dismissed(reg: ServiceRegistry = Depends(get_registry)):
    """List dismissed items (for a 'show skipped' / manage view)."""
    db = reg.db
    if db is None:
        raise HTTPException(status_code=503, detail="Database not available")
    items = db.get_dismissed_items()
    return {"items": items, "count": len(items)}


@router.delete("/dismissed")
def clear_dismissed(reg: ServiceRegistry = Depends(get_registry)):
    """Clear all dismissals so every item can reappear."""
    db = reg.db
    if db is None:
        raise HTTPException(status_code=503, detail="Database not available")
    db.clear_dismissed_items()
    return {"status": "ok", "dismissed_count": 0}


@router.post("/export")
def export_csv(reg: ServiceRegistry = Depends(get_registry)):
    download = reg.download
    if not download:
        raise HTTPException(status_code=503, detail="Download service not available")
    raw_items = get_last_scan_items()
    if not raw_items:
        raise HTTPException(status_code=400, detail="No results to export")
    try:
        filepath = download.export_results_csv(raw_items)
    except Exception as e:
        logger.exception("CSV export failed")
        raise HTTPException(status_code=500, detail=f"Export failed: {e}")
    return {"status": "ok", "filepath": filepath}
