# Pipeline Tracker Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reconcile every grab through download → extraction → rename → Plex ingestion into one categorized, browsable list with a Re-grab and a Search-other-sources action per stalled item.

**Architecture:** A read-mostly reconcile (`backend/pipeline_service.py`) joins `downloads` → `download_results` → `rename_jobs` → `plex_cache` by a canonical `package_name` string (uuid-first once discovered), persists dismissable verdicts, and runs hourly off the existing maintenance loop. Re-grab and "grab alternative" reuse the existing grab path via a new `force=True` bypass and a shared `_run_grab` wrapper extracted from the current route — no new JDownloader integration.

**Tech Stack:** FastAPI, SQLite (`DatabaseManager`), the existing `sources` registry, SvelteKit 5 (runes). Deploy via `docker compose up -d --build` only.

## Global Constraints

- Deploy ONLY via `docker compose up -d --build`.
- The reconcile is fail-safe per item — one malformed grab categorizes `unknown`, never crashes the pass.
- `package_name`/`last_grabbed_at`/`service_type` are nullable and best-effort; pre-feature grabs simply don't reconcile-link.
- `compute_package_name()` is the ONE place the truncated package-name string is computed; used at both the send site and the persist site.
- The not-in-Plex gate compares `rename_jobs.processed_at` against `get_plex_cache_max_timestamp()` — Plex-cache FRESHNESS, never raw elapsed time.
- Regrab/grab-alternative use `force=True` on `download_item` and go through the shared `_run_grab` helper — never call `download_item` bare from a new call site.
- All pipeline routes take identifiers in the request BODY, never a path parameter.
- `pipeline_verdicts.excluded_uuid` ACCUMULATES (comma-joined) across regrabs — never overwritten.
- Reconcile eligibility uses `IS NOT 'verified'`, never `!=` (NULL-safety — a just-cleared verdict has `category IS NULL`).
- Backend tests run on the HOST: `python -m pytest tests/<file> -v` (no `--timeout`). Frontend: `cd frontend && npx vitest run`, `npm run check`, `npm run build`.
- Tests accompany each unit; deploy only after the changed-module suites are green.

---

## File Structure

**Backend (new):** `backend/pipeline_service.py` (categorize + matching + reconcile_batch), `backend/api/routes/pipeline.py` (5 endpoints + `_run_grab`... actually `_run_grab` lives in `downloads.py`, imported by `pipeline.py`).

**Backend (modify):** `backend/database.py` (3 new `downloads` columns, `pipeline_verdicts` table + indexes + CRUD, `clear_history` cascade), `backend/download_service.py` (`compute_package_name`, `download_item(force=...)`), `backend/api/routes/downloads.py` (extract `_run_grab` from `_do_download`), `backend/app_service.py` (maintenance-loop hook), `backend/config.py` (2 new settings), `backend/api/main.py` (register the new router).

**Frontend (new):** `frontend/src/routes/pipeline/+page.svelte`, `frontend/src/lib/components/pipeline/SourceSearchModal.svelte`.

**Frontend (modify):** `frontend/src/lib/api/types.ts`, `frontend/src/lib/api/client.ts`, `frontend/src/routes/downloads/+page.svelte` (mobile `?view=pipeline` switch), `frontend/src/lib/components/Sidebar.svelte` (desktop nav entry).

**Tests:** `tests/test_database.py`, `tests/test_pipeline_service.py` (new), `tests/test_download_service.py`, `tests/test_api_routes.py`, `frontend/src/lib/downloads/pipeline.test.ts` (new, if pure frontend logic is extracted).

---

## Task 1: `downloads` schema additions + `compute_package_name`

**Files:** Modify `backend/database.py` (the `_column_migrations` list ~line 533; `add_to_history` ~line 911), `backend/download_service.py` (new module-level helper; the existing inline computation ~line 1976). Test `tests/test_database.py`, `tests/test_download_service.py`.

**Interfaces:**
- Produces: `compute_package_name(title: str, year: Optional[int], resolution: str) -> str` (module-level in `download_service.py`, importable).
- `add_to_history(url, title, normalized_title=None, season=None, resolution=None, size=None, status="completed", hdr=None, dovi=False, year=None, package_name=None, service_type=None)`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_download_service.py (add)
from backend.download_service import compute_package_name

def test_compute_package_name_title_year_resolution():
    assert compute_package_name("The Matrix", 1999, "1080p") == "The Matrix (1999) [1080p]"

def test_compute_package_name_no_year():
    assert compute_package_name("Some Show S01", None, "1080p") == "Some Show S01 [1080p]"

def test_compute_package_name_no_resolution():
    assert compute_package_name("The Matrix", 1999, "") == "The Matrix (1999)"

def test_compute_package_name_empty_title_falls_back():
    assert compute_package_name("", 1999, "1080p") == "ScanHound Download"

def test_compute_package_name_truncates_at_50():
    long_title = "The Lord of the Rings: The Return of the King"
    name = compute_package_name(long_title, 2003, "2160p")
    assert len(name) <= 50
    assert name == f"{long_title} (2003) [2160p]"[:50]
```

```python
# tests/test_database.py (add)
def test_add_to_history_persists_package_name_and_service_type(db_manager):
    db_manager.add_to_history("http://x/1", "Foo", package_name="Foo (2024) [1080p]",
                              service_type="Rapidgator")
    row = db_manager.get_downloaded_titles()  # or a direct query if that helper doesn't expose it
    # Prefer a direct row check:
    conn = db_manager.get_connection()
    r = conn.execute("SELECT package_name, service_type, last_grabbed_at FROM downloads "
                     "WHERE url = ?", ("http://x/1",)).fetchone()
    assert r[0] == "Foo (2024) [1080p]"
    assert r[1] == "Rapidgator"
    assert r[2] is not None  # last_grabbed_at bumped

def test_add_to_history_coalesces_package_name_on_status_update(db_manager):
    db_manager.add_to_history("http://x/2", "Foo", package_name="Foo (2024) [1080p]")
    db_manager.add_to_history("http://x/2", "Foo", status="failed")  # no package_name this call
    conn = db_manager.get_connection()
    r = conn.execute("SELECT package_name FROM downloads WHERE url = ?", ("http://x/2",)).fetchone()
    assert r[0] == "Foo (2024) [1080p]"  # not nulled out

def test_add_to_history_bumps_last_grabbed_at_on_every_call(db_manager):
    db_manager.add_to_history("http://x/3", "Foo", package_name="Foo [1080p]")
    conn = db_manager.get_connection()
    first = conn.execute("SELECT last_grabbed_at FROM downloads WHERE url = ?", ("http://x/3",)).fetchone()[0]
    import time; time.sleep(1.1)
    db_manager.add_to_history("http://x/3", "Foo", package_name="Foo [1080p]", status="completed")
    second = conn.execute("SELECT last_grabbed_at FROM downloads WHERE url = ?", ("http://x/3",)).fetchone()[0]
    assert second >= first  # bumped forward (or equal at 1s SQLite resolution boundary)
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/test_download_service.py -k compute_package_name tests/test_database.py -k "add_to_history" -v`
Expected: FAIL (`compute_package_name` doesn't exist; `package_name`/`service_type`/`last_grabbed_at` columns don't exist).

- [ ] **Step 3: Implement**

In `backend/database.py`, add to the `_column_migrations` list (near line 533, alongside the other `ALTER TABLE downloads ADD COLUMN` entries already there):

```python
                    'ALTER TABLE downloads ADD COLUMN package_name TEXT',
                    'ALTER TABLE downloads ADD COLUMN last_grabbed_at TIMESTAMP',
                    'ALTER TABLE downloads ADD COLUMN service_type TEXT',
```

Replace `add_to_history` (currently ~line 911) with:

```python
    def add_to_history(self, url, title, normalized_title=None, season=None,
                       resolution=None, size=None, status="completed",
                       hdr=None, dovi=False, year=None, package_name=None,
                       service_type=None):
        """Record a downloaded URL with optional metadata for title-based matching.

        Uses ON CONFLICT to preserve the original date_added when re-downloading.
        ``package_name``/``service_type`` are COALESCEd so a later status-only
        update never nulls out an already-known value. ``last_grabbed_at`` is
        bumped unconditionally on every call — every call that reaches this
        method (success, clipboard, browser, failed-send) is a genuine new
        attempt, and this is what the pipeline reconcile's matching window
        keys off for a regrab.
        """
        return self._mutate('''
            INSERT INTO downloads (url, title, normalized_title, season, resolution, size, status, hdr, dovi, year, package_name, service_type, last_grabbed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(url) DO UPDATE SET
                title = excluded.title,
                normalized_title = excluded.normalized_title,
                season = excluded.season,
                resolution = excluded.resolution,
                size = excluded.size,
                status = excluded.status,
                hdr = excluded.hdr,
                dovi = excluded.dovi,
                year = COALESCE(excluded.year, downloads.year),
                package_name = COALESCE(excluded.package_name, downloads.package_name),
                service_type = COALESCE(excluded.service_type, downloads.service_type),
                last_grabbed_at = CURRENT_TIMESTAMP
        ''', (url, title, normalized_title, season, resolution, size, status,
              hdr or None, 1 if dovi else 0, year, package_name, service_type),
            label="add_history")
```

In `backend/download_service.py`, add near the top of the file (module level, after imports):

```python
def compute_package_name(title: str, year: Optional[int], resolution: str) -> str:
    """Canonical JDownloader package-name string — the join key used by the
    pipeline tracker across downloads/download_results/rename_jobs. Must match
    send_to_jdownloader's truncation exactly (both its delivery paths truncate
    to 50 chars before JD ever sees the name) — this is the single place that
    string is computed, so the persisted value and the sent value can never
    drift apart."""
    if not title:
        return "ScanHound Download"[:50]
    name = f"{title} ({year})" if year else title
    package_name = f"{name} [{resolution}]" if resolution else name
    return package_name[:50]
```

Replace the inline computation at the existing call site (~line 1976-1980):

```python
        package_name = compute_package_name(title, year, resolution)
```

(Removes the old inline `if title: name = ...; package_name = ...; else: package_name = "ScanHound Download"` block — `compute_package_name` now owns that logic.)

Thread `package_name`/`service_type` into every `save_to_history(...)` call site in `download_item` (the four call sites at ~2000, 2020, 2034, 2048) by adding `package_name=package_name, service_type=service_type` to each call (`service_type` is already a parameter on `download_item`, just not yet passed to `save_to_history`).

- [ ] **Step 4: Run to verify it passes**

Run: `python -m pytest tests/test_database.py tests/test_download_service.py -v`
Expected: PASS (new tests + full existing suites unchanged).

- [ ] **Step 5: Commit**

```bash
git add backend/database.py backend/download_service.py tests/test_database.py tests/test_download_service.py
git commit -m "feat(pipeline): downloads gains package_name/last_grabbed_at/service_type; compute_package_name helper

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Task 2: `pipeline_verdicts` table + CRUD

**Files:** Modify `backend/database.py` (add the table creation near the `downloads`/`download_results` CREATE TABLE block ~line 261-373; add indexes near ~line 521; add CRUD methods near `add_to_history`/`clear_history` ~line 907; modify `clear_history` for cascade). Test `tests/test_database.py`.

