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
    reg: ServiceRegistry = Depends(get_registry),
):
    raw_items = get_last_scan_items()
    items = [_media_item_to_dict(i) for i in raw_items]

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

    # Compute stats from all items (unfiltered)
    all_items = [_media_item_to_dict(i) for i in raw_items]
    stats = _compute_status_counts(all_items)

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
