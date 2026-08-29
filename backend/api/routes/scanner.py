"""Scanner endpoints: start, stop, status."""
from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from enum import Enum
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from backend import release_grammar as grammar
from backend.api.dependencies import ServiceRegistry, get_registry
from backend.api.ws import ws_manager
from backend.config import source_enabled
from backend.scanner_service import resolve_rescan_media_type
from backend.scanner_service import ScanStatus, STATUS_COLORS, STATUS_TEXTS

logger = logging.getLogger(__name__)
def _cached_data(existing):
    """The JSON blob of a background_scan_cache row, or {} if unreadable.

    One reader, so an unparseable row fails the same way everywhere: closed,
    with no evidence, rather than half-parsed by one caller and not the other.
    """
    try:
        parsed = json.loads(existing.get("data") or "{}")
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def rescan_classification(existing):
    """The crawl classification a rescan CARRIES FORWARD: category, conflict.

    NOTE (R4-94-1): the authoritative type verdict is NOT decided here. It comes
    from ``resolve_rescan_media_type``, which weighs the same cached facts by
    AUTHORITY instead of OR-ing them.

    R4-94-2 removed the third return value, the flat OR of positive TV signals
    that fed the legacy ``is_tv`` field. It had no consumer left: each of the
    three sources it conflated (a 'tv' crawl route, a recorded season, a legacy
    row's recorded is_tv) is carried into the verdict by ``cached_type_evidence``
    at its own authority, and the route now derives ``is_tv`` from that verdict
    the way ``_process_posts`` and ``web_item_facts`` already do. Keeping the OR
    beside the verdict let the two contradict each other -- a conflicted row
    resolving 'ambiguous' while the OR asserted TV from the very route the
    conflict suppresses -- and the route persisted both.

    Extracted so it can be tested against production code rather than a test
    reimplementation. The first version of its test rebuilt this logic locally
    and therefore could not fail when the production line was wrong -- a
    mutation restoring the bug killed nothing.

    Peer review round 11. `source_category` is the SOURCE NAME -- "HDEncode" on
    all 4,145 live rows -- not the crawl category ('4k' | 'remux' | 'tv'). Two
    uses read it as though it were:

        details['category'] = source_category
            persisted "HDEncode" back into background_scan_cache, destroying
            the server-owned classification that authorises a destructive
            action for that release.

        post_source_is_tv = (source_category == 'tv')
            ALWAYS False, so rescanning a TV row whose season does not parse
            produced is_tv=False with season=None -- recreating on a live route
            the exact defect #93 exists to fix. That line was added BY the #93
            fix, which is the part worth noticing: a fix for "re-derived a fact
            that was already known" re-derived it again, one route over, from a
            field that never held it.

    A rescan re-fetches the DETAIL page. It observes nothing about which
    listing the release came from, so it carries that evidence rather than
    inventing it.

    The second of those two defects is why the round-11 fix mattered at all, and
    the carried facts it recovered are still carried -- as EVIDENCE now, through
    ``cached_type_evidence``, rather than as a boolean this helper returns.

    Returns ``(category, category_conflict)``.
    """
    cached = _cached_data(existing)
    category = str(cached.get("category") or "").strip().lower()
    # A conflict recorded against this release survives a rescan: re-reading
    # the detail page is not evidence about which listings carried it.
    conflict = bool(cached.get("category_conflict"))
    return category, conflict


router = APIRouter(prefix="/scan", tags=["scanner"])
TMDB_IMAGE_BASE = "https://image.tmdb.org/t/p/w500"

# Scan state tracking
#
# B3: _scan_state["state"] and the ScannerService's real scan-slot
# (try_acquire_scan/release_scan) are two separate sources of truth.
# scan_start()/scheduler_trigger() optimistically set state to "running"
# under _scan_lock *before* the spawned thread ever calls try_acquire_scan()
# -- that optimistic claim is needed to serialize racing /scan/start calls
# against each other (the _scan_lock check-then-set below), but it means
# _scan_state alone can say "running" for a scan that a moment later fails
# to acquire the slot (a background pre-cache scan got there first) and
# resets to "idle". _scan_state["holds_slot"] tracks whether *this* scan
# thread has actually acquired the slot; scan_status() reports "running"
# only when both are true, so a phantom claim never outlives the brief
# window before try_acquire_scan() runs.
_scan_thread: Optional[threading.Thread] = None
_scan_state: Dict[str, Any] = {
    "state": "idle",
    "progress": 0.0,
    "phase": "",
    "scanned": 0,
    "total": 0,
    "holds_slot": False,
}
_scan_lock = threading.Lock()

