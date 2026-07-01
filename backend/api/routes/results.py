"""Results endpoints: list, filter, select, export."""
from __future__ import annotations

import json
import logging
import re
import threading
from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from backend.api.dependencies import ServiceRegistry, get_registry
from backend.api.routes.scanner import get_last_scan_items, _media_item_to_dict

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/results", tags=["results"])

# Filter and sort helpers (Task 1: server-side filtering/sorting for pagination)
_KNOWN_CATEGORIES = {"4k", "remux", "tv"}


def _effective_category(item: Dict[str, Any]) -> str:
    """Determine the effective category for an item.

    If category is set, use it. Otherwise, use 'tv' if season is not None,
    else '4k'.
    """
    cat = item.get("category")
    if cat:
        return cat
    return "tv" if item.get("season") is not None else "4k"


def _has_plex_copy(item: Dict[str, Any]) -> bool:
    """Check if item has at least one Plex version.

    Safely parses plex_versions JSON array; fails gracefully to False.
    """
    try:
        return len(json.loads(item.get("plex_versions") or "[]")) > 0
    except (ValueError, TypeError):
        return False


def _parse_size_to_bytes(size: str) -> float:
    """Parse a human-readable size string to bytes.

    Examples: "4.5 GB" -> 4.5e9, "512 MB" -> 5.12e8
    Returns 0.0 if unparseable.
    """
    if not size:
        return 0.0
    m = re.search(r"([\d.]+)\s*(TB|GB|MB|KB|B)", size, re.IGNORECASE)
    if not m:
        return 0.0
    mult = {"B": 1, "KB": 1024, "MB": 1024 ** 2, "GB": 1024 ** 3, "TB": 1024 ** 4}
    try:
        return float(m.group(1)) * mult.get(m.group(2).upper(), 0)
    except ValueError:
        return 0.0


def _parse_posted_date(s: str) -> float:
    """Parse posted_date string to Unix timestamp.

    Expected format: "June 8, 2026 at 12:56 AM"
    Returns 0.0 if unparseable.
    """
    if not s:
        return 0.0
    txt = s.replace(" at ", " ").strip()
    for fmt in ("%B %d, %Y %I:%M %p", "%B %d, %Y"):
        try:
            return datetime.strptime(txt, fmt).timestamp()
        except ValueError:
            continue
    return 0.0


_SORT_KEYS = {
    "title": lambda i: str(i.get("title", "")).casefold(),
    "year": lambda i: float(i.get("year") or 0),
    "rating": lambda i: float(i.get("rating") or 0),
    "size": lambda i: _parse_size_to_bytes(i.get("size", "") or ""),
    "posted_date": lambda i: _parse_posted_date(i.get("posted_date", "") or ""),
}


def _filter_and_sort(items, *, filter=None, search=None, category=None,
                     genre=None, language=None, quick=None,
                     sort="title", order="asc"):
    """Filter and sort items server-side.

    Args:
        items: list of item dicts
        filter: status filter (e.g., "missing", "upgrade", "library")
        search: search in title
        category: list of enabled categories; shows unknowns + enabled
        genre: list of genres; item must have at least one
        language: list of languages; item must match
        quick: list of quick filters ('4k', 'hdrdv', 'inplex')
        sort: sort key (default "title")
        order: "asc" or "desc" (default "asc")

    Returns:
        Filtered, sorted list of items.
    """
    result = list(items)

    if filter:
        fl = filter.lower()
        result = [i for i in result if fl in str(i.get("status", "")).lower()]

    if search:
        sl = search.lower()
        result = [i for i in result if sl in str(i.get("title", "")).lower()]

    if category:
        enabled = set(category)
        result = [i for i in result
                  if _effective_category(i) not in _KNOWN_CATEGORIES
                  or _effective_category(i) in enabled]

    if genre:
        gset = set(genre)
        result = [i for i in result if any(g in gset for g in (i.get("genres") or []))]

    if language:
        lset = set(language)
        result = [i for i in result if i.get("language") in lset]

    if quick:
        q = set(quick)
        if "4k" in q:
            result = [i for i in result if i.get("resolution") == "4K"]
        if "hdrdv" in q:
            result = [i for i in result
                      if i.get("dovi") or (i.get("hdr") and i.get("hdr") != "SDR")]
        if "inplex" in q:
            result = [i for i in result if _has_plex_copy(i)]

    keyfn = _SORT_KEYS.get(sort)
    if keyfn:
        result = sorted(result, key=keyfn, reverse=(order == "desc"))

    return result

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


