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

# The durable inventory defaults to one file at a time.  It reads but never
# writes media, yet full HDR/DV parsing can still saturate an SMB share or NAS.
_MAX_CONCURRENCY = 1


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
        self.succeeded = 0
        self.failed = 0
        self.total = 0
        self.current_files: list[str] = []
        self.started_at: Optional[float] = None
        self.error: Optional[str] = None
        self._active_run_uuid: Optional[str] = None

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
            self.succeeded = 0
            self.failed = 0
            self.total = len(targets)
            self.current_files = []
            self.started_at = time.time()
            self.error = None
        self._emit()
        threading.Thread(
            target=self._run, args=(targets,),
            name="plex-metadata-scan", daemon=True).start()
        return True

    def start_run(self, scope: str, targets: list[dict]) -> dict:
        """Start a durable, read-only inventory run.

        The complete target manifest is committed before a worker is started,
        so an interruption leaves retryable evidence instead of an in-memory
        counter that disappears at process exit.  The legacy ``start`` method
        stays available for existing API/UI callers until their routes migrate.
        """
        with self._lock:
            if self.status == "running":
                raise RuntimeError("a metadata scan is already running")
            run = self._db.create_metadata_scan_run(scope=scope, expected_count=len(targets))
            if not run:
                raise RuntimeError("could not create metadata scan run")
            created = self._db.create_metadata_scan_items(run["run_uuid"], targets)
            if created != len({item.get("path") for item in targets if item.get("path")}):
                raise RuntimeError("could not persist complete metadata scan manifest")
            if not self._db.update_metadata_scan_run(run["run_uuid"], status="running"):
                raise RuntimeError("could not mark metadata scan running")
            self._active_run_uuid = run["run_uuid"]
            self._stop_flag = False
            self.status = "running"
            self.processed = self.succeeded = self.failed = 0
            self.total = created
            self.current_files = []
            self.started_at = time.time()
            self.error = None
        self._emit()
        threading.Thread(
            target=self._run_durable, args=(run["run_uuid"],),
            name="plex-metadata-inventory", daemon=True,
        ).start()
        return self._db.get_metadata_scan_run(run["run_uuid"])

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
                "succeeded": self.succeeded,
                "failed": self.failed,
                "total": self.total,
                "current_files": list(self.current_files),
                "elapsed_seconds": round(elapsed, 1),
                "eta_seconds": round(eta, 1) if eta is not None else None,
                "error": self.error,
                "run_uuid": self._active_run_uuid,
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

    def _run_durable(self, run_uuid: str) -> None:
        """Process the persisted manifest serially and retain every outcome."""
        try:
            for item in self._db.list_metadata_scan_items(run_uuid):
                with self._lock:
                    if self._stop_flag:
                        break
                self._process_one_durable(run_uuid, item)
            with self._lock:
                cancelled = self._stop_flag
                self.status = "cancelled" if cancelled else "done"
            self._db.update_metadata_scan_run(
                run_uuid, status="cancelled" if cancelled else "completed"
            )
        except Exception as exc:
            logger.exception("durable plex metadata scan failed")
            with self._lock:
                self.status = "error"
                self.error = str(exc)
            self._db.update_metadata_scan_run(
                run_uuid, status="failed", error_code="worker_error", error_message=str(exc)
            )
        finally:
            self._emit()

    def _process_one_durable(self, run_uuid: str, item: dict) -> None:
        path = item.get("path")
        label = item.get("title") or path or "?"
        with self._lock:
            self.current_files.append(label)
        self._db.update_metadata_scan_item(run_uuid, path, status="running")
        self._emit()
        succeeded = False
        try:
            before = os.stat(path)
            specs = mediainfo.probe_detailed(path, db=self._db)
            if not specs or not specs.get("present"):
                self._db.update_metadata_scan_item(
                    run_uuid, path, status="failed", failure_stage="ffprobe",
                    error_code="probe_unavailable", error_message="No usable local-file probe result",
                )
                return
            after = os.stat(path)
            if (before.st_mtime != after.st_mtime) or (before.st_size != after.st_size):
                self._db.upsert_media_inventory({
                    **item, "path": path, "scan_state": "source_changed",
                    "sig_mtime": after.st_mtime, "sig_size": after.st_size,
                    "scan_run_uuid": run_uuid,
                })
                self._db.update_metadata_scan_item(
                    run_uuid, path, status="failed", failure_stage="restat",
                    error_code="source_changed", error_message="File changed while it was analysed",
                )
                return
            if specs.get("hdr") == "Dolby Vision":
                dv_result = dv_detect.detect_layer(path)
                if dv_result.get("layer") in {None, "unknown"} or dv_result.get("error"):
                    self._db.update_metadata_scan_item(
                        run_uuid, path, status="failed", failure_stage="dovi",
                        error_code="dv_incomplete", error_message=str(dv_result.get("error") or "unknown"),
                    )
                    return
                specs["dv_layer"] = dv_result.get("layer")
                if not self._db.upsert_dv_scan(
                    path, dv_result.get("layer"), title=item.get("title"),
                    sig_mtime=after.st_mtime, sig_size=after.st_size, source="scan",
                    rating_key=item.get("rating_key"),
                ):
                    self._db.update_metadata_scan_item(
                        run_uuid, path, status="failed", failure_stage="persist",
                        error_code="dv_cache_write_failed", error_message="Could not persist DV evidence",
                    )
                    return
            if not self._db.upsert_media_inventory({
                **item, **specs, "path": path, "scan_state": "current",
                "sig_mtime": after.st_mtime, "sig_size": after.st_size,
                "scan_run_uuid": run_uuid,
            }):
                self._db.update_metadata_scan_item(
                    run_uuid, path, status="failed", failure_stage="persist",
                    error_code="inventory_write_failed", error_message="Could not persist metadata inventory",
                )
                return
            self._db.update_metadata_scan_item(run_uuid, path, status="current")
            succeeded = True
        except OSError as exc:
            self._db.update_metadata_scan_item(
                run_uuid, path, status="failed", failure_stage="stat",
                error_code="filesystem_error", error_message=str(exc),
            )
        except Exception as exc:
            logger.exception("durable metadata probe failed for %s", path)
            self._db.update_metadata_scan_item(
                run_uuid, path, status="failed", failure_stage="probe",
                error_code="probe_exception", error_message=str(exc),
            )
        finally:
            with self._lock:
                self.processed += 1
                if succeeded:
                    self.succeeded += 1
                else:
                    self.failed += 1
                if label in self.current_files:
                    self.current_files.remove(label)
            self._emit()

    def _process_one_tracked(self, item: dict) -> None:
        label = item.get("title") or item.get("path") or "?"
        with self._lock:
            if self._stop_flag:
                return
            self.current_files.append(label)
        self._emit()
        succeeded = False
        try:
            succeeded = self._process_one(item.get("path"))
        finally:
            with self._lock:
                self.processed += 1
                if succeeded:
                    self.succeeded += 1
                else:
                    self.failed += 1
                if label in self.current_files:
                    self.current_files.remove(label)
            self._emit()

    def _process_one(self, path: Optional[str]) -> bool:
        """Probe one file: fast fields always; DV FEL/MEL layer additionally
        when the fast probe reports Dolby Vision and the dv_scan cache for
        this file is stale. Every failure is logged and swallowed -- one bad
        file must never abort the batch."""
        if not path:
            return False
        try:
            specs = mediainfo.probe_specs(path, db=self._db)
        except Exception:
            logger.exception("probe_specs failed for %s", path)
            return False
        if not specs or not specs.get("present"):
            return False
        try:
            st = os.stat(path)
            if not self._db.media_probe_is_current(
                    path, st.st_mtime, st.st_size):
                logger.warning("media probe was not persisted for %s", path)
                return False
        except Exception:
            logger.exception("media_probe persistence check failed for %s", path)
            return False
        if specs.get("hdr") == "Dolby Vision":
            return self._scan_dv_layer(path)
        return True

    def _scan_dv_layer(self, path: str) -> bool:
        try:
            st = os.stat(path)
        except OSError:
            return False
        try:
            if self._db.dv_scan_is_current(path, st.st_mtime, st.st_size):
                return True
        except Exception:
            logger.exception("dv_scan_is_current check failed for %s", path)
            return False
        try:
            result = dv_detect.detect_layer(path)
        except Exception:
            logger.exception("detect_layer failed for %s", path)
            return False
        if result.get("layer") == "unknown" or result.get("error"):
            logger.warning("DV layer scan incomplete for %s: %s",
                           path, result.get("error") or "unknown result")
            return False
        try:
            return bool(self._db.upsert_dv_scan(
                path=path, dv_layer=result.get("layer"),
                sig_mtime=st.st_mtime, sig_size=st.st_size, source="scan"))
        except Exception:
            logger.exception("upsert_dv_scan failed for %s", path)
            return False
