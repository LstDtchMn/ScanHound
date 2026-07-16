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
from backend.app_service import normalize_title

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


_RES_RANK = {"2160p": 4, "4k": 4, "uhd": 4, "1080p": 3, "720p": 2, "480p": 1}


def _res_rank(res) -> int:
    return _RES_RANK.get((res or "").strip().lower(), 0)


def _is_better_grab(item, grab) -> bool:
    """Is `item` a genuinely better release than the best `grab` you already
    got? Higher resolution, or same resolution with a Dolby Vision gain — the
    exact rule the scanner uses (_download_status_for). Used to keep such a
    sibling actionable (still grabbable) instead of hiding it as a duplicate."""
    ir, gr = _res_rank(item.get("resolution")), grab["rank"]
    return ir > gr or (ir == gr and bool(item.get("dovi")) and not grab["dovi"])


def _overlay_download_state(items, db):
    """Overlay download state onto result items from the central downloads
    table so a grab is remembered across reloads / app + web without a re-scan,
    and its SIBLING releases (other URLs of the same title) stop reading
    'missing'. Sibling matching is by ``group_key`` (already normalized|year|
    season — year- and season-aware, the same key the client + scanner use), so
    a 2021 remake never contaminates the 1984 original. DV/HDR is honored via
    the item's own flags. Rules:
      * exact URL grabbed                         -> 'downloaded'
      * still-missing sibling, NOT better than the grab -> 'downloaded_similar'
        (you have a copy; non-actionable, leaves the swipe deck)
      * still-missing sibling that IS better (higher res, or =res + DV gain) ->
        left 'missing' + a prior_grab note (stays grabbable and OUT of the
        Upgrades tab/stat, exactly like the scanner) so a real upgrade isn't
        hidden or conflated with Plex upgrades.
    Never touches a library/upgrade/downloaded status. Pure/non-mutating
    (copies only changed dicts). `items` here is the full result set (paginated
    later), so every grabbed item sits alongside its siblings."""
    try:
        downloaded = db.get_downloaded_urls()
    except Exception:
        downloaded = set()

    # Best grabbed version per group_key. Two sources, merged:
    #   1. Title-keyed rows straight from the downloads table (normalized|year|
    #      Sseason — the scanner's group_key recipe). These survive the grabbed
    #      URL rolling out of the background cache, which used to silently
    #      un-mark a whole title ("downloaded items come up again").
    #   2. URL-anchored grabbed items still present here (covers legacy rows
    #      recorded before the year column existed).
    grabbed: Dict[str, Dict[str, Any]] = {}

    def _better(rank, dv, cur):
        return cur is None or rank > cur["rank"] or (rank == cur["rank"] and dv and not cur["dovi"])

    try:
        rows = db.get_downloaded_title_quality()
    except Exception:
        rows = []
    for row in rows if isinstance(rows, list) else []:
        try:
            nt, yr, se, res, dv = row[0], row[1], row[2], row[3], row[4]
        except Exception:
            continue
        if not nt:
            continue
        rank, dovi = _res_rank(res), bool(dv)
        # Reconstruct the scanner's uniform post-enrichment group_key
        # (_assign_group_keys): "{normalized_title}|{year or 0}|S{season or 0}".
        # Movies -> "{nt}|{year}|S0"; TV -> "{nt}|{year}|S{season}". A row with
        # neither year nor season can't be anchored — the URL-anchored pass below
        # covers it while the grab is still cached.
        if yr is None and se is None:
            continue
        gk = f"{nt}|{yr or 0}|S{se or 0}"
        if _better(rank, dovi, grabbed.get(gk)):
            grabbed[gk] = {"rank": rank, "dovi": dovi,
                           "resolution": res or "", "size": ""}

    for i in items:
        if i.get("url") in downloaded:
            gk = i.get("group_key")
            if not gk:
                continue
            rank, dv = _res_rank(i.get("resolution")), bool(i.get("dovi"))
            if _better(rank, dv, grabbed.get(gk)):
                grabbed[gk] = {"rank": rank, "dovi": dv,
                               "resolution": i.get("resolution") or "", "size": i.get("size") or ""}

    if not downloaded and not grabbed:
        return items

    out = []
    for i in items:
        url = i.get("url")
        if url in downloaded:
            out.append(i if i.get("status") == "downloaded" else {**i, "status": "downloaded"})
            continue
        status = (i.get("status") or "").lower()
        g = grabbed.get(i.get("group_key")) if grabbed else None
        # Only reclassify still-missing siblings; never touch library/upgrade/downloaded.
        if g is not None and status.startswith("missing"):
            note = i.get("prior_grab") or {
                "resolution": g["resolution"], "size": g["size"], "downloaded_at": None,
            }
            if _is_better_grab(i, g):
                out.append({**i, "prior_grab": note})            # stays missing/grabbable
            else:
                out.append({**i, "status": "downloaded_similar", "prior_grab": note})
            continue
        out.append(i)
    return out


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
                     genre=None, genre_exclude=None, language=None, quick=None, resolution=None,
                     posted_after=None, posted_before=None,
                     sort="title", order="asc"):
    """Filter and sort items server-side.

    Args:
        items: list of item dicts
        filter: status filter (e.g., "missing", "upgrade", "library")
        search: search in title
        category: list of enabled categories; shows unknowns + enabled
        genre: list of genres; item must have at least one
        genre_exclude: list of genres; item must have NONE of these
        language: list of languages; item must match
        quick: list of quick filters ('4k', 'hdrdv', 'inplex', 'bookmarked')
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

    if genre_exclude:
        gxset = set(genre_exclude)
        result = [i for i in result if not any(g in gxset for g in (i.get("genres") or []))]

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
        if "bookmarked" in q:
            result = [i for i in result if i.get("bookmarked")]

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
    genre_exclude: Optional[str] = None
    language: Optional[str] = None
    quick: Optional[str] = None
    resolution: Optional[str] = None
    posted_after: Optional[str] = None
    posted_before: Optional[str] = None


class DismissRequest(BaseModel):
    urls: List[str]
    # Optional url -> title map, stored for display in the "dismissed" manager.
    titles: Optional[Dict[str, str]] = None
    # Optional url -> {group_key, resolution, dovi} for title-level skip, so a
    # same-or-lower release of a skipped title stays hidden while an upgrade can
    # resurface. Absent = a plain per-URL dismissal (back-compatible).
    meta: Optional[Dict[str, Dict[str, Any]]] = None
    # True = dismiss (skip), False = un-dismiss (restore).
    dismissed: bool = True


class BookmarkRequest(BaseModel):
    imdb_id: Optional[str] = None
    title: str
    year: Optional[int] = None
    media_type: str
    # True = bookmark, False = un-bookmark (mirrors DismissRequest's explicit
    # boolean-flag shape rather than an implicit toggle).
    bookmarked: bool = True


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
    genre_exclude: Optional[List[str]] = None,
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
    # Overlay download state from the central downloads table at READ time, so
    # a grab is remembered across reloads and shared between the app and web —
    # independent of when the background scan last folded download history in,
    # and covering SIBLING releases (other URLs of the same title). Rules:
    #   * exact URL grabbed            -> 'downloaded'
    #   * a still-missing sibling of a grabbed title:
    #       - lower/equal resolution   -> 'downloaded_similar' (you have a copy;
    #                                     leaves the swipe deck, non-actionable)
    #       - higher resolution        -> 'upgrade' (genuinely better than what
    #                                     you grabbed; stays actionable to grab)
    # Never clobbers a library/upgrade/downloaded status. Non-mutating (copies
    # only the touched dicts). Mirrors the scanner's url/title history rules so
    # the deck stops re-offering versions of something you already grabbed.
    if reg.db is not None:
        items = _overlay_download_state(items, reg.db)

    # Hide items the user swiped away (unless explicitly requested). Two levels:
    #   * the exact dismissed URL is always hidden;
    #   * every same-or-LOWER-quality release of a skipped TITLE (group_key) is
    #     hidden too — so a smaller/similar re-upload doesn't resurface — while a
    #     genuine upgrade (higher res / DV gain) over what was skipped can still
    #     appear. Mirrors the grab-sibling rule via _is_better_grab.
    if not include_dismissed and reg.db is not None:
        dismissed = reg.db.get_dismissed_urls()
        skipped_titles: Dict[str, Dict[str, Any]] = {}
        try:
            for gk, res, dv in reg.db.get_dismissed_title_quality():
                if not gk:
                    continue
                rank, dovi = _res_rank(res), bool(dv)
                cur = skipped_titles.get(gk)
                if cur is None or rank > cur["rank"] or (rank == cur["rank"] and dovi and not cur["dovi"]):
                    skipped_titles[gk] = {"rank": rank, "dovi": dovi}
        except Exception:
            skipped_titles = {}
        if dismissed or skipped_titles:
            def _keep(i):
                if i.get("url") in dismissed:
                    return False
                s = skipped_titles.get(i.get("group_key")) if skipped_titles else None
                return s is None or _is_better_grab(i, s)
            items = [i for i in items if _keep(i)]

    # Bulk-annotate every item with its bookmarked state, ONE query per
    # request (list_bookmark_keys) instead of one per item -- mirrors the
    # dismissed/skipped_titles bulk-set pattern just above. Computed BEFORE
    # status/search/category/genre/language/quick filtering (and before the
    # stats snapshot) so the 'bookmarked' quick filter has something to
    # filter on and stats/facets see the flag too.
    bookmark_keys = reg.db.list_bookmark_keys() if reg.db is not None else set()

    def _item_bookmark_key(i):
        imdb = i.get("imdb_id")
        if imdb:
            return ("imdb", imdb)
        media_type = "tv" if i.get("season") is not None else "movie"
        return ("title", normalize_title(str(i.get("title", ""))), i.get("year"), media_type)

    for i in items:
        i["bookmarked"] = _item_bookmark_key(i) in bookmark_keys

    # Snapshot of all visible (non-dismissed) items for the overall stats,
    # before status/search/category/genre/language/quick filtering narrows
    # them further.
    visible_items = list(items)

    items = _filter_and_sort(
        items, filter=filter, search=search, category=category, genre=genre,
        genre_exclude=genre_exclude,
        language=language, quick=quick, resolution=resolution,
        posted_after=posted_after,
        posted_before=posted_before, sort=sort, order=order,
    )

    # Per-group_key counts over the filtered set (post-filter, pre-pagination).
    # Keyed by the canonical group_key (not bare title) so same-title/
    # different-year releases (e.g. Dune 1984 vs 2021) don't get merged into
    # one inflated count -- fall back to a composite key for legacy rows
    # lacking group_key.
    title_counts: Dict[str, int] = {}
    for i in items:
        gk = i.get("group_key") or f"{i.get('title', '')}|{i.get('year', '')}|S{i.get('season', '')}"
        title_counts[gk] = title_counts.get(gk, 0) + 1

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
    genre_exclude: Optional[str] = Query(None),
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
        category=_csv(category), genre=_csv(genre), genre_exclude=_csv(genre_exclude), language=_csv(language),
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
    genre_exclude: Optional[str] = Query(None),
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
        category=_csv(category), genre=_csv(genre), genre_exclude=_csv(genre_exclude), language=_csv(language),
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
        genre=_csv(req.genre), genre_exclude=_csv(req.genre_exclude), language=_csv(req.language), quick=_csv(req.quick),
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
    meta = req.meta or {}
    if req.dismissed:
        # Server-side meta backfill: an older app bundle (or any client that
        # doesn't send per-url meta) would otherwise record a URL-only
        # dismissal, and the title-level skip — "don't resurface a same-or-
        # lower re-upload" — silently wouldn't apply. The server already knows
        # every URL's group_key/resolution/dovi from the cached results, so
        # fill the gaps here instead of trusting the client to.
        missing = [u for u in req.urls
                   if u and not (meta.get(u) or {}).get("group_key")]
        if missing:
            try:
                cached_items, _ = _load_cached_items(reg)
                by_url = {i.get("url"): i for i in cached_items}
            except Exception:
                by_url = {}
            for u in missing:
                item = by_url.get(u)
                if item:
                    meta[u] = {"group_key": item.get("group_key"),
                               "resolution": item.get("resolution"),
                               "dovi": bool(item.get("dovi"))}
                    titles.setdefault(u, item.get("title"))

        def _rows():
            for url in req.urls:
                if not url:
                    continue
                m = meta.get(url) or {}
                yield (url, titles.get(url), m.get("group_key"), m.get("resolution"), m.get("dovi"))
        db.add_dismissed_items(_rows())
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


@router.post("/bookmark")
def bookmark_item(req: BookmarkRequest, reg: ServiceRegistry = Depends(get_registry)):
    """Bookmark (or un-bookmark) a title, independent of which release the
    user was looking at when they clicked it. Per-title identity: imdb_id
    when present, else normalized-title+year+media_type."""
    db = reg.db
    if db is None:
        raise HTTPException(status_code=503, detail="Database not available")
    if req.bookmarked:
        db.add_bookmark(req.imdb_id, req.title, req.year, req.media_type)
    else:
        db.remove_bookmark(req.imdb_id, req.title, req.year, req.media_type)
    return {"status": "ok", "bookmarked": req.bookmarked}


@router.get("/bookmarks")
def list_bookmarks(reg: ServiceRegistry = Depends(get_registry)):
    """List every bookmarked title (for a 'show bookmarks' / manage view)."""
    db = reg.db
    if db is None:
        raise HTTPException(status_code=503, detail="Database not available")
    items = db.list_bookmarks()
    return {"items": items, "count": len(items)}


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
