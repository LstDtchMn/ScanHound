# Plex Library Metadata Scan Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A bulk, resumable background job that populates rich technical metadata (`probe_specs()`, plus DV FEL/MEL layer for every DV-flagged file) across the existing Plex movie library, on demand — "scan all" or a user-selected subset — with the full heavy scan (including `dovi_tool` FEL/MEL) running by default.

**Architecture:** A new `PlexMetadataScanJob` class (`backend/plex_metadata_scan.py`) owns all job state (status/processed/total/current-file/ETA) and runs the scan on a background thread using a bounded `ThreadPoolExecutor(max_workers=2)` so at most 2 files run the expensive `dovi_tool` step concurrently. One instance lives on `ServiceRegistry` for the app's lifetime so status/cancel endpoints always see the current run. Per file: `mediainfo.probe_specs()` (fast, already cached by mtime/size) always; `dv_detect.detect_layer()` (slow, already cached via `dv_scan_is_current`/`upsert_dv_scan`) only when the fast probe reports `hdr == "Dolby Vision"` and the `dv_scan` cache is stale — this is what makes cancel+restart a true resume, not a re-scan. New FastAPI routes in `backend/api/routes/plex.py` start/cancel/status the job and broadcast progress over the existing `ws_manager`. Frontend: a panel on the Settings page with Scan-all / Scan-selected / Cancel controls and a live progress bar fed by the WS `plex:metadata_scan_progress` event.

**Tech Stack:** Python (`threading`, `concurrent.futures.ThreadPoolExecutor`), pytest; SvelteKit 5 (runes), vitest.

## Global Constraints