# Last scan results (shared with results route)
_last_scan_items: List[Any] = []
_items_lock = threading.Lock()


class ScanRequest(BaseModel):
    type: str = "deep"
    source: str = "HDEncode"
    sources: Optional[List[str]] = None
    search_query: str = ""
    pages: int = 1
    flags: Optional[Dict[str, bool]] = None


class RescanItemRequest(BaseModel):
    url: str


# Map frontend shorthand to backend scan_type values
_SCAN_TYPE_MAP = {
    "deep": "Deep Scan",
    "incremental": "Incremental",
    "loaded": "Loaded Scan",
    "search": "Site Search",
}

# Normalize source names (case-insensitive input → canonical form)
_SOURCE_NAME_MAP = {
    "hdencode": "HDEncode",
    "ddlbase": "DDLBase",
    "adit-hd": "Adit-HD",
    "adithd": "Adit-HD",
}


def _scan_request_uses_hdencode(req: ScanRequest) -> bool:
    """Mirror _run_scan's source normalization without starting a scan thread."""
    source_type = req.source
    if req.sources and len(req.sources) == 1:
        source_type = req.sources[0]
    source_type = _SOURCE_NAME_MAP.get(source_type.lower(), source_type)
    scan_type = _SCAN_TYPE_MAP.get(req.type, req.type)
    return scan_type == "Site Search" or source_type == "HDEncode"


def _normalized_hostname(url: str) -> str:
    try:
        host = (urlparse(url).hostname or "").lower()
    except Exception:
        return ""
    return host[4:] if host.startswith("www.") else host


def _is_hdencode_url(url: str, config: Dict[str, Any]) -> bool:
    """Recognize the normal HDEncode host and a configured HDEncode base URL."""
    host = _normalized_hostname(url)
    configured_host = _normalized_hostname(
        str(config.get("base_url", "https://hdencode.org"))
    )
    return bool(host and host in {"hdencode.org", configured_host})


def get_last_scan_items() -> List[Any]:
    """Access last scan items from the results route."""
    with _items_lock:
        return list(_last_scan_items)


def _progress_callback(progress: float, phase: str) -> None:
    """Called by ScannerService during scan — broadcasts via WebSocket."""
    with _scan_lock:
        _scan_state["progress"] = progress
        _scan_state["phase"] = phase
    ws_manager.broadcast_sync({
        "type": "scan:progress",
        "data": {"progress": progress, "phase": phase},
    })


def _log_callback(message: str, level: str = "info") -> None:
    """Forward scanner log messages to WebSocket."""
    ws_manager.broadcast_sync({
        "type": "log",
        "data": {"level": level, "message": message, "timestamp": time.strftime("%H:%M:%S")},
    })


