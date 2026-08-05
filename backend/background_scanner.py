"""Background pre-cache scanner.

Periodically runs the normal scan for the configured sources and persists the
results into the ``background_scan_cache`` table, so the app can open with
results already populated even after a restart (the live scan is in-memory
only). Entirely off by default; controlled by the ``background_scan_*``
settings. Runs on a daemon thread, matching the project's other background
workers (results poller, scheduler) rather than introducing asyncio.
"""
from __future__ import annotations

import json
import logging
import random
import threading
import time
from typing import Any, Dict, List, Optional

from backend.config import source_enabled

logger = logging.getLogger(__name__)

_DEFAULT_SOURCES = ["HDEncode", "DDLBase", "Adit-HD"]

# Pre-cache every category so the UI's 4K/Remux/TV toggles can filter the cached
# results instantly (no re-scrape). Superset of all per-source flag keys; each
# source's _build_sources picks the ones it understands.
_ALL_CATEGORY_FLAGS = {
    "4k": True, "remux": True, "tv": True,
    "4k_webdl": True, "4k_remux": True, "1080p_remux": True,
}


class BackgroundScanner:
    """Runs periodic pre-cache scans on a daemon thread."""

    def __init__(self, registry):
        self._reg = registry
        self._lifespan_generation = registry.lifespan_generation
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._running = threading.Event()  # a scan is currently executing
        self._lock = threading.Lock()
        # Timestamp the loop is currently sleeping toward — authoritative for
        # next_run_at so the banner reflects the real wake time even if the last
        # run failed (and didn't stamp background_scan_last_run).
        self._next_run_ts: Optional[float] = None
        # Summary of the most recent run, surfaced via /background/status.
        self._last_run: Optional[Dict[str, Any]] = None
        self._rss_jitter_seconds = random.uniform(-600.0, 600.0)
        # Process-lifetime marker.  HDEncodeRSSService is intentionally
        # short-lived (one instance per scan), so recovery evidence must live
        # here rather than on the service instance.
        self._rss_first_cycle_after_startup = True

    @staticmethod
    def _rss_normal_feeds_complete(feeds, *, listing_error=None) -> bool:
        """Return True only when both normal feeds and listing completed."""
        if listing_error:
            return False
        normal = {
            result.get("feed"): result.get("outcome")
            for result in (feeds or [])
            if result.get("feed") in {"movies_all", "tv_all"}
        }
        return (
            set(normal) == {"movies_all", "tv_all"}
            and all(
                outcome in {"changed", "not_modified"}
                for outcome in normal.values()
            )
        )

    @staticmethod
    def _listing_arm_incomplete(scanner, *, err: Optional[str]) -> bool:
        """Whether the listing (control) arm failed to observe the site at all.

        ``err`` on its own cannot detect this. ``run_scan`` swallows every
        exception, and a Cloudflare 403 or a whole-crawl connection failure is
        handled inside the crawl loop, so a completely blocked cycle returns
        normally with ``err is None`` and a non-zero request count — which is
        how blocked cycles were being recorded as shadow ``outcome='success'``
        with zero misses and counted as promotion evidence.

        Two signals, and BOTH are required.

        1. The crawl's seen-set: ``_crawl_pages`` fills it from every listing
           page it actually parsed, and ``run_scan`` clears it at the start of
           each run, so an empty set means no listing page was read this cycle.
           This catches the all-pages-blocked cycle.

        2. The crawl's completion reason. The seen-set alone cannot catch a
           PARTIAL failure — page 1 parses, page 2 blocks or throws — because
           page 1 already made the set non-empty. Only ``complete`` and
           ``cached_frontier`` mean the listing was observed as far as it needed
           to be; ``blocked``, ``transport_error``, ``coordinator_stopped``,
           ``cancelled``, ``scan_error`` and ``not_started`` all mean some of the
           listing was never seen, so the comparison's control arm is not sound
           and the cycle must not count as promotion evidence.

        ``_last_crawl_early_stopped`` is deliberately NOT used: it is True both
        for the healthy cached-frontier stop (the crawl reached the discovery
        frontier — the normal steady state for the background pre-cache, which
        runs with early_stop=True) and for a block, so keying on it would mark
        almost every cycle incomplete and stall the shadow evidence entirely.
        The reason field is what tells those two apart.
        """
        if err:
            return True
        if not getattr(scanner, "_last_crawl_seen_urls", None):
            return True
        if not hasattr(scanner, "_last_crawl_completion_reason"):
            # A stand-in, not a production crawl: the real ScannerService sets
            # this in __init__ and re-arms it in every run_scan, so the attribute
            # cannot be missing on a live scanner. Pre-existing test doubles
            # (built with ScannerService.__new__ or as plain classes) predate the
            # field; fall back to the seen-set rule for them instead of failing
            # every such cycle. hasattr, not a None/falsy check, on purpose — a
            # double that DOES set the field, to None or anything else
            # unratified, is judged by the allowlist like any real crawl.
            return False
        from backend.scanner_service import CRAWL_REASONS_FULLY_OBSERVED
        reason = getattr(scanner, "_last_crawl_completion_reason")
        return reason not in CRAWL_REASONS_FULLY_OBSERVED

    def _qualify_restart_recovery(
        self,
        *,
        preexisting_normal_feed_state: bool,
        metrics: Dict[str, Any],
    ) -> bool:
        """Consume startup evidence only on the first eligible comparison."""
        eligible = (
            bool(metrics.get("normal_feeds_complete"))
            and int(metrics.get("rss_requests") or 0) > 0
            and int(metrics.get("listing_requests") or 0) > 0
            and str(metrics.get("outcome") or "")
            in {"success", "relevant_miss"}
        )
        if not eligible:
            return False
        recovery = bool(
            self._rss_first_cycle_after_startup
            and preexisting_normal_feed_state
        )
        self._rss_first_cycle_after_startup = False
        return recovery

    # ── lifecycle ─────────────────────────────────────────────────────

    def start(self) -> None:
        """Start the periodic loop (no-op if already running)."""
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._loop, name="background-scanner", daemon=True)
            self._thread.start()
            logger.info("Background scanner started")

    def stop(self) -> None:
        """Stop the scheduler and interrupt any active shared scan."""
        self._stop.set()
        scanner = getattr(self._reg, "scanner", None)
        if scanner is not None and self._running.is_set():
            scanner.stop_scan_flag = True
        t = self._thread
        if t and t.is_alive():
            t.join(timeout=2.0)
        logger.info("Background scanner stopped")

    @property
    def is_active(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    @property
    def is_scanning(self) -> bool:
        return self._running.is_set()

    @property
    def last_run(self) -> Optional[Dict[str, Any]]:
        """Summary of the most recent completed run (per-source counts/errors)."""
        return self._last_run

    def _owns_lifespan(self) -> bool:
        """Whether this worker still belongs to the registry's active lifespan."""
        return self._reg.owns_lifespan(self._lifespan_generation)

    def next_run_at(self) -> Optional[float]:
        """Epoch timestamp of the next scheduled run, or None if disabled."""
        cfg = self._reg.config or {}
        rss_active = (
            source_enabled(cfg, "hdencode_enabled", missing_default=True)
            and cfg.get("hdencode_discovery_mode")
            in {"rss_shadow", "rss_primary"}
        )
        if not cfg.get("background_scan_enabled") and not rss_active:
            return None
        # The timestamp the loop is actually sleeping toward is authoritative;
        # fall back to last_run + interval before the loop has armed it.
        if self._next_run_ts:
            return self._next_run_ts
        last = cfg.get("background_scan_last_run") or 0
        base = last or time.time()
        return base + self._interval_seconds()

    # ── scheduling loop ───────────────────────────────────────────────

    def _interval_seconds(self) -> float:
        cfg = self._reg.config or {}
        intervals = []
        if cfg.get("background_scan_enabled"):
            try:
                hours = max(1, int(cfg.get("background_scan_interval_hours", 6)))
            except (TypeError, ValueError):
                hours = 6
            intervals.append(hours * 3600.0)
        if (
            source_enabled(cfg, "hdencode_enabled", missing_default=True)
            and cfg.get("hdencode_discovery_mode")
            in {"rss_shadow", "rss_primary"}
        ):
            try:
                minutes = max(15, min(int(
                    cfg.get("hdencode_rss_poll_minutes", 60)
                ), 360))
            except (TypeError, ValueError):
                minutes = 60
            intervals.append(max(300.0, minutes * 60.0 + self._rss_jitter_seconds))
        return min(intervals) if intervals else 3600.0

    def _wait_interval(self) -> bool:
        """Sleep one interval, re-reading it in short slices so a change to
        ``background_scan_interval_hours`` is honoured within ~a minute instead
        of only after the *old* (possibly hours-long) interval elapses.

        Returns True if a stop was requested during the wait.
        """
        elapsed = 0.0
        start = time.time()
        while not self._stop.is_set():
            target = self._interval_seconds()
            # Keep the reported next-run ETA in sync with the live interval, so a
            # mid-wait change doesn't leave a stale wake time on the status page.
            self._next_run_ts = start + target
            if elapsed >= target:
                return False
            slice_s = min(60.0, target - elapsed)
            if self._stop.wait(timeout=slice_s):
                return True
            elapsed += slice_s
        return True

    def _loop(self) -> None:
        # Wait one interval before the first run so startup isn't hammered, then
        # run on each interval while still enabled.
        while not self._stop.is_set():
            self._next_run_ts = time.time() + self._interval_seconds()
            if self._wait_interval():
                self._next_run_ts = None
                return  # stop requested
            cfg = self._reg.config or {}
            rss_active = (
                source_enabled(cfg, "hdencode_enabled", missing_default=True)
                and cfg.get("hdencode_discovery_mode")
                in {"rss_shadow", "rss_primary"}
            )
            if cfg.get("background_scan_enabled") or rss_active:
                try:
                    self.scan_once()
                except Exception:
                    logger.exception("Background scan failed")
                finally:
                    self._rss_jitter_seconds = random.uniform(-600.0, 600.0)

    # ── the scan itself ───────────────────────────────────────────────

    def scan_once(self) -> Dict[str, Any]:
        """Run one pre-cache pass: scan each configured source, upsert, purge.

        Safe to call directly (used by POST /background/scan-now). Returns a
        small summary dict.
        """
        reg = self._reg
        cfg = reg.config or {}
        scanner = reg.scanner
        db = reg.db
        if not self._owns_lifespan():
            logger.info("Background scan abandoned: stale app lifespan")
            return {
                "scanned": 0, "cached": 0, "skipped": True,
                "reason": "stale_lifespan",
            }
        if scanner is None or db is None:
            logger.warning("Background scan skipped: scanner/db unavailable")
            return {"scanned": 0, "cached": 0, "skipped": True}

        sources = cfg.get("background_scan_sources") or _DEFAULT_SOURCES
        rss_active = (
            source_enabled(cfg, "hdencode_enabled", missing_default=True)
            and cfg.get("hdencode_discovery_mode")
            in {"rss_shadow", "rss_primary"}
        )
        if rss_active and not cfg.get("background_scan_enabled"):
            sources = ["HDEncode"]
        try:
            pages = max(1, int(cfg.get("background_scan_pages", 3)))
        except (TypeError, ValueError):
            pages = 3

        # Two-part guard. ``self._running`` stops two background scans
        # overlapping; ``scanner.try_acquire_scan()`` is the global slot that
        # also makes the background scan YIELD to any foreground (manual or
        # scheduled) scan — they share one ScannerService and would corrupt each
        # other's in-memory state. This is both the race fix and the idle gate.
        with self._lock:
            if self._running.is_set():
                logger.info("Background scan already in progress; skipping")
                return {"scanned": 0, "cached": 0, "skipped": True}
            if not scanner.try_acquire_scan():
                logger.info("Background scan skipped: a foreground scan is in progress")
                return {"scanned": 0, "cached": 0, "skipped": True, "reason": "busy"}
            self._running.set()

        # Posts already cached are skipped from re-scraping and the crawl
        # early-stops once it reaches them; their last_seen is refreshed below so
        # they aren't purged while still listed.
        cached_urls = db.get_background_cache_urls()
        total = 0
        # Purging is safe only when every configured source completed a full
        # crawl.  A disabled source is intentionally not visited, just like an
        # early-stopped source is only partially visited.
        purge_safe = True
        source_results: List[Dict[str, Any]] = []
        rss_cycle = None
        preexisting_normal_feed_state = False
        discovery_mode = cfg.get(
            "hdencode_discovery_mode", "listing"
        )
        try:
            if (
                rss_active
                and "HDEncode" in sources
                and discovery_mode in {"rss_shadow", "rss_primary"}
            ):
                from backend.hdencode_rss_service import HDEncodeRSSService
                stop_requested = lambda: (
                    self._stop.is_set()
                    or not self._owns_lifespan()
                )
                preexisting_normal_feed_state = all(
                    bool(
                        (db.get_hdencode_feed_state(feed_key) or {}).get(
                            "last_checked_at"
                        )
                    )
                    for feed_key in ("movies_all", "tv_all")
                )
                rss_cycle = HDEncodeRSSService(cfg, db).poll_cycle(
                    stop_requested=stop_requested,
                )
                if stop_requested():
                    return {
                        "scanned": 0,
                        "cached": 0,
                        "skipped": True,
                        "reason": "stale_lifespan",
                    }
                from backend.hdencode_candidate_service import (
                    HDEncodeCandidateService,
                )
                candidate_service = HDEncodeCandidateService(cfg, db)
                rss_cycle["classification"] = (
                    candidate_service.classify_pending(
                        stop_requested=stop_requested,
                    )
                )
                detail_scraper = getattr(
                    getattr(scanner, "scrapers", None),
                    "_detail",
                    None,
                )
                if detail_scraper is not None and not stop_requested():
                    rss_cycle["hydration"] = (
                        candidate_service.hydrate_pending(
                            detail_scraper,
                            stop_requested=stop_requested,
                        )
                    )
                if (
                    cfg.get("hdencode_rss_auto_grab_enabled") is True
                    and not stop_requested()
                ):
                    from backend.hdencode_action_service import (
                        HDEncodeActionService,
                    )
                    try:
                        action_service = HDEncodeActionService(
                            cfg, db, getattr(self._reg, "download", None)
                        )
                        queued_actions = (
                            action_service.queue_approved_auto_actions(
                                limit=1,
                                lifespan_generation=getattr(
                                    self, "_lifespan_generation", None
                                ),
                                stop_requested=stop_requested,
                            )
                        )
                        action_results = []
                        for queued_action in queued_actions:
                            if stop_requested():
                                break
                            result = action_service.run_action(
                                queued_action["action_uuid"],
                                owns_lifespan=self._owns_lifespan,
                            )
                            action_results.append({
                                "action_uuid": result.get("action_uuid"),
                                "state": result.get("state"),
                            })
                        rss_cycle["auto_actions"] = action_results
                    except Exception:
                        logger.exception("RSS automatic action cycle failed")
                        rss_cycle["auto_actions_error"] = "action_cycle_failed"
                if stop_requested():
                    return {
                        "scanned": 0,
                        "cached": 0,
                        "skipped": True,
                        "reason": "stale_lifespan",
                    }
            for source in sources:
                if (
                    str(source).strip().lower() == "hdencode"
                    and not source_enabled(
                        cfg,
                        "hdencode_enabled",
                        missing_default=True,
                    )
                ):
                    logger.info("Background scan: HDEncode disabled; skipping without network access")
                    source_results.append({
                        "source": source, "new": 0, "error": None,
                        "skipped": "disabled",
                    })
                    purge_safe = False
                    continue

                is_hdencode = str(source).lower() == "hdencode"
                if is_hdencode and discovery_mode == "rss_primary":
                    if not (
                        rss_cycle
                        and rss_cycle.get("fallback_qualified")
                    ):
                        source_results.append({
                            "source": source,
                            "new": 0,
                            "error": None,
                            "skipped": "rss_primary",
                        })
                        # Same rule as the disabled-source branch above: the
                        # listing was intentionally not visited, so nothing
                        # refreshed last_seen for its cached rows. Purging here
                        # would age the whole HDEncode cache out over
                        # background_scan_retain_days with no crawl to blame.
                        purge_safe = False
                        continue
                    source_pages = 1
                    rss_cycle["listing_fallback_started"] = True
                    # The fallback is a deliberate PARTIAL visit (one page,
                    # against a default of three), so its seen-set cannot
                    # justify aging out rows that only appear on deeper pages.
                    purge_safe = False
                else:
                    source_pages = pages
                err: Optional[str] = None
                items: List[Any] = []
                try:
                    items = self._scan_source(
                        source, source_pages, cached_urls
                    )
                except Exception as e:
                    err = str(e)
                    logger.exception("Background scan of source %s failed", source)

                # A source scan can block past teardown's bounded join. Re-check
                # ownership before any captured DB object or the reused registry
                # can be mutated.
                if not self._owns_lifespan():
                    logger.info(
                        "Background scan abandoned after source %s: stale app lifespan",
                        source,
                    )
                    return {
                        "scanned": 0, "cached": 0, "skipped": True,
                        "reason": "stale_lifespan",
                    }

                # Refresh last_seen for still-listed items we skipped re-scraping.
                if not err:
                    seen = getattr(scanner, "_last_crawl_seen_urls", None)
                    if seen:
                        db.touch_background_cache(seen)
                    if getattr(scanner, "_last_crawl_early_stopped", False):
                        purge_safe = False

                if (
                    is_hdencode
                    and discovery_mode == "rss_shadow"
                    and rss_cycle
                    and cfg.get("hdencode_rss_shadow_compare_enabled", True)
                ):
                    from datetime import datetime, timezone
                    import uuid
                    from backend.hdencode_shadow import compare_shadow
                    normal = {
                        result.get("feed"): result.get("outcome")
                        for result in rss_cycle.get("feeds", [])
                        if result.get("feed") in {"movies_all", "tv_all"}
                    }
                    metrics = compare_shadow(
                        rss_urls=rss_cycle.get("candidate_urls", []),
                        listing_items=items,
                        rss_requests=rss_cycle.get("requests", 0),
                        listing_requests=getattr(
                            scanner, "_last_crawl_request_count", source_pages
                        ),
                        normal_feeds_complete=self._rss_normal_feeds_complete(
                            rss_cycle.get("feeds", []),
                            # NOT ``err``: run_scan swallows every exception,
                            # so a blocked/failed listing crawl arrives here
                            # with err=None. See _listing_arm_incomplete.
                            listing_error=self._listing_arm_incomplete(
                                scanner, err=err
                            ),
                        ),
                    ).as_dict()
                    # Provenance for the decision record. record_hdencode_shadow_
                    # comparison json-dumps everything it does not map to a
                    # column into details_json, so a later reader can see WHY a
                    # cycle was or was not counted instead of having to trust
                    # normal_feeds_complete alone. Not a ShadowComparison field:
                    # that dataclass is frozen and shared with the RSS package.
                    metrics["listing_completion_reason"] = getattr(
                        scanner, "_last_crawl_completion_reason", None
                    )
                    completed_at = datetime.now(timezone.utc).isoformat()
                    restart_recovery = self._qualify_restart_recovery(
                        preexisting_normal_feed_state=(
                            preexisting_normal_feed_state
                        ),
                        metrics=metrics,
                    )
                    db.record_hdencode_shadow_comparison(
                        cycle_uuid=str(uuid.uuid4()),
                        started_at=completed_at,
                        completed_at=completed_at,
                        metrics=metrics,
                        catchup_used=rss_cycle.get("catchup_used", False),
                        restart_recovery=restart_recovery,
                    )
                    rss_cycle["restart_recovery"] = restart_recovery
                    rss_cycle["comparison"] = metrics

                rows = self._to_cache_rows(items, source)
                if rows:
                    db.upsert_background_cache(rows)
                    total += len(rows)
                source_results.append({"source": source, "new": len(rows), "error": err})

            if not self._owns_lifespan():
                logger.info("Background scan abandoned before cache re-match: stale app lifespan")
                return {
                    "scanned": 0, "cached": 0, "skipped": True,
                    "reason": "stale_lifespan",
                }

            # Refresh library/downloaded status across the WHOLE cache (cheap —
            # no re-scraping) so already-cached items reflect the current Plex
            # library and recent grabs, not just their state when first scanned.
            rematched = 0
            try:
                rematched = scanner.rematch_cache()
            except Exception:
                logger.exception("Cache re-match failed")

            # Only purge after a FULL crawl. An early-stopped crawl never visited
            # deeper pages, so its seen-set is partial and last_seen wasn't
            # refreshed for still-listed items further down — purging now would
            # age out releases that are still on the site.
            if not purge_safe:
                logger.info(
                    "Background scan: a source was disabled or stopped early; "
                    "skipping cache purge this run"
                )
            else:
                try:
                    retain = max(1, int(cfg.get("background_scan_retain_days", 7)))
                except (TypeError, ValueError):
                    retain = 7
                db.purge_background_cache(retain)
            try:
                reg.config["background_scan_last_run"] = time.time()
                if reg.backend:
                    reg.backend.save_config()
            except Exception:
                logger.warning("Failed to stamp background_scan_last_run")
        finally:
            self._running.clear()
            scanner.release_scan()

        if not self._owns_lifespan():
            logger.info("Background scan abandoned before completion publish: stale app lifespan")
            return {
                "scanned": 0, "cached": 0, "skipped": True,
                "reason": "stale_lifespan",
            }

        cached = db.count_background_cache()
        self._last_run = {
            "at": time.time(),
            "new": total,
            "cached": cached,
            "rematched": rematched,
            "sources": source_results,
            "rss": rss_cycle,
        }
        logger.info(
            "Background scan complete: %d new/updated from %d source(s), %d cached",
            total, len(sources), cached)
        return {"scanned": total, "cached": cached, "sources": list(sources)}

    def _category_flags(self) -> dict:
        """Which categories to pre-cache. Defaults to ALL so the UI's instant
        4K/Remux/TV filters always have every category cached; an operator on a
        tight scrape/TMDB budget can set ``background_scan_categories`` to a
        subset (e.g. ``["4k"]``) to cut volume. An empty/all-false set falls
        back to ALL rather than scanning nothing."""
        wanted = (self._reg.config or {}).get("background_scan_categories")
        if not wanted:
            return dict(_ALL_CATEGORY_FLAGS)
        keep = {str(w).lower() for w in wanted}
        flags = {k: (k in keep) for k in _ALL_CATEGORY_FLAGS}
        return flags if any(flags.values()) else dict(_ALL_CATEGORY_FLAGS)

    def _scan_source(self, source: str, pages: int,
                     skip_urls: Optional[set] = None) -> List[Any]:
        """Run a single source's scan and return its MediaItems.

        Raises on hard failure so the caller can record a per-source error.
        Uses ``track_urls=False`` so it never disturbs the incremental URL
        history the scheduler relies on, ``skip_urls`` to avoid re-scraping
        already-cached posts, and ``early_stop`` to stop at the prior endpoint.
        """
        from backend.api.routes.scanner import _SOURCE_NAME_MAP, _SCAN_TYPE_MAP
        source_type = _SOURCE_NAME_MAP.get(str(source).lower(), source)
        items = self._reg.scanner.run_scan(
            scan_type=_SCAN_TYPE_MAP.get("deep", "Deep Scan"),
            source_type=source_type,
            pages=pages,
            resolution_flags=self._category_flags(),
            search_query="",
            track_urls=False,
            skip_urls=skip_urls,
            early_stop=True,
        )
        return list(items) if items else []

    def _to_cache_rows(self, items, source: str) -> List[Dict[str, Any]]:
        """Serialize MediaItems to cache rows (full dict stored as JSON)."""
        from backend.api.routes.scanner import _media_item_to_dict
        rows = []
        for it in items:
            d = _media_item_to_dict(it)
            url = d.get("url")
            if not url:
                continue
            rows.append({
                "url": url,
                "title": d.get("title"),
                "year": d.get("year"),
                "status": str(d.get("status", "")),
                "source_category": source,
                "data": json.dumps(d, default=str),
            })
        return rows