**Interfaces:**
- Produces: `get_pipeline_verdicts(category=None, include_dismissed=False) -> list[dict]`, `upsert_pipeline_verdict(url, category, detail=None, package_uuid=None, plex_rating_key=None, dismissed=False) -> None`, `dismiss_pipeline_verdict(url) -> None`, `clear_pipeline_verdict(url) -> None`, `get_downloads_needing_reconcile(limit=500) -> list[dict]`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_database.py (add)
def test_pipeline_verdict_upsert_and_get(db_manager):
    db_manager.add_to_history("http://p/1", "Foo", package_name="Foo [1080p]")
    db_manager.upsert_pipeline_verdict("http://p/1", "download_failed", detail="offline",
                                       package_uuid="111")
    rows = db_manager.get_pipeline_verdicts()
    assert len(rows) == 1
    assert rows[0]["category"] == "download_failed"
    assert rows[0]["detail"] == "offline"
    assert rows[0]["package_uuid"] == "111"

def test_pipeline_verdict_dismiss_excluded_by_default(db_manager):
    db_manager.add_to_history("http://p/2", "Foo", package_name="Foo [1080p]")
    db_manager.upsert_pipeline_verdict("http://p/2", "rename_failed")
    db_manager.dismiss_pipeline_verdict("http://p/2")
    assert db_manager.get_pipeline_verdicts() == []
    assert len(db_manager.get_pipeline_verdicts(include_dismissed=True)) == 1

def test_clear_pipeline_verdict_accumulates_excluded_uuid_not_overwrites(db_manager):
    db_manager.add_to_history("http://p/3", "Foo", package_name="Foo [1080p]")
    db_manager.upsert_pipeline_verdict("http://p/3", "download_failed", package_uuid="111")
    db_manager.clear_pipeline_verdict("http://p/3")  # 1st regrab: excludes 111
    db_manager.upsert_pipeline_verdict("http://p/3", "download_failed", package_uuid="222")
    db_manager.clear_pipeline_verdict("http://p/3")  # 2nd regrab: must ALSO still exclude 111
    conn = db_manager.get_connection()
    row = conn.execute("SELECT excluded_uuid, category, package_uuid FROM pipeline_verdicts "
                       "WHERE url = ?", ("http://p/3",)).fetchone()
    assert "111" in row[0] and "222" in row[0]
    assert row[1] is None  # category reset to NULL (pending re-evaluation)
    assert row[2] is None  # package_uuid cleared

def test_get_downloads_needing_reconcile_excludes_verified_and_dismissed(db_manager):
    db_manager.add_to_history("http://p/4", "A", package_name="A [1080p]")
    db_manager.add_to_history("http://p/5", "B", package_name="B [1080p]")
    db_manager.add_to_history("http://p/6", "C", package_name="C [1080p]")
    db_manager.upsert_pipeline_verdict("http://p/4", "verified")
    db_manager.upsert_pipeline_verdict("http://p/5", "download_failed")
    db_manager.dismiss_pipeline_verdict("http://p/5")
    # p/6 has no verdict yet — must be included once past the 30-min window; force
    # last_grabbed_at back in time for this test:
    conn = db_manager.get_connection()
    conn.execute("UPDATE downloads SET last_grabbed_at = datetime('now', '-1 hour') "
                 "WHERE url = 'http://p/6'")
    conn.commit()
    rows = db_manager.get_downloads_needing_reconcile(limit=100)
    urls = {r["url"] for r in rows}
    assert "http://p/4" not in urls  # verified, terminal
    assert "http://p/5" not in urls  # dismissed
    assert "http://p/6" in urls      # no verdict, past the 30-min window

def test_get_downloads_needing_reconcile_reincludes_null_category_after_clear(db_manager):
    # N1 regression: a category=NULL row (just-cleared by regrab) must be
    # reconcile-eligible, not silently excluded by a `!=` comparison.
    db_manager.add_to_history("http://p/7", "D", package_name="D [1080p]")
    conn = db_manager.get_connection()
    conn.execute("UPDATE downloads SET last_grabbed_at = datetime('now', '-1 hour') "
                 "WHERE url = 'http://p/7'")
    conn.commit()
    db_manager.upsert_pipeline_verdict("http://p/7", "download_failed")
    db_manager.clear_pipeline_verdict("http://p/7")  # category -> NULL
    rows = db_manager.get_downloads_needing_reconcile(limit=100)
    assert "http://p/7" in {r["url"] for r in rows}

def test_clear_history_cascades_pipeline_verdicts(db_manager):
    db_manager.add_to_history("http://p/8", "E", package_name="E [1080p]")
    db_manager.upsert_pipeline_verdict("http://p/8", "download_failed")
    db_manager.clear_history()
    assert db_manager.get_pipeline_verdicts(include_dismissed=True) == []
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/test_database.py -k pipeline_verdict -v`
Expected: FAIL (table/methods don't exist).

- [ ] **Step 3: Implement**

Add the table creation immediately after the `downloads` `CREATE TABLE IF NOT EXISTS` block (~line 266, right after its closing `''')`):

```python
                # Pipeline-tracker reconcile verdicts — one row per grab url,
                # persisted so 'verified' is terminal and Dismiss survives
                # even after the underlying stage rows age out. See
                # docs/superpowers/specs/2026-07-10-pipeline-tracker-design.md.
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS pipeline_verdicts (
                        url TEXT PRIMARY KEY REFERENCES downloads(url),
                        category TEXT,
                        detail TEXT,
                        package_uuid TEXT,
                        excluded_uuid TEXT,
                        plex_rating_key TEXT,
                        checked_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        dismissed INTEGER DEFAULT 0
                    )
                ''')
```

Add the index in the shared idempotent index section (near line 521, alongside the `download_results` indexes):

```python
                cursor.execute('CREATE INDEX IF NOT EXISTS idx_pipeline_verdicts_category '
                               'ON pipeline_verdicts(category)')
```

Add these methods to `DatabaseManager`, near `add_to_history`/`clear_history`:

```python
    def get_pipeline_verdicts(self, category=None, include_dismissed=False):
        """Return pipeline verdicts, joined with their downloads/rename_jobs
        display fields, most-recently-checked first."""
        clauses = []
        params = []
        if not include_dismissed:
            clauses.append("v.dismissed = 0")
        if category:
            clauses.append("v.category = ?")
            params.append(category)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        return self._query_dicts(f'''
            SELECT v.url, v.category, v.detail, v.package_uuid, v.excluded_uuid,
                   v.plex_rating_key, v.checked_at, v.dismissed,
                   d.title, d.year, d.season, d.resolution, d.package_name
            FROM pipeline_verdicts v
            JOIN downloads d ON d.url = v.url
            {where}
            ORDER BY v.checked_at DESC
        ''', tuple(params))

    def upsert_pipeline_verdict(self, url, category, detail=None, package_uuid=None,
                                plex_rating_key=None, dismissed=False):
        """Insert/update a verdict for url. checked_at is always refreshed
        explicitly — the column DEFAULT only fires on INSERT, never UPDATE."""
        return self._mutate('''
            INSERT INTO pipeline_verdicts (url, category, detail, package_uuid, plex_rating_key, dismissed, checked_at)
            VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(url) DO UPDATE SET
                category = excluded.category,
                detail = excluded.detail,
                package_uuid = excluded.package_uuid,
                plex_rating_key = excluded.plex_rating_key,
                dismissed = excluded.dismissed,
                checked_at = CURRENT_TIMESTAMP
        ''', (url, category, detail, package_uuid, plex_rating_key, 1 if dismissed else 0),
            label="upsert_pipeline_verdict")

    def dismiss_pipeline_verdict(self, url):
        return self._mutate(
            "UPDATE pipeline_verdicts SET dismissed = 1, checked_at = CURRENT_TIMESTAMP WHERE url = ?",
            (url,), label="dismiss_pipeline_verdict")

    def clear_pipeline_verdict(self, url):
        """Called by regrab/grab-alternative: move any confirmed package_uuid
        into excluded_uuid (accumulating — comma-joined, never overwritten, so
        a second-in-a-row regrab can't un-exclude the first's stale package),
        clear package_uuid, and reset category to NULL ('pending
        re-evaluation' — always reconcile-eligible)."""
        try:
            with self._lock:
                conn = self.get_connection()
                if not conn:
                    return False
                cur = conn.cursor()
                cur.execute("SELECT package_uuid, excluded_uuid FROM pipeline_verdicts WHERE url = ?", (url,))
                row = cur.fetchone()
                if row is None:
                    cur.execute(
                        "INSERT INTO pipeline_verdicts (url, category, checked_at) "
                        "VALUES (?, NULL, CURRENT_TIMESTAMP)", (url,))
                    conn.commit()
                    return True
                pkg_uuid, excluded = row[0], row[1]
                if pkg_uuid is None:
                    new_excluded = excluded
                elif excluded is None:
                    new_excluded = pkg_uuid
                else:
                    new_excluded = f"{excluded},{pkg_uuid}"
                cur.execute(
                    "UPDATE pipeline_verdicts SET excluded_uuid = ?, package_uuid = NULL, "
                    "category = NULL, dismissed = 0, checked_at = CURRENT_TIMESTAMP WHERE url = ?",
                    (new_excluded, url))
                conn.commit()
                return True
        except Exception as e:
            logger.error("DB Error (clear_pipeline_verdict): %s", e)
            return False

    def get_downloads_needing_reconcile(self, limit=500):
        """Grabs eligible for the pipeline reconcile pass: have a package_name,
        are past the 30-minute too-soon-to-judge window, and are not yet
        dismissed/verified (terminal). Uses IS NOT (not !=) so a just-cleared
        verdict — category IS NULL — is correctly re-included: SQL NULL != 'x'
        is NULL/falsy, which would otherwise permanently freeze a regrab.
        Ordered oldest-checked-first for round-robin fairness under a large
        backlog (NULLs — never checked — sort first)."""
        return self._query_dicts('''
            SELECT d.url, d.title, d.year, d.season, d.resolution, d.size, d.hdr, d.dovi,
                   d.package_name, d.service_type, d.last_grabbed_at,
                   v.category AS verdict_category, v.dismissed AS verdict_dismissed,
                   v.package_uuid, v.excluded_uuid
            FROM downloads d
            LEFT JOIN pipeline_verdicts v ON v.url = d.url
            WHERE d.package_name IS NOT NULL
              AND d.last_grabbed_at <= datetime('now', '-30 minutes')
              AND (v.url IS NULL OR (v.dismissed = 0 AND v.category IS NOT 'verified'))
            ORDER BY v.checked_at ASC
            LIMIT ?
        ''', (limit,))
```

Modify `clear_history` to cascade:

```python
    def clear_history(self):
        """Delete all download history records (and their pipeline verdicts)."""
        try:
            with self._lock:
                conn = self.get_connection()
                if not conn:
                    return False
                conn.execute("DELETE FROM pipeline_verdicts")
                conn.execute("DELETE FROM downloads")
                conn.commit()
            return True
        except Exception as e:
            logger.error("DB Error (clear_history): %s", e)
            return False
