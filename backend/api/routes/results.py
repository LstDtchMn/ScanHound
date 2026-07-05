"""Results endpoints: list, filter, select, export."""
from __future__ import annotations

import json
import logging
import re
import threading
from collections import OrderedDict
from datetime import datetime, timedelta
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


def _resolution_keys(item: Dict[str, Any]) -> set:
    """Filter keys an item satisfies for the resolution/type facet. A TV show
    keys ONLY as 'TV' (never by resolution) so the 4K/1080p filters are
    movies-only; a movie keys by its resolution ('4K'/'1080p'/'720p'). The
    frontend twin is resolutionKeysFor() in stores/results.ts — keep in sync.
    """
    if _effective_category(item) == "tv":
        return {"TV"}
    res = item.get("resolution")
    return {res} if res else set()


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


_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _parse_bound_date(s: Optional[str]) -> Optional[datetime]:
    """Parse a YYYY-MM-DD query param into a datetime, or None if unset.

    Raises ValueError if the string is set but doesn't match the expected
    format, so callers can turn that into a 422.
    """
    if not s:
        return None
    if not _DATE_RE.match(s):
        raise ValueError(f"Invalid date format (expected YYYY-MM-DD): {s!r}")
    return datetime.strptime(s, "%Y-%m-%d")


_SORT_KEYS = {
    "title": lambda i: str(i.get("title", "")).casefold(),
    "year": lambda i: float(i.get("year") or 0),
    "rating": lambda i: float(i.get("rating") or 0),
    "size": lambda i: _parse_size_to_bytes(i.get("size", "") or ""),
    "posted_date": lambda i: _parse_posted_date(i.get("posted_date", "") or ""),
}


