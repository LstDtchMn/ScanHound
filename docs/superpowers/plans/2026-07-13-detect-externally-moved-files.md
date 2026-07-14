# Detect Externally-Moved Source Files Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Proactively detect `rename_jobs` whose source file was moved/deleted outside ScanHound (not via the app's own apply flow), mark them, and auto-archive them to declutter the Renames page.

**Architecture:** A new `RenameService.detect_moved_source_files()` method runs a two-pass confirmation state machine per eligible job (`needs_review`/`matched`, not archived) using a new `rename_jobs.source_missing_since` column — the first missing-file sighting just records a timestamp; only a SECOND consecutive maintenance pass that still finds the file missing marks the job `failed` (with a distinct, honest error message) and archives it via the existing `archive_rename_jobs()`. Two passes at the maintenance loop's real 3600s interval give ample margin against a transient NAS/SMB hiccup falsely triggering this. Hooked into the existing `_run_maintenance_pass()`, gated by a new config toggle.

**Tech Stack:** Python 3.12 / FastAPI / sqlite3 (backend), SvelteKit 5 (Settings UI checkbox only), pytest.

## Global Constraints

- Spec: `docs/superpowers/specs/2026-07-13-detect-externally-moved-files-design.md`.
- Detection scope is `status IN ('needs_review', 'matched')` only — never `applying` (in-flight/crash-recovery-handled), never `applied`/`failed`/`reverted`/already-archived.
- A job's file must be found missing on TWO separate calls to the detector before any write happens — a single miss only sets `source_missing_since`, never archives. If the file reappears before the second miss, clear `source_missing_since` back to NULL.
- On confirmation: `status='failed'`, `error_message="Source file was moved or deleted outside ScanHound"` (distinct wording from the existing apply-time "Source file missing" message at `backend/rename/service.py:1339`), then `archive_rename_jobs([job_id])`.
- New config key: `rename_detect_moved_files_enabled`, default `True`, same pattern as `pipeline_reconcile_enabled` (`backend/config.py:481`).
- Per-job errors inside the detection loop must not abort the batch (same isolation pattern as `reconcile_batch`).
- Commit after each green test cycle. Work lands on `main`.

---

### Task 1: source_missing_since column + detect_moved_source_files()

**Files:**
- Modify: `backend/database.py` (`_RENAME_FIELDS` tuple at line 2309-2319; new guarded-ALTER migration alongside the existing list around line 590-649; no `_JSON_RENAME_FIELDS` entry needed — plain TIMESTAMP)
- Modify: `backend/rename/service.py` (new method)
- Test: `tests/test_rename_service.py` (new test class)

**Interfaces:**
- Consumes: `DatabaseManager.list_rename_jobs(status, limit, archived=False)` (existing, `backend/database.py:2406`), `DatabaseManager.update_rename_job(job_id, **fields)` (existing, `backend/database.py:2387`), `DatabaseManager.archive_rename_jobs(job_ids: list) -> int` (existing, `backend/database.py:2475`).
- Produces: `RenameService.detect_moved_source_files() -> dict` with keys `checked: int`, `confirmed_missing: int` — Task 2's maintenance-loop hook calls this exactly.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_rename_service.py` (find the file's existing test-class/fixture
conventions — it already has a `RenameService` test harness used throughout the file;
follow that exact setup pattern, e.g. however existing tests construct `svc`/`db`):

