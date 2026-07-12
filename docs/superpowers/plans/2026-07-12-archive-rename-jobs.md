# Archive Rename Jobs Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Jobs on the Renames page can be archived — automatically the instant they're applied, or manually in any status via bulk-select — moving them out of the active queue into a dedicated Archived view with restore.

**Architecture:** A nullable `archived_at` column on `rename_jobs`, orthogonal to `status`. `DatabaseManager.list_rename_jobs()` gains an `archived: bool = False` parameter so every existing caller (unchanged call sites) keeps excluding archived rows by default. Two new bulk DB methods (`archive_rename_jobs`/`unarchive_rename_jobs`) do a single set-based `UPDATE`, with the `'applying'` exclusion baked into the archive SQL's `WHERE` clause. `RenameService` gets thin `bulk_archive`/`bulk_unarchive` wrapper methods (matching the existing `bulk_delete`/`bulk_reidentify` pattern), exposed via two new routes under the established `/rename/jobs/bulk/*` convention (this project's existing convention here is one-action-per-endpoint, not the toggle-flag shape `/results/dismiss` uses elsewhere — Tasks below follow the *local* convention). Both `apply()` success paths set `archived_at` in the same `update_rename_job()` call that sets `status="applied"`. Frontend: `StatusDashboard` gains an "Archived" `StatCard`; selecting it switches the page to a separate `archivedRenameJobs` store (fetched fresh via `archived=true`) instead of filtering the existing `renameJobs` array, since archived jobs are excluded from the default (non-archived) load entirely; `BulkBar` gains Archive/Unarchive actions.

**Tech Stack:** Python (FastAPI, sqlite3), pytest; SvelteKit 5 (runes), vitest.

## Global Constraints