def _filter_and_sort(items, *, filter=None, search=None, category=None,
                     genre=None, language=None, quick=None, resolution=None,
                     posted_after=None, posted_before=None,
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
        posted_after: "YYYY-MM-DD" string; inclusive lower bound on posted_date
            (start of that day). Items with a missing/unparseable posted_date
            are excluded whenever either bound is set.
        posted_before: "YYYY-MM-DD" string; inclusive upper bound on
            posted_date (through the END of that day, i.e. < next-day
            midnight). Same missing-date exclusion as posted_after.
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

    if resolution:
        rset = set(resolution)
        result = [i for i in result if _resolution_keys(i) & rset]

    if quick:
        q = set(quick)
        if "4k" in q:
            result = [i for i in result if i.get("resolution") == "4K"]
        if "hdrdv" in q:
            result = [i for i in result
                      if i.get("dovi") or (i.get("hdr") and i.get("hdr") != "SDR")]
        if "inplex" in q:
            result = [i for i in result if _has_plex_copy(i)]

    if posted_after or posted_before:
        after_ts = _parse_bound_date(posted_after).timestamp() if posted_after else None
        # Inclusive of the whole end day: bound is midnight at the START of
        # the next day, and we require posted_ts < that.
        before_ts = None
        if posted_before:
            before_dt = _parse_bound_date(posted_before)
            before_ts = (before_dt + timedelta(days=1)).timestamp()

        def _in_range(i):
            ts = _parse_posted_date(i.get("posted_date", "") or "")
            if ts == 0.0:
                return False  # missing/unparseable posted_date can't be placed in a range
            if after_ts is not None and ts < after_ts:
                return False
            if before_ts is not None and ts >= before_ts:
                return False
            return True

        result = [i for i in result if _in_range(i)]

    keyfn = _SORT_KEYS.get(sort)
    if keyfn:
        result = sorted(result, key=keyfn, reverse=(order == "desc"))

    return result

# Selection state
#
# B3: ScanHound is a single-user, single-instance app (one browser session
# talking to one backend process) — there is no per-session/per-user
# partitioning anywhere in the API, and this module-global set is shared by
# every request. That's an accepted invariant, not a bug, PROVIDED it stays
# bounded: without a cap, a client that repeatedly calls POST /select with
# fresh group_keys (e.g. a buggy UI loop, or scanning a very large site
# across many sessions without ever deselecting) grows this set forever.
# _MAX_SELECTED caps it with FIFO eviction (oldest selections dropped first)
# — generous enough that no real user selection (even "select all" against
# the full 2000-row background cache) gets truncated, while still bounding
# worst-case memory.
_MAX_SELECTED = 5000
_selected: "OrderedDict[str, None]" = OrderedDict()  # insertion-ordered set
_selected_lock = threading.Lock()


def _selected_add(keys) -> None:
    """Add group_keys to _selected, evicting the oldest entries first if the
    result would exceed _MAX_SELECTED. Must be called while holding
    _selected_lock."""
    for k in keys:
        if k in _selected:
            _selected.move_to_end(k)
        else:
            _selected[k] = None
    while len(_selected) > _MAX_SELECTED:
        _selected.popitem(last=False)


def _selected_discard(keys) -> None:
    """Remove group_keys from _selected. Must be called while holding
    _selected_lock."""
    for k in keys:
        _selected.pop(k, None)


class SelectRequest(BaseModel):
    group_keys: List[str]
    selected: bool = True


class SelectAllRequest(BaseModel):
    source: str = "live"
    filter: Optional[str] = None
    search: Optional[str] = None
    category: Optional[str] = None
    genre: Optional[str] = None
    language: Optional[str] = None
    quick: Optional[str] = None
    resolution: Optional[str] = None
    posted_after: Optional[str] = None
    posted_before: Optional[str] = None


class DismissRequest(BaseModel):
    urls: List[str]
    # Optional url -> title map, stored for display in the "dismissed" manager.
    titles: Optional[Dict[str, str]] = None
    # True = dismiss (skip), False = un-dismiss (restore).
    dismissed: bool = True


def _csv(param: Optional[str]) -> Optional[List[str]]:
    """Split a comma-separated query param into a list, or None if empty."""
    if not param:
        return None
    vals = [p.strip() for p in param.split(",") if p.strip()]
    return vals or None


def _validate_date_param(name: str, value: Optional[str]) -> Optional[str]:
    """Validate a posted_after/posted_before query param is YYYY-MM-DD.

    Returns the value unchanged if valid/unset; raises HTTPException(422)
    with an explicit message if set but malformed.
    """
    if not value:
        return None
    if not _DATE_RE.match(value):
        raise HTTPException(
            status_code=422,
            detail=f"Invalid {name} format (expected YYYY-MM-DD): {value!r}",
        )
    try:
        # Regex alone admits calendar-invalid dates like 2026-02-31.
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        raise HTTPException(
            status_code=422,
            detail=f"Invalid {name} date (not a real calendar date): {value!r}",
        )
    return value


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
    category: Optional[List[str]] = None,
    genre: Optional[List[str]] = None,
    language: Optional[List[str]] = None,
    quick: Optional[List[str]] = None,
    resolution: Optional[List[str]] = None,
    posted_after: Optional[str] = None,
    posted_before: Optional[str] = None,
    extra: Optional[Dict[str, Any]] = None,
    include_facets: bool = False,
) -> Dict[str, Any]:
    """Filter / search / sort / paginate item dicts into a results response.

    Shared by the live results and the pre-cached results so both behave
    identically (dismissal hiding, stats, selection annotation).

    include_facets: when True, adds ``available_genres``/``available_languages``
        (B2) computed over the same whole-set basis as ``stats`` — i.e. all
        visible (non-dismissed) items, before status/search/category/genre/
        language/quick filtering and before pagination — so the facet lists
        never shrink to just the current page or the current filter selection.
    """
    # Overlay 'downloaded' from the central downloads table at READ time, so a
    # grab is remembered across reloads and shared between the app and web —
    # independent of when the background scan last folded download history in.
    # An exact-URL match wins over whatever the cached/scanned status was
    # (mirrors the scanner's own url-in-history -> DOWNLOADED rule). Non-mutating
    # (copies the touched dicts) so the underlying cache list isn't rewritten.
    if reg.db is not None:
        try:
            downloaded = reg.db.get_downloaded_urls()
        except Exception:
            downloaded = set()
        if downloaded:
            items = [
                {**i, "status": "downloaded"}
                if i.get("url") in downloaded and i.get("status") != "downloaded"
                else i
                for i in items
            ]

    # Hide items the user swiped away on the deck (unless explicitly requested).
    if not include_dismissed and reg.db is not None:
        dismissed = reg.db.get_dismissed_urls()
        if dismissed:
            items = [i for i in items if i.get("url") not in dismissed]

    # Snapshot of all visible (non-dismissed) items for the overall stats,
    # before status/search/category/genre/language/quick filtering narrows
    # them further.
    visible_items = list(items)

    items = _filter_and_sort(
        items, filter=filter, search=search, category=category, genre=genre,
        language=language, quick=quick, resolution=resolution,
        posted_after=posted_after,
        posted_before=posted_before, sort=sort, order=order,
    )

    # Per-title counts over the filtered set (post-filter, pre-pagination).
    title_counts: Dict[str, int] = {}
    for i in items:
        t = str(i.get("title", ""))
        title_counts[t] = title_counts.get(t, 0) + 1

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
        # Stats from all visible items (after dismissal, before any filters).
        "stats": _compute_status_counts(visible_items),
        # Filtered stats (after all filters, before pagination).
        "filtered_stats": _compute_status_counts(items),
        "title_counts": title_counts,
    }
    if include_facets:
        response.update(_compute_facets(visible_items))
    if extra:
        response.update(extra)
    return response