def _run_scan(reg: ServiceRegistry, req: ScanRequest) -> None:
    """Execute scan in background thread."""
    global _last_scan_items

    scanner = reg.scanner
    if not scanner:
        return

    # Claim the global scan slot so we never run concurrently with a background
    # pre-cache scan (they share one ScannerService and would corrupt each
    # other's in-memory state). Foreground-vs-foreground is already serialized
    # by _scan_state; this guards foreground-vs-background.
    if not scanner.try_acquire_scan():
        logger.info("Scan aborted: a background pre-cache scan is currently running")
        ws_manager.broadcast_sync({
            "type": "scan:error",
            "data": {"message": "A background pre-cache scan is running; please retry in a moment."},
        })
        with _scan_lock:
            _scan_state["state"] = "idle"
            _scan_state["progress"] = 0.0
            _scan_state["phase"] = ""
            _scan_state["holds_slot"] = False
        return

    with _scan_lock:
        _scan_state["state"] = "running"
        _scan_state["progress"] = 0.0
        _scan_state["phase"] = "starting"
        # Only now do we actually hold the scan slot -- scan_status() uses
        # this to avoid reporting "running" for the brief window between
        # scan_start()'s optimistic claim and this thread's real acquisition
        # (or its failure, handled just above).
        _scan_state["holds_slot"] = True

    # Clear stale selections from previous scans
    from backend.api.routes.results import _selected, _selected_lock
    with _selected_lock:
        _selected.clear()

    scanner.set_progress_callback(_progress_callback)
    scanner.set_log_callback(_log_callback)
    start_time = time.time()

    try:
        source_type = req.source
        if req.sources and len(req.sources) == 1:
            source_type = req.sources[0]
        # Normalize source name (case-insensitive)
        source_type = _SOURCE_NAME_MAP.get(source_type.lower(), source_type)

        scan_type = _SCAN_TYPE_MAP.get(req.type, req.type)
        items = scanner.run_scan(
            scan_type=scan_type,
            source_type=source_type,
            pages=req.pages,
            resolution_flags=req.flags,
            search_query=req.search_query,
        )

        duration = time.time() - start_time

        # Store results
        with _items_lock:
            _last_scan_items = list(items) if items else []

        # Broadcast each result
        for item in (items or []):
            item_dict = _media_item_to_dict(item)
            ws_manager.broadcast_sync({"type": "scan:result", "data": item_dict})

        # Stats
        stats = _compute_stats(items)
        ws_manager.broadcast_sync({
            "type": "scan:complete",
            "data": {
                "stats": stats,
                "total": stats["total"],
                "duration": round(duration, 1),
            },
        })

        # Stamp "Last scan" at COMPLETION. Previously only the scheduler set
        # last_scan_time, and it did so when the scan was *triggered* (start),
        # so the UI showed "Just now" the moment a scan began. Manual scans
        # never updated it at all. Stamp it here so it reflects the real last
        # completed scan.
        try:
            reg.config["last_scan_time"] = time.time()
            if reg.backend:
                reg.backend.save_config()
        except Exception:
            logger.warning("Failed to update last_scan_time")

        # Notify
        if reg.notifications:
            reg.notifications.notify_scan_complete(
                total=stats["total"],
                missing=stats["missing"],
                upgrades=stats["upgrade"],
            )

        # Auto-grab
        if reg.auto_grab and reg.auto_grab.enabled and items:
            try:
                ws_manager.broadcast_sync({
                    "type": "autograb:started",
                    "data": {"count": len(items)},
                })
                grabbed = reg.auto_grab.process_items(items)
                ws_manager.broadcast_sync({
                    "type": "autograb:complete",
                    "data": {"grabbed": grabbed if isinstance(grabbed, int) else 0, "total": len(items)},
                })
            except Exception as e:
                logger.warning("Auto-grab failed: %s", e)
                ws_manager.broadcast_sync({
                    "type": "autograb:complete",
                    "data": {"grabbed": 0, "total": len(items), "error": str(e)},
                })

    except Exception as e:
        logger.exception("Scan failed")
        ws_manager.broadcast_sync({
            "type": "scan:error",
            "data": {"message": str(e)},
        })
    finally:
        scanner.release_scan()
        with _scan_lock:
            _scan_state["state"] = "idle"
            _scan_state["progress"] = 0.0
            _scan_state["phase"] = ""
            _scan_state["holds_slot"] = False


def _media_item_to_dict(item: Any) -> Dict[str, Any]:
    """Convert a MediaItem to a JSON-serializable dict."""
    if hasattr(item, "__dict__"):
        d = {}
        for k, v in item.__dict__.items():
            if k.startswith("_"):
                continue
            if isinstance(v, set):
                d[k] = list(v)
            elif isinstance(v, Enum):
                d[k] = v.value
            else:
                d[k] = v
        # Construct full poster URL from TMDB path fragment
        poster_path = d.get("poster_path") or d.get("poster")
        if poster_path and isinstance(poster_path, str) and poster_path.startswith("/"):
            d["poster_url"] = f"{TMDB_IMAGE_BASE}{poster_path}"
        elif not d.get("poster_url"):
            d["poster_url"] = ""
        return d
    return item if isinstance(item, dict) else {}


def _compute_stats(items: Optional[List[Any]]) -> Dict[str, int]:
    """Compute result statistics."""
    if not items:
        return {"total": 0, "missing": 0, "upgrade": 0, "library": 0}

    missing = 0
    upgrade = 0
    library = 0
    for i in items:
        status = getattr(i, "status", "")
        # status may be an enum — convert to string
        status_str = str(status).lower() if status else ""
        if "missing" in status_str:
            missing += 1
        elif "upgrade" in status_str:
            upgrade += 1
        elif "library" in status_str:
            library += 1

    return {
        "total": len(items),
        "missing": missing,
        "upgrade": upgrade,
        "library": library,
    }