- Archiving is **orthogonal to `status`** — never conflate the two. An archived job keeps whatever status it had.
- `archived_at` must be added to `DatabaseManager._RENAME_FIELDS` (`backend/database.py:2184-2193`) or the generic `update_rename_job(job_id, **fields)` setter will silently drop it — this is the single easiest mistake to make in this plan; verify explicitly in Task 1.
- Auto-archive is **immediate and unconditional** on apply — no settings toggle, no delay. Both `apply()` success paths (`backend/rename/service.py:1395` and `:1501`) must set it; a full-codebase grep confirmed these are the ONLY two `status="applied"` assignment sites — verify this is still true at implementation time (grep `status\s*=\s*["\']applied["\']` across `backend/`) in case it has changed.
- Manual archive silently skips any job in the given id set whose status is `'applying'`, archiving the rest of the batch normally — never let one in-flight job block the others.
- `list_rename_jobs()`'s default (`archived=False`) must not change behavior for any existing caller that doesn't pass `archived=` explicitly.
- The existing dedup check `path_has_rename_job`/`original_path` matching (`backend/database.py:2330-2338`) is explicitly **not** touched — archived rows must keep counting toward it.
- Backend tests: throwaway container pattern (`docker run -d --name <c> --entrypoint sleep scanhound:latest infinity`, `docker cp backend/. tests/. <c>:/app/...`, `pip install -q pytest httpx`, run, `docker rm -f`). Frontend tests: host node (`cd frontend && npm run check && npm run build && npx vitest run`).
- Work directly on `main`. Commit only when genuinely green.
- Smart/curly-quote hazard: plain ASCII quotes only in all new/changed source; grep before committing.
- Desktop only — no mobile Renames view changes in this plan (confirmed scope decision).
- All user-facing copy (button labels, tooltips, toast text, the Archived tab's empty state) is drafted using the **Fable** model tier, not the implementer's own default phrasing — Task 3 calls this out explicitly at the relevant step.

---

### Task 1: Schema, DB methods, auto-archive hook

**Files:**
- Modify: `backend/database.py`
- Modify: `backend/rename/service.py`
- Test: `tests/test_database.py` (or whichever file already covers `DatabaseManager`/`rename_jobs` — check `ls tests/ | grep -i database` first and match its existing `db_manager` fixture / class-based style, per this session's established convention for this file)
- Test: `tests/test_rename_service.py` (for the `apply()` auto-archive regression — confirm this file exists and covers `apply()` before adding to it)

**Interfaces:**
- Produces: `DatabaseManager.list_rename_jobs(self, status=None, limit=200, archived=False)` (modified signature — the new `archived` kwarg is additive, existing positional/keyword callers unaffected). `DatabaseManager.archive_rename_jobs(self, job_ids) -> int` (returns count actually archived; skips `'applying'` jobs and already-archived jobs). `DatabaseManager.unarchive_rename_jobs(self, job_ids) -> int` (returns count actually unarchived).

- [ ] **Step 1: Write the failing tests**

First run `ls tests/ | grep -i database` and read the existing `DatabaseManager` test file's fixture (likely `db_manager`, a tempfile-backed instance — mirror its exact setup/teardown style). Add:

```python
class TestArchiveRenameJobs:
    def _make_job(self, db_manager, **overrides):
        job = {
            "original_path": overrides.get("original_path", "/downloads/Movie.mkv"),
            "status": overrides.get("status", "matched"),
            "title": "Movie",
        }
        return db_manager.create_rename_job(job)

    def test_list_rename_jobs_default_excludes_archived(self, db_manager):
        jid = self._make_job(db_manager)
        db_manager.archive_rename_jobs([jid])
        assert db_manager.list_rename_jobs() == []

    def test_list_rename_jobs_archived_true_returns_only_archived(self, db_manager):
        active_id = self._make_job(db_manager, original_path="/downloads/Active.mkv")
        archived_id = self._make_job(db_manager, original_path="/downloads/Archived.mkv")
        db_manager.archive_rename_jobs([archived_id])
        archived_jobs = db_manager.list_rename_jobs(archived=True)
        assert [j["id"] for j in archived_jobs] == [archived_id]

    def test_archive_rename_jobs_sets_archived_at(self, db_manager):
        jid = self._make_job(db_manager)
        count = db_manager.archive_rename_jobs([jid])
        assert count == 1
        job = db_manager.get_rename_job(jid)
        assert job["archived_at"] is not None

    def test_archive_rename_jobs_skips_applying_status(self, db_manager):
        jid = self._make_job(db_manager, status="applying")
        count = db_manager.archive_rename_jobs([jid])
        assert count == 0
        job = db_manager.get_rename_job(jid)
        assert job["archived_at"] is None

    def test_archive_rename_jobs_mixed_batch_archives_the_rest(self, db_manager):
        applying_id = self._make_job(db_manager, original_path="/downloads/A.mkv", status="applying")
        matched_id = self._make_job(db_manager, original_path="/downloads/B.mkv", status="matched")
        count = db_manager.archive_rename_jobs([applying_id, matched_id])
        assert count == 1
        assert db_manager.get_rename_job(applying_id)["archived_at"] is None
        assert db_manager.get_rename_job(matched_id)["archived_at"] is not None

    def test_unarchive_rename_jobs_clears_archived_at(self, db_manager):
        jid = self._make_job(db_manager)
        db_manager.archive_rename_jobs([jid])
        count = db_manager.unarchive_rename_jobs([jid])
        assert count == 1
        assert db_manager.get_rename_job(jid)["archived_at"] is None

    def test_archive_rename_jobs_empty_list_is_a_noop(self, db_manager):
        assert db_manager.archive_rename_jobs([]) == 0
        assert db_manager.unarchive_rename_jobs([]) == 0

    def test_path_has_rename_job_still_matches_archived_rows(self, db_manager):
        jid = self._make_job(db_manager, original_path="/downloads/Kept.mkv")
        db_manager.archive_rename_jobs([jid])
        assert db_manager.path_has_rename_job("/downloads/Kept.mkv") is True
```

Also add to `tests/test_rename_service.py` (read that file's existing `apply()` test fixtures first — it will already have a way to construct a `RenameService` with a fake/mocked `_db` and a real temp file as `original_path`; mirror that exact setup rather than inventing a new one):

```python
def test_apply_sets_archived_at_on_success(rename_service_and_db, tmp_path):
    """Guards the main move-success path (service.py ~line 1501)."""
    service, db = rename_service_and_db
    src = tmp_path / "Movie.mkv"
    src.write_bytes(b"x")
    dest_dir = tmp_path / "library"
    dest_dir.mkdir()
    job_id = db.create_rename_job({
        "original_path": str(src), "status": "matched",
        "destination_path": str(dest_dir), "new_filename": "Movie.mkv",
    })
    result = service.apply(job_id)
    assert result["ok"] is True
    job = db.get_rename_job(job_id)
    assert job["status"] == "applied"
    assert job["archived_at"] is not None


def test_apply_sets_archived_at_on_already_applied_samefile_noop(rename_service_and_db, tmp_path):
    """Guards the OTHER success path (service.py ~line 1395): re-applying a
    job whose file is already hardlinked/placed at the exact destination
    (os.path.samefile(src, dst) is True) is a no-op success, not a conflict —
    that path sets status='applied' independently of the main move path and
    must independently set archived_at too."""
    service, db = rename_service_and_db
    src = tmp_path / "Movie.mkv"
    src.write_bytes(b"x")
    dest_dir = tmp_path / "library"
    dest_dir.mkdir()
    dst = dest_dir / "Movie.mkv"
    import os
    os.link(str(src), str(dst))  # same-inode: os.path.samefile(src, dst) is True
    job_id = db.create_rename_job({
        "original_path": str(src), "status": "matched",
        "destination_path": str(dest_dir), "new_filename": "Movie.mkv",
    })
    result = service.apply(job_id)
    assert result["ok"] is True
    assert result.get("already") is True
    job = db.get_rename_job(job_id)
    assert job["status"] == "applied"
    assert job["archived_at"] is not None
```

(Adapt the exact fixture name `rename_service_and_db` and job-construction shape to whatever this file's existing `apply()` tests already use — do not invent new scaffolding if equivalent fixtures exist. The second test's `os.link` setup must produce two paths that are the same inode — on a filesystem where hardlinks aren't available (rare in a Linux container), fall back to whatever this file's existing tests already use to exercise the `os.path.samefile` no-op branch, if any prior art exists — check first.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_database.py -v -k archive` (adjust filename per Step 1) and `pytest tests/test_rename_service.py -v -k archived_at`
Expected: FAIL — `archived_at`/`archive_rename_jobs`/`unarchive_rename_jobs` don't exist yet.

- [ ] **Step 3: Write the implementation**

In `backend/database.py`, add to the `_column_migrations` list (near line 591-611, alongside the other `rename_jobs` ALTERs):

```python
                    'ALTER TABLE rename_jobs ADD COLUMN archived_at TIMESTAMP',
```

Add `"archived_at"` to `_RENAME_FIELDS` (line 2184-2193):

```python
    _RENAME_FIELDS = (
        "package_name", "original_path", "original_filename", "new_filename",
        "destination_path", "status", "media_type", "title", "year", "season",
        "episode", "tmdb_id", "imdb_id", "resolution", "match_confidence",
        "match_source", "move_method", "proposed_match", "plex_sort_title",
        "warning_message", "error_message", "processed_at", "reverted_at",
        "suggested_correction", "combined_episode", "split_file", "poster_path",
        "match_reasons", "prior_status", "conflict_kind", "conflict_same_size",
        "conflict_existing_size", "conflict_incoming_size", "conflict_analysis",
        "archived_at",
    )
```

Replace `list_rename_jobs` (line 2280-2290):

```python
    def list_rename_jobs(self, status=None, limit=200, archived=False):
        """Return rename jobs (optionally filtered by status), newest first.

        ``archived`` defaults to False so every existing/未-updated caller
        keeps excluding archived rows exactly as before this column existed.
        Archiving is orthogonal to status: archived=True returns archived
        rows of ANY status when no status filter is also given.
        """
        conditions = []
        params = []
        if status:
            conditions.append("status = ?")
            params.append(status)
        conditions.append("archived_at IS NOT NULL" if archived else "archived_at IS NULL")
        where = " WHERE " + " AND ".join(conditions)
        params.append(limit)
        rows = self._query_dicts(
            f"SELECT * FROM rename_jobs{where} ORDER BY detected_at DESC LIMIT ?",
            tuple(params), default=[])
        return [self._deserialize_rename_row(r) for r in (rows or [])]
```

Add near `path_has_rename_job` (after line 2338):

```python
    def archive_rename_jobs(self, job_ids):
        """Archive the given jobs (set archived_at to now), skipping any job
        whose status is 'applying' (the transient mid-move state) and any
        already-archived job. One in-flight job in the batch never blocks
        archiving the rest. Returns the number of rows actually archived."""
        ids = [int(i) for i in (job_ids or []) if i is not None]
        if not ids:
            return 0
        placeholders = ",".join("?" * len(ids))
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        conn = None
        try:
            with self._lock:
                conn = self.get_connection()
                if not conn:
                    return 0
                cur = conn.cursor()
                cur.execute(
                    f"UPDATE rename_jobs SET archived_at = ? "
                    f"WHERE id IN ({placeholders}) AND status != 'applying' "
                    f"AND archived_at IS NULL",
                    (now, *ids))
                archived = cur.rowcount
                conn.commit()
            return archived
        except Exception as e:
            try:
                if conn:
                    conn.rollback()
            except Exception:
                pass
            logger.error("DB Error (archive_rename_jobs): %s", e)
            return 0

    def unarchive_rename_jobs(self, job_ids):
        """Clear archived_at for the given jobs. Returns the number of rows
        actually unarchived."""
        ids = [int(i) for i in (job_ids or []) if i is not None]
        if not ids:
            return 0
        placeholders = ",".join("?" * len(ids))
        conn = None
        try:
            with self._lock:
                conn = self.get_connection()
                if not conn:
                    return 0
                cur = conn.cursor()
                cur.execute(
                    f"UPDATE rename_jobs SET archived_at = NULL "
                    f"WHERE id IN ({placeholders}) AND archived_at IS NOT NULL",
                    tuple(ids))
                unarchived = cur.rowcount
                conn.commit()
            return unarchived
        except Exception as e:
            try:
                if conn:
                    conn.rollback()
            except Exception:
                pass
            logger.error("DB Error (unarchive_rename_jobs): %s", e)
            return 0
```

`database.py` already has `import datetime` (module import, not `from datetime import ...`) — use `datetime.datetime.now(datetime.timezone.utc)`, matching that existing import style exactly.

In `backend/rename/service.py`, update both `status="applied"` call sites to also set `archived_at`. First re-run `grep -n 'status\s*=\s*["\x27]applied["\x27]' backend/rename/service.py` to confirm the two line numbers are still 1395 and 1501 (or find their current locations if shifted) before editing. At the same-inode no-op success path (~line 1395):

```python
                    db.update_rename_job(job_id, status="applied", processed_at=_now(),
                                         archived_at=_now(),
                                         conflict_kind=None, conflict_same_size=None,
                                         conflict_existing_size=None, conflict_incoming_size=None)
```

At the main move-success path (~line 1501):

```python
            db.update_rename_job(job_id, status="applied", move_method=used,
                                 processed_at=_now(), plex_sort_title=sort_title,
                                 error_message=None, archived_at=_now(),
                                 conflict_kind=None, conflict_same_size=None,
                                 conflict_existing_size=None, conflict_incoming_size=None)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_database.py -v -k archive` and `pytest tests/test_rename_service.py -v -k archived_at` (adjust filenames per Step 1)
Expected: PASS, all new tests green. Also run each full file to confirm no regressions: `pytest tests/test_database.py -v` and `pytest tests/test_rename_service.py -v`.

- [ ] **Step 5: Commit**

```bash
git add backend/database.py backend/rename/service.py tests/test_database.py tests/test_rename_service.py
git commit -m "feat(rename): archived_at column + archive/unarchive DB methods + auto-archive on apply"
```

(Adjust test filenames in `git add` to whatever Step 1's investigation actually found.)

---

### Task 2: Bulk archive/unarchive routes + jobs/status endpoint updates

**Files:**
- Modify: `backend/rename/service.py`
- Modify: `backend/api/routes/rename.py`
- Test: `tests/test_rename_routes.py` (check `ls tests/ | grep rename` first for the correct existing route-test file)

**Interfaces:**
- Consumes: `db.archive_rename_jobs(job_ids) -> int`, `db.unarchive_rename_jobs(job_ids) -> int`, `db.list_rename_jobs(status=None, limit=200, archived=False)` (Task 1).
- Produces: `RenameService.bulk_archive(self, ids: list) -> dict` (returns `{"archived": N}`), `RenameService.bulk_unarchive(self, ids: list) -> dict` (returns `{"unarchived": N}`). Routes `POST /rename/jobs/bulk/archive` and `POST /rename/jobs/bulk/unarchive`, both body `BulkIdsRequest` (`{ids: list[int]}`, existing model at `backend/api/routes/rename.py:115`). `GET /rename/jobs` gains an `archived: bool = False` query param, threaded into `db.list_rename_jobs(status=status, limit=limit, archived=archived)` — when `archived=true` is requested, `status` is typically omitted by the frontend (Task 3) but the route must not force that; compose both filters if both are given. `GET /rename/status`'s response gains an `"archived": <count>` key.

- [ ] **Step 1: Write the failing tests**

First run `ls tests/ | grep rename` and read the existing route-test file's FastAPI `TestClient`/`ServiceRegistry` fixture (mirror its exact setup). Add:

```python
def test_bulk_archive_archives_jobs(client, db_manager):
    jid = db_manager.create_rename_job({
        "original_path": "/downloads/M.mkv", "status": "matched", "title": "M",
    })
    resp = client.post("/rename/jobs/bulk/archive", json={"ids": [jid]})
    assert resp.status_code == 200
    assert resp.json()["archived"] == 1
    assert db_manager.get_rename_job(jid)["archived_at"] is not None


def test_bulk_unarchive_restores_jobs(client, db_manager):
    jid = db_manager.create_rename_job({
        "original_path": "/downloads/M.mkv", "status": "matched", "title": "M",
    })
    db_manager.archive_rename_jobs([jid])
    resp = client.post("/rename/jobs/bulk/unarchive", json={"ids": [jid]})
    assert resp.status_code == 200
    assert resp.json()["unarchived"] == 1
    assert db_manager.get_rename_job(jid)["archived_at"] is None


def test_list_jobs_default_excludes_archived(client, db_manager):
    jid = db_manager.create_rename_job({
        "original_path": "/downloads/M.mkv", "status": "matched", "title": "M",
    })
    db_manager.archive_rename_jobs([jid])
    resp = client.get("/rename/jobs")
    assert resp.status_code == 200
    ids = [j["id"] for j in resp.json()["jobs"]]
    assert jid not in ids


def test_list_jobs_archived_true_returns_archived(client, db_manager):
    jid = db_manager.create_rename_job({
        "original_path": "/downloads/M.mkv", "status": "matched", "title": "M",
    })
    db_manager.archive_rename_jobs([jid])
    resp = client.get("/rename/jobs?archived=true")
    assert resp.status_code == 200
    ids = [j["id"] for j in resp.json()["jobs"]]
    assert jid in ids


def test_rename_status_reports_archived_count(client, db_manager):
    jid = db_manager.create_rename_job({
        "original_path": "/downloads/M.mkv", "status": "matched", "title": "M",
    })
    db_manager.archive_rename_jobs([jid])
    resp = client.get("/rename/status")
    assert resp.status_code == 200
    assert resp.json()["archived"] == 1
```

(Adapt fixture names `client`/`db_manager` to whatever this file's existing tests actually use.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_rename_routes.py -v -k "archive or archived"` (adjust filename per Step 1)
Expected: FAIL — routes/params don't exist yet (404s / unexpected key errors).

- [ ] **Step 3: Write the implementation**

In `backend/rename/service.py`, add near `bulk_delete` (after line 2089):

```python
    def bulk_archive(self, ids: list) -> dict:
        db = self._db
        archived = db.archive_rename_jobs(ids) if db else 0
        return {"archived": archived}

    def bulk_unarchive(self, ids: list) -> dict:
        db = self._db
        unarchived = db.unarchive_rename_jobs(ids) if db else 0
        return {"unarchived": unarchived}
```

In `backend/api/routes/rename.py`, add routes near `bulk_delete` (after line 274):

```python
@router.post("/jobs/bulk/archive")
def bulk_archive(body: BulkIdsRequest, reg: ServiceRegistry = Depends(get_registry)):
    if reg.db is None:
        raise HTTPException(status_code=503, detail="Database unavailable")
    return _service(reg).bulk_archive(body.ids)


@router.post("/jobs/bulk/unarchive")
def bulk_unarchive(body: BulkIdsRequest, reg: ServiceRegistry = Depends(get_registry)):
    if reg.db is None:
        raise HTTPException(status_code=503, detail="Database unavailable")
    return _service(reg).bulk_unarchive(body.ids)
```

Update `list_jobs` (line 137-213) — add the `archived` parameter to the signature and thread it into the primary `jobs` fetch (do NOT change the separate `all_active_jobs = reg.db.list_rename_jobs(limit=100000)` call at line 155 — that one must keep its `archived=False` default so conflict/duplicate annotation still only considers active, non-archived jobs):

```python
@router.get("/jobs")
def list_jobs(status: Optional[str] = None, limit: int = 200, archived: bool = False,
              reg: ServiceRegistry = Depends(get_registry)):
```

```python
    jobs = reg.db.list_rename_jobs(status=status, limit=limit, archived=archived)
```

Update `rename_status` (line 216-229) to add the archived count:

```python
@router.get("/status")
def rename_status(reg: ServiceRegistry = Depends(get_registry)):
    """Config + counts for the Renames tab / settings card."""
    cfg = reg.config or {}
    counts = reg.db.count_rename_jobs_by_status() if reg.db else {}
    archived_count = len(reg.db.list_rename_jobs(archived=True, limit=100000)) if reg.db else 0
    return {
        "enabled": bool(cfg.get("auto_rename_enabled")),
        "require_confirmation": bool(cfg.get("auto_rename_require_confirmation", True)),
        "confidence_threshold": cfg.get("auto_rename_confidence_threshold", 70),
        "move_method": cfg.get("auto_rename_move_method", "hardlink"),
        "llm_enabled": bool(cfg.get("auto_rename_llm_enabled")),
        "counts": counts,
        "needs_review": counts.get("needs_review", 0),
        "archived": archived_count,
    }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_rename_routes.py -v -k "archive or archived"` (adjust filename per Step 1)
Expected: PASS, all 5 new tests green. Also run the full file to confirm no regressions: `pytest tests/test_rename_routes.py -v`.

- [ ] **Step 5: Commit**

```bash
git add backend/rename/service.py backend/api/routes/rename.py tests/test_rename_routes.py
git commit -m "feat(rename): bulk archive/unarchive routes + archived filter on jobs/status endpoints"
```

(Adjust the test filename in `git add` to whatever Step 1's investigation actually found.)

---

### Task 3: Frontend — Archived tab, bulk actions, restore

**Files:**
- Modify: `frontend/src/lib/api/types.ts`
- Modify: `frontend/src/lib/api/client.ts`
- Modify: `frontend/src/lib/stores/renames.ts`
- Modify: `frontend/src/lib/components/renames/StatusDashboard.svelte`
- Modify: `frontend/src/lib/components/renames/BulkBar.svelte`
- Modify: `frontend/src/routes/renames/+page.svelte`
- Test: `frontend/src/lib/stores/renames.test.ts` (check this file exists first; if store tests for this area live elsewhere, match that location instead)

**Interfaces:**
- Consumes: `POST /rename/jobs/bulk/archive`, `POST /rename/jobs/bulk/unarchive`, `GET /rename/jobs?archived=true`, `GET /rename/status`'s new `archived` count (Task 2).
- Produces: `archivedRenameJobs: Writable<RenameJob[]>` (new store, `frontend/src/lib/stores/renames.ts`). `loadArchivedRenameJobs(): Promise<void>` (fetches `archived=true` jobs into that store). `bulkArchive(): void` / `bulkUnarchive(): void` (new bulk actions, same `runBulk()` helper shape as `bulkDelete`/`bulkReidentify`). `BulkBar` gains a required prop `viewingArchived: boolean` that swaps its Archive button for an Unarchive button.

- [ ] **Step 1: Add types**

In `frontend/src/lib/api/types.ts`, add to `RenameJob` (near `reverted_at`, line ~99):

```typescript
  archived_at: string | null;
```

Add to `RenameStatus` (line 159-167):

```typescript
  archived: number;
```

- [ ] **Step 2: Add API client methods**

In `frontend/src/lib/api/client.ts`, modify `getRenameJobs` (line 367-370) to accept an optional `archived` flag:

```typescript
  getRenameJobs: (status?: string, archived?: boolean) => {
    const params = new URLSearchParams();
    if (status) params.set('status', status);
    if (archived) params.set('archived', 'true');
    const qs = params.toString() ? `?${params.toString()}` : '';
    return request<{ jobs: RenameJob[]; counts: Record<string, number> }>(`/rename/jobs${qs}`);
  },
```

Add near `bulkDelete` (line 407-411), matching its exact request shape:

```typescript
  bulkArchive: (ids: number[]) =>
    request<{ archived: number }>('/rename/jobs/bulk/archive', {
      method: 'POST',
      body: JSON.stringify({ ids })
    }),
  bulkUnarchive: (ids: number[]) =>
    request<{ unarchived: number }>('/rename/jobs/bulk/unarchive', {
      method: 'POST',
      body: JSON.stringify({ ids })
    }),
```

- [ ] **Step 3: Write the failing store tests**

Read `frontend/src/lib/stores/renames.ts` in full first (specifically the `runBulk` helper at line ~108-121 and the `bulkDelete`/`bulkReidentify` definitions at line ~144-156) to match their exact structure. Add to `frontend/src/lib/stores/renames.test.ts` (or wherever this file's existing bulk-action tests live — check first):

```typescript
import { archivedRenameJobs, loadArchivedRenameJobs, bulkArchive, bulkUnarchive, selectedJobIds } from './renames';
import { api } from '$lib/api/client';
import { get } from 'svelte/store';
import { vi, describe, it, expect, beforeEach } from 'vitest';

vi.mock('$lib/api/client');

describe('archivedRenameJobs', () => {
	beforeEach(() => {
		archivedRenameJobs.set([]);
		selectedJobIds.set(new Set());
		vi.clearAllMocks();
	});

	it('loadArchivedRenameJobs populates the store from the archived endpoint', async () => {
		const fakeJob = { id: 1, status: 'applied', archived_at: '2026-07-12T00:00:00Z' };
		vi.mocked(api.getRenameJobs).mockResolvedValue({ jobs: [fakeJob] as any, counts: {} });
		await loadArchivedRenameJobs();
		expect(api.getRenameJobs).toHaveBeenCalledWith(undefined, true);
		expect(get(archivedRenameJobs)).toEqual([fakeJob]);
	});

	it('bulkArchive calls the archive endpoint with selected ids and clears selection', async () => {
		selectedJobIds.set(new Set([1, 2]));
		vi.mocked(api.bulkArchive).mockResolvedValue({ archived: 2 });
		vi.mocked(api.getRenameJobs).mockResolvedValue({ jobs: [], counts: {} });
		vi.mocked(api.getRenameStatus).mockResolvedValue({} as any);
		await bulkArchive();
		expect(api.bulkArchive).toHaveBeenCalledWith([1, 2]);
		expect(get(selectedJobIds).size).toBe(0);
	});

	it('bulkUnarchive calls the unarchive endpoint with selected ids', async () => {
		selectedJobIds.set(new Set([3]));
		vi.mocked(api.bulkUnarchive).mockResolvedValue({ unarchived: 1 });
		vi.mocked(api.getRenameJobs).mockResolvedValue({ jobs: [], counts: {} });
		vi.mocked(api.getRenameStatus).mockResolvedValue({} as any);
		await bulkUnarchive();
		expect(api.bulkUnarchive).toHaveBeenCalledWith([3]);
	});
});
```

Read the existing `refresh()` function `runBulk` calls in its `finally` block (mirror what `bulkDelete` triggers) before finalizing this test's mock expectations — adjust the `getRenameJobs`/`getRenameStatus` mock calls above to match whatever `refresh()` actually calls.

- [ ] **Step 4: Run tests to verify they fail**

Run: `npx vitest run src/lib/stores/renames.test.ts -t archivedRenameJobs` (adjust path per Step 3's file-location check)
Expected: FAIL — `archivedRenameJobs`/`loadArchivedRenameJobs`/`bulkArchive`/`bulkUnarchive` not exported yet.

- [ ] **Step 5: Write the store implementation**

In `frontend/src/lib/stores/renames.ts`, add near `renameJobs`/`renameStatus` (line ~10):

```typescript
/** Archived rename jobs — a SEPARATE store from renameJobs, since the
 *  default (non-archived) load never includes them. Fetched fresh each
 *  time the Archived tab is selected, not filtered client-side from
 *  renameJobs. */
export const archivedRenameJobs = writable<RenameJob[]>([]);

export async function loadArchivedRenameJobs() {
  try {
    const { jobs } = await api.getRenameJobs(undefined, true);
    archivedRenameJobs.set(jobs);
  } catch {
    /* offline / no server */
  }
}
```

Add near `bulkDelete` (after line 156), using the existing `runBulk` helper:

```typescript
export function bulkArchive() {
  return runBulk('Archive', async (ids) => {
    const r = await api.bulkArchive(ids);
    addToast('Archived', `Archived ${r.archived}`, 'success');
  });
}

export function bulkUnarchive() {
  return runBulk('Unarchive', async (ids) => {
    const r = await api.bulkUnarchive(ids);
    addToast('Unarchived', `Restored ${r.unarchived}`, 'success');
    await loadArchivedRenameJobs();
  });
}
```

(The extra `loadArchivedRenameJobs()` call in `bulkUnarchive` is needed because `runBulk`'s own `finally` block calls whatever `refresh()` already refreshes — likely `renameJobs`/`renameStatus`, not the separate `archivedRenameJobs` store — so unarchiving must also explicitly refresh the Archived view the user is currently looking at. Read `runBulk`'s `finally` block and `refresh()`'s definition first to confirm exactly what it does and does not cover before finalizing this.)

- [ ] **Step 6: Run store tests to verify they pass**

Run: `npx vitest run src/lib/stores/renames.test.ts -t archivedRenameJobs` (adjust path per Step 3)
Expected: PASS, all 3 new tests green.

- [ ] **Step 7: Wire the Archived tab into `StatusDashboard.svelte`**

Read the full current file first (it may have shifted from the version excerpted here). Add a new `StatCard` for Archived, following the existing four cards' exact prop shape (`label`, `count`, `variant`, `borderStatus`, `active`, `onclick`) — but sourced from `renameStatus.archived`, not from the `counts` status map, and toggling `statusFilter` to `'archived'`:

```svelte
<script lang="ts">
  import StatCard from './StatCard.svelte';
  import { renameStatus, dvCounts, applyConfident, loadDvScans, applyActive } from '$lib/stores/renames';
  // ...existing imports unchanged...

  let archivedCount = $derived($renameStatus?.archived ?? 0);
</script>
```

```svelte
  <StatCard
    label="Archived"
    count={archivedCount}
    variant="neutral"
    borderStatus="archived"
    active={statusFilter === 'archived'}
    onclick={() => toggle('archived')}
  />
```

Place it after the existing "Failed" card. If `StatCard`'s `variant` prop is a strict union type that does not already include `"neutral"`, check its existing accepted values first and pick whichever already-supported variant reads as the most neutral/muted (do not add a new variant option to `StatCard` itself unless genuinely necessary — reuse what exists).

- [ ] **Step 8: Branch `+page.svelte`'s rendering on the Archived tab**

Read the current file's job-list rendering section in full (the `.filter((j) => statusFilter === 'all' || j.status === statusFilter || j.status === 'applying')` line and its surrounding `#each` blocks — both the list-view and grid-view loops noted in this project's existing season-grouping work) before editing, since exact line numbers have likely shifted.

Import `archivedRenameJobs` and `loadArchivedRenameJobs` from `$lib/stores/renames` alongside the existing imports. Add an `$effect` (or extend the existing `onMount`/filter-change handling — match whatever reactive pattern this file already uses for `statusFilter` changes) that calls `loadArchivedRenameJobs()` when `statusFilter` becomes `'archived'`:

```typescript
$effect(() => {
  if (statusFilter === 'archived') {
    loadArchivedRenameJobs();
  }
});
```

Change the source of the rendered job list so that when `statusFilter === 'archived'`, the page renders from `$archivedRenameJobs` directly (already archived-only, no further client-side status filtering needed) instead of applying the existing `.filter(...)` over `$renameJobs`:

```typescript
let shown = $derived(
  statusFilter === 'archived'
    ? $archivedRenameJobs
    : $renameJobs.filter((j) => statusFilter === 'all' || j.status === statusFilter || j.status === 'applying')
);
```

(Match this to whatever the existing derived variable is actually named and however season-grouping already restructured this — the goal is: exactly one place decides what's "shown," and it branches on `statusFilter === 'archived'` to pick the right data source.)

- [ ] **Step 9: Wire Archive/Unarchive into `BulkBar.svelte`**

Add a `viewingArchived` prop and swap the action shown:

```svelte
<script lang="ts">
  import {
    selectedJobIds, selectAll, clearSelection, bulkBusy, applyActive,
    bulkApply, bulkReidentify, bulkDelete, bulkSetDestination, applyConfident,
    bulkArchive, bulkUnarchive
  } from '$lib/stores/renames';
  import { settings } from '$lib/stores/settings';

  let { shownIds, viewingArchived = false }: { shownIds: number[]; viewingArchived?: boolean } = $props();
  // ...existing derived state unchanged...
</script>
```

Replace the existing action button row's content conditionally — when `viewingArchived` is true, show only an Unarchive button (Delete/Apply/Re-identify/Set-destination don't apply to archived jobs); otherwise show the existing buttons plus a new Archive button:

```svelte
{#if viewingArchived}
  <button
    class="px-2 py-1 rounded text-[11px] font-medium bg-[var(--accent)] text-white disabled:opacity-50"
    disabled={controlsDisabled}
    onclick={bulkUnarchive}
  >Unarchive</button>
{:else}
  <!-- existing Apply / Re-identify / Set destination / Apply confident / Delete buttons unchanged -->
  <button
    class="px-2 py-1 rounded text-[11px] font-medium bg-[var(--bg-tertiary)] disabled:opacity-50"
    disabled={controlsDisabled}
    onclick={bulkArchive}
  >Archive</button>
{/if}
```

In `+page.svelte`, pass `viewingArchived={statusFilter === 'archived'}` to the existing `<BulkBar shownIds={...} />` invocation.

- [ ] **Step 10: Draft user-facing copy with Fable**

The button labels used in Steps 7-9 above ("Archived", "Archive", "Unarchive") are functional placeholders written by the plan author, not final copy — per this project's standing preference, dispatch a Fable-tier agent (or run this step yourself with the Fable model) to draft: the Archived `StatCard`'s label, the Archive/Unarchive button labels and hover titles, the `bulkArchive`/`bulkUnarchive` toast titles/messages (currently "Archived"/"Archived {n}" and "Unarchived"/"Restored {n}" above), and an empty-state message for when `$archivedRenameJobs` is empty while viewing that tab (check whether `+page.svelte` already has an empty-state pattern for the other tabs — e.g. "No jobs need review" — and add an equivalent one here if so, styled consistently). Apply the drafted copy in place of the placeholders used above.

- [ ] **Step 11: Verify manually in the browser**

Start the dev server, open Renames, confirm: the Archived card shows a count and toggles the view; applying a job removes it from the active list and the Archived count increments; selecting jobs in any non-applying status and clicking Archive moves them to the Archived view; while viewing Archived, selecting jobs and clicking Unarchive returns them to the active queue with their original status intact.

- [ ] **Step 12: Run the full verification suite**

```bash
cd frontend
npm run check
npm run build
npx vitest run
```
Backend (throwaway container, confirm no regressions from Tasks 1-2):
```bash
pytest tests/test_database.py tests/test_rename_service.py tests/test_rename_routes.py -v
```
(Adjust filenames to whatever earlier tasks' investigations actually found.)
Expected: all green, no regressions. Grep every file touched across Tasks 1-3 for curly/smart quotes and confirm zero matches.

- [ ] **Step 13: Commit**

```bash
git add frontend/src/lib/api/types.ts frontend/src/lib/api/client.ts frontend/src/lib/stores/renames.ts \
        frontend/src/lib/stores/renames.test.ts frontend/src/lib/components/renames/StatusDashboard.svelte \
        frontend/src/lib/components/renames/BulkBar.svelte frontend/src/routes/renames/+page.svelte
git commit -m "feat(renames): Archived tab, Archive/Unarchive bulk actions, Fable-drafted copy"
```
