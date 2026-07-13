# Pipeline Feature Fix & Redesign Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix the two Critical pipeline-matching bugs (season-less package names; JD punctuation sanitization breaking name joins), split the ambiguous `in_progress` category, expose the two orphaned pipeline settings, and redesign the Pipeline UI to the Renames page's visual standard.

**Architecture:** The package name computed by `compute_package_name()` is the join key across `downloads` → `download_results` → `rename_jobs`. JD-side rows (`download_results.name`, `rename_jobs.package_name`) store JDownloader's *sanitized* version of that name, so matching prefers a new `downloads.jd_confirmed_name` column captured empirically from JD's own queue by the existing results poller (punctuation-folded compare, once per row). Categorization (`categorize()` in `backend/pipeline_service.py`) is a pure function; the category split and detail-text changes happen there. The UI is one shared component (`PipelineList.svelte`) used by both desktop `/pipeline` and mobile `?view=pipeline`.

**Tech Stack:** Python 3.12 / FastAPI / sqlite3 (backend), SvelteKit 5 runes + Tailwind CSS vars (frontend), pytest + vitest.

## Global Constraints

- Spec: `docs/superpowers/specs/2026-07-12-pipeline-redesign-design.md` — read for rationale; exact values below are copied from it.
- Package name format (TV): `{title} ({year}) S{season:02d} [{resolution}]`; 50-char cap trims the TITLE portion, never the year/season/resolution suffix.
- Do NOT recompute `package_name` for existing `downloads` rows (it is the live join key for old JD-side rows; rewriting orphans healthy grabs).
- Punctuation folding = remove every non-alphanumeric character, casefold. Capture `jd_confirmed_name` only on a UNIQUE fold-match; ambiguous → skip (leave NULL).
- Category split: `in_progress` is replaced by `downloading` (active download states) and `awaiting_plex_refresh` (applied renames inside the Plex-cache grace window). No DB migration for old category values — the next reconcile pass recomputes them.
- New settings keys in `SettingsUpdate`: `pipeline_reconcile_enabled: Optional[bool] = None`, `pipeline_verify_grace_margin_minutes: Optional[int] = None` (both already exist in `backend/config.py` defaults: `True`, `30`).
- StatCard variant mapping (all nine categories): `verified`→`success`; `rename_failed`,`download_failed`,`not_in_plex`→`error`; `pending_rename`,`awaiting_plex_refresh`,`never_started`→`warning`; `downloading`→`accent`; `unknown`→`default`. Zero-count cards still render.
- Posters only for categories that have reached a rename job (`pending_rename`, `rename_failed`, `awaiting_plex_refresh`, `verified`, `not_in_plex`); no placeholder for `downloading`/`never_started`.
- Action wiring (Dismiss/Re-grab/Search sources/Grab alternative) is UNCHANGED — do not alter `ACTIONABLE` gating semantics beyond renaming categories.
- User-facing copy (empty-state messages, `never_started` detail strings, category labels) is drafted by a Fable-tier agent at implementation time; the strings in this plan are functional placeholders.
- Backend tests run in a throwaway `scanhound:latest` container with code docker-cp'd in (`pip install pytest pytest-timeout httpx` first); frontend tests run on host node. Never bind-mount for tests.
- Commit after each green test cycle. All work on `main` (repo's established practice — no feature branches).

---

### Task 1: Season-aware names + jd_confirmed_name capture + matching precedence

**Files:**
- Modify: `backend/download_service.py:26-37` (compute_package_name), `backend/download_service.py:2003` (call site)
- Modify: `backend/database.py` (~line 623: column migration list; new methods after `get_downloads_needing_reconcile` ~line 1195)
- Modify: `backend/pipeline_service.py:184-237` (`_match_download_results`, `_match_rename_rows`), `backend/pipeline_service.py:255-267` (reconcile_batch wiring)
- Modify: `backend/api/main.py:280-321` (poller capture hook)
- Test: `tests/test_pipeline_service.py` (extend), `tests/test_download_service.py` (extend or create if absent)

**Interfaces:**
- Consumes: existing `compute_package_name(title, year, resolution)`, `DatabaseManager._mutate/_query_dicts`, poller loop in `_start_results_poller`.
- Produces: `compute_package_name(title, year, resolution, season=None) -> str`; `fold_name(name: str) -> str` (module-level, `backend/download_service.py`); `DatabaseManager.capture_jd_confirmed_names(jd_names: list[str]) -> int` (returns rows captured); `downloads.jd_confirmed_name` TEXT column; reconcile matching that prefers `jd_confirmed_name`. Task 3's `/pipeline/items` and Task 2's categorize changes build on these.

- [ ] **Step 1: Write failing tests for the new name format + folding**

Add to `tests/test_pipeline_service.py` (or a new `tests/test_download_service.py` if package-name tests don't fit the existing file's scope — check first; follow where `compute_package_name` is currently tested):

```python
from backend.download_service import compute_package_name, fold_name


class TestSeasonAwarePackageName:
    def test_movie_format_unchanged(self):
        assert compute_package_name("Heat", 1995, "1080p") == "Heat (1995) [1080p]"

    def test_tv_includes_season(self):
        assert compute_package_name("Joey", 2004, "1080p", season=1) == "Joey (2004) S01 [1080p]"

    def test_two_seasons_get_distinct_names(self):
        s1 = compute_package_name("Joey", 2004, "1080p", season=1)
        s2 = compute_package_name("Joey", 2004, "1080p", season=2)
        assert s1 != s2

    def test_long_title_truncates_title_not_suffix(self):
        name = compute_package_name("X" * 60, 2010, "1080p", season=3)
        assert len(name) <= 50
        assert name.endswith("(2010) S03 [1080p]")

    def test_no_title_fallback(self):
        assert compute_package_name("", None, "") == "ScanHound Download"


class TestFoldName:
    def test_colon_vs_semicolon_fold_equal(self):
        assert fold_name("Law & Order: LA (2010) [1080p]") == fold_name("Law & Order; LA (2010) [1080p]")

    def test_different_titles_do_not_fold_equal(self):
        assert fold_name("Joey (2004) S01 [1080p]") != fold_name("Joey (2004) S02 [1080p]")

    def test_casefold(self):
        assert fold_name("ABC") == fold_name("abc")
```

- [ ] **Step 2: Run to verify failure**

Run (throwaway container): `python -m pytest tests/test_pipeline_service.py -k "SeasonAware or FoldName" -v`
Expected: FAIL — `season` unexpected kwarg / `fold_name` import error.

- [ ] **Step 3: Implement compute_package_name + fold_name**

Replace `backend/download_service.py:26-37`:

```python
def compute_package_name(title: str, year: Optional[int], resolution: str,
                         season: Optional[int] = None) -> str:
    """Canonical JDownloader package-name string — the join key used by the
    pipeline tracker across downloads/download_results/rename_jobs. Must match
    send_to_jdownloader's truncation exactly (both its delivery paths truncate
    to 50 chars before JD ever sees the name) — this is the single place that
    string is computed, so the persisted value and the sent value can never
    drift apart. Season is embedded for TV so multiple seasons of one show
    never collapse onto the same join key; the 50-char cap trims the TITLE,
    never the year/season/resolution suffix (a tail-truncation could chop
    'S03' off a long title and silently recreate the collision)."""
    if not title:
        return "ScanHound Download"[:50]
    suffix = f" ({year})" if year else ""
    if season is not None:
        suffix += f" S{season:02d}"
    if resolution:
        suffix += f" [{resolution}]"
    max_title = 50 - len(suffix)
    return f"{title[:max_title]}{suffix}" if max_title > 0 else (title + suffix)[:50]


def fold_name(name: str) -> str:
    """Punctuation-folded comparison key: JDownloader sanitizes package names
    character-for-character (':' -> ';', etc.) before reporting them back, so
    exact comparison of our computed name against JD's reported name fails for
    any title containing such a character. Folding both sides — drop every
    non-alphanumeric, casefold — is immune to any substitution JD performs."""
    return "".join(ch for ch in name if ch.isalnum()).casefold()
```

Update the call site at `backend/download_service.py:2003`:

```python
        package_name = compute_package_name(title, year, resolution, season=season)
```

- [ ] **Step 4: Run tests to verify pass**

Run: `python -m pytest tests/test_pipeline_service.py -k "SeasonAware or FoldName" -v`
Expected: PASS (all 8).

- [ ] **Step 5: Write failing tests for the DB column + capture method**

Add to `tests/test_pipeline_service.py` (reuse the file's existing in-memory/tmp DatabaseManager fixture pattern — read the top of the file first and follow it exactly):

```python
class TestJdConfirmedNameCapture:
    def test_capture_unique_fold_match(self, db):
        db.save_download(url="u1", title="Law & Order: LA", year=2010, season=None,
                         resolution="1080p", package_name="Law & Order: LA (2010) [1080p]")
        captured = db.capture_jd_confirmed_names(["Law & Order; LA (2010) [1080p]"])
        assert captured == 1
        row = db.get_download_by_url("u1")
        assert row["jd_confirmed_name"] == "Law & Order; LA (2010) [1080p]"

    def test_ambiguous_fold_match_skipped(self, db):
        # Two rows folding to the same key (season-less legacy names)
        db.save_download(url="u1", title="Joey", year=2004, season=1,
                         resolution="1080p", package_name="Joey (2004) [1080p]")
        db.save_download(url="u2", title="Joey", year=2004, season=2,
                         resolution="1080p", package_name="Joey (2004) [1080p]")
        captured = db.capture_jd_confirmed_names(["Joey (2004) [1080p]"])
        assert captured == 0

    def test_capture_is_once_per_row(self, db):
        db.save_download(url="u1", title="Heat", year=1995, season=None,
                         resolution="1080p", package_name="Heat (1995) [1080p]")
        assert db.capture_jd_confirmed_names(["Heat (1995) [1080p]"]) == 1
        assert db.capture_jd_confirmed_names(["Heat (1995) [1080p]"]) == 0
```

NOTE: `save_download`/`get_download_by_url` are illustrative — use whatever helper the existing fixture exposes for inserting `downloads` rows (grep the test file; if inserts are raw SQL there, use raw SQL with the same columns). `last_grabbed_at` must be set to a recent timestamp on insert (capture is windowed, Step 7).

- [ ] **Step 6: Run to verify failure**

Run: `python -m pytest tests/test_pipeline_service.py -k JdConfirmedNameCapture -v`
Expected: FAIL — no such column / no such method.

- [ ] **Step 7: Implement column migration + capture method**

In `backend/database.py`, append to the `_column_migrations` list (after line 623's `service_type` entry):

```python
                    # JDownloader's own reported package name, captured
                    # empirically by the results poller the first time the
                    # package appears in JD's queue. JD sanitizes punctuation
                    # (':' -> ';') before reporting, so this — not our computed
                    # package_name — is the string download_results.name and
                    # rename_jobs.package_name actually carry. Matching prefers
                    # it when present. NULL until captured; captured at most
                    # once per row.
                    'ALTER TABLE downloads ADD COLUMN jd_confirmed_name TEXT',
```

Add the capture method to `DatabaseManager` (after `get_downloads_needing_reconcile`):

```python
    def capture_jd_confirmed_names(self, jd_names):
        """Empirical capture of JD's reported package names (pipeline matching).

        For each name JD reports, find downloads rows still awaiting capture
        (jd_confirmed_name IS NULL, grabbed within the last 7 days) whose
        computed package_name FOLDS equal to it; persist only on a UNIQUE
        match — an ambiguous fold (legacy season-less names) is skipped
        rather than guessed. Returns the number of rows captured."""
        from backend.download_service import fold_name
        if not jd_names:
            return 0
        try:
            with self._lock:
                conn = self.get_connection()
                if not conn:
                    return 0
                cur = conn.cursor()
                cur.execute(
                    "SELECT url, package_name FROM downloads "
                    "WHERE jd_confirmed_name IS NULL AND package_name IS NOT NULL "
                    "AND last_grabbed_at >= datetime('now', '-7 days')")
                pending = [(r[0], r[1]) for r in cur.fetchall()]
                if not pending:
                    return 0
                captured = 0
                for jd_name in set(jd_names):
                    key = fold_name(jd_name)
                    hits = [url for url, pkg in pending if fold_name(pkg) == key]
                    if len(hits) != 1:
                        continue  # 0 = unrelated package; >1 = ambiguous, skip
                    cur.execute(
                        "UPDATE downloads SET jd_confirmed_name = ? "
                        "WHERE url = ? AND jd_confirmed_name IS NULL",
                        (jd_name, hits[0]))
                    captured += cur.rowcount
                    pending = [(u, p) for u, p in pending if u != hits[0]]
                conn.commit()
                return captured
        except Exception as e:
            logger.error("DB Error (capture_jd_confirmed_names): %s", e)
            return 0
```

Also extend `get_downloads_needing_reconcile`'s SELECT (line 1184) to include the new column — change

```sql
            SELECT d.url, d.title, d.year, d.season, d.resolution, d.size, d.hdr, d.dovi,
                   d.package_name, d.service_type, d.last_grabbed_at,
```

to

```sql
            SELECT d.url, d.title, d.year, d.season, d.resolution, d.size, d.hdr, d.dovi,
                   d.package_name, d.jd_confirmed_name, d.service_type, d.last_grabbed_at,
```

- [ ] **Step 8: Run tests to verify pass**

Run: `python -m pytest tests/test_pipeline_service.py -k JdConfirmedNameCapture -v`
Expected: PASS (3).

- [ ] **Step 9: Write failing tests for matching precedence**

Add to `tests/test_pipeline_service.py`, following the file's existing `_match_download_results`/`_match_rename_rows` test setup (it already builds fake `download_results`/`rename_jobs` rows — reuse that pattern):

```python
class TestMatchingPrecedence:
    def test_download_results_matched_via_jd_confirmed_name(self, db_with_results):
        # download_results.name holds JD's sanitized name; computed package_name
        # differs (colon). Row must match via jd_confirmed_name.
        row = {"package_name": "Law & Order: LA (2010) [1080p]",
               "jd_confirmed_name": "Law & Order; LA (2010) [1080p]",
               "last_grabbed_at": "2026-07-12 10:00:00"}
        # insert a download_results row named "Law & Order; LA (2010) [1080p]"
        # with updated_at >= last_grabbed_at, then:
        result = _match_download_results(conn, row)
        assert result is not None

    def test_falls_back_to_package_name_when_unconfirmed(self, db_with_results):
        row = {"package_name": "Heat (1995) [1080p]", "jd_confirmed_name": None,
               "last_grabbed_at": "2026-07-12 10:00:00"}
        result = _match_download_results(conn, row)
        assert result is not None  # matched via package_name as before

    def test_rename_rows_matched_via_jd_confirmed_name(self, db_with_results):
        # rename_jobs.package_name stores JD's reported name too
        rows = _match_rename_rows(conn, "Law & Order; LA (2010) [1080p]")
        assert rows  # caller passes the effective (confirmed-first) name
```

(Adapt fixture names/insert helpers to the file's real ones.)

- [ ] **Step 10: Run to verify failure, then implement precedence**

In `backend/pipeline_service.py`:

`_match_download_results` (line 209) — change the name lookup:

```python
    name = download_row.get("jd_confirmed_name") or download_row.get("package_name")
```

`reconcile_batch` (line 267) — pass the effective name to `_match_rename_rows`:

```python
            effective_name = row.get("jd_confirmed_name") or row.get("package_name")
            rename_rows = _match_rename_rows(conn, effective_name)
```

(`_match_rename_rows` itself is unchanged — the caller now passes the right key.)

- [ ] **Step 11: Poller capture hook**

In `backend/api/main.py`, inside the poller loop after the `ws_manager.broadcast_sync` block (after line 298), add:

```python
                    # Empirical package-name capture: persist JD's own reported
                    # name for any grab still awaiting confirmation, so pipeline
                    # matching is immune to JD's punctuation sanitization.
                    # Cheap no-op once every recent grab is captured.
                    if results and reg.db:
                        try:
                            reg.db.capture_jd_confirmed_names([r["name"] for r in results])
                        except Exception:
                            logger.debug("jd_confirmed_name capture failed", exc_info=True)
```

- [ ] **Step 12: One-time backfill migration**

In `backend/database.py`, immediately after the `_column_migrations` loop completes (find where the list is executed; add after it, inside the same `init` transaction scope), add a guarded backfill that runs only when the column was just created. Detect first-run via a sentinel: the ALTER raising "duplicate column name" means the column pre-existed. Restructure minimally — pull the `jd_confirmed_name` ALTER out of the list into its own try/except:

```python
                # jd_confirmed_name: own guarded block (not the shared list)
                # because its FIRST creation triggers a one-time best-effort
                # backfill from download_results history. Fold-match each
                # legacy downloads row against download_results.name; capture
                # only unique matches (ambiguous legacy season-less names are
                # left NULL — they resolve via Re-grab, which now sends
                # season-aware names).
                try:
                    cursor.execute('ALTER TABLE downloads ADD COLUMN jd_confirmed_name TEXT')
                    from backend.download_service import fold_name
                    cursor.execute("SELECT url, package_name FROM downloads "
                                   "WHERE package_name IS NOT NULL")
                    dl_rows = cursor.fetchall()
                    cursor.execute("SELECT DISTINCT name FROM download_results "
                                   "WHERE name IS NOT NULL")
                    jd_names = [r[0] for r in cursor.fetchall()]
                    by_fold = {}
                    for url, pkg in dl_rows:
                        by_fold.setdefault(fold_name(pkg), []).append(url)
                    for jd_name in jd_names:
                        hits = by_fold.get(fold_name(jd_name), [])
                        if len(hits) == 1:
                            cursor.execute(
                                "UPDATE downloads SET jd_confirmed_name = ? "
                                "WHERE url = ? AND jd_confirmed_name IS NULL",
                                (jd_name, hits[0]))
                except sqlite3.OperationalError as e:
                    if "duplicate column name" not in str(e).lower():
                        raise
```

(Remove the list entry added in Step 7 — this block replaces it. If the migration loop's exception-handling idiom differs, match it exactly.) NOTE: unlike the poller capture, the backfill is NOT windowed to 7 days — legacy rows are old by definition.

- [ ] **Step 13: Full-module test run + commit**

Run: `python -m pytest tests/test_pipeline_service.py -v --timeout=60`
Expected: ALL PASS (existing + new).

```bash
git add backend/download_service.py backend/database.py backend/pipeline_service.py backend/api/main.py tests/
git commit -m "fix(pipeline): season-aware package names + empirical JD name capture/matching"
```

---

### Task 2: Category split + never_started detail + find_plex_match error narrowing

**Files:**
- Modify: `backend/pipeline_service.py` (categorize: lines 111-121, 165; find_plex_match: lines 38-74)
- Modify: `frontend/src/lib/components/pipeline/PipelineList.svelte:9-19` (labels + ACTIONABLE — rename only; full redesign is Task 4)
- Test: `tests/test_pipeline_service.py`

**Interfaces:**
- Consumes: Task 1's merged code (categorize signature unchanged).
- Produces: category vocabulary `{unknown, never_started, download_failed, downloading, pending_rename, rename_failed, awaiting_plex_refresh, verified, not_in_plex}` — Task 4's UI consumes exactly these strings. `find_plex_match` raises→`("error", exc)` sentinel behavior replaced by explicit return contract below.

- [ ] **Step 1: Write failing tests**

```python
class TestCategorySplit:
    def test_active_download_state_is_downloading(self):
        cat, *_ = categorize({"url": "u"}, {"state": "downloading", "package_uuid": "p1"},
                             [], {}, jd_method="api")
        assert cat == "downloading"

    def test_applied_inside_grace_window_is_awaiting_plex_refresh(self):
        # rename_rows all applied, plex cache max older than processed+grace
        cat, *_ = categorize(
            {"url": "u", "resolution": "1080p"},
            {"state": "extracted", "package_uuid": "p1"},
            [{"status": "applied", "processed_at": "2026-07-12T10:00:00",
              "media_type": "movie", "title": "Heat", "year": 1995}],
            {"Movies": 0}, jd_method="api")
        assert cat == "awaiting_plex_refresh"

    def test_in_progress_never_returned(self):
        # guard against regression: the old label must be gone
        import backend.pipeline_service as ps
        import inspect
        assert '"in_progress"' not in inspect.getsource(ps)


class TestNeverStartedDetail:
    def test_never_started_has_detail_text(self):
        cat, detail, *_ = categorize(
            {"url": "u", "last_grabbed_at": "2020-01-01 00:00:00"},
            None, [], {}, jd_method="api")
        assert cat == "never_started"
        assert detail  # non-empty explanation


class TestFindPlexMatchErrorNarrowing:
    def test_db_error_yields_unknown_not_not_in_plex(self, monkeypatch):
        # find_plex_match raising must surface as 'unknown', never 'not_in_plex'
        import backend.pipeline_service as ps
        def boom(*a, **k):
            raise RuntimeError("db exploded")
        monkeypatch.setattr(ps, "find_plex_match", boom)
        cat, *_ = ps.categorize(
            {"url": "u", "resolution": "1080p"},
            {"state": "extracted", "package_uuid": "p1"},
            [{"status": "applied", "processed_at": "2020-01-01T00:00:00",
              "media_type": "movie", "title": "Heat", "year": 1995}],
            {"Movies": 9999999999}, jd_method="api")
        assert cat == "unknown"
```

(Adapt the existing test file's categorize-call conventions — it already has categorize tests; mirror their argument style.)

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/test_pipeline_service.py -k "CategorySplit or NeverStartedDetail or ErrorNarrowing" -v`
Expected: FAIL.

- [ ] **Step 3: Implement**

`backend/pipeline_service.py`:

Line 121 — `return ("in_progress", None, package_uuid, None)` → `return ("downloading", None, package_uuid, None)`.

Line 165 — `return ("in_progress", None, package_uuid, None)` → `return ("awaiting_plex_refresh", None, package_uuid, None)`.

Line 112 — `never_started` detail (placeholder copy; Fable pass in Task 4 finalizes):

```python
            if last_grabbed and _minutes_since(last_grabbed) > 30:
                return ("never_started",
                        "Grabbed over 30 minutes ago but never appeared in "
                        "JDownloader's queue — the links may have failed to send.",
                        None, None)
```

`find_plex_match` error narrowing — change the except at lines 72-74 to re-raise a sentinel the caller maps to `unknown`. Keep the function's no-match `None` distinct from errors:

```python
    except Exception:
        logger.exception("find_plex_match failed")
        raise _PlexLookupError()
```

with, at module scope:

```python
class _PlexLookupError(Exception):
    """find_plex_match hit a real error (DB failure, bad data) — distinct from
    a clean no-match (None). Callers map this to 'unknown' rather than letting
    it masquerade as a confirmed 'not_in_plex'."""
```

and in `_categorize_from_rename_rows` (line 167):

```python
        try:
            match = find_plex_match(db, latest.get("imdb_id"), latest.get("title"),
                                    latest.get("year"), latest.get("season"), resolution)
        except _PlexLookupError:
            return ("unknown", "Plex lookup failed — will retry next pass", package_uuid, None)
```

`PipelineList.svelte:9-19` — rename only (Task 4 does the redesign):

```typescript
  const CATEGORY_LABELS: Record<string, string> = {
    never_started: 'Never started',
    download_failed: 'Download failed',
    downloading: 'Downloading',
    pending_rename: 'Pending rename',
    rename_failed: 'Rename failed',
    awaiting_plex_refresh: 'Waiting on Plex',
    not_in_plex: 'Not in Plex',
    verified: 'Verified',
    unknown: 'Unknown',
  };
```

(`ACTIONABLE` list is unchanged — none of its five entries were renamed.)

- [ ] **Step 4: Run tests, verify pass, run whole module, commit**

Run: `python -m pytest tests/test_pipeline_service.py -v --timeout=60`
Expected: ALL PASS. Frontend: `cd frontend && npm run check` — 0 errors.

```bash
git add backend/pipeline_service.py frontend/src/lib/components/pipeline/PipelineList.svelte tests/test_pipeline_service.py
git commit -m "feat(pipeline): split in_progress into downloading/awaiting_plex_refresh; never_started detail; plex-lookup error narrowing"
```

---

### Task 3: Settings plumbing + poster_path on /pipeline/items

**Files:**
- Modify: `backend/api/routes/settings.py` (~line 148, after `strict_resolution`)
- Modify: `backend/database.py:1088-1096` (`get_pipeline_verdicts` SELECT)
- Modify: `frontend/src/routes/settings/+page.svelte` (new "Pipeline" card — place near the Auto-Rename section), `frontend/src/lib/api/types.ts:681-695` (PipelineItem)
- Test: `tests/test_dv_settings.py` (settings round-trip pattern lives here), `tests/test_pipeline_service.py`

**Interfaces:**
- Consumes: config keys `pipeline_reconcile_enabled` (bool, default True), `pipeline_verify_grace_margin_minutes` (int, default 30) — already in `backend/config.py:136-137,480-481` and read by `app_service.py:595-600`.
- Produces: `PipelineItem.poster_path: string | null` — Task 4 renders it via the existing `/poster/` serving route used by Renames (grep `poster_path` in `frontend/src/lib/components/renames/RenameRow.svelte` for the exact URL prefix and reuse it verbatim).

- [ ] **Step 1: Write failing settings round-trip tests**

Add to `tests/test_dv_settings.py` (mirror its existing test style exactly):

```python
def test_settings_model_accepts_pipeline_keys():
    from backend.api.routes.settings import SettingsUpdate
    upd = SettingsUpdate(pipeline_reconcile_enabled=False,
                         pipeline_verify_grace_margin_minutes=45)
    assert upd.pipeline_reconcile_enabled is False
    assert upd.pipeline_verify_grace_margin_minutes == 45


def test_put_settings_round_trips_pipeline_keys(client):
    resp = client.put("/settings", json={"pipeline_reconcile_enabled": False,
                                         "pipeline_verify_grace_margin_minutes": 45})
    assert resp.status_code == 200
    got = client.get("/settings").json()
    assert got["pipeline_reconcile_enabled"] is False
    assert got["pipeline_verify_grace_margin_minutes"] == 45
```

- [ ] **Step 2: Run to verify failure** — expect 422 under `extra="forbid"`.

- [ ] **Step 3: Implement settings fields**

`backend/api/routes/settings.py`, after line 148 (`strict_resolution`):

```python
    # Pipeline tracker: reconcile on/off switch + the Plex-cache grace window
    # (minutes) used by the applied->verified gate. Both were read live by the
    # maintenance loop but missing here, so saving them 422'd under
    # extra="forbid" — the documented off switch didn't exist in the running app.
    pipeline_reconcile_enabled: Optional[bool] = None
    pipeline_verify_grace_margin_minutes: Optional[int] = None
```

- [ ] **Step 4: Verify settings tests pass.**

- [ ] **Step 5: Write failing poster_path test**

```python
def test_pipeline_verdicts_include_poster_path(db):
    # downloads row + verdict + a rename_jobs row sharing the JD-side name,
    # carrying poster_path
    ...  # insert via the file's fixtures: downloads(url=u1, package_name=P, jd_confirmed_name=J)
    ...  # rename_jobs(package_name=J, poster_path="/posters/abc.jpg", status="applied")
    rows = db.get_pipeline_verdicts()
    assert rows[0]["poster_path"] == "/posters/abc.jpg"
```

- [ ] **Step 6: Implement poster_path join**

`backend/database.py` `get_pipeline_verdicts` — replace the query with a LEFT-JOINed poster lookup (newest rename job wins; joins on the effective JD-side name):

```python
        return self._query_dicts(f'''
            SELECT v.url, v.category, v.detail, v.package_uuid, v.excluded_uuid,
                   v.plex_rating_key, v.checked_at, v.dismissed,
                   d.title, d.year, d.season, d.resolution, d.package_name,
                   (SELECT r.poster_path FROM rename_jobs r
                     WHERE r.package_name = COALESCE(d.jd_confirmed_name, d.package_name)
                       AND r.poster_path IS NOT NULL
                     ORDER BY r.id DESC LIMIT 1) AS poster_path
            FROM pipeline_verdicts v
            JOIN downloads d ON d.url = v.url
            {where}
            ORDER BY v.checked_at DESC
        ''', tuple(params))
```

(If `rename_jobs` has no `id` column, order by its primary key / `detected_at DESC` — check the schema first.)

`frontend/src/lib/api/types.ts` — add to `PipelineItem`:

```typescript
  /** Poster from the newest matched rename job; null until a rename job
   *  exists (downloading / never_started rows have no identified title). */
  poster_path: string | null;
```

- [ ] **Step 7: Settings UI card**

In `frontend/src/routes/settings/+page.svelte`, add a card following the existing card pattern (place it after the Auto-Rename card; copy the exact classes from the Grid Layout card at lines 316-330):

```svelte
        <!-- Pipeline card -->
        <div class="bg-[var(--bg-secondary)] rounded-lg p-5 border border-[var(--border)] space-y-4">
          <h3 class="text-sm font-semibold text-[var(--text-secondary)] uppercase tracking-wide">Pipeline</h3>

          <label class="flex items-center gap-3">
            <input
              type="checkbox"
              checked={$settings.pipeline_reconcile_enabled as boolean ?? true}
              onchange={(e) => settings.update((s) => ({ ...s, pipeline_reconcile_enabled: e.currentTarget.checked }))}
              class="accent-[var(--accent)]"
            />
            <span class="text-sm">Reconcile grabs against Plex automatically</span>
          </label>

          <label class="block">
            <span class="text-sm text-[var(--text-secondary)]">Plex verification grace window (minutes)</span>
            <input
              type="number"
              min="0"
              max="1440"
              value={$settings.pipeline_verify_grace_margin_minutes as number ?? 30}
              oninput={(e) => settings.update((s) => ({ ...s, pipeline_verify_grace_margin_minutes: parseInt(e.currentTarget.value) || 30 }))}
              class={inputSmClass}
            />
          </label>
        </div>
```

- [ ] **Step 8: Run all tests + commit**

Run: `python -m pytest tests/test_dv_settings.py tests/test_pipeline_service.py -v --timeout=60` and `cd frontend && npm run check`.
Expected: ALL PASS / 0 errors.

```bash
git add backend/api/routes/settings.py backend/database.py frontend/src/routes/settings/+page.svelte frontend/src/lib/api/types.ts tests/
git commit -m "feat(pipeline): expose reconcile settings; poster_path on pipeline items"
```

---

### Task 4: PipelineList.svelte redesign (StatCards, rich rows, posters, empty states)

**Files:**
- Modify: `frontend/src/lib/components/pipeline/PipelineList.svelte` (full rework of the template; script's load/dismiss/regrab logic unchanged)
- Test: `frontend/src/lib/components/pipeline/pipelineDisplay.test.ts` (create — pure display-helper tests; the component itself is covered by `npm run check`/`build` per this repo's frontend-test practice)

**Interfaces:**
- Consumes: `PipelineItem.poster_path` (Task 3), category vocabulary (Task 2), `StatCard.svelte` (props: `label, count, variant, active?, onclick` — variant is a `BadgeVariant`: `default|success|warning|error|accent|info|orange`), `RenamePoster.svelte` (props: `posterUrl?: string|null, alt?: string, class?: string`).
- Produces: final user-visible feature. No downstream consumers.

- [ ] **Step 1: Fable copy pass**

Draft final user-facing copy with a Fable-tier agent: the nine category labels, per-category empty-state messages (all nine + the "All" view), and the `never_started` detail string from Task 2 (update `backend/pipeline_service.py`'s placeholder if the draft differs). Constraints for the drafter: labels ≤ 2 words where possible; empty states one short sentence, no exclamation marks, match the app's existing tone (e.g. Renames' "No jobs need review").

- [ ] **Step 2: Create display-helper module + failing tests**

Extract pure helpers so they're unit-testable. Create `frontend/src/lib/components/pipeline/pipelineDisplay.ts`:

```typescript
import type { BadgeVariant } from '$lib/components/Badge.svelte';

/** Spec'd mapping — all nine categories. */
export const CATEGORY_VARIANT: Record<string, BadgeVariant> = {
  verified: 'success',
  rename_failed: 'error',
  download_failed: 'error',
  not_in_plex: 'error',
  pending_rename: 'warning',
  awaiting_plex_refresh: 'warning',
  never_started: 'warning',
  downloading: 'accent',
  unknown: 'default',
};

/** Categories whose items have reached a rename job — the only ones a poster
 *  can exist for (no identified title before that). */
export const POSTER_CATEGORIES = new Set([
  'pending_rename', 'rename_failed', 'awaiting_plex_refresh', 'verified', 'not_in_plex',
]);

/** "5m ago" / "3h ago" / "2d ago" from a sqlite UTC timestamp. */
export function checkedAgo(sqliteTs: string, now: Date = new Date()): string {
  const dt = new Date(sqliteTs.replace(' ', 'T') + 'Z');
  const mins = Math.max(0, Math.floor((now.getTime() - dt.getTime()) / 60000));
  if (mins < 60) return `${mins}m ago`;
  const hours = Math.floor(mins / 60);
  if (hours < 24) return `${hours}h ago`;
  return `${Math.floor(hours / 24)}d ago`;
}
```

Create `frontend/src/lib/components/pipeline/pipelineDisplay.test.ts`:

```typescript
import { describe, expect, it } from 'vitest';
import { CATEGORY_VARIANT, POSTER_CATEGORIES, checkedAgo } from './pipelineDisplay';

describe('CATEGORY_VARIANT', () => {
  it('covers all nine categories', () => {
    expect(Object.keys(CATEGORY_VARIANT)).toHaveLength(9);
  });
  it('maps failures to error and verified to success', () => {
    expect(CATEGORY_VARIANT.verified).toBe('success');
    expect(CATEGORY_VARIANT.download_failed).toBe('error');
    expect(CATEGORY_VARIANT.rename_failed).toBe('error');
    expect(CATEGORY_VARIANT.not_in_plex).toBe('error');
  });
});

describe('POSTER_CATEGORIES', () => {
  it('excludes pre-rename categories', () => {
    expect(POSTER_CATEGORIES.has('downloading')).toBe(false);
    expect(POSTER_CATEGORIES.has('never_started')).toBe(false);
    expect(POSTER_CATEGORIES.has('unknown')).toBe(false);
  });
  it('includes post-rename categories', () => {
    for (const c of ['pending_rename', 'rename_failed', 'awaiting_plex_refresh', 'verified', 'not_in_plex']) {
      expect(POSTER_CATEGORIES.has(c)).toBe(true);
    }
  });
});

describe('checkedAgo', () => {
  const now = new Date('2026-07-12T12:00:00Z');
  it('minutes', () => expect(checkedAgo('2026-07-12 11:55:00', now)).toBe('5m ago'));
  it('hours', () => expect(checkedAgo('2026-07-12 09:00:00', now)).toBe('3h ago'));
  it('days', () => expect(checkedAgo('2026-07-10 12:00:00', now)).toBe('2d ago'));
  it('clamps future skew to 0m', () => expect(checkedAgo('2026-07-12 12:05:00', now)).toBe('0m ago'));
});
```

Run: `cd frontend && npx vitest run src/lib/components/pipeline/pipelineDisplay.test.ts` — expect fail (module missing) → create module → expect pass.

- [ ] **Step 3: Rework the template**

Rewrite `PipelineList.svelte`'s template (script's `load/selectCategory/dismiss/regrab` stand; add imports). Structure:

```svelte
<script lang="ts">
  // ...existing imports...
  import StatCard from '$lib/components/renames/StatCard.svelte';
  import RenamePoster from '$lib/components/renames/RenamePoster.svelte';
  import { CATEGORY_VARIANT, POSTER_CATEGORIES, checkedAgo } from './pipelineDisplay';

  // CATEGORY_LABELS / EMPTY_STATES: Fable-drafted copy from Step 1.
  const EMPTY_STATES: Record<string, string> = { /* Fable copy, one per category + 'all' */ };
  // ...existing script body unchanged...
</script>

<div class="flex-1 min-h-0 overflow-auto p-4 space-y-4">
  <h1 class="text-lg font-semibold">Pipeline</h1>

  <!-- Stat cards: every category always renders (stable layout), plus All -->
  <div class="flex flex-wrap gap-3">
    <StatCard label="All" count={Object.values(counts).reduce((a, b) => a + b, 0)}
      variant="default" active={activeCategory === null} onclick={() => selectCategory(null)} />
    {#each Object.entries(CATEGORY_LABELS) as [cat, label]}
      <StatCard {label} count={counts[cat] ?? 0} variant={CATEGORY_VARIANT[cat] ?? 'default'}
        active={activeCategory === cat} onclick={() => selectCategory(cat)} />
    {/each}
  </div>

  {#if loading}
    <p class="text-center text-[var(--text-secondary)] py-12 text-sm">Loading…</p>
  {:else if loadError}
    <ErrorCard message={loadError} onretry={load} />
  {:else if items.length === 0}
    <p class="text-center text-[var(--text-secondary)] py-12">
      {EMPTY_STATES[activeCategory ?? 'all'] ?? EMPTY_STATES.all}
    </p>
  {:else}
    <ul class="divide-y divide-[var(--border)] rounded-lg border border-[var(--border)] overflow-hidden">
      {#each items as item (item.url)}
        <li class="p-3 flex items-center gap-3">
          {#if item.category && POSTER_CATEGORIES.has(item.category)}
            <RenamePoster posterUrl={item.poster_path} alt={item.title ?? ''} class="w-10 rounded" />
          {/if}
          <div class="flex-1 min-w-0">
            <div class="font-medium truncate">
              {item.title || item.package_name || item.url}
              {#if item.year}<span class="text-[var(--text-secondary)] font-normal"> ({item.year})</span>{/if}
            </div>
            <div class="text-xs text-[var(--text-secondary)] flex flex-wrap gap-x-2">
              <span style="color: {`var(--${CATEGORY_VARIANT[item.category ?? ''] === 'default' ? 'text-secondary' : CATEGORY_VARIANT[item.category ?? '']})`}">{categoryLabel(item.category)}</span>
              {#if item.season != null}<span>S{String(item.season).padStart(2, '0')}</span>{/if}
              {#if item.resolution}<span>{item.resolution}</span>{/if}
              <span>checked {checkedAgo(item.checked_at)}</span>
            </div>
            {#if item.detail}
              <div class="text-xs text-[var(--error)] truncate" title={item.detail}>{item.detail}</div>
            {/if}
          </div>
          <!-- action buttons: UNCHANGED from current file -->
        </li>
      {/each}
    </ul>
  {/if}
</div>
```

NOTE for the category-color span: the CSS var names are `--success/--warning/--error/--accent`; `default` falls back to `--text-secondary`. If that inline template expression reads poorly, lift it into `pipelineDisplay.ts` as `categoryColor(cat: string | null): string` with a unit test — implementer's choice, but keep it pure and tested. `RenamePoster` sizes via its aspect-ratio store — pass a width class (`w-10`) and verify it renders at list-row scale; if the store's aspect class fights the row height, wrap in a fixed-size container div instead.

- [ ] **Step 4: Verify + commit**

Run: `cd frontend && npm run check && npm run build && npx vitest run`
Expected: 0 errors, all tests pass (318 existing + new).

If the backend `never_started` detail placeholder changed in Step 1's Fable pass, update `backend/pipeline_service.py` + its test, and re-run `python -m pytest tests/test_pipeline_service.py -v --timeout=60`.

```bash
git add frontend/src/lib/components/pipeline/ backend/pipeline_service.py tests/test_pipeline_service.py
git commit -m "feat(pipeline): StatCard dashboard, rich rows with posters, per-category empty states"
```