```

(Note: `pipeline_verdicts.url REFERENCES downloads(url)` is documentation only —
this connection never enables `PRAGMA foreign_keys` — so the manual two-DELETE
cascade above is the real enforcement; do not rely on the `REFERENCES` clause.)

- [ ] **Step 4: Run to verify it passes**

Run: `python -m pytest tests/test_database.py -v`
Expected: PASS (new tests + full existing suite).

- [ ] **Step 5: Commit**

```bash
git add backend/database.py tests/test_database.py
git commit -m "feat(pipeline): pipeline_verdicts table + CRUD; clear_history cascades

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Task 3: `pipeline_service.categorize` (pure function)

**Files:** Create `backend/pipeline_service.py`, `tests/test_pipeline_service.py`.

**Interfaces:**
- Produces: `categorize(download_row: dict, result_row: dict | None, rename_rows: list[dict], plex_max_ts: dict, jd_method: str, grace_margin_minutes: int = 30) -> tuple[str, str | None, str | None, str | None]` — returns `(category, detail, package_uuid, plex_rating_key)`.
- Produces: `find_plex_match(db, imdb_id, title, year, season, resolution) -> dict | None` (helper called by `categorize` for the `verified` check — kept separate so it's independently testable).
- Consumes: `DatabaseManager.get_plex_cache_max_timestamp()` (existing), a `plex_cache` row lookup (new small query — see Step 3).

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_pipeline_service.py
from datetime import datetime, timezone, timedelta
from backend.pipeline_service import categorize

def _download_row(**kw):
    base = {"url": "http://x/1", "title": "Foo", "year": 2024, "season": None,
            "resolution": "2160p", "last_grabbed_at": "2026-07-10 10:00:00"}
    base.update(kw)
    return base

def _rename_row(**kw):
    base = {"status": "applied", "media_type": "movie", "imdb_id": "tt123",
            "title": "Foo", "year": 2024, "season": None, "resolution": "2160p",
            "processed_at": datetime.now(timezone.utc).isoformat(),
            "error_message": None, "warning_message": None}
    base.update(kw)
    return base

class TestNeverStartedAndFolderMode:
    def test_no_results_row_api_mode_past_30min_is_never_started(self):
        d = _download_row(last_grabbed_at="2020-01-01 00:00:00")  # long past 30 min
        cat, detail, uuid, rk = categorize(d, None, [], {}, jd_method="api")
        assert cat == "never_started"

    def test_no_results_row_folder_mode_is_unknown_not_never_started(self):
        d = _download_row(last_grabbed_at="2020-01-01 00:00:00")
        cat, *_ = categorize(d, None, [], {}, jd_method="folder")
        assert cat == "unknown"

    def test_no_results_row_within_30min_writes_no_verdict(self):
        d = _download_row(last_grabbed_at=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"))
        cat, *_ = categorize(d, None, [], {}, jd_method="api")
        assert cat is None  # too soon to judge

class TestDownloadStates:
    def test_failed_state_is_download_failed_with_detail(self):
        r = {"state": "failed", "error": "Link offline", "package_uuid": "111"}
        cat, detail, uuid, rk = categorize(_download_row(), r, [], {}, jd_method="api")
        assert cat == "download_failed" and detail == "Link offline" and uuid == "111"

    def test_queued_downloading_extracting_are_in_progress(self):
        for state in ("queued", "downloading", "extracting", "downloaded"):
            r = {"state": state, "error": None, "package_uuid": "111"}
            cat, *_ = categorize(_download_row(), r, [], {}, jd_method="api")
            assert cat == "in_progress", state

    def test_extracted_with_no_rename_rows_is_pending_rename(self):
        r = {"state": "extracted", "error": None, "package_uuid": "111"}
        cat, *_ = categorize(_download_row(), r, [], {}, jd_method="api")
        assert cat == "pending_rename"

class TestRenameStates:
    def _extracted_result(self):
        return {"state": "extracted", "error": None, "package_uuid": "111"}

    def test_any_failed_or_needs_review_is_rename_failed(self):
        rows = [_rename_row(status="applied"), _rename_row(status="failed", error_message="boom")]
        cat, detail, *_ = categorize(_download_row(), self._extracted_result(), rows, {}, jd_method="api")
        assert cat == "rename_failed" and detail == "boom"

    def test_pending_matched_applying_map_to_pending_rename(self):
        for status in ("pending", "matched", "applying"):
            rows = [_rename_row(status=status)]
            cat, *_ = categorize(_download_row(), self._extracted_result(), rows, {}, jd_method="api")
            assert cat == "pending_rename", status

    def test_reverted_is_rename_failed(self):
        rows = [_rename_row(status="reverted")]
        cat, detail, *_ = categorize(_download_row(), self._extracted_result(), rows, {}, jd_method="api")
        assert cat == "rename_failed" and detail == "reverted"

class TestPlexGate:
    def _extracted_result(self):
        return {"state": "extracted", "error": None, "package_uuid": "111"}

    def test_cache_stale_relative_to_rename_stays_in_progress(self):
        # rename applied "now"; plex cache max timestamp is from BEFORE that,
        # even though wall-clock time since the rename is large.
        processed = datetime.now(timezone.utc).isoformat()
        rows = [_rename_row(status="applied", processed_at=processed)]
        stale_cache = {"Movies": (datetime.now(timezone.utc) - timedelta(days=2)).timestamp()}
        cat, *_ = categorize(_download_row(), self._extracted_result(), rows, stale_cache, jd_method="api")
        assert cat == "in_progress"

    def test_cache_fresh_after_rename_plus_margin_runs_real_check(self, monkeypatch):
        processed = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        rows = [_rename_row(status="applied", processed_at=processed, resolution="2160p")]
        fresh_cache = {"Movies": datetime.now(timezone.utc).timestamp()}
        import backend.pipeline_service as ps
        monkeypatch.setattr(ps, "find_plex_match", lambda *a, **k: None)
        cat, *_ = categorize(_download_row(), self._extracted_result(), rows, fresh_cache, jd_method="api")
        assert cat == "not_in_plex"

    def test_resolution_normalization_2160p_matches_4k(self, monkeypatch):
        import backend.pipeline_service as ps
        rows = [_rename_row(status="applied", resolution="2160p")]
        fresh_cache = {"Movies": datetime.now(timezone.utc).timestamp() + 10000}
        monkeypatch.setattr(ps, "find_plex_match",
                            lambda db, imdb_id, title, year, season, resolution: {"rating_key": "rk1"})
        cat, detail, uuid, rk = categorize(_download_row(), self._extracted_result(), rows,
                                           fresh_cache, jd_method="api")
        assert cat == "verified" and rk == "rk1"

class TestMalformed:
    def test_malformed_input_never_raises(self):
        cat, *_ = categorize({}, {"state": "bogus"}, [{"status": "bogus"}], {}, jd_method="api")
        assert cat == "unknown"
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/test_pipeline_service.py -v`
Expected: FAIL (`backend/pipeline_service.py` doesn't exist).

- [ ] **Step 3: Implement**

```python
# backend/pipeline_service.py
"""Reconcile every grab through download -> extraction -> rename -> Plex
ingestion into one categorized verdict. Pure/fail-safe: categorize() never
raises. See docs/superpowers/specs/2026-07-10-pipeline-tracker-design.md."""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)

_ACTIVE_DOWNLOAD_STATES = {"queued", "downloading", "extracting", "downloaded"}
_PENDING_RENAME_STATUSES = {"pending", "matched", "applying"}
_FAILED_RENAME_STATUSES = {"failed", "needs_review"}

# ffprobe-style '2160p'/'4K' both mean UHD; Plex's plex_cache.res literal is
# one of "4K"/"1080p"/"720p"/"?" (backend/plex_service.py).
_RES_EQUIV = {"2160p": "4k", "4k": "4k", "1080p": "1080p", "720p": "720p"}


def _normalize_res(res: Optional[str]) -> Optional[str]:
    if not res:
        return None
    return _RES_EQUIV.get(str(res).lower(), str(res).lower())


def find_plex_match(db, imdb_id: Optional[str], title: Optional[str],
                    year: Optional[int], season: Optional[int],
                    resolution: Optional[str]) -> Optional[dict]:
    """Look up a plex_cache row for this rename: imdb_id first, else
    normalized title+year; require season match for TV; skip the resolution
    check when either side is unknown rather than failing it strictly."""
    from backend.app_service import normalize_title  # existing helper (clean_string alias)
    try:
        conn = db.get_connection()
        if not conn:
            return None
        cur = conn.cursor()
        row = None
        if imdb_id:
            cur.execute("SELECT * FROM plex_cache WHERE imdb_id = ?", (imdb_id,))
            row = cur.fetchone()
        if row is None and title:
            norm = normalize_title(title)
            cur.execute("SELECT * FROM plex_cache")
            for candidate in cur.fetchall():
                cdict = dict(candidate)
                if normalize_title(cdict.get("title") or "") != norm:
                    continue
                if year and cdict.get("year") and int(cdict["year"]) != int(year):
                    continue
                row = candidate
                break
        if row is None:
            return None
        rdict = dict(row)
        if season is not None and rdict.get("season") is not None and int(rdict["season"]) != int(season):
            return None
        want_res = _normalize_res(resolution)
        have_res = _normalize_res(rdict.get("res"))
        if want_res and have_res and want_res != have_res:
            return None
        return rdict
    except Exception:
        logger.exception("find_plex_match failed")
        return None


def categorize(download_row: dict, result_row: Optional[dict], rename_rows: list,
               plex_max_ts: dict, jd_method: str, grace_margin_minutes: int = 30,
               db=None) -> tuple:
    """Returns (category, detail, package_uuid, plex_rating_key). Never raises
    — any unexpected shape falls through to ('unknown', None, None, None)."""
    try:
        if result_row is None:
            if jd_method != "api":
                return ("unknown", "folder-mode grab has no results row to reconcile", None, None)
            last_grabbed = download_row.get("last_grabbed_at")
            if last_grabbed and _minutes_since(last_grabbed) > 30:
                return ("never_started", None, None, None)
            return (None, None, None, None)  # too soon to judge

        state = result_row.get("state")
        package_uuid = result_row.get("package_uuid")

        if state == "failed":
            return ("download_failed", result_row.get("error"), package_uuid, None)
        if state in _ACTIVE_DOWNLOAD_STATES:
            return ("in_progress", None, package_uuid, None)
        if state == "extracted" and not rename_rows:
            return ("pending_rename", None, package_uuid, None)

        if any(r.get("status") in _FAILED_RENAME_STATUSES for r in rename_rows):
            failed = next(r for r in rename_rows if r.get("status") in _FAILED_RENAME_STATUSES)
            detail = failed.get("error_message") or failed.get("warning_message")
            return ("rename_failed", detail, package_uuid, None)
        if any(r.get("status") in _PENDING_RENAME_STATUSES for r in rename_rows):
            return ("pending_rename", None, package_uuid, None)
        if any(r.get("status") == "reverted" for r in rename_rows):
            return ("rename_failed", "reverted", package_uuid, None)

        if rename_rows and all(r.get("status") == "applied" for r in rename_rows):
            latest = max(rename_rows, key=lambda r: r.get("processed_at") or "")
            processed_at = latest.get("processed_at")
            if not processed_at:
                return ("unknown", None, package_uuid, None)
            dt = datetime.fromisoformat(processed_at)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            content_type = "TV Shows" if latest.get("media_type") == "tv" else "Movies"
            cache_max = plex_max_ts.get(content_type, 0)
            if cache_max < dt.timestamp() + grace_margin_minutes * 60:
                return ("in_progress", None, package_uuid, None)
            resolution = latest.get("resolution") or download_row.get("resolution")
            match = find_plex_match(db, latest.get("imdb_id"), latest.get("title"),
                                    latest.get("year"), latest.get("season"), resolution)
            if match:
                return ("verified", None, package_uuid, str(match.get("rating_key") or ""))
            return ("not_in_plex", None, package_uuid, None)

        return ("unknown", None, package_uuid, None)
    except Exception:
        logger.exception("categorize failed for %s", download_row.get("url"))
        return ("unknown", "categorize error", None, None)


def _minutes_since(sqlite_timestamp: str) -> float:
    try:
        dt = datetime.strptime(sqlite_timestamp, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return 0.0
    return (datetime.now(timezone.utc) - dt).total_seconds() / 60.0
```

- [ ] **Step 4: Run to verify it passes**

Run: `python -m pytest tests/test_pipeline_service.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/pipeline_service.py tests/test_pipeline_service.py
git commit -m "feat(pipeline): categorize() reconcile core logic

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Task 4: Matching logic + `reconcile_batch`

**Files:** Modify `backend/pipeline_service.py`. Test `tests/test_pipeline_service.py`.

**Interfaces:**
- Produces: `reconcile_batch(db, limit: int = 500) -> int` — returns count of grabs processed.
- Consumes: `db.get_downloads_needing_reconcile`, `db.upsert_pipeline_verdict`, `db.get_plex_cache_max_timestamp` (all from Task 1/2), a new small `db` query for `download_results`/`rename_jobs` batched by name/uuid (added in this task as private helpers, not new `DatabaseManager` methods — keep the batching logic in `pipeline_service.py` using `db.get_connection()` directly, matching the pattern other reconcile-style code in this codebase uses).

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_pipeline_service.py (add)
from backend.pipeline_service import reconcile_batch
from backend.database import DatabaseManager

class TestMatchingAndReconcileBatch:
    def test_uuid_recorded_verdict_matches_directly(self, db_manager):
        db_manager.add_to_history("http://m/1", "Foo", package_name="Foo [1080p]")
        conn = db_manager.get_connection()
        conn.execute("UPDATE downloads SET last_grabbed_at = datetime('now','-1 hour') "
                     "WHERE url='http://m/1'")
        conn.execute("INSERT INTO download_results (package_uuid, name, state, updated_at) "
                     "VALUES ('999', 'Foo [1080p]', 'failed', datetime('now'))")
        conn.commit()
        db_manager.upsert_pipeline_verdict("http://m/1", "download_failed", package_uuid="999")
        n = reconcile_batch(db_manager)
        assert n >= 1
        rows = db_manager.get_pipeline_verdicts()
        assert rows[0]["package_uuid"] == "999"

    def test_max_id_tiebreak_not_state_progression(self, db_manager):
        # Two rows, SAME name, SAME updated_at (simulating a post-restart
        # repoll bump) — the OLDER row (lower id) is further-along ('extracted'),
        # the NEWER row (higher id) is earlier-stage ('downloading'). The
        # higher id must win.
        db_manager.add_to_history("http://m/2", "Bar", package_name="Bar [1080p]")
        conn = db_manager.get_connection()
        conn.execute("UPDATE downloads SET last_grabbed_at = datetime('now','-1 hour') "
                     "WHERE url='http://m/2'")
        conn.execute("INSERT INTO download_results (package_uuid, name, state, updated_at) "
                     "VALUES ('old-uuid', 'Bar [1080p]', 'extracted', datetime('now'))")
        conn.execute("INSERT INTO download_results (package_uuid, name, state, updated_at) "
                     "VALUES ('new-uuid', 'Bar [1080p]', 'downloading', datetime('now'))")
        conn.commit()
        reconcile_batch(db_manager)
        rows = db_manager.get_pipeline_verdicts()
        row = next(r for r in rows if r["url"] == "http://m/2")
        assert row["package_uuid"] == "new-uuid"

    def test_excluded_uuid_prevents_readopting_stale_package(self, db_manager):
        db_manager.add_to_history("http://m/3", "Baz", package_name="Baz [1080p]")
        conn = db_manager.get_connection()
        conn.execute("UPDATE downloads SET last_grabbed_at = datetime('now','-1 hour') "
                     "WHERE url='http://m/3'")
        conn.execute("INSERT INTO download_results (package_uuid, name, state, updated_at) "
                     "VALUES ('stale-uuid', 'Baz [1080p]', 'extracted', datetime('now'))")
        conn.commit()
        db_manager.upsert_pipeline_verdict("http://m/3", "rename_failed", package_uuid="stale-uuid")
        db_manager.clear_pipeline_verdict("http://m/3")  # excludes stale-uuid
        n = reconcile_batch(db_manager)  # only the stale row exists — must NOT re-adopt it
        rows = db_manager.get_pipeline_verdicts(include_dismissed=True)
        row = next(r for r in rows if r["url"] == "http://m/3")
        assert row["package_uuid"] is None  # no match found, not the excluded stale row

    def test_reconcile_batch_is_batched_not_n_plus_1(self, db_manager, monkeypatch):
        # Seed 5 eligible grabs; assert the number of raw connection queries
        # stays small (a handful, not 5x per-item queries). Approximate via a
        # call-count wrapper on get_connection().execute.
        for i in range(5):
            db_manager.add_to_history(f"http://m/batch{i}", f"T{i}", package_name=f"T{i} [1080p]")
        conn = db_manager.get_connection()
        conn.execute("UPDATE downloads SET last_grabbed_at = datetime('now','-1 hour') "
                     "WHERE url LIKE 'http://m/batch%'")
        conn.commit()
        calls = {"n": 0}
        real_execute = conn.execute
        def counting_execute(*a, **k):
            calls["n"] += 1
            return real_execute(*a, **k)
        monkeypatch.setattr(conn, "execute", counting_execute)
        reconcile_batch(db_manager)
        assert calls["n"] < 20  # well under one-query-per-item x several tables

    def test_malformed_row_does_not_stop_the_batch(self, db_manager):
        db_manager.add_to_history("http://m/ok", "OK", package_name="OK [1080p]")
        db_manager.add_to_history("http://m/bad", None, package_name="Bad [1080p]")  # malformed title
        conn = db_manager.get_connection()
        conn.execute("UPDATE downloads SET last_grabbed_at = datetime('now','-1 hour') "
                     "WHERE url IN ('http://m/ok','http://m/bad')")
        conn.commit()
        n = reconcile_batch(db_manager)
        assert n == 2  # both processed, one may categorize 'unknown'

    def test_dismissed_and_verified_not_recomputed(self, db_manager):
        db_manager.add_to_history("http://m/term1", "V", package_name="V [1080p]")
        db_manager.add_to_history("http://m/term2", "D", package_name="D [1080p]")
        db_manager.upsert_pipeline_verdict("http://m/term1", "verified")
        db_manager.upsert_pipeline_verdict("http://m/term2", "download_failed")
        db_manager.dismiss_pipeline_verdict("http://m/term2")
        n = reconcile_batch(db_manager)
        assert n == 0  # nothing eligible

    def test_in_progress_verdict_reconsidered_on_second_pass(self, db_manager):
        # N1 regression: a non-terminal verdict must be re-picked-up even
        # though last_grabbed_at hasn't changed.
        db_manager.add_to_history("http://m/prog", "P", package_name="P [1080p]")
        conn = db_manager.get_connection()
        conn.execute("UPDATE downloads SET last_grabbed_at = datetime('now','-1 hour') "
                     "WHERE url='http://m/prog'")
        conn.execute("INSERT INTO download_results (package_uuid, name, state, updated_at) "
                     "VALUES ('p-uuid', 'P [1080p]', 'downloading', datetime('now'))")
        conn.commit()
        reconcile_batch(db_manager)  # pass 1: writes in_progress
        n2 = reconcile_batch(db_manager)  # pass 2: must still pick it up
        assert n2 >= 1
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/test_pipeline_service.py -k "Matching or reconcile_batch" -v`
Expected: FAIL (`reconcile_batch` doesn't exist).

- [ ] **Step 3: Implement**

Append to `backend/pipeline_service.py`:

```python
def _match_download_results(conn, download_row: dict) -> Optional[dict]:
    """Match one grab to its download_results row: uuid-first (via a
    previously-recorded verdict.package_uuid, passed in via download_row
    under the key 'verdict_package_uuid'), else name+last_grabbed_at-window
    fallback with excluded_uuid filtered out, tiebreaking on MAX(id) (never
    state-progression — see the plan's Task 4 rationale)."""
    uuid = download_row.get("verdict_package_uuid")
    if uuid:
        cur = conn.execute(
            "SELECT * FROM download_results WHERE package_uuid = ?", (uuid,))
        row = cur.fetchone()
        if row is not None:
            return dict(row)
    excluded = (download_row.get("excluded_uuid") or "").split(",")
    excluded = [e for e in excluded if e]
    name = download_row.get("package_name")
    last_grabbed = download_row.get("last_grabbed_at")
    if not name or not last_grabbed:
        return None
    placeholders = ",".join("?" * len(excluded)) if excluded else None
    sql = ("SELECT * FROM download_results WHERE name = ? "
           "AND updated_at >= datetime(?, '-5 seconds') "
           "AND (package_uuid IS NULL")
    params = [name, last_grabbed]
    if placeholders:
        sql += f" OR package_uuid NOT IN ({placeholders})"
        params.extend(excluded)
    sql += ") ORDER BY id DESC LIMIT 1"
    cur = conn.execute(sql, tuple(params))
    row = cur.fetchone()
    return dict(row) if row else None


def _match_rename_rows(conn, package_name: Optional[str]) -> list:
    if not package_name:
        return []
    cur = conn.execute("SELECT * FROM rename_jobs WHERE package_name = ?", (package_name,))
    return [dict(r) for r in cur.fetchall()]


def reconcile_batch(db, limit: int = 500) -> int:
    """Reconcile up to `limit` eligible grabs and upsert their verdicts.
    Returns the count processed. Per-item failures are caught and categorized
    'unknown' rather than aborting the batch (this function itself does not
    swallow batch-level errors — the maintenance-loop caller wraps this in
    its own try/except)."""
    from backend.config import get_default_config  # for defaults only; live config passed by caller
    candidates = db.get_downloads_needing_reconcile(limit=limit)
    if not candidates:
        return 0
    conn = db.get_connection()
    if not conn:
        return 0
    plex_max_ts = db.get_plex_cache_max_timestamp()
    jd_method = (db._cfg.get("jd_method", "folder") if hasattr(db, "_cfg")
                else "api")  # DatabaseManager has no config; caller injects via reconcile_batch's caller (Task 5)
    processed = 0
    for row in candidates:
        try:
            row["verdict_package_uuid"] = row.get("package_uuid")
            result_row = _match_download_results(conn, row)
            rename_rows = _match_rename_rows(conn, row.get("package_name"))
            category, detail, package_uuid, plex_rating_key = categorize(
                row, result_row, rename_rows, plex_max_ts, jd_method=jd_method, db=db)
            if category is not None:
                db.upsert_pipeline_verdict(row["url"], category, detail=detail,
                                          package_uuid=package_uuid,
                                          plex_rating_key=plex_rating_key)
            processed += 1
        except Exception:
            logger.exception("reconcile_batch: item failed for %s", row.get("url"))
            try:
                db.upsert_pipeline_verdict(row["url"], "unknown", detail="reconcile error")
            except Exception:
                pass
            processed += 1
    return processed
```

**Correction during implementation:** `reconcile_batch`'s `jd_method` lookup as
drafted above (`db._cfg`) is a placeholder that does not exist on
`DatabaseManager` — `DatabaseManager` has no config reference. Fix: add a
`jd_method: str = "api"` parameter to `reconcile_batch(db, limit=500,
jd_method="api")` and have the caller (Task 5's maintenance-loop hook, which
DOES have `self.config`) pass `self.config.get("jd_method", "folder")`
explicitly. Remove the dead `get_default_config` import and the `db._cfg`
line entirely; use the parameter instead.

- [ ] **Step 4: Run to verify it passes**

Run: `python -m pytest tests/test_pipeline_service.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/pipeline_service.py tests/test_pipeline_service.py
git commit -m "feat(pipeline): matching (uuid-first, MAX(id) tiebreak, excluded_uuid) + reconcile_batch

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Task 5: Maintenance-loop hook + config settings

**Files:** Modify `backend/app_service.py` (`_run_maintenance_pass`, ~line 569-592), `backend/config.py` (the `AppConfig` class ~line 134 and the default dict ~line 475).

**Interfaces:** Consumes `reconcile_batch(db, limit, jd_method)` from Task 4.

- [ ] **Step 1: Implement (no new test file — this is a thin integration wire; covered by Task 4's unit tests + Task 14's full verification)**

In `backend/config.py`, add to the `AppConfig` class (near `trash_retention_days: int`, ~line 134):

```python
    pipeline_verify_grace_margin_minutes: int
    pipeline_reconcile_enabled: bool
```

Add to the default dict (near `"trash_retention_days": 30,`, ~line 475):

```python
    "pipeline_verify_grace_margin_minutes": 30,
    "pipeline_reconcile_enabled": True,
```

In `backend/app_service.py`, add one more fail-safe block to `_run_maintenance_pass` (immediately after the existing WAL-checkpoint block, ~line 592):

```python
        try:
            if self.db is not None and self.config.get("pipeline_reconcile_enabled", True):
                from backend.pipeline_service import reconcile_batch
                jd_method = self.config.get("jd_method", "folder")
                n = reconcile_batch(self.db, jd_method=jd_method)
                if n:
                    logger.info("Pipeline reconcile: checked %d grab(s)", n)
        except Exception:
            logger.exception("Pipeline reconcile failed (non-fatal)")
```

- [ ] **Step 2: Verify manually**

Run: `python -c "from backend.app_service import AppService"` — must import cleanly (no syntax errors). Then run the Task 4 test suite once more to confirm nothing broke: `python -m pytest tests/test_pipeline_service.py -v`.
Expected: import succeeds, tests still PASS.

- [ ] **Step 3: Commit**

```bash
git add backend/app_service.py backend/config.py
git commit -m "feat(pipeline): hourly maintenance-loop reconcile hook + config settings

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Task 6: `download_item(force=...)` bypass

**Files:** Modify `backend/download_service.py` (`download_item`, ~line 1870-1929). Test `tests/test_download_service.py`.

**Interfaces:** `download_item(url, title, season, resolution, size, service_type="Rapidgator", year=None, hdr="", dovi=False, progress_callback=None, force=False)`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_download_service.py (add)
def test_download_item_force_bypasses_is_downloaded_gate(download_service, monkeypatch):
    # Arrange: url already marked completed in history.
    download_service.db.add_to_history("http://f/1", "Foo", status="completed")
    monkeypatch.setattr(download_service, "scrape_links", lambda *a, **k: ["http://link1"])
    monkeypatch.setattr(download_service, "send_to_jdownloader", lambda *a, **k: True)
    result = download_service.download_item(
        url="http://f/1", title="Foo", season=None, resolution="1080p", size="1GB",
        force=True)
    assert result["method"] != "duplicate"

def test_download_item_force_bypasses_quality_gate(download_service, monkeypatch):
    download_service.db.add_to_history("http://f/2", "Foo", status="completed",
                                       normalized_title="foo", resolution="1080p", year=2024)
    monkeypatch.setattr(download_service, "scrape_links", lambda *a, **k: ["http://link2"])
    monkeypatch.setattr(download_service, "send_to_jdownloader", lambda *a, **k: True)
    result = download_service.download_item(
        url="http://f/3", title="Foo", season=None, resolution="1080p", size="1GB",
        year=2024, force=True)  # different url, same-or-lower quality
    assert result["method"] != "duplicate_similar"

def test_download_item_default_force_false_unchanged(download_service, monkeypatch):
    download_service.db.add_to_history("http://f/4", "Foo", status="completed")
    result = download_service.download_item(
        url="http://f/4", title="Foo", season=None, resolution="1080p", size="1GB")
    assert result["method"] == "duplicate"  # existing gate still blocks a normal accidental duplicate
```

(These reuse whatever `download_service` fixture already exists in `tests/test_download_service.py`'s test harness — construct one identically to the file's existing tests if no shared fixture is present; do not invent a new fixture pattern.)

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/test_download_service.py -k force -v`
Expected: FAIL (`force` param doesn't exist; `force=True` calls raise `TypeError`).

- [ ] **Step 3: Implement**

In `backend/download_service.py`, change the `download_item` signature (~line 1870-1873):

```python
    def download_item(self, url: str, title: str, season: Optional[int],
                      resolution: str, size: str, service_type: str = "Rapidgator",
                      year: Optional[int] = None, hdr: str = "", dovi: bool = False,
                      progress_callback: Optional[Callable] = None,
                      force: bool = False) -> Dict[str, Any]:
```

Wrap both dedup gates (~lines 1893-1929) so they're skipped when `force=True`:

```python
        # Dedup: if this exact release was already grabbed successfully, don't
        # scrape or re-send it — that just creates a duplicate JDownloader entry.
        # (A prior *failed* grab doesn't count, so retries still work.)
        # `force=True` (used only by the pipeline tracker's regrab/grab-alternative
        # actions) skips both gates entirely — that's the user explicitly
        # overriding "don't re-grab," not an accident to guard against.
        if self.db is not None and not force:
            try:
                already = self.db.is_downloaded(url)
            except Exception:
                already = False
            if already:
                result["success"] = True
                result["method"] = "duplicate"
                result["message"] = f"Already grabbed — skipped: {title}"
                self._log(f"[Download] skip duplicate: {title}", "info")
                self._progress("download:complete",
                               {"title": title, "url": url, "method": "duplicate", "link_count": 0},
                               _cb=progress_callback)
                return result
            prior = self._best_prior_grab(title, year, season)
            if prior is not None and not self._is_quality_upgrade(
                    resolution, dovi, prior):
                result["success"] = True
                result["method"] = "duplicate_similar"
                result["message"] = (
                    f"Already grabbed {prior.get('resolution') or '?'} of "
                    f"{title} — skipped (this is not an upgrade)")
                self._log(f"[Download] skip same-title duplicate: {title} "
                          f"({resolution or '?'} vs grabbed {prior.get('resolution') or '?'})",
                          "info")
                self._progress("download:complete",
                               {"title": title, "url": url,
                                "method": "duplicate_similar", "link_count": 0},
                               _cb=progress_callback)
                return result
```

(This is the SAME existing `if self.db is not None:` block, just with `and not force` appended to the outer condition — the inner logic is verbatim unchanged.)

- [ ] **Step 4: Run to verify it passes**

Run: `python -m pytest tests/test_download_service.py -v`
Expected: PASS (new tests + full existing suite — the default-`force=False` regression test proves nothing else changed).

- [ ] **Step 5: Commit**

```bash
git add backend/download_service.py tests/test_download_service.py
git commit -m "feat(pipeline): download_item(force=True) bypasses both dedup gates

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Task 7: Shared `_run_grab` helper (extract from the existing route)

**Files:** Modify `backend/api/routes/downloads.py` (`download_item` route ~line 62-127). Test `tests/test_api_routes.py`.

**Interfaces:** Produces `_run_grab(dl, reg, req: DownloadRequest, force: bool = False) -> None` (module-level function in `downloads.py`, importable by `pipeline.py`).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_api_routes.py (add, using the existing TestClient/mocked-registry harness in this file)
def test_existing_download_route_behavior_unchanged_after_refactor(client, monkeypatch):
    # Regression: the existing POST /download route must still queue a
    # background task and return {"status": "started", ...} exactly as before.
    calls = {}
    def fake_add_task(fn, *a, **kw):
        calls["fn"] = fn
    monkeypatch.setattr("fastapi.BackgroundTasks.add_task", fake_add_task)
    resp = client.post("/download", json={"url": "http://t/1", "title": "Test Movie",
                                          "resolution": "1080p"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "started"
    assert "fn" in calls
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/test_api_routes.py -k existing_download_route -v`
Expected: FAIL only if the refactor is already (incorrectly) broken; more likely this test PASSES against the pre-refactor code too — its real purpose is to catch a regression introduced by Step 3, so run it once now to confirm it passes on the ORIGINAL code, then again after Step 3.

- [ ] **Step 3: Implement**

In `backend/api/routes/downloads.py`, extract the existing `_do_download` closure (currently defined inline inside `download_item`, ~line 74-125) into a module-level function, and have the route call it via a small wrapper closure (so `background_tasks.add_task` still works the same way):

```python
def _run_grab(dl, reg: ServiceRegistry, req: "DownloadRequest", force: bool = False) -> None:
    """Execute one grab and report its outcome over WS — the shared body used
    by BOTH the existing POST /download route and the pipeline tracker's
    regrab/grab-alternative actions. `force=True` (pipeline-only) bypasses
    download_item's two dedup gates."""
    try:
        def _on_progress(event: str, data: dict):
            ws_manager.broadcast_sync({"type": event, "data": data})

        result = dl.download_item(
            url=req.url, title=req.title, season=req.season,
            resolution=req.resolution, size=req.size,
            service_type=req.service_type, year=req.year,
            hdr=req.hdr, dovi=req.dovi,
            progress_callback=_on_progress,
            force=force,
        )
        success = bool((result or {}).get("success"))
        method = (result or {}).get("method", "")
        message = (result or {}).get("message", "") or f"Sent: {req.title}"
        if not success:
            ws_manager.broadcast_sync({
                "type": "notification",
                "data": {"title": "Download Failed", "body": message, "priority": "high"},
            })
        elif method in ("duplicate", "duplicate_similar"):
            ws_manager.broadcast_sync({
                "type": "notification",
                "data": {"title": "Already grabbed", "body": message, "priority": "normal"},
            })
        elif method == "jdownloader":
            ws_manager.broadcast_sync({
                "type": "notification",
                "data": {"title": "Download", "body": message, "priority": "normal"},
            })
            _persist_grab_annotations(reg)
        else:
            ws_manager.broadcast_sync({
                "type": "notification",
                "data": {"title": "Download", "body": f"{message} (not sent to JDownloader — method: {method})", "priority": "warning"},
            })
    except Exception as e:
        logger.exception("Download failed for %s", req.title)
        try:
            dl.save_to_history(req.url, req.title, req.season, req.resolution, req.size,
                               status="failed", hdr=req.hdr, dovi=req.dovi)
        except Exception:
            pass
        ws_manager.broadcast_sync({
            "type": "notification",
            "data": {"title": "Download Failed", "body": str(e), "priority": "high"},
        })
```

Then replace the route's inline `_do_download` + its `background_tasks.add_task(_do_download)` call with:

```python
@router.post("")
def download_item(
    req: DownloadRequest,
    background_tasks: BackgroundTasks,
    reg: ServiceRegistry = Depends(get_registry),
):
    if len(req.title.strip()) < 2:
        raise HTTPException(status_code=400, detail="Title must be at least 2 characters")
    dl = reg.download
    if not dl:
        raise HTTPException(status_code=503, detail="Download service not available")

    background_tasks.add_task(_run_grab, dl, reg, req, False)
    return {"status": "started", "title": req.title}
```

(This is a pure extraction — `_run_grab`'s body is byte-identical to the old `_do_download`'s body, just parameterized on `dl`/`reg`/`req`/`force` instead of closing over them, plus the one new `force=force` argument passed to `download_item`.)

- [ ] **Step 4: Run to verify it passes**

Run: `python -m pytest tests/test_api_routes.py -k "download_route or existing_download" -v`
Expected: PASS (both the pre-existing route tests in this file AND the new regression test).

- [ ] **Step 5: Commit**

```bash
git add backend/api/routes/downloads.py tests/test_api_routes.py
git commit -m "refactor(pipeline): extract _run_grab from the download route's _do_download closure

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Task 8: Pipeline router — items/counts/dismiss/regrab

**Files:** Create `backend/api/routes/pipeline.py`. Modify `backend/api/main.py` (register the router, alongside the existing router imports ~line 499-505). Test `tests/test_api_routes.py`.

**Interfaces:** `GET /pipeline/items`, `GET /pipeline/counts`, `POST /pipeline/dismiss`, `POST /pipeline/regrab`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_api_routes.py (add, using the existing TestClient harness)
class TestPipelineRoutes:
    def test_items_returns_verdicts(self, client, db_manager):
        db_manager.add_to_history("http://pi/1", "Foo", package_name="Foo [1080p]")
        db_manager.upsert_pipeline_verdict("http://pi/1", "download_failed", detail="offline")
        resp = client.get("/pipeline/items")
        assert resp.status_code == 200
        items = resp.json()
        assert any(i["url"] == "http://pi/1" and i["category"] == "download_failed" for i in items)

    def test_counts_excludes_dismissed(self, client, db_manager):
        db_manager.add_to_history("http://pi/2", "Bar", package_name="Bar [1080p]")
        db_manager.upsert_pipeline_verdict("http://pi/2", "rename_failed")
        db_manager.dismiss_pipeline_verdict("http://pi/2")
        resp = client.get("/pipeline/counts")
        counts = resp.json()
        assert counts.get("rename_failed", 0) == 0

    def test_dismiss_endpoint(self, client, db_manager):
        db_manager.add_to_history("http://pi/3", "Baz", package_name="Baz [1080p]")
        db_manager.upsert_pipeline_verdict("http://pi/3", "not_in_plex")
        resp = client.post("/pipeline/dismiss", json={"url": "http://pi/3"})
        assert resp.status_code == 200
        assert db_manager.get_pipeline_verdicts() == []

    def test_regrab_clears_verdict_and_backgrounds(self, client, db_manager, monkeypatch):
        db_manager.add_to_history("http://pi/4", "Qux", package_name="Qux [1080p]",
                                  resolution="1080p", year=2024)
        db_manager.upsert_pipeline_verdict("http://pi/4", "download_failed", package_uuid="555")
        calls = {}
        def fake_add_task(fn, *a, **kw):
            calls["called"] = True
        monkeypatch.setattr("fastapi.BackgroundTasks.add_task", fake_add_task)
        resp = client.post("/pipeline/regrab", json={"url": "http://pi/4"})
        assert resp.status_code == 200
        assert calls.get("called") is True
        conn = db_manager.get_connection()
        row = conn.execute("SELECT category, excluded_uuid FROM pipeline_verdicts "
                           "WHERE url = ?", ("http://pi/4",)).fetchone()
        assert row[0] is None  # cleared to pending
        assert row[1] == "555"  # old uuid excluded
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/test_api_routes.py -k TestPipelineRoutes -v`
Expected: FAIL (router doesn't exist / not registered).

- [ ] **Step 3: Implement**

```python
# backend/api/routes/pipeline.py
"""Pipeline tracker endpoints: browse reconcile verdicts, dismiss, regrab,
search other sources, grab an alternative release."""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel

from backend.api.dependencies import ServiceRegistry, get_registry
from backend.api.routes.downloads import _run_grab, DownloadRequest

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/pipeline", tags=["pipeline"])


@router.get("/items")
def get_items(category: Optional[str] = None, include_dismissed: bool = False,
             reg: ServiceRegistry = Depends(get_registry)):
    if not reg.db:
        return []
    return reg.db.get_pipeline_verdicts(category=category, include_dismissed=include_dismissed)


@router.get("/counts")
def get_counts(reg: ServiceRegistry = Depends(get_registry)):
    if not reg.db:
        return {}
    rows = reg.db.get_pipeline_verdicts()
    counts: dict = {}
    for r in rows:
        counts[r["category"]] = counts.get(r["category"], 0) + 1
    return counts


class UrlRequest(BaseModel):
    url: str


@router.post("/dismiss")
def dismiss_item(req: UrlRequest, reg: ServiceRegistry = Depends(get_registry)):
    if not reg.db:
        raise HTTPException(status_code=503, detail="Database unavailable")
    reg.db.dismiss_pipeline_verdict(req.url)
    return {"ok": True}


@router.post("/regrab")
def regrab_item(req: UrlRequest, background_tasks: BackgroundTasks,
                reg: ServiceRegistry = Depends(get_registry)):
    dl = reg.download
    if not dl or not reg.db:
        raise HTTPException(status_code=503, detail="Download service not available")
    rows = reg.db.get_downloads_needing_reconcile(limit=100000)
    row = next((r for r in rows if r["url"] == req.url), None)
    if row is None:
        # Grab may already be in a terminal/dismissed state (not in the
        # eligible set) — fetch the raw downloads row directly instead.
        conn = reg.db.get_connection()
        cur = conn.execute(
            "SELECT title, year, season, resolution, size, hdr, dovi, service_type "
            "FROM downloads WHERE url = ?", (req.url,))
        raw = cur.fetchone()
        if raw is None:
            raise HTTPException(status_code=404, detail="Grab not found")
        row = dict(raw)
    reg.db.clear_pipeline_verdict(req.url)
    dl_req = DownloadRequest(
        url=req.url, title=row.get("title") or "Untitled", season=row.get("season"),
        year=row.get("year"), resolution=row.get("resolution") or "",
        size=row.get("size") or "", hdr=row.get("hdr") or "", dovi=bool(row.get("dovi")),
        service_type=row.get("service_type") or "Rapidgator",
    )
    background_tasks.add_task(_run_grab, dl, reg, dl_req, True)
    return {"status": "started"}
```

In `backend/api/main.py`, add `pipeline` to the router import list (~line 499) and the `include_router` calls (~line 505):

```python
    from backend.api.routes import system, settings, sources, plex, scanner, results, downloads, analytics, watchlist, scheduler, auth, background, rename, pipeline
```

and add `pipeline.router` to whichever `app.include_router(...)` call groups the other routers.

- [ ] **Step 4: Run to verify it passes**

Run: `python -m pytest tests/test_api_routes.py -k TestPipelineRoutes -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/api/routes/pipeline.py backend/api/main.py tests/test_api_routes.py
git commit -m "feat(pipeline): GET /pipeline/items,/counts + POST /dismiss,/regrab

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Task 9: Pipeline router — search-sources + grab-alternative

**Files:** Modify `backend/api/routes/pipeline.py`. Test `tests/test_api_routes.py`.

**Interfaces:** `POST /pipeline/search-sources`, `POST /pipeline/grab-alternative`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_api_routes.py (add)
class TestPipelineSearch:
    def test_search_sources_returns_partial_results_on_one_source_failure(self, client, db_manager, monkeypatch):
        db_manager.add_to_history("http://ps/1", "Foo", package_name="Foo [1080p]")

        class FakeSource:
            name = "fakegood"
            class config:
                requires_auth = False
        class FakeBadSource:
            name = "fakebad"
            class config:
                requires_auth = False

        async def fake_search_all(self, query, mode="all", **kw):
            from backend.sources.base import PageResult, ParsedRelease
            return {
                "fakegood": PageResult(releases=[ParsedRelease(title="Foo", url="http://alt/1", source="fakegood")]),
                "fakebad": PageResult(releases=[], errors=["boom"]),
            }
        monkeypatch.setattr("backend.sources.registry.SourceRegistry.search_all", fake_search_all)
        resp = client.post("/pipeline/search-sources", json={"url": "http://ps/1"})
        assert resp.status_code == 200
        body = resp.json()
        assert len(body["releases"]) == 1
        assert any("boom" in e for e in body["errors"])

    def test_grab_alternative_maps_parsed_release_to_force_grab(self, client, db_manager, monkeypatch):
        calls = {}
        def fake_add_task(fn, *a, **kw):
            calls["args"] = a
        monkeypatch.setattr("fastapi.BackgroundTasks.add_task", fake_add_task)
        resp = client.post("/pipeline/grab-alternative", json={
            "display_title": "Foo", "url": "http://alt/2", "year": 2024,
            "res": "1080p", "size": "5 GB", "dovi": False, "hdr": "",
            "season": None,
        })
        assert resp.status_code == 200
        assert "args" in calls
        # args[2] is the DownloadRequest, args[3] is force=True
        req = calls["args"][2]
        assert req.url == "http://alt/2" and req.resolution == "1080p"
        assert calls["args"][3] is True
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/test_api_routes.py -k TestPipelineSearch -v`
Expected: FAIL (endpoints don't exist).

- [ ] **Step 3: Implement**

Append to `backend/api/routes/pipeline.py`:

```python
@router.post("/search-sources")
async def search_sources(req: UrlRequest, reg: ServiceRegistry = Depends(get_registry)):
    if not reg.db:
        raise HTTPException(status_code=503, detail="Database unavailable")
    conn = reg.db.get_connection()
    cur = conn.execute("SELECT title, season FROM downloads WHERE url = ?", (req.url,))
    row = cur.fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Grab not found")
    title = row["title"] if hasattr(row, "keys") else row[0]
    season = row["season"] if hasattr(row, "keys") else row[1]
    mode = "tv" if season is not None else "movies"

    from backend.sources.registry import SourceRegistry
    registry = SourceRegistry()
    registry.discover_sources()
    try:
        import asyncio
        results = await asyncio.wait_for(registry.search_all(title, mode), timeout=45.0)
    except asyncio.TimeoutError:
        return {"releases": [], "errors": ["Search timed out"]}
    except Exception as e:
        logger.exception("search-sources failed")
        return {"releases": [], "errors": [str(e)]}

    releases, errors, seen_urls = [], [], set()
    for source_name, page in results.items():
        source_cfg = next((s.config for s in registry.get_enabled_sources()
                           if s.name == source_name), None)
        if source_cfg is not None and getattr(source_cfg, "requires_auth", False):
            continue  # excluded: needs an authenticated Selenium session (e.g. adithd)
        for rel in page.releases:
            if rel.url in seen_urls:
                continue
            seen_urls.add(rel.url)
            releases.append(rel.to_dict())
        errors.extend(page.errors)
    return {"releases": releases, "errors": errors}


class AlternativeReleaseRequest(BaseModel):
    display_title: str
    url: str
    year: Optional[int] = None
    res: str = ""
    size: str = ""
    dovi: bool = False
    hdr: str = ""
    season: Optional[int] = None


@router.post("/grab-alternative")
def grab_alternative(req: AlternativeReleaseRequest, background_tasks: BackgroundTasks,
                     reg: ServiceRegistry = Depends(get_registry)):
    dl = reg.download
    if not dl:
        raise HTTPException(status_code=503, detail="Download service not available")
    dl_req = DownloadRequest(
        url=req.url, title=req.display_title, season=req.season, year=req.year,
        resolution=req.res, size=req.size, hdr=req.hdr, dovi=req.dovi,
    )
    background_tasks.add_task(_run_grab, dl, reg, dl_req, True)
    return {"status": "started"}
```

- [ ] **Step 4: Run to verify it passes**

Run: `python -m pytest tests/test_api_routes.py -k "TestPipelineRoutes or TestPipelineSearch" -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/api/routes/pipeline.py tests/test_api_routes.py
git commit -m "feat(pipeline): POST /pipeline/search-sources,/grab-alternative

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Task 10: Frontend types + client

**Files:** Modify `frontend/src/lib/api/types.ts`, `frontend/src/lib/api/client.ts`.

**Interfaces:** `PipelineItem`, `PipelineCounts`, `AlternativeRelease`, `SearchSourcesResponse`; client methods `getPipelineItems`, `getPipelineCounts`, `dismissPipelineItem`, `regrabPipelineItem`, `searchPipelineSources`, `grabAlternative`.

- [ ] **Step 1: Implement (types + client are additive, no existing behavior to regress — verify via `npm run check`)**

Add to `frontend/src/lib/api/types.ts`:

```ts
export interface PipelineItem {
  url: string;
  category: string;
  detail: string | null;
  package_uuid: string | null;
  excluded_uuid: string | null;
  plex_rating_key: string | null;
  checked_at: string;
  dismissed: number;
  title: string | null;
  year: number | null;
  season: number | null;
  resolution: string | null;
  package_name: string | null;
}

export type PipelineCounts = Record<string, number>;

export interface AlternativeRelease {
  display_title: string;
  url: string;
  year: number | null;
  res: string;
  size: string;
  dovi: boolean;
  hdr: string;
  imdb_id?: string | null;
  tmdb_id?: string | null;
  is_tv?: boolean;
  season?: number | null;
  episode_number?: number | null;
  episodes?: number | null;
  search_key?: string;
  source: string;
  codec?: string;
  audio?: string;
  release_group?: string;
}

export interface SearchSourcesResponse {
  releases: AlternativeRelease[];
  errors: string[];
}
```

Add to `frontend/src/lib/api/client.ts`:

```ts
  getPipelineItems: (category?: string, includeDismissed = false) => {
    const qs = new URLSearchParams();
    if (category) qs.set('category', category);
    if (includeDismissed) qs.set('include_dismissed', 'true');
    const suffix = qs.toString() ? `?${qs}` : '';
    return request<PipelineItem[]>(`/pipeline/items${suffix}`);
  },
  getPipelineCounts: () => request<PipelineCounts>('/pipeline/counts'),
  dismissPipelineItem: (url: string) =>
    request<{ ok: boolean }>('/pipeline/dismiss', { method: 'POST', body: JSON.stringify({ url }) }),
  regrabPipelineItem: (url: string) =>
    request<{ status: string }>('/pipeline/regrab', { method: 'POST', body: JSON.stringify({ url }) }),
  searchPipelineSources: (url: string) =>
    request<SearchSourcesResponse>('/pipeline/search-sources', { method: 'POST', body: JSON.stringify({ url }) }),
  grabAlternative: (release: AlternativeRelease) =>
    request<{ status: string }>('/pipeline/grab-alternative', { method: 'POST', body: JSON.stringify(release) }),
```

(Import `PipelineItem, PipelineCounts, AlternativeRelease, SearchSourcesResponse` into `client.ts`'s type imports.)

- [ ] **Step 2: Verify**

Run: `cd frontend && npm run check`
Expected: 0 new errors.

- [ ] **Step 3: Commit**

```bash
git add frontend/src/lib/api/types.ts frontend/src/lib/api/client.ts
git commit -m "feat(pipeline): frontend types + API client methods

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Task 11: Desktop `/pipeline` page + sidebar nav

**Files:** Create `frontend/src/routes/pipeline/+page.svelte`. Modify `frontend/src/lib/components/Sidebar.svelte` (add a nav entry — read the file first to match its existing entry pattern for `/renames`/`/watchlist` exactly).

**Interfaces:** Consumes `api.getPipelineItems`, `api.getPipelineCounts`, `api.dismissPipelineItem`, `api.regrabPipelineItem`; opens `SourceSearchModal` (Task 12) for search-sources.

- [ ] **Step 1: Implement**

```svelte
<!-- frontend/src/routes/pipeline/+page.svelte -->
<script lang="ts">
  import { onMount } from 'svelte';
  import { api } from '$lib/api/client';
  import { addToast } from '$lib/stores/notifications';
  import type { PipelineItem, PipelineCounts } from '$lib/api/types';
  import SourceSearchModal from '$lib/components/pipeline/SourceSearchModal.svelte';

  const CATEGORY_LABELS: Record<string, string> = {
    never_started: 'Never started',
    download_failed: 'Download failed',
    in_progress: 'In progress',
    pending_rename: 'Pending rename',
    rename_failed: 'Rename failed',
    not_in_plex: 'Not in Plex',
    verified: 'Verified',
    unknown: 'Unknown',
  };
  const ACTIONABLE = ['never_started', 'download_failed', 'rename_failed', 'not_in_plex', 'unknown'];

  let items = $state<PipelineItem[]>([]);
  let counts = $state<PipelineCounts>({});
  let activeCategory = $state<string | null>(null);
  let searchModalUrl = $state<string | null>(null);
  let busy = $state<string | null>(null);

  async function load() {
    counts = await api.getPipelineCounts();
    items = await api.getPipelineItems(activeCategory ?? undefined);
  }

  onMount(load);

  function selectCategory(cat: string | null) {
    activeCategory = cat;
    load();
  }

  async function dismiss(item: PipelineItem) {
    busy = item.url;
    try {
      await api.dismissPipelineItem(item.url);
      items = items.filter((i) => i.url !== item.url);
      addToast('Dismissed', item.title || item.url);
    } catch (e) {
      addToast('Error', e instanceof Error ? e.message : 'Could not dismiss', 'error');
    } finally {
      busy = null;
    }
  }

  async function regrab(item: PipelineItem) {
    busy = item.url;
    try {
      await api.regrabPipelineItem(item.url);
      addToast('Re-grab', `Retrying ${item.title || item.url}…`);
    } catch (e) {
      addToast('Error', e instanceof Error ? e.message : 'Could not regrab', 'error');
    } finally {
      busy = null;
    }
  }
</script>

<div class="flex-1 min-h-0 overflow-auto p-4 space-y-4">
  <h1 class="text-lg font-semibold">Pipeline</h1>

  <div class="flex flex-wrap gap-2">
    <button
      class="px-3 py-1.5 rounded-lg text-sm {activeCategory === null ? 'bg-[var(--accent)] text-white' : 'bg-[var(--bg-tertiary)]'}"
      onclick={() => selectCategory(null)}
    >All ({Object.values(counts).reduce((a, b) => a + b, 0)})</button>
    {#each Object.entries(CATEGORY_LABELS) as [cat, label]}
      {#if counts[cat]}
        <button
          class="px-3 py-1.5 rounded-lg text-sm {activeCategory === cat ? 'bg-[var(--accent)] text-white' : 'bg-[var(--bg-tertiary)]'}"
          onclick={() => selectCategory(cat)}
        >{label} ({counts[cat]})</button>
      {/if}
    {/each}
  </div>

  {#if items.length === 0}
    <p class="text-center text-[var(--text-secondary)] py-12">Nothing to review.</p>
  {/if}

  <ul class="divide-y divide-[var(--border)] rounded-lg border border-[var(--border)] overflow-hidden">
    {#each items as item (item.url)}
      <li class="p-3 flex items-center gap-3">
        <div class="flex-1 min-w-0">
          <div class="font-medium truncate">{item.title || item.package_name || item.url}</div>
          <div class="text-xs text-[var(--text-secondary)]">
            {CATEGORY_LABELS[item.category] || item.category}
            {#if item.detail}<span class="text-[var(--error)]"> — {item.detail}</span>{/if}
          </div>
        </div>
        {#if ACTIONABLE.includes(item.category)}
          <button class="px-2 py-1 text-xs rounded bg-[var(--accent)] text-white disabled:opacity-50"
            disabled={busy === item.url} onclick={() => regrab(item)}>Re-grab</button>
          <button class="px-2 py-1 text-xs rounded bg-[var(--bg-tertiary)] disabled:opacity-50"
            disabled={busy === item.url} onclick={() => (searchModalUrl = item.url)}>Search sources</button>
        {/if}
        <button class="px-2 py-1 text-xs rounded bg-[var(--bg-tertiary)] disabled:opacity-50"
          disabled={busy === item.url} onclick={() => dismiss(item)}>Dismiss</button>
      </li>
    {/each}
  </ul>
</div>

{#if searchModalUrl}
  <SourceSearchModal url={searchModalUrl} onClose={() => (searchModalUrl = null)} />
{/if}
```

For `Sidebar.svelte`: read the existing `/renames` or `/watchlist` nav-item markup and add an identically-structured entry pointing at `/pipeline` with an appropriate label ("Pipeline") — match the existing pattern exactly (icon usage, active-route highlighting), do not introduce a new nav pattern.

- [ ] **Step 2: Verify**

Run: `cd frontend && npm run check && npm run build`
Expected: 0 errors, build succeeds.

- [ ] **Step 3: Commit**

```bash
git add frontend/src/routes/pipeline/+page.svelte frontend/src/lib/components/Sidebar.svelte
git commit -m "feat(pipeline): desktop /pipeline page + sidebar nav entry

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Task 12: `SourceSearchModal.svelte`

**Files:** Create `frontend/src/lib/components/pipeline/SourceSearchModal.svelte`.

**Interfaces:** Props `{ url: string; onClose: () => void }`. Consumes `api.searchPipelineSources`, `api.grabAlternative`.

- [ ] **Step 1: Implement**

```svelte
<!-- frontend/src/lib/components/pipeline/SourceSearchModal.svelte -->
<script lang="ts">
  import { onMount } from 'svelte';
  import { api } from '$lib/api/client';
  import { addToast } from '$lib/stores/notifications';
  import type { AlternativeRelease } from '$lib/api/types';

  let { url, onClose }: { url: string; onClose: () => void } = $props();

  let loading = $state(true);
  let releases = $state<AlternativeRelease[]>([]);
  let errors = $state<string[]>([]);
  let grabbing = $state<string | null>(null);

  onMount(async () => {
    try {
      const res = await api.searchPipelineSources(url);
      releases = res.releases;
      errors = res.errors;
    } catch (e) {
      errors = [e instanceof Error ? e.message : 'Search failed'];
    } finally {
      loading = false;
    }
  });

  async function grab(rel: AlternativeRelease) {
    grabbing = rel.url;
    try {
      await api.grabAlternative(rel);
      addToast('Grabbing', rel.display_title);
      onClose();
    } catch (e) {
      addToast('Error', e instanceof Error ? e.message : 'Could not grab', 'error');
    } finally {
      grabbing = null;
    }
  }
</script>

<div class="fixed inset-0 z-50 flex items-center justify-center bg-[var(--bg-overlay)] p-4">
  <div class="w-full max-w-lg bg-[var(--bg-secondary)] border border-[var(--border)] rounded-2xl shadow-2xl p-5 max-h-[80vh] overflow-y-auto">
    <div class="flex items-center justify-between mb-3">
      <h2 class="text-base font-bold">Alternative sources</h2>
      <button onclick={onClose} aria-label="Close" class="p-1 text-[var(--text-secondary)] hover:text-[var(--text-primary)]">&times;</button>
    </div>

    {#if loading}
      <p class="text-sm text-[var(--text-secondary)]">Searching…</p>
    {:else}
      {#if errors.length > 0}
        <p class="text-xs text-[var(--error)] mb-2">
          {errors.join('; ')}{#if releases.length === 0} — adithd requires the desktop scraper and is not searched here.{/if}
        </p>
      {/if}
      {#if releases.length === 0 && errors.length === 0}
        <p class="text-sm text-[var(--text-secondary)]">No results found.</p>
      {/if}
      <ul class="divide-y divide-[var(--border)]">
        {#each releases as rel (rel.url)}
          <li class="py-2 flex items-center gap-2">
            <div class="flex-1 min-w-0">
              <div class="text-sm font-medium truncate">{rel.display_title}</div>
              <div class="text-xs text-[var(--text-secondary)]">
                {rel.source} · {rel.res || '?'} · {rel.size || '?'}
                {#if rel.dovi}<span class="text-amber-500"> · DV</span>{/if}
                {#if rel.hdr}<span class="text-amber-500"> · {rel.hdr}</span>{/if}
              </div>
            </div>
            <button class="px-2 py-1 text-xs rounded bg-[var(--accent)] text-white disabled:opacity-50"
              disabled={grabbing === rel.url} onclick={() => grab(rel)}>Grab</button>
          </li>
        {/each}
      </ul>
    {/if}
  </div>
</div>
```

- [ ] **Step 2: Verify**

Run: `cd frontend && npm run check && npm run build`
Expected: 0 errors, build succeeds.

- [ ] **Step 3: Commit**

```bash
git add frontend/src/lib/components/pipeline/SourceSearchModal.svelte
git commit -m "feat(pipeline): SourceSearchModal for grabbing an alternative release

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Task 13: Mobile `?view=pipeline` switch

**Files:** Modify `frontend/src/routes/downloads/+page.svelte` (read the file's current mobile-fork structure first — it already forks on the `mobile` store per the prior mobile-downloads feature; this task adds a query-param-driven embed of the Pipeline view alongside the existing Queue view, WITHOUT navigating to a separate URL, so `MobileTabBar`'s exact-path highlighting stays correct).

**Interfaces:** Consumes `$page.url.searchParams`, reuses `frontend/src/routes/pipeline/+page.svelte`'s list-rendering logic (extract the category-chips + list markup into a shared component if the desktop page and this mobile embed would otherwise duplicate significant markup — recommended: factor the body of Task 11's page into `frontend/src/lib/components/pipeline/PipelineList.svelte` and have BOTH the desktop route and this mobile embed render it, rather than duplicating).

- [ ] **Step 1: Refactor Task 11's page body into a shared component**

Extract everything inside Task 11's `<div class="flex-1 min-h-0 overflow-auto p-4 space-y-4">...</div>` (the chips + list + modal) into `frontend/src/lib/components/pipeline/PipelineList.svelte` with no props (it's self-contained, same as the original page). `frontend/src/routes/pipeline/+page.svelte` becomes:

```svelte
<script lang="ts">
  import PipelineList from '$lib/components/pipeline/PipelineList.svelte';
</script>
<PipelineList />
```

- [ ] **Step 2: Implement the mobile switch**

In `frontend/src/routes/downloads/+page.svelte`, inside the existing `{#if $mobile}` branch (read the file to confirm the exact existing structure before editing), add a segmented switch reading `$page.url.searchParams.get('view')`:

```svelte
<script lang="ts">
  // ...existing imports...
  import { page } from '$app/stores';
  import PipelineList from '$lib/components/pipeline/PipelineList.svelte';
  import { goto } from '$app/navigation';

  let mobileView = $derived($page.url.searchParams.get('view') === 'pipeline' ? 'pipeline' : 'queue');
  function setMobileView(v: 'queue' | 'pipeline') {
    goto(v === 'pipeline' ? '/downloads?view=pipeline' : '/downloads', { replaceState: true, noScroll: true, keepFocus: true });
  }
</script>
```

Add a two-button segmented control at the top of the mobile branch's markup:

```svelte
<div class="flex gap-1 p-2">
  <button class="flex-1 py-1.5 rounded-lg text-sm {mobileView === 'queue' ? 'bg-[var(--accent)] text-white' : 'bg-[var(--bg-tertiary)]'}"
    onclick={() => setMobileView('queue')}>Queue</button>
  <button class="flex-1 py-1.5 rounded-lg text-sm {mobileView === 'pipeline' ? 'bg-[var(--accent)] text-white' : 'bg-[var(--bg-tertiary)]'}"
    onclick={() => setMobileView('pipeline')}>Pipeline</button>
</div>
{#if mobileView === 'pipeline'}
  <PipelineList />
{:else}
  <!-- existing MobileDownloadsView / queue markup, unchanged -->
{/if}
```

(`replaceState: true` keeps browser-back from stepping through every toggle; the URL stays `/downloads` or `/downloads?view=pipeline`, both matching `MobileTabBar`'s `$page.url.pathname === '/downloads'` exact-match highlighting since only the pathname is compared, not query params.)

- [ ] **Step 3: Verify**

Run: `cd frontend && npm run check && npm run build`
Expected: 0 errors, build succeeds. Manually confirm (via `preview_resize` to a mobile viewport) that toggling the switch keeps the "Downloads" tab highlighted in `MobileTabBar`.

- [ ] **Step 4: Commit**

```bash
git add frontend/src/lib/components/pipeline/PipelineList.svelte frontend/src/routes/pipeline/+page.svelte frontend/src/routes/downloads/+page.svelte
git commit -m "feat(pipeline): mobile ?view=pipeline switch inside the Downloads tab

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Task 14: Full verification + deploy

**Files:** none (verification + changelog + deploy).

- [ ] **Step 1: Backend suite**

Run: `python -m pytest tests/test_database.py tests/test_pipeline_service.py tests/test_download_service.py -v` and the scoped `tests/test_api_routes.py -k "Pipeline or existing_download"`.
Expected: all PASS.

- [ ] **Step 2: Frontend suite + typecheck + build**

Run: `cd frontend && npx vitest run && npm run check && npm run build`
Expected: all PASS, clean build.

- [ ] **Step 3: Live device checklist**
  - Desktop `/pipeline`: category chips show real counts; a `download_failed` item's Re-grab actually re-sends (watch the JD queue/WS toast); Search sources opens the modal, shows results or a graceful "no results"/error line; Dismiss removes an item and it stays gone on reload.
  - Mobile `/downloads`: the Queue/Pipeline segmented switch works; the bottom "Downloads" tab stays highlighted in both states.
  - Confirm the maintenance-loop log line (`Pipeline reconcile: checked N grab(s)`) appears in `docker logs scanhound` within an hour of a real stalled grab existing.

- [ ] **Step 4: Changelog + version bump**

Add an entry to `frontend/src/lib/changelog.ts` (next version) summarizing: a Pipeline tracker showing every grab that didn't cleanly reach Plex, with Re-grab and Search-other-sources actions.

```bash
git add frontend/src/lib/changelog.ts
git commit -m "chore: changelog — pipeline tracker

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

- [ ] **Step 5: Deploy**

Run: `docker compose up -d --build`. Confirm healthy startup logs ("All services initialized", "Application startup complete") and that the schema additions applied without error (check `docker logs scanhound` for any `ALTER TABLE`/pipeline_verdicts-related warnings).

---

## Self-Review Notes (author)

- **Spec coverage:** schema+compute_package_name (T1), pipeline_verdicts+CRUD (T2), categorize (T3), matching+reconcile_batch (T4), maintenance hook+config (T5), force bypass (T6), shared _run_grab (T7), items/counts/dismiss/regrab (T8), search-sources/grab-alternative (T9), FE types/client (T10), desktop page+nav (T11), search modal (T12), mobile switch (T13), verify+deploy (T14). Every spec §1-§7 section maps to a task.
- **Type/name consistency:** `force` param name identical across `download_item`/`_run_grab`/both new endpoints; `package_uuid`/`excluded_uuid` column names identical across schema, CRUD, matching SQL, and tests; `PipelineItem` fields match `get_pipeline_verdicts`'s SELECT list exactly.
- **Known deviation flagged inline:** Task 4's Step 3 draft referenced a non-existent `db._cfg` for `jd_method` — caught and corrected within the same task (parameterize `reconcile_batch(..., jd_method=...)`, caller in Task 5 supplies it from `self.config`) rather than left for an implementer to discover via a crash.
- **Data safety:** no destructive migration — three new nullable columns + one new additive table; `clear_history` cascade only deletes rows that reference deleted `downloads` rows, in the same transaction.
- **Regression guard:** Task 7's test explicitly locks in that the EXISTING `POST /download` route's behavior (background_tasks.add_task, `{"status": "started"}`) survives the `_run_grab` extraction; Task 6's test locks in `force=False` default behavior is unchanged.