```python
class TestDetectMovedSourceFiles:
    def test_first_miss_sets_timestamp_no_action(self, tmp_path, db, svc):
        missing_path = str(tmp_path / "gone.mkv")  # never created — always missing
        job_id = db.create_rename_job({
            "package_name": "Foo (2020) [1080p]", "original_path": missing_path,
            "status": "needs_review", "media_type": "movie", "title": "Foo", "year": 2020,
        })
        result = svc.detect_moved_source_files()
        job = db.get_rename_job(job_id)
        assert job["status"] == "needs_review"  # untouched
        assert job["archived_at"] is None
        assert job["source_missing_since"] is not None
        assert result == {"checked": 1, "confirmed_missing": 0}

    def test_second_consecutive_miss_confirms_and_archives(self, tmp_path, db, svc):
        missing_path = str(tmp_path / "gone2.mkv")
        job_id = db.create_rename_job({
            "package_name": "Bar (2021) [1080p]", "original_path": missing_path,
            "status": "matched", "media_type": "movie", "title": "Bar", "year": 2021,
        })
        svc.detect_moved_source_files()  # first miss — just records the timestamp
        result = svc.detect_moved_source_files()  # second miss — confirms
        job = db.get_rename_job(job_id)
        assert job["status"] == "failed"
        assert job["error_message"] == "Source file was moved or deleted outside ScanHound"
        assert job["archived_at"] is not None
        assert result == {"checked": 1, "confirmed_missing": 1}

    def test_file_reappearing_clears_the_timer(self, tmp_path, db, svc):
        real_path = tmp_path / "here.mkv"
        job_id = db.create_rename_job({
            "package_name": "Baz (2022) [1080p]", "original_path": str(real_path),
            "status": "needs_review", "media_type": "movie", "title": "Baz", "year": 2022,
        })
        # Pass 1: file doesn't exist yet — first miss
        svc.detect_moved_source_files()
        assert db.get_rename_job(job_id)["source_missing_since"] is not None
        # File shows up before the second pass (e.g. a slow network mount)
        real_path.write_bytes(b"x")
        svc.detect_moved_source_files()
        job = db.get_rename_job(job_id)
        assert job["source_missing_since"] is None  # cleared, self-healed
        assert job["status"] == "needs_review"
        assert job["archived_at"] is None

    def test_file_present_the_whole_time_is_never_touched(self, tmp_path, db, svc):
        real_path = tmp_path / "present.mkv"
        real_path.write_bytes(b"x")
        job_id = db.create_rename_job({
            "package_name": "Qux (2023) [1080p]", "original_path": str(real_path),
            "status": "matched", "media_type": "movie", "title": "Qux", "year": 2023,
        })
        for _ in range(3):
            svc.detect_moved_source_files()
        job = db.get_rename_job(job_id)
        assert job["status"] == "matched"
        assert job["source_missing_since"] is None
        assert job["archived_at"] is None

    def test_applying_status_never_examined(self, tmp_path, db, svc):
        missing_path = str(tmp_path / "inflight.mkv")
        job_id = db.create_rename_job({
            "package_name": "Quux (2024) [1080p]", "original_path": missing_path,
            "status": "applying", "media_type": "movie", "title": "Quux", "year": 2024,
        })
        result = svc.detect_moved_source_files()
        job = db.get_rename_job(job_id)
        assert job["status"] == "applying"  # untouched
        assert job["source_missing_since"] is None
        assert result == {"checked": 0, "confirmed_missing": 0}

    def test_already_archived_job_never_examined(self, tmp_path, db, svc):
        missing_path = str(tmp_path / "archived_already.mkv")
        job_id = db.create_rename_job({
            "package_name": "Corge (2025) [1080p]", "original_path": missing_path,
            "status": "needs_review", "media_type": "movie", "title": "Corge", "year": 2025,
        })
        db.archive_rename_jobs([job_id])
        result = svc.detect_moved_source_files()
        assert result == {"checked": 0, "confirmed_missing": 0}

    def test_one_bad_job_does_not_abort_the_batch(self, tmp_path, db, svc, monkeypatch):
        # A job whose original_path check raises must not stop the other jobs
        # in the batch from being checked (mirrors reconcile_batch's isolation).
        good_missing = str(tmp_path / "good_gone.mkv")
        job_good = db.create_rename_job({
            "package_name": "Grault (2020) [1080p]", "original_path": good_missing,
            "status": "needs_review", "media_type": "movie", "title": "Grault", "year": 2020,
        })
        job_bad = db.create_rename_job({
            "package_name": "Garply (2020) [1080p]", "original_path": None,
            "status": "needs_review", "media_type": "movie", "title": "Garply", "year": 2020,
        })
        # A NULL original_path should be skipped cleanly (not raise), but this
        # test also proves the loop tolerates a genuinely raising path check —
        # simulate via monkeypatching os.path.isfile to raise for one specific path.
        import backend.rename.service as svc_module
        real_isfile = svc_module.os.path.isfile
        def flaky_isfile(path):
            if path == good_missing:
                raise OSError("simulated stat failure")
            return real_isfile(path)
        monkeypatch.setattr(svc_module.os.path, "isfile", flaky_isfile)
        result = svc.detect_moved_source_files()
        assert result["checked"] >= 1  # job_bad (NULL path) still got processed
        job_good_row = db.get_rename_job(job_good)
        assert job_good_row["status"] == "needs_review"  # untouched by the raise, not crashed
```