def _shape_results(
    items: List[Dict[str, Any]],
    *,
    filter: Optional[str],
    search: Optional[str],
    sort: str,
    order: str,
    page: int,
    per_page: int,
    include_dismissed: bool,
    reg: ServiceRegistry,
    extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Filter / search / sort / paginate item dicts into a results response.

    Shared by the live results and the pre-cached results so both behave
    identically (dismissal hiding, stats, selection annotation).
    """
    # Hide items the user swiped away on the deck (unless explicitly requested).
    if not include_dismissed and reg.db is not None:
        dismissed = reg.db.get_dismissed_urls()
        if dismissed:
            items = [i for i in items if i.get("url") not in dismissed]

    # Snapshot of all visible (non-dismissed) items for the overall stats,
    # before status/search filtering narrows them further.
    visible_items = list(items)

    if filter:
        filter_lower = filter.lower()
        items = [i for i in items if filter_lower in str(i.get("status", "")).lower()]

    if search:
        search_lower = search.lower()
        items = [i for i in items if search_lower in str(i.get("title", "")).lower()]

    items.sort(key=lambda x: str(x.get(sort, "")), reverse=(order == "desc"))

    total = len(items)
    start = (page - 1) * per_page
    page_items = items[start:start + per_page]

    # Annotate selection state
    with _selected_lock:
        selected_snapshot = set(_selected)
    for item in page_items:
        item["selected"] = item.get("group_key", "") in selected_snapshot

    response = {
        "items": page_items,
        "total": total,
        "page": page,
        "per_page": per_page,
        # Stats from all visible items (after dismissal, before filter/search).
        "stats": _compute_status_counts(visible_items),
        # Filtered stats (after filter/search, before pagination).
        "filtered_stats": _compute_status_counts(items),
    }
    if extra:
        response.update(extra)
    return response


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
    items = [_media_item_to_dict(i) for i in get_last_scan_items()]
    return _shape_results(
        items, filter=filter, search=search, sort=sort, order=order,
        page=page, per_page=per_page, include_dismissed=include_dismissed, reg=reg,
    )


@router.get("/cached")
def get_cached_results(
    filter: Optional[str] = Query(None, description="Status filter: missing, upgrade, library"),
    search: Optional[str] = Query(None),
    sort: str = Query("title"),
    order: str = Query("asc"),
    page: int = Query(1, ge=1),
    per_page: int = Query(100, ge=1, le=500),
    include_dismissed: bool = Query(False, description="Include swiped-away items"),
    reg: ServiceRegistry = Depends(get_registry),
):
    """Pre-cached background-scan results (same shape as GET /results).

    Lets the frontend show something immediately on open, before/without a live
    scan. ``source`` is ``"cache"`` and ``last_updated`` is the most recent
    ``last_seen_at`` so the UI can show a "cached as of …" banner.
    """
    items: List[Dict[str, Any]] = []
    last_updated: Optional[str] = None
    if reg.db is not None:
        for row in reg.db.get_background_cache():
            try:
                data = json.loads(row.get("data") or "{}")
            except (ValueError, TypeError):
                data = {}
            if not data.get("url"):
                data["url"] = row.get("url")
            items.append(data)
            seen = row.get("last_seen_at")
            if seen and (last_updated is None or seen > last_updated):
                last_updated = seen
    return _shape_results(
        items, filter=filter, search=search, sort=sort, order=order,
        page=page, per_page=per_page, include_dismissed=include_dismissed, reg=reg,
        extra={"source": "cache", "last_updated": last_updated},
    )


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