# Parse-cache for the "cache" source (B2): get_background_cache() can hold up
# to 2000 rows, and every row's `data` column is a JSON blob that previously
# got re-parsed with json.loads() on every single request/page. Since the
# blobs only change when the background scanner upserts a row (which always
# bumps last_seen_at — see DatabaseManager.upsert_background_cache), we can
# cheaply detect "nothing changed since last time" via
# get_background_cache_version() (a single COUNT+MAX query) and skip the
# full re-parse when the version token is unchanged.
_cache_parse_lock = threading.Lock()
_cache_parse_cache: Dict[str, Any] = {"version": None, "items": [], "last_updated": None}


def _load_cached_items(reg: ServiceRegistry):
    """Return (item_dicts, last_updated) for the pre-cached background-scan
    rows, reusing the previous request's parsed items when the underlying
    background_scan_cache table hasn't changed (see module docstring above
    _cache_parse_cache)."""
    if reg.db is None:
        return [], None

    version = reg.db.get_background_cache_version()
    with _cache_parse_lock:
        if _cache_parse_cache["version"] == version:
            # Return copies so callers (which mutate items in-place, e.g. to
            # annotate "selected") never corrupt the cached snapshot.
            return (
                [dict(i) for i in _cache_parse_cache["items"]],
                _cache_parse_cache["last_updated"],
            )

    items: List[Dict[str, Any]] = []
    last_updated: Optional[str] = None
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

    with _cache_parse_lock:
        _cache_parse_cache["version"] = version
        _cache_parse_cache["items"] = items
        _cache_parse_cache["last_updated"] = last_updated

    return [dict(i) for i in items], last_updated


def _load_items(source: str, reg: ServiceRegistry):
    """Return (item_dicts, last_updated) for the live last-scan set or the
    pre-cached background-scan rows."""
    if source == "cache":
        return _load_cached_items(reg)
    return [_media_item_to_dict(i) for i in get_last_scan_items()], None


def _compute_facets(items: List[Dict[str, Any]]) -> Dict[str, List[str]]:
    """Compute server-side facets (available_genres, available_languages)
    over the given item set. Callers pass the same "whole appropriate set"
    basis used for the ``stats`` block (post-dismissal, pre status/search/
    category/genre/language/quick filtering) so facets don't shrink to just
    the current filter selection or the current page.
    """
    genres: set = set()
    languages: set = set()
    for i in items:
        for g in (i.get("genres") or []):
            if g:
                genres.add(g)
        lang = i.get("language")
        if lang:
            languages.add(lang)
    return {
        "available_genres": sorted(genres),
        "available_languages": sorted(languages),
    }


