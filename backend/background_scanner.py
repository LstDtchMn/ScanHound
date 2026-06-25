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
import threading
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_DEFAULT_SOURCES = ["HDEncode", "DDLBase", "Adit-HD"]


class BackgroundScanner:
    """Runs periodic pre-cache scans on a daemon thread."""

    def __init__(self, registry):
        self._reg = registry
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._running = threading.Event()  # a scan is currently executing
        self._lock = threading.Lock()

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
        """Signal the loop to stop and wait briefly for it to exit."""
        self._stop.set()
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

    def next_run_at(self) -> Optional[float]:
        """Epoch timestamp of the next scheduled run, or None if disabled."""
        cfg = self._reg.config or {}
        if not cfg.get("background_scan_enabled"):
            return None
        last = cfg.get("background_scan_last_run") or 0
        base = last or time.time()
        return base + self._interval_seconds()

    # ── scheduling loop ───────────────────────────────────────────────

    def _interval_seconds(self) -> float:
        cfg = self._reg.config or {}
        try:
            hours = max(1, int(cfg.get("background_scan_interval_hours", 6)))
        except (TypeError, ValueError):
            hours = 6
        return hours * 3600.0

    def _wait_interval(self) -> bool:
        """Sleep one interval, re-reading it in short slices so a change to
        ``background_scan_interval_hours`` is honoured within ~a minute instead
        of only after the *old* (possibly hours-long) interval elapses.

        Returns True if a stop was requested during the wait.
        """
        elapsed = 0.0
        while not self._stop.is_set():
            target = self._interval_seconds()
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
            if self._wait_interval():
                return  # stop requested
            cfg = self._reg.config or {}
            if cfg.get("background_scan_enabled"):
                try:
                    self.scan_once()
                except Exception:
                    logger.exception("Background scan failed")

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
        if scanner is None or db is None:
            logger.warning("Background scan skipped: scanner/db unavailable")
            return {"scanned": 0, "cached": 0, "skipped": True}

        sources = cfg.get("background_scan_sources") or _DEFAULT_SOURCES
        try:
            pages = max(1, int(cfg.get("background_scan_pages", 3)))
        except (TypeError, ValueError):
            pages = 3

        # Atomic test-and-set so a manual /background/scan-now can't race the
        # scheduled loop (or another manual trigger) into two concurrent scans
        # interleaving writes to the cache. The endpoint's own pre-check is a
        # friendly 409; this is the actual guarantee.
        with self._lock:
            if self._running.is_set():
                logger.info("Background scan already in progress; skipping")
                return {"scanned": 0, "cached": 0, "skipped": True}
            self._running.set()
        total = 0
        try:
            for source in sources:
                rows = self._to_cache_rows(self._scan_source(source, pages), source)
                if rows:
                    db.upsert_background_cache(rows)
                    total += len(rows)
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

        cached = db.count_background_cache()
        logger.info(
            "Background scan complete: %d results from %d source(s), %d cached",
            total, len(sources), cached)
        return {"scanned": total, "cached": cached, "sources": list(sources)}

    def _scan_source(self, source: str, pages: int) -> List[Any]:
        """Run the normal scan for a single source. Returns MediaItems."""
        from backend.api.routes.scanner import _SOURCE_NAME_MAP, _SCAN_TYPE_MAP
        source_type = _SOURCE_NAME_MAP.get(str(source).lower(), source)
        try:
            items = self._reg.scanner.run_scan(
                scan_type=_SCAN_TYPE_MAP.get("deep", "Deep Scan"),
                source_type=source_type,
                pages=pages,
                resolution_flags=None,
                search_query="",
            )
            return list(items) if items else []
        except Exception:
            logger.exception("Background scan of source %s failed", source)
            return []

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