- **Movies only.** Every target file comes from `db.list_plex_cache_movies()` — no TV episode-level scanning in this cut (confirmed scope, matches `find_library_duplicate`'s existing movies-only precedent).
- **Full heavy scan (including DV FEL/MEL) runs by default** — not gated behind an opt-in flag. This is a deliberate reversal from an earlier draft of the design; do not add a "fast metadata only" mode unless a later task asks for it.
- **Resume = re-run.** Cancelling and re-starting simply re-walks the same target set; `media_probe_is_current()`/`dv_scan_is_current()` make every already-current file a fast no-op. Do not build separate persisted "resume point" state — the existing caches already provide this.
- **Bounded concurrency of 2** for the per-file pipeline (which includes the DV step) — never unbounded parallel `dovi_tool` calls (disk-I/O-heavy, would thrash the disk).
- **One bad file never aborts the batch** — every per-file probe/detect call is wrapped so a single failure is logged and skipped, not raised.
- **Reuses existing tables/caches** — `media_probe` (via `mediainfo.probe_specs(path, db=db)`) and `dv_scan` (via `db.dv_scan_is_current`/`db.upsert_dv_scan`). Do NOT create new cache tables; this feature only populates the existing ones.
- **Does not touch** the existing reactive `needs_dv_layer_scan()` gate in `backend/rename/conflicts.py` — that stays exactly as-is for its own comparison-time purpose.
- Backend tests: throwaway container pattern (`docker run -d --name <c> --entrypoint sleep scanhound:latest infinity`, `docker cp backend/. tests/. <c>:/app/...`, `pip install -q pytest httpx`, run, `docker rm -f`). Frontend tests: host node (`cd frontend && npm run check && npm run build && npx vitest run`).
- Work directly on `main`. Commit only when genuinely green.
- **Smart/curly-quote hazard**: never emit U+201C/U+201D/U+2018/U+2019 in Svelte/JS/Python source — plain ASCII quotes only. Grep new/changed files for these before committing.

---

### Task 1: `PlexMetadataScanJob` — job state + bounded-concurrency scan engine

**Files:**
- Create: `backend/plex_metadata_scan.py`
- Test: `tests/test_plex_metadata_scan.py`

**Interfaces:**
- Consumes: `mediainfo.probe_specs(path, db=db) -> dict | None` (existing, `backend/rename/mediainfo.py`), `dv_detect.detect_layer(path) -> {"layer": str, "tool": bool, "error": str|None}` (existing, `backend/rename/dv_detect.py`), `db.dv_scan_is_current(path, sig_mtime, sig_size) -> bool`, `db.upsert_dv_scan(path, dv_layer, *, sig_mtime=None, sig_size=None, source="scan", rating_key=None, imdb_id=None) -> bool` (existing, `backend/database.py`).
- Produces: `PlexMetadataScanJob(db, progress_cb=None)` with methods `start(targets: list[dict]) -> bool` (targets are dicts with at least `path`, optionally `title`/`rating_key`/`imdb_id`; returns `False` if already running, `True` once the background thread is launched), `cancel() -> None` (sets a stop flag; the running scan finishes its in-flight files then stops picking up new ones), `status_dict() -> dict` (keys: `status` one of `"idle"|"running"|"cancelled"|"done"|"error"`, `processed: int`, `total: int`, `current_files: list[str]`, `elapsed_seconds: float`, `eta_seconds: float | None`, `error: str | None`). `progress_cb`, if given, is called with the same dict returned by `status_dict()` after every state change (used by Task 2 to broadcast over the websocket).

- [ ] **Step 1: Write the failing tests**

Create `tests/test_plex_metadata_scan.py`:

```python
import os
import time
from unittest.mock import MagicMock, patch

import pytest

from backend.plex_metadata_scan import PlexMetadataScanJob


def _fake_db():
    db = MagicMock()
    db.dv_scan_is_current.return_value = False
    db.upsert_dv_scan.return_value = True
    return db


def _wait_until_done(job, timeout=5.0):
    start = time.time()
    while job.status_dict()["status"] == "running":
        if time.time() - start > timeout:
            raise AssertionError("job never finished")
        time.sleep(0.01)


def test_idle_status_before_start():
    job = PlexMetadataScanJob(_fake_db())
    s = job.status_dict()
    assert s["status"] == "idle"
    assert s["processed"] == 0
    assert s["total"] == 0


def test_start_processes_every_target_and_reaches_done(tmp_path):
    db = _fake_db()
    files = []
    for i in range(3):
        f = tmp_path / f"movie{i}.mkv"
        f.write_bytes(b"x")
        files.append(str(f))
    targets = [{"path": p, "title": f"Movie {i}"} for i, p in enumerate(files)]

    with patch("backend.plex_metadata_scan.mediainfo.probe_specs",
               return_value={"present": True, "hdr": "HDR10"}):
        job = PlexMetadataScanJob(db)
        assert job.start(targets) is True
        _wait_until_done(job)

    s = job.status_dict()
    assert s["status"] == "done"
    assert s["processed"] == 3
    assert s["total"] == 3


def test_dolby_vision_file_triggers_dv_layer_detection(tmp_path):
    db = _fake_db()
    f = tmp_path / "dv_movie.mkv"
    f.write_bytes(b"x")
    targets = [{"path": str(f), "title": "DV Movie"}]

    with patch("backend.plex_metadata_scan.mediainfo.probe_specs",
               return_value={"present": True, "hdr": "Dolby Vision"}), \
         patch("backend.plex_metadata_scan.dv_detect.detect_layer",
               return_value={"layer": "fel", "tool": True, "error": None}) as mock_detect:
        job = PlexMetadataScanJob(db)
        job.start(targets)
        _wait_until_done(job)

    mock_detect.assert_called_once_with(str(f))
    db.upsert_dv_scan.assert_called_once()
    assert db.upsert_dv_scan.call_args.kwargs["dv_layer"] == "fel"


def test_non_dolby_vision_file_skips_dv_layer_detection(tmp_path):
    db = _fake_db()
    f = tmp_path / "hdr10_movie.mkv"
    f.write_bytes(b"x")
    targets = [{"path": str(f), "title": "HDR10 Movie"}]

    with patch("backend.plex_metadata_scan.mediainfo.probe_specs",
               return_value={"present": True, "hdr": "HDR10"}), \
         patch("backend.plex_metadata_scan.dv_detect.detect_layer") as mock_detect:
        job = PlexMetadataScanJob(db)
        job.start(targets)
        _wait_until_done(job)

    mock_detect.assert_not_called()
    db.upsert_dv_scan.assert_not_called()


def test_dv_layer_detection_skipped_when_cache_already_current(tmp_path):
    db = _fake_db()
    db.dv_scan_is_current.return_value = True
    f = tmp_path / "dv_movie.mkv"
    f.write_bytes(b"x")
    targets = [{"path": str(f), "title": "DV Movie"}]

    with patch("backend.plex_metadata_scan.mediainfo.probe_specs",
               return_value={"present": True, "hdr": "Dolby Vision"}), \
         patch("backend.plex_metadata_scan.dv_detect.detect_layer") as mock_detect:
        job = PlexMetadataScanJob(db)
        job.start(targets)
        _wait_until_done(job)

    mock_detect.assert_not_called()


def test_one_bad_file_does_not_abort_the_batch(tmp_path):
    db = _fake_db()
    good = tmp_path / "good.mkv"; good.write_bytes(b"x")
    bad = tmp_path / "bad.mkv"; bad.write_bytes(b"x")
    targets = [{"path": str(bad), "title": "Bad"}, {"path": str(good), "title": "Good"}]

    def _probe(path, db=None):
        if "bad" in path:
            raise RuntimeError("ffprobe exploded")
        return {"present": True, "hdr": "HDR10"}

    with patch("backend.plex_metadata_scan.mediainfo.probe_specs", side_effect=_probe):
        job = PlexMetadataScanJob(db)
        job.start(targets)
        _wait_until_done(job)

    s = job.status_dict()
    assert s["status"] == "done"
    assert s["processed"] == 2


def test_cancel_stops_the_job():
    db = _fake_db()
    targets = [{"path": f"/fake/movie{i}.mkv"} for i in range(50)]

    def _slow_probe(path, db=None):
        time.sleep(0.05)
        return {"present": True, "hdr": None}

    with patch("backend.plex_metadata_scan.mediainfo.probe_specs", side_effect=_slow_probe):
        job = PlexMetadataScanJob(db)
        job.start(targets)
        time.sleep(0.05)
        job.cancel()
        _wait_until_done(job, timeout=10.0)

    s = job.status_dict()
    assert s["status"] == "cancelled"
    assert s["processed"] < 50


def test_start_returns_false_when_already_running(tmp_path):
    f = tmp_path / "movie.mkv"; f.write_bytes(b"x")
    targets = [{"path": str(f)}]

    def _slow_probe(path, db=None):
        time.sleep(0.2)
        return {"present": True, "hdr": None}

    with patch("backend.plex_metadata_scan.mediainfo.probe_specs", side_effect=_slow_probe):
        job = PlexMetadataScanJob(_fake_db())
        assert job.start(targets) is True
        assert job.start(targets) is False
        _wait_until_done(job, timeout=5.0)


def test_progress_callback_invoked_on_state_changes(tmp_path):
    f = tmp_path / "movie.mkv"; f.write_bytes(b"x")
    targets = [{"path": str(f), "title": "M"}]
    seen_statuses = []

    def _cb(status_dict):
        seen_statuses.append(status_dict["status"])

    with patch("backend.plex_metadata_scan.mediainfo.probe_specs",
               return_value={"present": True, "hdr": None}):
        job = PlexMetadataScanJob(_fake_db(), progress_cb=_cb)
        job.start(targets)
        _wait_until_done(job)

    assert "running" in seen_statuses
    assert "done" in seen_statuses


def test_eta_is_none_before_any_progress():
    job = PlexMetadataScanJob(_fake_db())
    s = job.status_dict()
    assert s["eta_seconds"] is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run (inside the throwaway container, per Global Constraints): `pytest tests/test_plex_metadata_scan.py -v`
Expected: FAIL / collection error — `backend.plex_metadata_scan` does not exist yet.

- [ ] **Step 3: Write the implementation**

Create `backend/plex_metadata_scan.py`:

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_plex_metadata_scan.py -v`
Expected: PASS, all 10 tests green.

- [ ] **Step 5: Commit**

```bash
git add backend/plex_metadata_scan.py tests/test_plex_metadata_scan.py
git commit -m "feat(plex): bulk library metadata-scan job engine (bounded concurrency, resumable)"
```

---

### Task 2: API routes — start/cancel/status + WS progress broadcast

**Files:**
- Modify: `backend/api/routes/plex.py`
- Modify: `backend/api/dependencies.py`
- Test: `tests/test_plex_routes.py` (create if it does not already exist — check first)

**Interfaces:**
- Consumes: `PlexMetadataScanJob` from Task 1 (`backend/plex_metadata_scan.py`). `reg.db` (existing `ServiceRegistry.db`, `backend/api/dependencies.py`). `db.list_plex_cache_movies() -> list[dict]` (existing, `backend/database.py:869`, each dict has `key, title, original_title, year, res, size, imdb_id, rating_key, media_id, is_tv, dovi, hdr, file_path`). `ws_manager.broadcast_sync(dict) -> None` (existing, `backend/api/ws.py`).
- Produces: `ServiceRegistry.plex_metadata_scan_job -> PlexMetadataScanJob` (a new field, lazily constructed once, reused across requests). `POST /plex/scan-metadata` (body `{"scope": "all" | "selected", "ids": list[str] | None}` where `ids` are `plex_cache.key` values, required when `scope == "selected"`), `POST /plex/scan-metadata/cancel`, `GET /plex/scan-metadata/status` — all under the existing `router = APIRouter(prefix="/plex", ...)` in `backend/api/routes/plex.py`. WS event `{"type": "plex:metadata_scan_progress", "data": <status_dict>}` broadcast on every job state change.

- [ ] **Step 1: Write the failing tests**

First check whether `tests/test_plex_routes.py` already exists (`ls tests/ | grep plex`); if it does, read it to match its existing fixture/client setup and append these tests instead of duplicating fixtures. If it does not exist, create it following the fixture pattern used by `tests/test_rename_routes.py` (a FastAPI `TestClient` built from the app with a mocked/real `ServiceRegistry`) — read that file first for the exact fixture shape used in this codebase before writing these:

```python
def test_scan_metadata_all_starts_job(client, monkeypatch):
    from backend.api.routes import plex as plex_routes
    monkeypatch.setattr(
        plex_routes, "_movie_targets_for_scope",
        lambda reg, scope, ids: [{"path": "/x/movie.mkv", "title": "Movie"}])
    resp = client.post("/plex/scan-metadata", json={"scope": "all"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "starting"


def test_scan_metadata_selected_requires_ids(client):
    resp = client.post("/plex/scan-metadata", json={"scope": "selected"})
    assert resp.status_code == 400


def test_scan_metadata_status_reports_idle_before_any_scan(client):
    resp = client.get("/plex/scan-metadata/status")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] in ("idle", "done", "cancelled")


def test_scan_metadata_cancel_is_safe_when_not_running(client):
    resp = client.post("/plex/scan-metadata/cancel")
    assert resp.status_code == 200
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_plex_routes.py -v -k scan_metadata`
Expected: FAIL — `/plex/scan-metadata` returns 404 (route does not exist yet).

- [ ] **Step 3: Write the implementation**

In `backend/api/dependencies.py`, inside the `ServiceRegistry` dataclass (near the other `_*` singleton fields around line 100-109), add:

```python
    _plex_metadata_scan_job: Any = None
```

And add a property near the existing `scanner` property (line ~117):

```python
    @property
    def plex_metadata_scan_job(self):
        if self._plex_metadata_scan_job is None:
            from backend.plex_metadata_scan import PlexMetadataScanJob
            from backend.api.ws import ws_manager

            def _broadcast(status_dict):
                ws_manager.broadcast_sync({
                    "type": "plex:metadata_scan_progress",
                    "data": status_dict,
                })

            self._plex_metadata_scan_job = PlexMetadataScanJob(self.db, progress_cb=_broadcast)
        return self._plex_metadata_scan_job
```

In `backend/api/routes/plex.py`, add near the top (after existing imports) a scope-resolution helper and the three routes. Add this helper function above the existing `@router.get("/status")`:

```python
def _movie_targets_for_scope(reg: ServiceRegistry, scope: str, ids: Optional[List[str]]) -> list:
    """Resolve a scan scope into a list of {path, title, rating_key, imdb_id}
    dicts, movies only, skipping rows with no known file_path."""
    movies = reg.db.list_plex_cache_movies() if reg.db else []
    if scope == "selected":
        wanted = set(ids or [])
        movies = [m for m in movies if m.get("key") in wanted]
    targets = []
    for m in movies:
        path = m.get("file_path")
        if not path:
            continue
        targets.append({
            "path": path,
            "title": m.get("title"),
            "rating_key": m.get("rating_key"),
            "imdb_id": m.get("imdb_id"),
        })
    return targets
```

Add these routes at the end of `backend/api/routes/plex.py`:

```python
class ScanMetadataRequest(BaseModel):
    scope: str
    ids: Optional[List[str]] = None


@router.post("/scan-metadata")
def plex_scan_metadata(
    body: ScanMetadataRequest,
    reg: ServiceRegistry = Depends(get_registry),
):
    if body.scope not in ("all", "selected"):
        raise HTTPException(status_code=400, detail="scope must be 'all' or 'selected'")
    if body.scope == "selected" and not body.ids:
        raise HTTPException(status_code=400, detail="ids required when scope is 'selected'")

    targets = _movie_targets_for_scope(reg, body.scope, body.ids)
    job = reg.plex_metadata_scan_job
    started = job.start(targets)
    if not started:
        return {"status": "already_running", **job.status_dict()}
    return {"status": "starting", "total": len(targets)}


@router.post("/scan-metadata/cancel")
def plex_scan_metadata_cancel(reg: ServiceRegistry = Depends(get_registry)):
    reg.plex_metadata_scan_job.cancel()
    return {"status": "cancelling"}


@router.get("/scan-metadata/status")
def plex_scan_metadata_status(reg: ServiceRegistry = Depends(get_registry)):
    return reg.plex_metadata_scan_job.status_dict()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_plex_routes.py -v -k scan_metadata`
Expected: PASS, all 4 tests green. Also run `pytest tests/test_plex_routes.py -v` (full file) to confirm no regression on any pre-existing tests in that file.

- [ ] **Step 5: Commit**

```bash
git add backend/api/routes/plex.py backend/api/dependencies.py tests/test_plex_routes.py
git commit -m "feat(plex): scan-metadata start/cancel/status routes + WS progress broadcast"
```

---

### Task 3: Frontend — types, API client, Settings-page scan panel

**Files:**
- Modify: `frontend/src/lib/api/types.ts`
- Modify: `frontend/src/lib/api/client.ts` (check exact filename first: `ls frontend/src/lib/api/` — this codebase's existing API client module; use its established fetch-wrapper pattern for the three new calls)
- Create: `frontend/src/lib/components/settings/PlexMetadataScanPanel.svelte`
- Modify: `frontend/src/routes/settings/+page.svelte`
- Test: `frontend/src/lib/components/settings/PlexMetadataScanPanel.test.ts` (if this codebase already has component tests for Settings-page panels, match that pattern; otherwise a plain logic test of the polling/formatting helpers is sufficient — check `frontend/src/lib` for existing `*.test.ts` component-adjacent tests before deciding)

**Interfaces:**
- Consumes: WS message `{"type": "plex:metadata_scan_progress", "data": {status, processed, total, current_files, elapsed_seconds, eta_seconds, error}}` (Task 2). Existing WS subscription mechanism already used elsewhere in the frontend for other `type`-tagged broadcasts (grep `case 'dv:scan_progress'` or similar existing WS message switch/handler before wiring a new case — follow that exact pattern, do not invent a second WS listener mechanism).
- Produces: `PlexMetadataScanPanel.svelte` — self-contained, no props required. Exported helper `formatEta(seconds: number | null): string` (e.g. `null` -> `"--"`, `65` -> `"1m 5s"`, `3661` -> `"1h 1m"`) — pure function, unit-tested, used by the panel for its ETA display.

- [ ] **Step 1: Add types**

In `frontend/src/lib/api/types.ts`, add near other scan/status-shaped interfaces:

```typescript
export interface PlexMetadataScanStatus {
	status: 'idle' | 'running' | 'cancelled' | 'done' | 'error';
	processed: number;
	total: number;
	current_files: string[];
	elapsed_seconds: number;
	eta_seconds: number | null;
	error: string | null;
}
```

- [ ] **Step 2: Add API client methods**

First run `ls frontend/src/lib/api/` to find the exact client module name and read a couple of its existing methods (e.g. the ones backing `/plex/refresh` or `/plex/connect`) to match its fetch-wrapper signature exactly. Then add three methods following that same pattern for:
- `POST /plex/scan-metadata` with body `{scope: 'all' | 'selected', ids?: string[]}`, returning `{status: string; total?: number}`
- `POST /plex/scan-metadata/cancel`, returning `{status: string}`
- `GET /plex/scan-metadata/status`, returning `PlexMetadataScanStatus`

Name them `startPlexMetadataScan(scope, ids?)`, `cancelPlexMetadataScan()`, `getPlexMetadataScanStatus()` to match the existing naming convention of other Plex-prefixed client methods in the same file (grep `Plex` in that file for the exact existing naming style and mirror it — e.g. if existing methods are `refreshPlex()`/`connectPlex()`, use that ordering convention instead).

- [ ] **Step 3: Write the failing test for the pure formatting helper**

Create `frontend/src/lib/components/settings/PlexMetadataScanPanel.test.ts`:

```typescript
import { describe, expect, it } from 'vitest';
import { formatEta } from './PlexMetadataScanPanel.svelte';

describe('formatEta', () => {
	it('returns -- for null', () => {
		expect(formatEta(null)).toBe('--');
	});

	it('formats seconds under a minute', () => {
		expect(formatEta(45)).toBe('45s');
	});

	it('formats minutes and seconds', () => {
		expect(formatEta(65)).toBe('1m 5s');
	});

	it('formats hours and minutes, dropping seconds', () => {
		expect(formatEta(3661)).toBe('1h 1m');
	});

	it('rounds down fractional seconds', () => {
		expect(formatEta(45.9)).toBe('45s');
	});
});
```

Note: exporting a plain function from a `.svelte` file's `<script module>` block is idiomatic Svelte 5 — if this codebase's existing components instead keep pure helpers in an adjacent `.ts` file (check `frontend/src/lib/components/` for that pattern first), create `frontend/src/lib/components/settings/plexMetadataScanFormat.ts` instead and import `formatEta` from there in both the component and this test — match whichever pattern the codebase already uses.

- [ ] **Step 4: Run test to verify it fails**

Run: `npx vitest run src/lib/components/settings/PlexMetadataScanPanel.test.ts`
Expected: FAIL — module/export does not exist yet.

- [ ] **Step 5: Write the component**

Create `frontend/src/lib/components/settings/PlexMetadataScanPanel.svelte` (adapt the WS-subscription and toast/notification calls to this codebase's existing established patterns — read `frontend/src/lib/stores/results.ts` or wherever the existing WS message dispatch lives, e.g. search for `plex:status` handling client-side, before wiring this in):

```svelte
<script module lang="ts">
	export function formatEta(seconds: number | null): string {
		if (seconds === null) return '--';
		const total = Math.floor(seconds);
		const h = Math.floor(total / 3600);
		const m = Math.floor((total % 3600) / 60);
		const s = total % 60;
		if (h > 0) return `${h}h ${m}m`;
		if (m > 0) return `${m}m ${s}s`;
		return `${s}s`;
	}
</script>

<script lang="ts">
	import { onMount, onDestroy } from 'svelte';
	import {
		startPlexMetadataScan,
		cancelPlexMetadataScan,
		getPlexMetadataScanStatus
	} from '$lib/api/client';
	import type { PlexMetadataScanStatus } from '$lib/api/types';

	let status = $state<PlexMetadataScanStatus>({
		status: 'idle',
		processed: 0,
		total: 0,
		current_files: [],
		elapsed_seconds: 0,
		eta_seconds: null,
		error: null
	});
	let busy = $state(false);

	const isRunning = $derived(status.status === 'running');
	const progressPct = $derived(status.total > 0 ? Math.round((status.processed / status.total) * 100) : 0);

	async function refresh() {
		status = await getPlexMetadataScanStatus();
	}

	async function scanAll() {
		busy = true;
		try {
			await startPlexMetadataScan('all');
			await refresh();
		} finally {
			busy = false;
		}
	}

	async function cancel() {
		busy = true;
		try {
			await cancelPlexMetadataScan();
			await refresh();
		} finally {
			busy = false;
		}
	}

	function handleWsMessage(event: MessageEvent) {
		try {
			const msg = JSON.parse(event.data);
			if (msg.type === 'plex:metadata_scan_progress') {
				status = msg.data;
			}
		} catch {
			/* not a JSON WS message this panel cares about */
		}
	}

	onMount(() => {
		refresh();
	});
</script>

<div class="plex-metadata-scan-panel">
	<h3>Library Metadata Scan</h3>
	<p class="hint">
		Populates resolution, audio profile, HDR/HDR10+, and Dolby Vision FEL/MEL layer data for
		every movie already in your Plex library. This is a full heavy scan and can take hours for
		large 4K/DV libraries.
	</p>

	{#if isRunning}
		<div class="progress">
			<div class="bar" style="width: {progressPct}%"></div>
		</div>
		<p>
			{status.processed} / {status.total} ({progressPct}%) - ETA {formatEta(status.eta_seconds)}
		</p>
		{#if status.current_files.length > 0}
			<p class="current">Scanning: {status.current_files.join(', ')}</p>
		{/if}
		<button onclick={cancel} disabled={busy}>Cancel</button>
	{:else}
		<button onclick={scanAll} disabled={busy}>Scan all movies</button>
		{#if status.status === 'done'}
			<p class="done">Last scan complete ({status.processed} files).</p>
		{:else if status.status === 'cancelled'}
			<p class="cancelled">Last scan cancelled at {status.processed} / {status.total}.</p>
		{:else if status.status === 'error'}
			<p class="error">Last scan failed: {status.error}</p>
		{/if}
	{/if}
</div>

<style>
	.plex-metadata-scan-panel {
		display: flex;
		flex-direction: column;
		gap: 0.5rem;
	}
	.hint {
		font-size: 0.85rem;
		opacity: 0.8;
	}
	.progress {
		height: 8px;
		border-radius: 4px;
		background: var(--surface-2, #333);
		overflow: hidden;
	}
	.bar {
		height: 100%;
		background: var(--accent, #4a9eff);
		transition: width 0.3s ease;
	}
	.current {
		font-size: 0.8rem;
		opacity: 0.7;
	}
	.error {
		color: var(--danger, #e55);
	}
</style>
```

After writing this, read the existing WS-subscription plumbing used elsewhere in the frontend (search for where `onmessage` or a shared WS store is already wired — e.g. a `wsStore`/`useWebSocket` in `frontend/src/lib`) and replace the placeholder `handleWsMessage`/manual `onMount` wiring above with whatever the established subscription mechanism actually is, so this panel receives `plex:metadata_scan_progress` events exactly the way every other feature this session (Rescan, search-fallback, season-grouping) already receives its own WS events. Do not invent a second WebSocket connection.

- [ ] **Step 6: Wire into the Settings page**

Read `frontend/src/routes/settings/+page.svelte` to find its existing section layout (likely a series of labeled `<section>`/card blocks — e.g. the Plex connection section). Add, near the existing Plex-related section:

```svelte
<script lang="ts">
	import PlexMetadataScanPanel from '$lib/components/settings/PlexMetadataScanPanel.svelte';
	// ...existing imports
</script>

<!-- near the existing Plex connection/library section -->
<section class="settings-section">
	<PlexMetadataScanPanel />
</section>
```

Match the existing section wrapper markup/class exactly (do not invent a new `settings-section` class if the file already uses a different one — copy whatever class the neighboring section uses).

- [ ] **Step 7: Run tests to verify they pass**

Run:
```bash
cd frontend
npx vitest run src/lib/components/settings/PlexMetadataScanPanel.test.ts
npm run check
npm run build
npx vitest run
```
Expected: the new test file passes (5/5), `npm run check` reports 0 errors, `npm run build` succeeds, and the full `vitest run` suite has no new failures versus its pre-task baseline.

- [ ] **Step 8: Commit**

```bash
git add frontend/src/lib/api/types.ts frontend/src/lib/api/client.ts \
        frontend/src/lib/components/settings/PlexMetadataScanPanel.svelte \
        frontend/src/lib/components/settings/PlexMetadataScanPanel.test.ts \
        frontend/src/routes/settings/+page.svelte
git commit -m "feat(settings): Plex library metadata-scan panel (scan all, live progress, cancel)"
```

(If Step 5's helper ended up in a separate `plexMetadataScanFormat.ts` file per that step's note, include it in this commit too, and update the test's import accordingly.)

---

### Task 4: Selection-based scan entry point + full verification

**Files:**
- Modify: `frontend/src/lib/components/settings/PlexMetadataScanPanel.svelte` (or wherever Task 3 landed the panel/helper)
- No new backend changes (the `scope: "selected"` route already exists from Task 2)

**Interfaces:**
- Consumes: `startPlexMetadataScan('selected', ids)` (Task 3's client method).

This task exists because Task 2's route already supports `scope: "selected"` but Task 3 only wired the "Scan all" button — this task closes that gap with the minimum viable selection UI, and then runs the mandatory full-project verification pass.

- [ ] **Step 1: Add a manual ID-entry fallback for selected-scope scanning**

There is no existing Plex-library item picker/checklist component in this codebase to reuse (confirm by searching `frontend/src/lib/components` for anything Plex-library-list-shaped before writing this — if one exists, use it instead of the textarea below and skip straight to wiring `startPlexMetadataScan('selected', ids)` against its selection state). If none exists, add a minimal textarea-based fallback to `PlexMetadataScanPanel.svelte` so the already-built backend capability is reachable, without building a full item picker (out of scope for this plan — YAGNI):

```svelte
<!-- inside the !isRunning branch, below the "Scan all movies" button -->
<details class="selected-scan">
	<summary>Scan specific titles</summary>
	<p class="hint">Paste one Plex item key per line (found via the library API or an existing conflict/compare view).</p>
	<textarea bind:value={selectedIdsRaw} rows="3" placeholder="key1&#10;key2"></textarea>
	<button onclick={scanSelected} disabled={busy || !selectedIdsRaw.trim()}>Scan these</button>
</details>
```

```typescript
// add alongside the existing script-block state in PlexMetadataScanPanel.svelte
let selectedIdsRaw = $state('');

async function scanSelected() {
	const ids = selectedIdsRaw
		.split('\n')
		.map((s) => s.trim())
		.filter(Boolean);
	if (ids.length === 0) return;
	busy = true;
	try {
		await startPlexMetadataScan('selected', ids);
		await refresh();
	} finally {
		busy = false;
	}
}
```

- [ ] **Step 2: Verify manually in the browser**

Start the dev server / container, open Settings, confirm:
- "Scan all movies" starts a run and the progress bar/ETA update live (via the WS event, not just polling).
- "Scan specific titles" with a pasted key starts a `scope: "selected"` run.
- Cancel mid-run flips status to `cancelled` and a second "Scan all movies" click afterward starts a fresh run whose already-current files complete near-instantly (proves the cache-based resume works) — pick a small movie subset for this manual check rather than the full library.

- [ ] **Step 3: Run the full verification suite**

Backend (throwaway container):
```bash
pytest tests/test_plex_metadata_scan.py tests/test_plex_routes.py -v
pytest tests/ -k "not network" --timeout=60
```
Frontend (host node):
```bash
cd frontend
npm run check
npm run build
npx vitest run
```
Expected: all green; no regressions versus the pre-Task-1 baseline. Grep all files touched across Tasks 1-4 for curly/smart quotes (`U+201C`, `U+201D`, `U+2018`, `U+2019`) and confirm zero matches.

- [ ] **Step 4: Commit**

```bash
git add frontend/src/lib/components/settings/PlexMetadataScanPanel.svelte
git commit -m "feat(settings): selected-scope scan entry point + full verification pass"
```