(Adapt `db`/`svc`/`tmp_path`-based fixture usage to the file's real conventions if they
differ — e.g. if the file uses a different tmp-directory fixture name, or `db` isn't
the fixture name. Check the top of `tests/test_rename_service.py` first.)

- [ ] **Step 2: Run to verify failure**

Run (throwaway container): `python -m pytest tests/test_rename_service.py -k TestDetectMovedSourceFiles -v`
Expected: FAIL — `no such column: source_missing_since` / `AttributeError: detect_moved_source_files`.

- [ ] **Step 3: Add the column + migration**

In `backend/database.py`, add to the `_column_migrations` guarded-ALTER list (the same
list `jd_confirmed_name` was pulled OUT of in the pipeline feature — this one stays in
the shared list since it needs no backfill, just the guarded ALTER):

```python
                    # Two-pass confirmation timer for detect_moved_source_files():
                    # NULL normally; set to CURRENT_TIMESTAMP on the first
                    # maintenance pass that finds a needs_review/matched job's
                    # original_path missing, cleared if the file reappears, and
                    # left permanently set (for audit) once a SECOND consecutive
                    # miss confirms the file is genuinely gone and the job is
                    # archived. See detect_moved_source_files in rename/service.py.
                    'ALTER TABLE rename_jobs ADD COLUMN source_missing_since TIMESTAMP',
```

Add `"source_missing_since"` to `_RENAME_FIELDS` (line 2318, right after
`"archived_at",`):

```python
        "archived_at", "source_missing_since",
    )
```

- [ ] **Step 4: Implement detect_moved_source_files()**

In `backend/rename/service.py`, add this method to `RenameService` (near the existing
`apply()` method for locality — both deal with `original_path` existence):

```python
    def detect_moved_source_files(self) -> dict:
        """Two-pass confirmation: mark and archive rename_jobs whose source
        file vanished outside the app (moved/renamed/deleted directly in
        Windows), without acting on a single transient miss.

        Scope: status IN (needs_review, matched) only — 'applying' is
        in-flight/crash-recovery-handled, applied/failed/reverted are
        terminal, archived jobs are excluded by list_rename_jobs' default.

        A job's source_missing_since is NULL until the first maintenance
        pass that finds original_path missing (sets it, takes no further
        action). Only a SECOND pass that still finds it missing confirms —
        marks the job failed with an honest, distinct error message and
        archives it via the existing archive_rename_jobs(). If the file
        reappears at any point before confirmation, source_missing_since is
        cleared back to NULL (self-heals against a transient NAS/SMB blip —
        this app's maintenance loop runs hourly, so two misses are ~1 hour
        apart, ample margin against a momentary glitch).

        Per-job failures are caught and skipped (retried next pass), never
        aborting the batch — mirrors reconcile_batch's isolation.
        """
        db = self._db
        checked = 0
        confirmed_missing = 0
        jobs = (db.list_rename_jobs(status="needs_review", limit=100000, archived=False) +
                db.list_rename_jobs(status="matched", limit=100000, archived=False))
        for job in jobs:
            try:
                src = job.get("original_path")
                if not src:
                    continue
                checked += 1
                if os.path.isfile(src):
                    if job.get("source_missing_since"):
                        db.update_rename_job(job["id"], source_missing_since=None)
                    continue
                if not job.get("source_missing_since"):
                    db.update_rename_job(job["id"], source_missing_since=_now())
                    continue
                db.update_rename_job(
                    job["id"], status="failed",
                    error_message="Source file was moved or deleted outside ScanHound")
                db.archive_rename_jobs([job["id"]])
                self._broadcast(job["id"])
                confirmed_missing += 1
            except Exception:
                logger.exception("detect_moved_source_files: job %s failed", job.get("id"))
        return {"checked": checked, "confirmed_missing": confirmed_missing}
```