@router.get("/status")
def scan_status(reg: ServiceRegistry = Depends(get_registry)):
    with _scan_lock:
        state = dict(_scan_state)

    # B3: never report "running" unless this scan thread actually holds the
    # scan slot. holds_slot already guards the scan_start()-vs-_run_scan
    # optimistic-claim race (see the module docstring above _scan_state); this
    # extra cross-check against the ScannerService's own slot covers the
    # equally-phantom case where holds_slot is stale (e.g. a hard-crashed
    # scan thread never reached its `finally`) and the slot has since been
    # released or taken over by something else.
    if state.get("state") == "running":
        scanner = reg.scanner
        slot_held = bool(scanner.scan_in_progress) if scanner else False
        if not state.get("holds_slot") or not slot_held:
            state["state"] = "idle"
    state.pop("holds_slot", None)
    return state


@router.post("/start")
def scan_start(
    req: ScanRequest,
    reg: ServiceRegistry = Depends(get_registry),
):
    global _scan_thread

    if (_scan_request_uses_hdencode(req)
            and not source_enabled(reg.config, "hdencode_enabled", missing_default=True)):
        raise HTTPException(
            status_code=409,
            detail="HDEncode is disabled in Settings; no request was made")

    scanner = reg.scanner
    if scanner and scanner.scan_in_progress:
        raise HTTPException(
            status_code=409,
            detail="A background pre-cache scan is running; please retry in a moment")

    with _scan_lock:
        if _scan_state["state"] == "running":
            raise HTTPException(status_code=409, detail="Scan already running")
        _scan_state["state"] = "running"
        _scan_thread = threading.Thread(target=_run_scan, args=(reg, req), daemon=True)
        _scan_thread.start()
    return {"status": "started", "type": req.type}


@router.post("/stop")
def scan_stop(reg: ServiceRegistry = Depends(get_registry)):
    scanner = reg.scanner
    if scanner:
        scanner.stop_scan_flag = True
    with _scan_lock:
        _scan_state["state"] = "stopping"
    return {"status": "stopping"}


