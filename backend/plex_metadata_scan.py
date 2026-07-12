"""Bulk technical-metadata scan across the existing Plex movie library.

Populates probe_specs() (and, for Dolby Vision files, the FEL/MEL layer via
dv_detect) for every targeted file path -- using the SAME caches
(media_probe, dv_scan) the reactive duplicate-comparison path already
relies on, so a re-run (e.g. after cancel) only does new work for files
whose cache signature has gone stale. Movies only.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Optional

from backend.rename import dv_detect, mediainfo

logger = logging.getLogger(__name__)

_MAX_CONCURRENCY = 2


class PlexMetadataScanJob:
    """Tracks and runs one bulk Plex-library metadata scan. Intended to be a
    single long-lived instance (held on ServiceRegistry) so status/cancel
    endpoints always observe the current run; a new start() call is rejected
    while a previous run is still "running"."""

    def __init__(self, db, progress_cb: Optional[Callable[[dict], None]] = None):
        self._db = db
        self._progress_cb = progress_cb
        self._lock = threading.Lock()
        self._stop_flag = False
        self.status = "idle"
        self.processed = 0
        self.total = 0
        self.current_files: list[str] = []
        self.started_at: Optional[float] = None
        self.error: Optional[str] = None

    def is_running(self) -> bool:
        with self._lock:
            return self.status == "running"

    def start(self, targets: list[dict]) -> bool:
        with self._lock:
            if self.status == "running":
                return False
            self._stop_flag = False
            self.status = "running"
            self.processed = 0
            self.total = len(targets)
            self.current_files = []
            self.started_at = time.time()
            self.error = None
        self._emit()
        threading.Thread(
            target=self._run, args=(targets,),
            name="plex-metadata-scan", daemon=True).start()
        return True

    def cancel(self) -> None:
        with self._lock:
            self._stop_flag = True

    def status_dict(self) -> dict:
        with self._lock:
            elapsed = (time.time() - self.started_at) if self.started_at else 0.0
            rate = (self.processed / elapsed) if elapsed > 0 and self.processed else 0.0
            remaining = max(self.total - self.processed, 0)
            eta = (remaining / rate) if rate > 0 else None
            return {
                "status": self.status,
                "processed": self.processed,
                "total": self.total,
                "current_files": list(self.current_files),
                "elapsed_seconds": round(elapsed, 1),
                "eta_seconds": round(eta, 1) if eta is not None else None,
                "error": self.error,
            }

    def _emit(self) -> None:
        if not self._progress_cb:
            return
        try:
            self._progress_cb(self.status_dict())
        except Exception:
            logger.exception("plex-metadata-scan progress callback failed")

    def _run(self, targets: list[dict]) -> None:
        try:
            with ThreadPoolExecutor(max_workers=_MAX_CONCURRENCY) as pool:
                futures = []
                for item in targets:
                    with self._lock:
                        if self._stop_flag:
                            break
                    futures.append(pool.submit(self._process_one_tracked, item))
                for fut in futures:
                    fut.result()
            with self._lock:
                self.status = "cancelled" if self._stop_flag else "done"
        except Exception as e:
            logger.exception("plex-metadata-scan failed")
            with self._lock:
                self.status = "error"
                self.error = str(e)
        self._emit()

    def _process_one_tracked(self, item: dict) -> None:
        label = item.get("title") or item.get("path") or "?"
        with self._lock:
            if self._stop_flag:
                return
            self.current_files.append(label)
        self._emit()
        try:
            self._process_one(item.get("path"))
        finally:
            with self._lock:
                self.processed += 1
                if label in self.current_files:
                    self.current_files.remove(label)
            self._emit()

    def _process_one(self, path: Optional[str]) -> None:
        """Probe one file: fast fields always; DV FEL/MEL layer additionally
        when the fast probe reports Dolby Vision and the dv_scan cache for
        this file is stale. Every failure is logged and swallowed -- one bad
        file must never abort the batch."""
        if not path:
            return
        try:
            specs = mediainfo.probe_specs(path, db=self._db)
        except Exception:
            logger.exception("probe_specs failed for %s", path)
            return
        if not specs or not specs.get("present"):
            return
        if specs.get("hdr") == "Dolby Vision":
            self._scan_dv_layer(path)

    def _scan_dv_layer(self, path: str) -> None:
        try:
            st = os.stat(path)
        except OSError:
            return
        try:
            if self._db.dv_scan_is_current(path, st.st_mtime, st.st_size):
                return
        except Exception:
            logger.exception("dv_scan_is_current check failed for %s", path)
            return
        try:
            result = dv_detect.detect_layer(path)
        except Exception:
            logger.exception("detect_layer failed for %s", path)
            return
        try:
            self._db.upsert_dv_scan(
                path=path, dv_layer=result.get("layer"),
                sig_mtime=st.st_mtime, sig_size=st.st_size, source="metadata-scan")
        except Exception:
            logger.exception("upsert_dv_scan failed for %s", path)