(`os`, `_now`, `logger`, and `self._broadcast` already exist in this file — used
identically by `apply()`/`revert()` a few hundred lines up. If `_now()` isn't a
module-level helper here, use whatever this file's existing timestamp helper is —
check `revert()`'s `reverted_at=_now()` call at line 1555 for the exact name.)

- [ ] **Step 5: Run tests to verify pass**

Run: `python -m pytest tests/test_rename_service.py -k TestDetectMovedSourceFiles -v --timeout=60`
Expected: PASS (all 7).

- [ ] **Step 6: Full-module run + commit**

Run: `python -m pytest tests/test_rename_service.py -v --timeout=90`
Expected: ALL PASS (existing + new).

```bash
git add backend/database.py backend/rename/service.py tests/test_rename_service.py
git commit -m "feat(renames): detect and archive rename jobs whose source file was moved outside the app"
```

---

### Task 2: maintenance-loop hook + settings toggle

**Files:**
- Modify: `backend/app_service.py` (`_run_maintenance_pass`, insert after the pipeline-reconcile block at line ~604)
- Modify: `backend/config.py` (default-config dict, alongside `pipeline_reconcile_enabled` at line 481)
- Modify: `backend/api/routes/settings.py` (`SettingsUpdate` model, alongside the Pipeline card fields at line ~148-150)
- Modify: `frontend/src/routes/settings/+page.svelte` (Renaming settings section — add a checkbox near the other rename-related toggles)
- Test: `tests/test_app_service.py` (hook gating — check this file exists and its
  existing maintenance-pass test conventions first; if no such file exists, add the
  test to whichever file already covers `_run_maintenance_pass`'s other hooks — grep
  for `pipeline_reconcile_enabled` in `tests/` to find it), `tests/test_dv_settings.py`
  (settings round-trip, same pattern as the Pipeline settings task)

**Interfaces:**
- Consumes: `RenameService.detect_moved_source_files() -> {"checked": int, "confirmed_missing": int}` (Task 1).
- Produces: config key `rename_detect_moved_files_enabled: bool` (default `True`), reachable via `PUT /settings` and the Settings UI. No downstream consumers.

- [ ] **Step 1: Write the failing settings round-trip test**

Add to `tests/test_dv_settings.py` (mirror its existing pattern exactly, e.g. the
Pipeline settings tests added in the prior feature):

```python
def test_settings_model_accepts_rename_detect_moved_files_enabled():
    from backend.api.routes.settings import SettingsUpdate
    upd = SettingsUpdate(rename_detect_moved_files_enabled=False)
    assert upd.rename_detect_moved_files_enabled is False


def test_put_settings_round_trips_rename_detect_moved_files_enabled(client):
    resp = client.put("/settings", json={"rename_detect_moved_files_enabled": False})
    assert resp.status_code == 200
    got = client.get("/settings").json()
    assert got["rename_detect_moved_files_enabled"] is False
```

- [ ] **Step 2: Run to verify failure** — expect 422 under `extra="forbid"`.

- [ ] **Step 3: Add the config key + settings field**

`backend/config.py`, in the default-config dict alongside line 481:

```python
    "rename_detect_moved_files_enabled": True,
```

`backend/api/routes/settings.py`, after the existing Pipeline fields (line ~150):

```python
    # Proactively detect and auto-archive rename_jobs whose source file was
    # moved/renamed/deleted directly in Windows (outside ScanHound's own
    # apply flow) — declutters the Renames page instead of leaving a
    # permanently-stale needs_review/matched job.
    rename_detect_moved_files_enabled: Optional[bool] = None
```

- [ ] **Step 4: Verify settings tests pass.**

- [ ] **Step 5: Write the maintenance-loop hook test**

Find where `pipeline_reconcile_enabled`'s gating is tested (grep `tests/` for that
string) and add an equivalent test in the same file/style for
`rename_detect_moved_files_enabled`:

```python
def test_maintenance_pass_calls_detect_moved_files_when_enabled(monkeypatch):
    # Follow the exact pattern the pipeline_reconcile_enabled test uses in
    # this file — construct an AppService with config={"rename_detect_moved_files_enabled": True},
    # monkeypatch RenameService.detect_moved_source_files to a spy, call
    # _run_maintenance_pass(), assert the spy was called once.
    ...

def test_maintenance_pass_skips_detect_moved_files_when_disabled(monkeypatch):
    # Same setup with rename_detect_moved_files_enabled=False; assert the spy
    # was NOT called.
    ...
```

(Write these against whatever `AppService` test-construction helper the
`pipeline_reconcile_enabled` test in this codebase already uses — do not invent a new
one.)

- [ ] **Step 6: Run to verify failure, then wire the hook**

In `backend/app_service.py`, insert into `_run_maintenance_pass()` right after the
pipeline-reconcile `try/except` block (after line 604's `except Exception:` /
`logger.exception(...)` pair, before the conflict-analysis block):

```python
        try:
            if (self.db is not None and self._rename_service is not None
                    and self.config.get("rename_detect_moved_files_enabled", True)):
                result = self._rename_service.detect_moved_source_files()
                if result.get("confirmed_missing"):
                    logger.info("Detected %d rename job(s) with a source file moved "
                               "outside the app; archived", result["confirmed_missing"])
        except Exception:
            logger.exception("Detect-moved-source-files pass failed (non-fatal)")
```

(Check the exact attribute name this class uses for its `RenameService` instance —
other hooks in this same method reference `self._rename_service` per the auto-rename
hook comment at `backend/api/main.py:303` [`reg._rename_service`], but confirm the
attribute name on `AppService`/`self` specifically inside `app_service.py` before
using it verbatim; it may differ from the `ServiceRegistry`'s attribute name.)

- [ ] **Step 7: Run tests to verify pass**

Run: `python -m pytest tests/test_dv_settings.py <the maintenance-pass test file> -v --timeout=60`
Expected: ALL PASS.

- [ ] **Step 8: Settings UI checkbox**

In `frontend/src/routes/settings/+page.svelte`, add a checkbox near the other
renaming-related toggles (follow the exact checkbox pattern used for
`pipeline_reconcile_enabled`'s card from the prior feature — same
`<label class="flex items-center gap-3">` / `<input type="checkbox">` /
`settings.update()` structure):

```svelte
          <label class="flex items-center gap-3">
            <input
              type="checkbox"
              checked={$settings.rename_detect_moved_files_enabled as boolean ?? true}
              onchange={(e) => settings.update((s) => ({ ...s, rename_detect_moved_files_enabled: e.currentTarget.checked }))}
              class="accent-[var(--accent)]"
            />
            <span class="text-sm">Auto-archive jobs whose file was moved outside ScanHound</span>
          </label>
```

- [ ] **Step 9: Verify + commit**

Run: `cd frontend && npm run check && npm run build`
Expected: 0 errors.

Run: `python -m pytest tests/test_dv_settings.py -v --timeout=60` and the maintenance-pass
test file located in Step 5.
Expected: ALL PASS.

```bash
git add backend/app_service.py backend/config.py backend/api/routes/settings.py frontend/src/routes/settings/+page.svelte tests/
git commit -m "feat(renames): wire moved-file detection into the maintenance loop + settings toggle"
```