@router.post("/rescan-item")
def rescan_item(
    req: RescanItemRequest,
    reg: ServiceRegistry = Depends(get_registry),
):
    """Force-refresh a single cached scan result: re-fetch its detail page
    and re-run TMDB/OMDb/RT enrichment, bypassing the normal scan's
    skip-already-cached-URLs optimization. Reuses the exact same scraping/
    enrichment pipeline the bulk scan uses — no new matching logic."""
    if not req.url:
        raise HTTPException(status_code=400, detail="No URL provided")

    if (_is_hdencode_url(req.url, reg.config)
            and not source_enabled(reg.config, "hdencode_enabled", missing_default=True)):
        raise HTTPException(
            status_code=409,
            detail="HDEncode is disabled in Settings; no request was made")
    db = reg.db
    scanner = reg.scanner
    if not db or not scanner:
        raise HTTPException(status_code=503, detail="Scanner not available")

    existing = db.get_background_cache_by_url(req.url)
    if not existing:
        raise HTTPException(status_code=404, detail="Item not found in cache")

    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
    try:
        details = scanner.scrapers.scrape_details(req.url, headers)
    except Exception as e:
        logger.exception("Rescan failed for %s", req.url)
        raise HTTPException(status_code=502, detail=f"Rescan failed: {e}")
    if not details:
        raise HTTPException(status_code=502, detail="Could not fetch a fresh copy of this page")

    post_source = existing.get("source_category") or "hdencode"
    details['source'] = post_source
    # MERGE UNION (PR #94 x main, 2026-08-28). Each side fixed a different
    # defect on this route and neither subsumes the other:
    #   main (rescan_classification, round-11): source_category is the SOURCE
    #     NAME, not the crawl category -- the helper carries forward the
    #     cached row's real category, its recorded is_tv (category=='tv' OR
    #     cached is_tv OR season), and a recorded category_conflict.
    #   #94 (round-13): this route never resolved a media-type verdict, so
    #     _create_media_item defaulted every rescanned item to 'ambiguous';
    #     resolve_listing_media_type composes the cached crawl category with
    #     the fresh detail-page filename signal.
    #
    # R4-94-1 CORRECTION to that union. The first version fed only the cached
    # CATEGORY into resolve_listing_media_type, so main's carried decision
    # survived in the legacy `is_tv` field while being absent from
    # `media_type` -- the field the matcher now actually routes on. A row with
    # category '4k' (or blank), is_tv=True, season=None and a neutral title,
    # rescanned against a detail page with no season token, came back
    # media_type='movie' AND is_tv=True: internally contradictory, and the
    # contradiction was then persisted back into the cache below, so the
    # re-derived movie became the next carried verdict.
    #
    # resolve_rescan_media_type reconstructs the cache's type evidence with the
    # authority each source deserves (category->ROUTE, season->TITLE,
    # is_tv->DETAIL, stored verdict->its provisional flag) and combines it with
    # the one thing a rescan genuinely re-observes: the fresh detail filename.
    # It shares cached_type_evidence with _media_item_from_dict rather than
    # holding a third copy of the rule.
    cached_row = _cached_data(existing)
    (details['category'],
     details['category_conflict']) = rescan_classification(existing)
    verdict = resolve_rescan_media_type(
        cached_row, details, listing_title=existing.get("title") or "")
    details['media_type_verdict'] = verdict.media_type.value
    details['media_type_provisional'] = verdict.provisional
    details['media_type_because'] = list(verdict.because)

    # R4-94-2. The legacy boolean is a SHADOW of the verdict -- the same rule
    # _process_posts's worker uses ("is_tv = verdict.media_type is TV") and the
    # same rule web_item_facts already applies when the matcher asks. It was an
    # OR over the verdict, the fresh detail flag and rescan_classification's
    # carried boolean, and that OR could contradict the verdict it sat beside:
    # a row recording a category_conflict resolved 'ambiguous' -- refusing to
    # decide its type -- while the OR still asserted is_tv=True off the very
    # route the conflict suppresses, and the route PERSISTED both. AMBIGUOUS
    # yields False here for web_item_facts' stated reason: it must not select
    # the TV library, and media_type keeps the distinction so the library query
    # can refuse rather than defaulting to Movies.
    #
    # Nothing is lost. Each of the three sources the OR conflated is carried
    # into the verdict by cached_type_evidence at its own authority -- the
    # crawl route at ROUTE, a recorded season at TITLE, a legacy row's recorded
    # is_tv at DETAIL -- and a fresh detail is_tv is DETAIL evidence there too,
    # so every input that used to force True still resolves TV.
    item = scanner._create_media_item({
        'details': details,
        'is_tv': verdict.media_type is grammar.MediaType.TV,
        'url': req.url,
    })
    if not item:
        raise HTTPException(status_code=502, detail="Could not parse the refreshed page")

    asyncio.run(scanner._enricher.enrich([item]))

    # Rescanning never consults Plex (see the docstring above) -- it only
    # re-fetches the detail page + re-runs TMDB/OMDb enrichment. That means
    # _create_media_item() just derived `item`'s status from download history
    # alone (never IN_LIBRARY/UPGRADE/DV_UPGRADE) and left plex_info/
    # plex_versions/plex_rating_key at their MediaItem dataclass defaults.
    # If the row we're about to overwrite already had a known Plex match,
    # carry it forward so a rescan can't wipe it out -- same rule
    # rematch_cache() applies for its "no Plex this run" branch: never
    # downgrade a cached IN_LIBRARY/UPGRADE/DV_UPGRADE row to Missing.
    library_states = {"in_library", "upgrade", "dv_upgrade"}
    existing_data = cached_row
    existing_status = str(existing_data.get("status", ""))
    if existing_status in library_states:
        try:
            status_enum = ScanStatus(existing_status)
        except ValueError:
            status_enum = None
        if status_enum is not None:
            item.status = status_enum
            item.status_text = STATUS_TEXTS.get(status_enum, item.status_text)
            item.color = STATUS_COLORS.get(status_enum, item.color)
            item.plex_info = existing_data.get("plex_info", item.plex_info)
            item.plex_versions = existing_data.get("plex_versions", item.plex_versions)
            item.plex_rating_key = existing_data.get("plex_rating_key", item.plex_rating_key)

    d = _media_item_to_dict(item)
    db.upsert_background_cache([{
        "url": req.url,
        "title": d.get("title"),
        "year": d.get("year"),
        "status": str(d.get("status", "")),
        "source_category": post_source,
        "data": json.dumps(d, default=str),
    }])
    return {"status": "ok", "item": d}
