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

#: Distinguishes "the evidence could not be read" from "there is none yet".
#: A plain None for both let an unreadable database grade a canary a success,
#: which refreshes the protection clock on evidence nobody saw.
_UNREADABLE = object()

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
        # EFFECTIVE mode, not the persisted one (round-7 HDE-1): a stored
        # rss_primary that is not authorized runs as rss_shadow, so a config
        # value cannot bypass the route's refusal.
        from backend.rss_primary_authority import (
            effective_discovery_mode, reconcile_requested_primary,
        )
        # A durable safety finding demotes BEFORE this cycle decides anything.
        # It is asked on the REQUESTED mode, not the effective one: a runtime
        # that has already dropped to shadow would otherwise never reach the
        # demotion, and the promotion record would survive to authorize
        # primary again as soon as the blocker cleared.
        try:
            reconcile_requested_primary(cfg, db, getattr(self._reg, "backend", None))
        except Exception:  # noqa: BLE001 -- a scan must not die on bookkeeping
            logger.exception("RSS primary reconciliation failed")
        discovery_mode, _primary_authority = effective_discovery_mode(cfg, db)
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
                #: True when THIS listing crawl is the coverage canary rather
                #: than an ordinary shadow crawl or a transient fallback. It
                #: decides the comparison row's mode and whether canary
                #: scheduling state is updated below.
                canary_run = False
                source_early_stop = True
                if is_hdencode and discovery_mode == "rss_primary":
                    # `discovery_mode` is the EFFECTIVE mode, so reaching here
                    # means the runtime authority authorized primary for this
                    # cycle. Under the hybrid the listing does not stop: a
                    # reduced-frequency canary keeps running so coverage gaps
                    # stay observable, and it is the only reason the shadow
                    # comparison survives promotion.
                    canary_due = self._canary_is_due(db, cfg)
                    fallback = bool(
                        rss_cycle and rss_cycle.get("fallback_qualified")
                    )
                    if not (canary_due or fallback):
                        source_results.append({
                            "source": source,
                            "new": 0,
                            "error": None,
                            "skipped": "rss_primary",
                        })
                        continue
                    if canary_due:
                        canary_run = True
                        source_pages = self._canary_pages(cfg)
                        # NO EARLY STOP for a canary. The crawler normally
                        # stops at the first page with nothing new, which is
                        # right for discovery and wrong for evidence: the
                        # canary's claim is "these pages were observed", and a
                        # crawl that stopped early observed fewer pages than
                        # the depth its protection is calculated from.
                        source_early_stop = False
                        if fallback:
                            # Both at once: the RSS poll degraded AND the
                            # canary was due. ONE crawl serves both, at the
                            # canary's depth, which strictly covers the
                            # fallback's single page. Crawling twice would
                            # spend the requests the hybrid exists to save,
                            # and dropping the fallback flag would hide that
                            # this cycle acquired through the listing.
                            rss_cycle["listing_fallback_started"] = True
                    else:
                        source_pages = 1
                        rss_cycle["listing_fallback_started"] = True
                elif (
                    is_hdencode
                    and discovery_mode == "rss_shadow"
                    and cfg.get("hdencode_listing_membership_full_depth") is True
                ):
                    # Qualification only: the dense crawl that the virtual
                    # canary replay is measured against. Same reason as above,
                    # and it costs more requests, which is why it is a switch
                    # rather than the default.
                    source_pages = pages
                    source_early_stop = False
                else:
                    source_pages = pages
                err: Optional[str] = None
                items: List[Any] = []
                try:
                    items = self._scan_source(
                        source, source_pages, cached_urls,
                        early_stop=source_early_stop,
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

                # THE COMPARISON SURVIVES PROMOTION. Before the hybrid this
                # ran only in rss_shadow, so promoting stopped producing the
                # very evidence the readiness gate reads -- the gate opened on
                # evidence its own promoted mode destroyed. A canary crawl
                # records the same comparison, marked as its own mode.
                if (
                    is_hdencode
                    and rss_cycle
                    and (discovery_mode == "rss_shadow" or canary_run)
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
                            listing_error=err,
                        ),
                        # Per-feed provenance was already sitting in the cycle
                        # dict; the old code reduced it to a single boolean and
                        # threw the rest away. Passing it lets compare_shadow
                        # decide validity per release instead of per cycle, so a
                        # movie gap can still block when only the TV feed failed.
                        normal_feed_outcomes=normal,
                        # LISTING-ARM AUTHORITY, separate from feed health.
                        # _rss_normal_feeds_complete() folds listing_error into
                        # normal_feeds_complete, so the stored outcome cannot tell
                        # "a feed failed" from "the listing crawl failed". Miss
                        # resolution needs them apart: a movie miss may be resolved
                        # by a cycle where tv_all failed, but never by one whose
                        # listing was broken -- the listing is the other half of
                        # the comparison.
                        # LISTING AUTHORITY from the crawler's OWN verdict, not
                        # from whether run_scan threw. Peer review found `err` is
                        # not equivalent to "the listing completed": run_scan
                        # CATCHES its own exception and still returns
                        # list(self.items), _crawl_pages swallows per-page errors,
                        # and a non-200 page simply continues. Worse, this very
                        # block already distrusts a partial crawl for cache purge
                        # via _last_crawl_early_stopped while I was writing
                        # listing_complete=True from `err` alone -- one cycle could
                        # be "too partial to purge" and "listing complete" at once.
                        #
                        # Only the crawler's "complete" state counts, and an
                        # exception that escaped to here still disqualifies it.
                        # RAW LISTING MEMBERSHIP. `items` is detail-processed and
                        # drops anything whose detail scrape failed, which used to
                        # turn a real miss into apparent acquisition. This set is
                        # every listing URL the crawl actually saw.
                        raw_listing_urls=getattr(
                            scanner, "_last_crawl_seen_urls", None),
                        # Genuine attribution failures -- scheduled minus completed.
                        # Round 6: an in-scope listing-only release whose detail
                        # scrape failed must not vanish from readiness, and this is
                        # the only signal clean enough to block on (cached skips and
                        # policy exclusions are excluded by construction).
                        detail_failed_urls=(
                            scanner.last_crawl_detail_failed()
                            if hasattr(scanner, "last_crawl_detail_failed")
                            else None),
                        listing_complete=(
                            not bool(err)
                            # Keyed on the EXPLICIT termination reason. Round 6's
                            # counterexample: `if self.stop_scan_flag: break` set no
                            # boolean at all, so an externally cancelled crawl fell
                            # through to "complete" and a partial listing certified
                            # itself. Only a crawl that ran to the end qualifies.
                            and getattr(scanner, "_last_crawl_termination",
                                        "not_run") == "complete"
                        ),
                    ).as_dict()
                    completed_at = datetime.now(timezone.utc).isoformat()
                    restart_recovery = self._qualify_restart_recovery(
                        preexisting_normal_feed_state=(
                            preexisting_normal_feed_state
                        ),
                        metrics=metrics,
                    )
                    cycle_uuid = str(uuid.uuid4())
                    db.record_hdencode_shadow_comparison(
                        cycle_uuid=cycle_uuid,
                        started_at=completed_at,
                        completed_at=completed_at,
                        metrics=metrics,
                        catchup_used=rss_cycle.get("catchup_used", False),
                        restart_recovery=restart_recovery,
                        mode=("rss_primary_canary" if canary_run
                              else "rss_shadow"),
                    )
                    self._record_canary_evidence(
                        db, cfg, scanner,
                        cycle_uuid=cycle_uuid,
                        canary_run=canary_run,
                        listing_complete=bool(metrics.get("listing_complete")),
                        rss_requests=rss_cycle.get("requests", 0),
                    )
                    rss_cycle["restart_recovery"] = restart_recovery
                    rss_cycle["comparison"] = metrics
                    rss_cycle["canary_run"] = canary_run

                rows = self._to_cache_rows(items, source)
                if rows:
                    db.upsert_background_cache(rows)
                    total += len(rows)
                source_results.append({"source": source, "new": len(rows), "error": err})

                # CLASSIFICATION CONFLICTS, persisted and then RETRACTED.
                #
                # Peer review round 11. Two listings disagreeing about a release is
                # evidence that the recorded media kind is unsafe, and it has to reach
                # two places the crawl does not otherwise touch:
                #
                #   M1b  the CACHED row, which is never rewritten for a release this
                #        crawl skipped -- i.e. the entire deployed corpus
                #   M1a  the PERSISTED downloads row, because the destructive identity
                #        reads downloads.media_kind and not the cache, so refusing to
                #        record a NEW kind leaves an old one authoritative
                #
                # Retraction is deliberately a separate named operation. Routing it
                # through add_to_history(media_kind=None) would do nothing: that path
                # COALESCEs, because there None means "no observation this time".
                _conflicted = getattr(scanner, "_last_crawl_conflicted_urls", None) or set()
                # BACKFILL THE ATTESTATION for everything this crawl observed cleanly.
                # A cached row written before conflict detection has no attestation, and
                # get_scan_category refuses to answer for it -- correctly, since nothing
                # had ever checked it. Observing it now IS the check. Written only where
                # the key is absent, so this is a one-time backfill per release rather
                # than a write on every crawl.
                _seen = getattr(scanner, "_last_crawl_seen_urls", None) or set()
                _clean = _seen - (getattr(scanner, "_last_crawl_conflicted_urls", None) or set())
                if _clean:
                    try:
                        db.attest_scan_categories(_clean)
                    except Exception:
                        logger.exception("failed to attest scan categories")
                if _conflicted:
                    try:
                        db.mark_scan_category_conflict(_conflicted)
                        db.retract_download_media_kind(
                            _conflicted, reason="classification_conflict")
                    except Exception:
                        # Never let bookkeeping abort a scan. An unrecorded conflict
                        # leaves the PREVIOUS state, which was already the status quo.
                        logger.exception("failed to record classification conflicts")

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

    # ── the coverage canary ──────────────────────────────────────────
    #
    # Under the hybrid, RSS is the fast path and a reduced-frequency listing
    # crawl keeps running as an independent coverage canary. It is what makes
    # promotion safe to reverse: it keeps producing the same comparison
    # evidence after promotion, which pure rss_primary destroyed, and it is
    # the fallback acquisition path for anything RSS missed.

    def _canary_contract(self, cfg) -> Dict[str, Any]:
        from backend.rss_primary_authority import contract_inputs
        return contract_inputs(cfg)

    def _canary_sources(self, cfg) -> List[str]:
        sources = self._canary_contract(cfg).get(
            "hdencode_listing_canary_sources") or []
        return [str(s) for s in sources]

    def _canary_membership_keys(self, cfg) -> List[str]:
        """Configured canary sources as the keys the crawler actually writes.

        The contract names them by category ("4k"); membership and scheduling
        state are keyed "hdencode:4k". Both sides of that boundary now resolve
        through the same function.
        """
        from backend.rss_primary_authority import canary_source_key
        return [canary_source_key(s) for s in self._canary_sources(cfg)]

    def _canary_pages(self, cfg) -> int:
        try:
            return max(1, int(
                self._canary_contract(cfg)["hdencode_listing_canary_pages"]))
        except (TypeError, ValueError, KeyError):
            return 3

    def _canary_interval_seconds(self, cfg) -> int:
        try:
            minutes = int(
                self._canary_contract(cfg)["hdencode_listing_canary_minutes"])
        except (TypeError, ValueError, KeyError):
            minutes = 360
        return max(900, minutes * 60)

    def _canary_is_due(self, db, cfg) -> bool:
        """Is any canary source due for its crawl?

        UNREADABLE STATE COUNTS AS DUE, deliberately. Running one extra canary
        costs a handful of requests; skipping one because the state could not
        be read lets the protection clock age toward staleness while the
        system still calls itself canary-protected, which is the failure this
        whole mechanism exists to prevent.
        """
        from datetime import datetime, timezone
        states = None
        if hasattr(db, "list_canary_states"):
            try:
                states = db.list_canary_states()
            except Exception:  # noqa: BLE001 -- unreadable is due, never an error here
                logger.warning("canary state unreadable; treating the canary as due")
                states = None
        if states is None:
            return True
        by_key = {str(s.get("source_key")): s for s in states}
        now = datetime.now(timezone.utc)
        for key in self._canary_membership_keys(cfg) or [""]:
            state = by_key.get(key)
            if not state or not state.get("next_attempt_at"):
                return True
            try:
                due = datetime.fromisoformat(str(state["next_attempt_at"]))
                if due.tzinfo is None:
                    due = due.replace(tzinfo=timezone.utc)
            except (TypeError, ValueError):
                return True
            if now >= due.astimezone(timezone.utc):
                return True
        return False

    def _record_canary_evidence(self, db, cfg, scanner, *, cycle_uuid,
                                canary_run, listing_complete, rss_requests):
        """Persist what this crawl observed, and what it cost.

        Membership is per SOURCE and per cycle, and the crawler collected it
        before its own global dedup, so a release listed under two categories
        is recorded under both. The request ledger is kept separate from the
        comparison table because that table's columns are NOT NULL and two of
        its consumers read every row as a comparison.
        """
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat()
        rows = list(getattr(scanner, "_last_crawl_membership", None) or [])
        by_source: Dict[str, List[Dict[str, Any]]] = {}
        for row in rows:
            by_source.setdefault(str(row.get("source_key")), []).append(row)

        #: Sources whose durable membership write FAILED this cycle. A canary's
        #: success claim is "these pages were observed and the evidence to
        #: prove it is on disk"; if the second half did not happen, the first
        #: half must not refresh the protection clock. Without this the write
        #: failure was logged and grading carried on from the in-memory rows,
        #: advancing last_success_at while the evidence needed to detect a gap
        #: had just been lost -- protection asserted on evidence nobody kept.
        evidence_lost: set = set()
        if hasattr(db, "record_listing_membership"):
            for source_key, source_rows in by_source.items():
                try:
                    db.record_listing_membership(cycle_uuid, source_key, [
                        {"canonical_url": r.get("canonical_url"),
                         "page_index": int(r.get("page_index") or 1),
                         "rank_on_page": int(r.get("rank_on_page") or 0),
                         "rss_present": bool(r.get("rss_present")),
                         "observed_at": now}
                        for r in source_rows
                    ])
                except Exception:  # noqa: BLE001 -- evidence loss is reported, never fatal
                    evidence_lost.add(source_key)
                    logger.exception(
                        "could not record listing membership for %s", source_key)

        if hasattr(db, "record_request_batch"):
            listing_requests = int(
                getattr(scanner, "_last_crawl_request_count", 0) or 0)
            mode = "rss_primary" if canary_run else "rss_shadow"
            try:
                if rss_requests:
                    db.record_request_batch(mode, "rss_poll", int(rss_requests))
                if listing_requests and canary_run:
                    # In shadow the listing arm is qualification overhead, not
                    # hybrid cost: the projected cost of a canary cadence comes
                    # from replaying the dense evidence, not from this crawl.
                    db.record_request_batch(mode, "canary", listing_requests)
            except Exception:  # noqa: BLE001
                logger.exception("could not record canary request accounting")

        if canary_run and hasattr(db, "record_canary_attempt"):
            interval = self._canary_interval_seconds(cfg)
            from datetime import timedelta
            depth = self._canary_pages(cfg)
            # A failed canary backs off, but ONLY a success refreshes the
            # protection clock, so a source that keeps failing goes stale and
            # the authority revokes rather than calling itself protected.
            #
            # Every CONFIGURED source is graded, not only the ones this crawl
            # produced rows for. Grading the produced set left a source that
            # returned nothing -- disabled in background_scan_categories,
            # renamed, or simply failing -- with no attempt recorded at all:
            # its last outcome still read "success" from hours earlier while it
            # was observing nothing, and it stayed permanently due because its
            # next_attempt_at never moved. Silence is now recorded as a
            # failure, with a reason, which is what it is.
            for source_key in (self._canary_membership_keys(cfg) or [""]):
                source_rows = by_source.get(source_key, [])
                try:
                    if source_key in evidence_lost:
                        # The crawl may have been perfect; the evidence for it
                        # is not on disk, so this cycle proves nothing that can
                        # be re-read, and a success here would refresh the
                        # protection clock on evidence that was just lost.
                        outcome, reason = "error", "membership_write_failed"
                    elif listing_complete and not source_rows:
                        # Only when the crawl finished. An unfinished crawl
                        # explains its own emptiness, and _grade_canary already
                        # reports that as listing_incomplete.
                        outcome, reason = "error", "no_membership_recorded"
                    else:
                        outcome, reason = self._grade_canary(
                            db, source_key, source_rows,
                            cycle_uuid=cycle_uuid,
                            listing_complete=listing_complete,
                            depth=depth,
                        )
                    state = (db.get_canary_state(source_key)
                             if hasattr(db, "get_canary_state") else None) or {}
                    failures = int(state.get("consecutive_failures") or 0)
                    delay = interval if outcome == "success" else min(
                        interval, 900 * (2 ** min(failures, 6)))
                    db.record_canary_attempt(
                        source_key,
                        at=now,
                        next_attempt_at=(
                            datetime.now(timezone.utc)
                            + timedelta(seconds=delay)).isoformat(),
                        outcome=outcome,
                        reason=reason,
                    )
                except Exception:  # noqa: BLE001
                    logger.exception(
                        "could not record the canary attempt for %s", source_key)

    def _grade_canary(self, db, source_key, source_rows, *, cycle_uuid,
                      listing_complete, depth):
        """Decide what this canary crawl proved, and record its overlap.

        Three things can be wrong with a crawl that technically finished:

        * it did not complete, so it never traversed the depth it claims;
        * it shares NO url with the previous canary, so posts may have paged
          off between the two entirely unseen. Overlap is a NEGATIVE signal
          only: seeing an old release proves the window did not slide past,
          while zero overlap proves nothing except that it might have;
        * more urls are new than half the window can hold, so the source is
          churning faster than this cadence can watch it.

        None of those refreshes the protection clock, because none of them
        protected anything. The counter for consecutive overlap losses is
        durable, since two in a row is a revocation trigger and a trigger held
        only in memory would be forgotten by the restart that follows a crash.
        """
        from backend import rss_canary_policy as policy

        if not listing_complete:
            return "incomplete", "listing_incomplete"

        previous = self._previous_canary_rows(db, source_key, cycle_uuid)
        if previous is _UNREADABLE:
            # NOT the same as having no predecessor. A read that failed tells
            # us nothing about overlap, and calling that a success would
            # refresh the protection clock on the strength of evidence we
            # could not see -- the fail-open shape this whole feature exists
            # to avoid. It is an error: it backs off and leaves the clock
            # where it was, so a persistent outage ages into canary_stale.
            return "error", "previous_membership_unreadable"
        if previous is None:
            # The first canary after promotion legitimately has no
            # predecessor; it is a success on its own terms.
            return "success", None

        current = [{"canonical_url": r.get("canonical_url"),
                    "page_index": int(r.get("page_index") or 1)}
                   for r in source_rows]
        shared = policy.overlap(previous, current, depth)
        if hasattr(db, "record_overlap_loss"):
            db.record_overlap_loss(source_key, lost=(shared == 0))
        if shared == 0:
            return "overlap_lost", "no url shared with the previous canary"

        ranks = [int(r.get("rank_on_page") or 0) for r in source_rows]
        per_page = (max(ranks) + 1) if ranks else 0
        capacity = depth * per_page
        new_urls = policy.churn(previous, current, depth)
        if capacity and new_urls > capacity / 2:
            return "incomplete", "visibility_margin_lost"
        return "success", None

    def _previous_canary_rows(self, db, source_key, cycle_uuid):
        """Membership from this source's most recent EARLIER cycle.

        Three answers, deliberately distinct:

        * ``_UNREADABLE`` -- the evidence could not be read. Says nothing
          about overlap, and must never be graded as a clean comparison.
        * ``None`` -- read fine, there is no earlier cycle. The first canary
          after promotion is legitimately in this position.
        * a list -- the previous cycle's rows.

        Folding the first into either of the others is how an outage becomes
        an apparent success, which is why the reader below is tri-state.
        """
        if not hasattr(db, "list_listing_membership"):
            return None
        rows = db.list_listing_membership(source_key=source_key)
        if rows is None:
            return _UNREADABLE
        if not rows:
            return None
        earlier = [r for r in rows if r.get("cycle_uuid") != cycle_uuid]
        if not earlier:
            return None
        newest = max(str(r.get("observed_at") or "") for r in earlier)
        latest_cycle = next(
            (r.get("cycle_uuid") for r in earlier
             if str(r.get("observed_at") or "") == newest), None)
        return [{"canonical_url": r.get("canonical_url"),
                 "page_index": int(r.get("page_index") or 1)}
                for r in earlier if r.get("cycle_uuid") == latest_cycle]

    def _scan_source(self, source: str, pages: int,
                     skip_urls: Optional[set] = None,
                     *, early_stop: bool = True) -> List[Any]:
        """Run a single source's scan and return its MediaItems.

        Raises on hard failure so the caller can record a per-source error.
        Uses ``track_urls=False`` so it never disturbs the incremental URL
        history the scheduler relies on, ``skip_urls`` to avoid re-scraping
        already-cached posts, and ``early_stop`` to stop at the prior endpoint.

        ``early_stop`` is a PARAMETER since 2026-09-06, and was hard-coded
        True before. Discovery is right to stop at the first page with nothing
        new; evidence is not. A canary claims "these pages were observed", and
        a crawl that stopped early observed fewer pages than the depth its
        protection is calculated from, so the coverage claim would be wider
        than the crawl behind it.
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
            early_stop=early_stop,
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