@router.get("")
def get_results(
    filter: Optional[str] = Query(None, description="Status filter: missing, upgrade, library"),
    search: Optional[str] = Query(None),
    sort: str = Query("title"),
    order: str = Query("asc"),
    page: int = Query(1, ge=1),
    per_page: int = Query(100, ge=1, le=500),
    category: Optional[str] = Query(None),
    genre: Optional[str] = Query(None),
    language: Optional[str] = Query(None),
    quick: Optional[str] = Query(None),
    resolution: Optional[str] = Query(None, description="Resolution/type facet CSV: 4K,1080p,TV (OR)"),
    posted_after: Optional[str] = Query(None, description="Inclusive lower bound, YYYY-MM-DD"),
    posted_before: Optional[str] = Query(None, description="Inclusive upper bound, YYYY-MM-DD"),
    include_dismissed: bool = Query(False, description="Include swiped-away items"),
    reg: ServiceRegistry = Depends(get_registry),
):
    posted_after = _validate_date_param("posted_after", posted_after)
    posted_before = _validate_date_param("posted_before", posted_before)
    items, _ = _load_items("live", reg)
    return _shape_results(
        items, filter=filter, search=search, sort=sort, order=order,
        page=page, per_page=per_page, include_dismissed=include_dismissed, reg=reg,
        category=_csv(category), genre=_csv(genre), language=_csv(language),
        quick=_csv(quick), resolution=_csv(resolution), posted_after=posted_after, posted_before=posted_before,
    )


@router.get("/cached")
def get_cached_results(
    filter: Optional[str] = Query(None, description="Status filter: missing, upgrade, library"),
    search: Optional[str] = Query(None),
    sort: str = Query("title"),
    order: str = Query("asc"),
    page: int = Query(1, ge=1),
    per_page: int = Query(100, ge=1, le=500),
    category: Optional[str] = Query(None),
    genre: Optional[str] = Query(None),
    language: Optional[str] = Query(None),
    quick: Optional[str] = Query(None),
    resolution: Optional[str] = Query(None, description="Resolution/type facet CSV: 4K,1080p,TV (OR)"),
    posted_after: Optional[str] = Query(None, description="Inclusive lower bound, YYYY-MM-DD"),
    posted_before: Optional[str] = Query(None, description="Inclusive upper bound, YYYY-MM-DD"),
    include_dismissed: bool = Query(False, description="Include swiped-away items"),
    reg: ServiceRegistry = Depends(get_registry),
):
    """Pre-cached background-scan results (same shape as GET /results).

    Lets the frontend show something immediately on open, before/without a live
    scan. ``source`` is ``"cache"`` and ``last_updated`` is the most recent
    ``last_seen_at`` so the UI can show a "cached as of …" banner.
    """
    posted_after = _validate_date_param("posted_after", posted_after)
    posted_before = _validate_date_param("posted_before", posted_before)
    items, last_updated = _load_items("cache", reg)
    return _shape_results(
        items, filter=filter, search=search, sort=sort, order=order,
        page=page, per_page=per_page, include_dismissed=include_dismissed, reg=reg,
        category=_csv(category), genre=_csv(genre), language=_csv(language),
        quick=_csv(quick), resolution=_csv(resolution), posted_after=posted_after, posted_before=posted_before,
        extra={"source": "cache", "last_updated": last_updated},
        include_facets=True,
    )


@router.post("/select")
def select_items(req: SelectRequest):
    with _selected_lock:
        if req.selected:
            _selected_add(req.group_keys)
        else:
            _selected_discard(req.group_keys)
        return {"status": "ok", "selected_count": len(_selected)}


@router.post("/select-all")
def select_all(req: Optional[SelectAllRequest] = None,
               reg: ServiceRegistry = Depends(get_registry)):
    if req is None:
        raw_items = get_last_scan_items()
        with _selected_lock:
            keys = []
            for item in raw_items:
                gk = getattr(item, "group_key", None) or (
                    item.get("group_key") if isinstance(item, dict) else None)
                if gk:
                    keys.append(gk)
            _selected_add(keys)
            return {"status": "ok", "selected_count": len(_selected),
                    "group_keys": sorted(_selected)}
    posted_after = _validate_date_param("posted_after", req.posted_after)
    posted_before = _validate_date_param("posted_before", req.posted_before)
    items, _ = _load_items("cache" if req.source == "cache" else "live", reg)
    matched = _filter_and_sort(
        items, filter=req.filter, search=req.search, category=_csv(req.category),
        genre=_csv(req.genre), language=_csv(req.language), quick=_csv(req.quick),
        resolution=_csv(req.resolution),
        posted_after=posted_after, posted_before=posted_before,
    )
    keys = [str(i.get("group_key")) for i in matched if i.get("group_key")]
    with _selected_lock:
        _selected.clear()
        _selected_add(keys)
        return {"status": "ok", "selected_count": len(_selected), "group_keys": keys}


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
