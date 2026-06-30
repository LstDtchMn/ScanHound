# ScanHound Renames Section Redesign — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Rebuild the ScanHound Renames page into a rich review queue — real poster art, a click-to-filter status dashboard, category chips, multi-select bulk actions, list/grid views, and a search-based Rematch picker — while closing the library-guard gap in rematch.

**Architecture:** Backend adds one column (`poster_path`), captures it during identification, extends the GET /rename/jobs serializer (poster_url + a read-only dv_scan join), re-checks the library guard in rematch, and adds bulk + apply-confident + tmdb-search + rematch-preview endpoints (bulk handlers single-flight via the existing `_bulk_lock` and reuse single-job methods). Frontend decomposes the 497-line monolith into small single-purpose Svelte 5 components that reuse the Scan page's `persisted()`, grid-pref stores, and `Badge`.

**Tech Stack:** FastAPI + SQLite (backend), SvelteKit 5 runes + Tailwind (frontend), pytest, Docker. Full design spec: `docs/superpowers/specs/2026-06-30-renames-redesign-design.md`.

## Global Constraints

- Deploy ONLY via `docker compose up -d --build` from `X:\Docker Apps\ScanHound` — the frontend is baked into the image; `docker restart` deploys nothing.
- `poster_path` is the ONLY new column on `rename_jobs`. No `dv_layer`/`media_type`/`resolution`/`confidence` columns are added (they already exist or are derived).
- The DV layer shown on rows is a READ-ONLY join of `dv_scan` by path at serialize time — never a new column on `rename_jobs` (that stays in the separate DV track).
- The apply-confident threshold (`status == "matched"` AND `match_confidence >= 95`) is enforced SERVER-SIDE; any client-side filter is cosmetic only.
- The library-not-configured guard is re-checked server-side in rematch confirm, rematch-preview, and bulk set-destination — never place a file under an unconfigured/empty root; force `needs_review` + warning instead.
- Reuse the Scan page's primitives: `persisted<T>()`, the grid-pref stores/constants (`tileSize`, `posterAspect`, `gridGap`, `gridColumns`, `TILE_MIN_PX`, `POSTER_ASPECT_CLASS`, `GRID_GAP_CLASS`), and `Badge.svelte`. Do not reinvent them.
- The Set-destination picker shows FRIENDLY labels (e.g. `TV`, `Movies 4K`, `Movies 1080p`) mapped to configured roots; the backend rebuilds the real path and re-runs the per-job guard.
- `Apply confident` in the bulk bar is SELECTION-scoped; the Matched stat card carries a separate `Apply all confident` shortcut (calls apply-confident with no ids).
- Reuse the exact TMDB image base + size the Scan serializer already uses for poster URLs (so thumbnails come from cache); do not introduce a new size.
- All serialization/detection is FAIL-SAFE: an absent poster, a missing dv_scan row, or a TMDB hiccup must never crash the rename pipeline — store/return null.
- Frontend tasks verify via the repo's ACTUAL commands (typecheck/build, and the unit-test runner only where one exists) — do not invent a pytest-style flow for Svelte components.

---

### Task 1: Add `poster_path` column to `rename_jobs`

**Goal:** Fresh installs get `poster_path` in the CREATE TABLE; existing installs get it via an idempotent `ALTER TABLE` migration. Running migrations twice is a no-op.

**Files:**
- Modify `X:\Docker Apps\ScanHound\backend\database.py` — CREATE TABLE `rename_jobs` block (lines 350–381, add `poster_path TEXT` after `imdb_id TEXT`); `_column_migrations` list (lines 420–432, append the `ALTER TABLE rename_jobs ADD COLUMN poster_path TEXT` entry).
- Create `X:\Docker Apps\ScanHound\tests\test_rename_poster_migration.py` — migration tests.

**Interfaces:**
- Consumes: `DatabaseManager()` (no-arg, conftest-isolated DB).
- Produces: `rename_jobs.poster_path` column (SQLite `TEXT`, nullable).

**Steps:**

- [ ] Write the failing test file `X:\Docker Apps\ScanHound\tests\test_rename_poster_migration.py`:
  ```python
  """Migration tests: rename_jobs.poster_path exists and ALTER is idempotent."""
  import sqlite3

  from backend.database import DatabaseManager


  def _rename_columns():
      dm = DatabaseManager()
      conn = dm._connect()
      try:
          cols = {row[1] for row in conn.execute("PRAGMA table_info(rename_jobs)")}
      finally:
          conn.close()
      dm.close()
      return cols


  def test_fresh_db_has_poster_path_column():
      assert "poster_path" in _rename_columns()


  def test_rerunning_migrations_is_a_noop():
      # First init already ran migrations via the autouse-isolated DB; a second
      # DatabaseManager() re-runs init_database() against the same file and must
      # not raise on the duplicate ADD COLUMN.
      dm = DatabaseManager()
      dm.init_database()
      dm.init_database()
      cols = _rename_columns()
      dm.close()
      assert "poster_path" in cols
  ```

- [ ] Confirm the connection/init helper names match the codebase. Run:
  ```
  pytest tests/test_rename_poster_migration.py -v
  ```
  If it errors with `AttributeError: '_connect'` or `'init_database'`, open `X:\Docker Apps\ScanHound\backend\database.py` and adjust the helper names in the test to the real method (the private connection helper and the schema-bootstrap method). Re-run until the failure is `AssertionError: 'poster_path' in ...` (the column does not yet exist) — that is the expected RED state. Expected output: `2 failed` with `assert 'poster_path' in {...}`.

- [ ] Add the column to the CREATE TABLE block. In `X:\Docker Apps\ScanHound\backend\database.py`, change:
  ```python
          tmdb_id INTEGER,
          imdb_id TEXT,
          resolution TEXT,
  ```
  to:
  ```python
          tmdb_id INTEGER,
          imdb_id TEXT,
          poster_path TEXT,
          resolution TEXT,
  ```

- [ ] Append the idempotent migration. In `X:\Docker Apps\ScanHound\backend\database.py`, change:
  ```python
      'ALTER TABLE downloads ADD COLUMN hdr TEXT',
      'ALTER TABLE downloads ADD COLUMN dovi INTEGER DEFAULT 0',
  ]
  ```
  to:
  ```python
      'ALTER TABLE downloads ADD COLUMN hdr TEXT',
      'ALTER TABLE downloads ADD COLUMN dovi INTEGER DEFAULT 0',
      'ALTER TABLE rename_jobs ADD COLUMN poster_path TEXT',
  ]
  ```

- [ ] Run the migration tests — expect GREEN:
  ```
  pytest tests/test_rename_poster_migration.py -v
  ```
  Expected output: `2 passed`.

- [ ] Run the existing rename suites to confirm no regression:
  ```
  pytest tests/test_api_rename.py tests/test_rename_service.py -v
  ```
  Expected output: all collected tests `passed` (existing baseline count unchanged).

- [ ] Commit:
  ```
  git add backend/database.py tests/test_rename_poster_migration.py
  git commit -m "Add poster_path column to rename_jobs (idempotent migration)"
  ```

---

### Task 2: Capture `poster_path` through identification

**Goal:** `_normalize_candidate()` retains `poster_path` from the raw TMDB result; it flows through `_process_file_inner()` into the persisted job. Absent poster → stored `null`, no crash.

**Files:**
- Modify `X:\Docker Apps\ScanHound\backend\rename\service.py` — `_normalize_candidate()` (lines 475–486, add `poster_path` to the returned dict); `_process_file_inner()` job-update block (lines 1255–1263, add `poster_path=match.get("poster_path")`).
- Modify `X:\Docker Apps\ScanHound\tests\test_rename_service.py` — add capture tests + a `poster_path`-bearing search fixture.

**Interfaces:**
- Consumes: raw TMDB result dict (`result.get("poster_path")` → `"/abc.jpg"` or absent); `match` dict in `_process_file_inner`.
- Produces: persisted `rename_jobs.poster_path` (string or `None`).

**Steps:**

- [ ] Add failing tests to `X:\Docker Apps\ScanHound\tests\test_rename_service.py`. First add a poster-bearing search fixture near the existing `_matrix_search` helper (module scope, top of file after the `_service` factory):
  ```python
  def _matrix_search_poster(query, media_type="movie", year=None):
      """Like _matrix_search but the TMDB result carries a poster_path."""
      if media_type != "movie":
          return []
      return [{
          "id": 603, "title": "The Matrix", "original_title": "The Matrix",
          "release_date": "1999-03-31", "poster_path": "/matrix.jpg",
      }]


  def _no_poster_search(query, media_type="movie", year=None):
      """A movie result with poster_path entirely absent."""
      if media_type != "movie":
          return []
      return [{
          "id": 603, "title": "The Matrix", "original_title": "The Matrix",
          "release_date": "1999-03-31",
      }]
  ```
  Then add the test methods inside the existing test class (alongside `test_high_confidence_is_matched_not_applied`):
  ```python
  def test_normalize_candidate_retains_poster_path(self):
      from backend.rename.service import RenameService
      r = {"id": 603, "title": "The Matrix", "release_date": "1999-03-31",
           "poster_path": "/matrix.jpg"}
      cand = RenameService._normalize_candidate(r, "movie")
      assert cand["poster_path"] == "/matrix.jpg"

  def test_normalize_candidate_poster_path_absent_is_none(self):
      from backend.rename.service import RenameService
      r = {"id": 603, "title": "The Matrix", "release_date": "1999-03-31"}
      cand = RenameService._normalize_candidate(r, "movie")
      assert cand["poster_path"] is None

  def test_identified_job_persists_poster_path(self, db, tmp_path):
      save_to, _ = _extracted(tmp_path, "The.Matrix.1999.1080p.BluRay.x264.mkv")
      ids = _service(db, _matrix_search_poster,
                     movie_lib=str(tmp_path / "lib")).process_package("pkg1", save_to)
      job = db.get_rename_job(ids[0])
      assert job["poster_path"] == "/matrix.jpg"

  def test_job_without_poster_stores_null(self, db, tmp_path):
      save_to, _ = _extracted(tmp_path, "The.Matrix.1999.1080p.BluRay.x264.mkv")
      ids = _service(db, _no_poster_search,
                     movie_lib=str(tmp_path / "lib")).process_package("pkg1", save_to)
      job = db.get_rename_job(ids[0])
      assert job["poster_path"] is None
  ```

- [ ] Run the new tests — expect RED:
  ```
  pytest tests/test_rename_service.py -v -k poster
  ```
  Expected output: the two `_normalize_candidate` tests fail with `KeyError: 'poster_path'`; the persistence tests fail with `KeyError: 'poster_path'` (the column has no value written, but `get_rename_job` returns all columns so it will instead show `assert None == '/matrix.jpg'` for the capture test). Net: `4 failed`.

- [ ] Add `poster_path` to `_normalize_candidate()`. In `X:\Docker Apps\ScanHound\backend\rename\service.py`, change:
  ```python
      year = int(date[:4]) if date[:4].isdigit() else None
      return {"title": name, "year": year, "tmdb_id": r.get("id"), "media_type": media_type}
  ```
  to:
  ```python
      year = int(date[:4]) if date[:4].isdigit() else None
      return {"title": name, "year": year, "tmdb_id": r.get("id"),
              "media_type": media_type, "poster_path": r.get("poster_path")}
  ```

- [ ] Persist `poster_path` in `_process_file_inner()`. In `X:\Docker Apps\ScanHound\backend\rename\service.py`, change:
  ```python
      job.update(
          media_type=match.get("media_type"), title=match.get("title"),
          year=match.get("year"), season=match.get("season"),
          episode=match.get("episode"), tmdb_id=match.get("tmdb_id"),
          imdb_id=match.get("imdb_id"), resolution=match.get("resolution"),
          match_confidence=conf, match_source=match.get("source"),
  ```
  to:
  ```python
      job.update(
          media_type=match.get("media_type"), title=match.get("title"),
          year=match.get("year"), season=match.get("season"),
          episode=match.get("episode"), tmdb_id=match.get("tmdb_id"),
          imdb_id=match.get("imdb_id"), resolution=match.get("resolution"),
          poster_path=match.get("poster_path"),
          match_confidence=conf, match_source=match.get("source"),
  ```

- [ ] Confirm `poster_path` is a writable rename field. Open `X:\Docker Apps\ScanHound\backend\database.py` and check the `_RENAME_FIELDS` set used by `update_rename_job`/`create_rename_job`. If `poster_path` is not listed, add it to that set (it gates which columns persist). If `_RENAME_FIELDS` is derived dynamically from the table schema, no change is needed.

- [ ] Run the poster tests — expect GREEN:
  ```
  pytest tests/test_rename_service.py -v -k poster
  ```
  Expected output: `4 passed`.

- [ ] Run the full service suite — no regression:
  ```
  pytest tests/test_rename_service.py -v
  ```
  Expected output: all `passed`.

- [ ] Commit:
  ```
  git add backend/rename/service.py backend/database.py tests/test_rename_service.py
  git commit -m "Capture poster_path through identification into persisted job"
  ```

---

### Task 3: `GET /rename/jobs` serializer emits `poster_url` + read-only `dv_layer`

**Goal:** The route serializer builds `poster_url` from stored `poster_path` using the same TMDB image base/size the Scan serializer uses, and joins `dv_layer` from `dv_scan` by `original_path`. Both `null` when unavailable. Fail-safe.

**Files:**
- Modify `X:\Docker Apps\ScanHound\backend\api\routes\rename.py` — `list_jobs` handler (lines 34–59, enrich each job in the loop); add a module-level `_poster_url()` helper and import of the Scan TMDB image base/size constant.
- Modify `X:\Docker Apps\ScanHound\tests\test_api_rename.py` — serializer tests.

**Interfaces:**
- Consumes: `reg.db.list_rename_jobs(...)`, `reg.db.get_dv_scan(path)` → row dict with `dv_layer` or `None`, the Scan serializer's TMDB image-base+size constant.
- Produces: per-job keys `poster_url: str|None`, `dv_layer: str|None`.

**Steps:**

- [ ] Find the exact TMDB image base+size the Scan serializer uses. Run:
  ```
  grep -nR "TMDB_IMAGE_BASE\|image.tmdb.org\|/t/p/" backend/
  ```
  Note the constant name and module (per the reference, `scanner.py:231` uses `TMDB_IMAGE_BASE` and checks `poster_path.startswith("/")` before prefixing). Record the exact size segment (e.g. `w342`) so the rename serializer mirrors it byte-for-byte.

- [ ] Add failing tests to `X:\Docker Apps\ScanHound\tests\test_api_rename.py`. First extend `_seed_job` usage with a DV-scan seeding helper at module scope (after the existing `_seed_job`):
  ```python
  def _seed_dv_scan(path, dv_layer):
      dm = DatabaseManager()
      dm.upsert_dv_scan(path=path, title="x", dv_layer=dv_layer,
                        sig_mtime=0.0, sig_size=0, source="test",
                        rating_key=None, imdb_id=None)
      dm.close()
  ```
  Then add the tests inside the test class:
  ```python
  def test_poster_url_built_when_poster_path_set(self, client):
      _seed_job(status="matched", title="M", poster_path="/abc.jpg")
      job = client.get("/rename/jobs").json()["jobs"][0]
      assert job["poster_url"] is not None
      assert job["poster_url"].endswith("/abc.jpg")
      assert "image.tmdb.org/t/p/" in job["poster_url"]

  def test_poster_url_null_when_no_poster_path(self, client):
      _seed_job(status="matched", title="M")
      job = client.get("/rename/jobs").json()["jobs"][0]
      assert job["poster_url"] is None

  def test_dv_layer_joined_when_dv_scan_exists(self, client):
      _seed_dv_scan("/x/y.mkv", "fel")
      _seed_job(status="matched", title="M", original_path="/x/y.mkv")
      job = client.get("/rename/jobs").json()["jobs"][0]
      assert job["dv_layer"] == "fel"

  def test_dv_layer_null_when_no_dv_scan(self, client):
      _seed_job(status="matched", title="M", original_path="/x/none.mkv")
      job = client.get("/rename/jobs").json()["jobs"][0]
      assert job["dv_layer"] is None
  ```

- [ ] Confirm the DV-scan upsert helper signature. Open `X:\Docker Apps\ScanHound\backend\database.py` and locate the method that inserts a `dv_scan` row (used by `scan_folder_dv`). Adjust `_seed_dv_scan` in the test to match the real method name/signature (the reference confirms `get_dv_scan`/`get_dv_scans` readers and the `dv_scan` columns `path, title, dv_layer, sig_mtime, sig_size, source, rating_key, imdb_id`).

- [ ] Run the new tests — expect RED:
  ```
  pytest tests/test_api_rename.py -v -k "poster_url or dv_layer"
  ```
  Expected output: `4 failed` with `KeyError: 'poster_url'` / `KeyError: 'dv_layer'`.

- [ ] Add the serializer helper and import to `X:\Docker Apps\ScanHound\backend\api\routes\rename.py`. After the existing imports add (use the exact constant found above — shown here as `TMDB_IMAGE_BASE` from the scanner module):
  ```python
  from backend.api.routes.scanner import TMDB_IMAGE_BASE  # same base+size as Scan posters


  def _poster_url(poster_path):
      """Build a TMDB poster URL from a stored path, fail-safe."""
      try:
          if poster_path and str(poster_path).startswith("/"):
              return f"{TMDB_IMAGE_BASE}{poster_path}"
      except Exception:
          pass
      return None
  ```
  If `TMDB_IMAGE_BASE` is not importable from `scanner.py` (e.g. it is local to `_serialize_item`), instead define the same literal here matching the verified size segment, e.g. `TMDB_IMAGE_BASE = "https://image.tmdb.org/t/p/w342"`, with a comment pointing at the Scan serializer as the source of truth.

- [ ] Enrich each job in `list_jobs`. In `X:\Docker Apps\ScanHound\backend\api\routes\rename.py`, change:
  ```python
      for j in jobs:
          ann = annotations.get(j.get("id")) or {}
          j["destination_conflict"] = ann.get("destination_conflict", False)
          j["keep_recommended"] = ann.get("keep_recommended", False)
          j["keep_reason"] = ann.get("keep_reason")
  ```
  to:
  ```python
      for j in jobs:
          ann = annotations.get(j.get("id")) or {}
          j["destination_conflict"] = ann.get("destination_conflict", False)
          j["keep_recommended"] = ann.get("keep_recommended", False)
          j["keep_reason"] = ann.get("keep_reason")
          j["poster_url"] = _poster_url(j.get("poster_path"))
          try:
              dv = reg.db.get_dv_scan(j.get("original_path"))
          except Exception:
              dv = None
          j["dv_layer"] = (dv or {}).get("dv_layer")
  ```

- [ ] Run the serializer tests — expect GREEN:
  ```
  pytest tests/test_api_rename.py -v -k "poster_url or dv_layer"
  ```
  Expected output: `4 passed`.

- [ ] Run the full rename API suite — no regression:
  ```
  pytest tests/test_api_rename.py -v
  ```
  Expected output: all `passed`.

- [ ] Commit:
  ```
  git add backend/api/routes/rename.py tests/test_api_rename.py
  git commit -m "Serialize poster_url and read-only dv_layer in GET /rename/jobs"
  ```

---

### Task 4: `rematch()` re-checks library guard, persists `poster_path`, accepts season/episode overrides

**Goal:** `rematch()` re-runs the library-not-configured guard (TV → `auto_rename_tv_library`; Movie → `_movie_root`), persists `poster_path` from details, and honors optional `season`/`episode` TV overrides before `build_target()`.

**Files:**
- Modify `X:\Docker Apps\ScanHound\backend\rename\service.py` — `rematch()` signature + body (lines 1424–1453).
- Modify `X:\Docker Apps\ScanHound\backend\api\routes\rename.py` — `RematchRequest` model (lines 19–21, add `season`/`episode`) and `rematch_job` handler (lines 100–106, forward overrides).
- Modify `X:\Docker Apps\ScanHound\tests\test_rename_service.py` — rematch guard + override tests.

**Interfaces:**
- Consumes: `client.details(tmdb_id, media_type)` → dict with `poster_path`, `_movie_root(resolution)`, `self._cfg.get("auto_rename_tv_library")`, `_naming.build_target(meta, ...)`.
- Produces: `rematch(job_id, tmdb_id, media_type=None, season=None, episode=None) -> {ok, status, new_filename, destination_path, warning}`.

**Steps:**

- [ ] Add failing tests to `X:\Docker Apps\ScanHound\tests\test_rename_service.py`. Add a details-returning fake tmdb client and tests inside the test class. The `RenameService` fetches details via `self._tmdb_client()`; seed a job then patch that. Insert:
  ```python
  class _FakeTmdb:
      def __init__(self, details):
          self._details = details
      def details(self, tmdb_id, media_type="movie", language="en-US"):
          return dict(self._details, id=tmdb_id)

  def test_rematch_tv_library_unset_needs_review(self, db, monkeypatch):
      svc = _service(db, _matrix_search, tv_lib="")  # TV library unset
      jid = db.create_rename_job({
          "original_path": "/x/show.mkv", "original_filename": "show.mkv",
          "status": "needs_review", "media_type": "tv", "season": 1, "episode": 2})
      monkeypatch.setattr(svc, "_tmdb_client",
          lambda: _FakeTmdb({"name": "The Show", "first_air_date": "2020-01-01",
                             "poster_path": "/show.jpg"}))
      out = svc.rematch(jid, 1234, media_type="tv")
      job = db.get_rename_job(jid)
      assert out["ok"] is True
      assert job["status"] == "needs_review"
      assert job["warning_message"]
      assert job["destination_path"] in (None, "")

  def test_rematch_tv_library_set_matched_under_root(self, db, monkeypatch, tmp_path):
      tv = str(tmp_path / "tv")
      svc = _service(db, _matrix_search, tv_lib=tv)
      jid = db.create_rename_job({
          "original_path": "/x/show.mkv", "original_filename": "show.mkv",
          "status": "needs_review", "media_type": "tv", "season": 1, "episode": 2})
      monkeypatch.setattr(svc, "_tmdb_client",
          lambda: _FakeTmdb({"name": "The Show", "first_air_date": "2020-01-01",
                             "poster_path": "/show.jpg"}))
      out = svc.rematch(jid, 1234, media_type="tv")
      job = db.get_rename_job(jid)
      assert job["status"] == "matched"
      assert job["destination_path"].startswith(tv)
      assert job["poster_path"] == "/show.jpg"

  def test_rematch_season_episode_override_changes_filename(self, db, monkeypatch, tmp_path):
      tv = str(tmp_path / "tv")
      svc = _service(db, _matrix_search, tv_lib=tv)
      jid = db.create_rename_job({
          "original_path": "/x/show.mkv", "original_filename": "show.mkv",
          "status": "needs_review", "media_type": "tv", "season": 1, "episode": 2})
      monkeypatch.setattr(svc, "_tmdb_client",
          lambda: _FakeTmdb({"name": "The Show", "first_air_date": "2020-01-01",
                             "poster_path": "/show.jpg"}))
      svc.rematch(jid, 1234, media_type="tv", season=3, episode=7)
      fname = db.get_rename_job(jid)["new_filename"]
      assert "S03E07" in fname
  ```

- [ ] Run the new tests — expect RED:
  ```
  pytest tests/test_rename_service.py -v -k rematch
  ```
  Expected output: `test_rematch_tv_library_unset_needs_review` fails (current `rematch` always sets `status="matched"`); the override test fails with `TypeError: rematch() got an unexpected keyword argument 'season'`.

- [ ] Rewrite `rematch()` in `X:\Docker Apps\ScanHound\backend\rename\service.py`. Replace the entire method (lines 1424–1453) with:
  ```python
  def rematch(self, job_id: int, tmdb_id: int, media_type: Optional[str] = None,
              season: Optional[int] = None, episode: Optional[int] = None) -> dict:
      db = self._db
      job = db.get_rename_job(job_id) if db else None
      if not job:
          return {"ok": False, "error": "Job not found"}
      mtype = media_type or job.get("media_type") or "movie"
      client = self._tmdb_client()
      details = None
      if client:
          try:
              details = client.details(int(tmdb_id), media_type=mtype)
          except Exception:
              details = None
      if not details:
          return {"ok": False, "error": "Could not fetch TMDB details"}
      title = details.get("title") or details.get("name") or job.get("title")
      date = details.get("release_date") or details.get("first_air_date") or ""
      year = int(date[:4]) if date[:4].isdigit() else job.get("year")
      poster_path = details.get("poster_path") or job.get("poster_path")
      sea = season if season is not None else job.get("season")
      epi = episode if episode is not None else job.get("episode")
      meta = {**job, "media_type": mtype, "title": title, "year": year,
              "tmdb_id": int(tmdb_id), "season": sea, "episode": epi}
      # Library-not-configured guard (mirrors _process_file_inner).
      if mtype == "tv":
          lib_set = bool(self._cfg.get("auto_rename_tv_library"))
          lib_label = "TV"
      else:
          lib_set = bool(self._movie_root(job.get("resolution")))
          lib_label = "Movie"
      if not lib_set:
          warning = (f"{lib_label} library not configured — set it in "
                     f"Settings → Renaming before applying")
          db.update_rename_job(job_id, title=title, year=year, tmdb_id=int(tmdb_id),
                               media_type=mtype, season=sea, episode=epi,
                               poster_path=poster_path, destination_path=None,
                               match_confidence=100.0, match_source="manual",
                               status="needs_review", warning_message=warning)
          self._broadcast(job_id)
          return {"ok": True, "status": "needs_review", "new_filename": None,
                  "destination_path": None, "warning": warning}
      fname, dest = _naming.build_target(
          meta, movie_root=self._movie_root(job.get("resolution")),
          tv_root=self._cfg.get("auto_rename_tv_library", ""),
          template=self._template_for(mtype))
      db.update_rename_job(job_id, title=title, year=year, tmdb_id=int(tmdb_id),
                           media_type=mtype, season=sea, episode=epi,
                           poster_path=poster_path, new_filename=fname,
                           destination_path=dest, match_confidence=100.0,
                           match_source="manual", status="matched",
                           warning_message=None)
      self._broadcast(job_id)
      return {"ok": True, "status": "matched", "new_filename": fname,
              "destination_path": dest, "warning": None}
  ```

- [ ] Extend the route to forward overrides. In `X:\Docker Apps\ScanHound\backend\api\routes\rename.py`, change:
  ```python
  class RematchRequest(BaseModel):
      tmdb_id: int
      media_type: Optional[str] = None
  ```
  to:
  ```python
  class RematchRequest(BaseModel):
      tmdb_id: int
      media_type: Optional[str] = None
      season: Optional[int] = None
      episode: Optional[int] = None
  ```
  and change the handler:
  ```python
      out = _service(reg).rematch(job_id, body.tmdb_id, body.media_type)
  ```
  to:
  ```python
      out = _service(reg).rematch(job_id, body.tmdb_id, body.media_type,
                                  season=body.season, episode=body.episode)
  ```

- [ ] Run the rematch tests — expect GREEN:
  ```
  pytest tests/test_rename_service.py -v -k rematch
  ```
  Expected output: `3 passed`. If the override test fails on the exact `SxxExx` token, confirm `naming.build_target()`'s TV token formatting and align the asserted token to the real convention (the reference documents `episode` zero-padded; the default TV pattern emits `SNNENN`).

- [ ] Run the full service + API suites — no regression:
  ```
  pytest tests/test_rename_service.py tests/test_api_rename.py -v
  ```
  Expected output: all `passed`.

- [ ] Commit:
  ```
  git add backend/rename/service.py backend/api/routes/rename.py tests/test_rename_service.py
  git commit -m "rematch: re-check library guard, persist poster_path, accept season/episode overrides"
  ```

---

### Task 5: `POST /rename/jobs/{id}/rematch-preview` (non-persisting)

**Goal:** A non-persisting endpoint that fetches TMDB details, builds the target via `_naming.build_target()` without writing the DB, runs the library guard, and returns `{new_filename, destination_path, library_configured, warning}`.

**Files:**
- Modify `X:\Docker Apps\ScanHound\backend\rename\service.py` — add `rematch_preview()` method (place after `rematch()`).
- Modify `X:\Docker Apps\ScanHound\backend\api\routes\rename.py` — add `RematchPreviewRequest` model + `rematch_preview` route (after `rematch_job`).
- Modify `X:\Docker Apps\ScanHound\tests\test_api_rename.py` — preview tests.

**Interfaces:**
- Consumes: same as Task 4 (`client.details`, `_movie_root`, `_naming.build_target`).
- Produces: `rematch_preview(job_id, tmdb_id, media_type=None, season=None, episode=None) -> {new_filename, destination_path, library_configured, warning}`; route `POST /rename/jobs/{job_id}/rematch-preview`.

**Steps:**

- [ ] Add failing tests to `X:\Docker Apps\ScanHound\tests\test_api_rename.py`. The route uses the registry's tmdb client; patch the service method via the registry. Add:
  ```python
  def test_rematch_preview_does_not_mutate_db(self, client, monkeypatch):
      import backend.rename.service as svc_mod
      jid = _seed_job(status="needs_review", title="Old", media_type="movie",
                      destination_path="", new_filename="old.mkv")
      monkeypatch.setattr(
          svc_mod.RenameService, "_tmdb_client",
          lambda self: type("T", (), {"details": staticmethod(
              lambda tmdb_id, media_type="movie", language="en-US": {
                  "title": "New Title", "release_date": "2021-01-01",
                  "poster_path": "/n.jpg"})})())
      before = DatabaseManager(); snap = before.get_rename_job(jid); before.close()
      r = client.post(f"/rename/jobs/{jid}/rematch-preview",
                      json={"tmdb_id": 99, "media_type": "movie"}).json()
      assert "new_filename" in r and "library_configured" in r
      after = DatabaseManager(); now = after.get_rename_job(jid); after.close()
      assert now["new_filename"] == snap["new_filename"]
      assert now["title"] == snap["title"]

  def test_rematch_preview_library_unconfigured_flag(self, client, monkeypatch):
      import backend.rename.service as svc_mod
      jid = _seed_job(status="needs_review", title="Old", media_type="movie")
      monkeypatch.setattr(
          svc_mod.RenameService, "_tmdb_client",
          lambda self: type("T", (), {"details": staticmethod(
              lambda tmdb_id, media_type="movie", language="en-US": {
                  "title": "New Title", "release_date": "2021-01-01"})})())
      r = client.post(f"/rename/jobs/{jid}/rematch-preview",
                      json={"tmdb_id": 99, "media_type": "movie"}).json()
      assert r["library_configured"] is False
      assert r["warning"]
  ```
  Note: the `client` fixture builds the app with empty movie/TV libraries by default (`config_override={"plex_url": "", "plex_token": ""}` and unset rename libs), so the Movie guard fails → `library_configured: False`. Confirm the test app's config has no `auto_rename_movie_library` set; if it does, the second test must override it to empty.

- [ ] Run the new tests — expect RED:
  ```
  pytest tests/test_api_rename.py -v -k rematch_preview
  ```
  Expected output: `2 failed` with `404 Not Found` (route does not exist).

- [ ] Add `rematch_preview()` to `X:\Docker Apps\ScanHound\backend\rename\service.py`, immediately after `rematch()`:
  ```python
  def rematch_preview(self, job_id: int, tmdb_id: int,
                      media_type: Optional[str] = None,
                      season: Optional[int] = None,
                      episode: Optional[int] = None) -> dict:
      """Build a would-be target WITHOUT persisting; run the library guard."""
      db = self._db
      job = db.get_rename_job(job_id) if db else None
      if not job:
          return {"new_filename": None, "destination_path": None,
                  "library_configured": False, "warning": "Job not found"}
      mtype = media_type or job.get("media_type") or "movie"
      client = self._tmdb_client()
      details = None
      if client:
          try:
              details = client.details(int(tmdb_id), media_type=mtype)
          except Exception:
              details = None
      if not details:
          return {"new_filename": None, "destination_path": None,
                  "library_configured": False,
                  "warning": "Could not fetch TMDB details"}
      title = details.get("title") or details.get("name") or job.get("title")
      date = details.get("release_date") or details.get("first_air_date") or ""
      year = int(date[:4]) if date[:4].isdigit() else job.get("year")
      sea = season if season is not None else job.get("season")
      epi = episode if episode is not None else job.get("episode")
      meta = {**job, "media_type": mtype, "title": title, "year": year,
              "tmdb_id": int(tmdb_id), "season": sea, "episode": epi}
      if mtype == "tv":
          lib_set = bool(self._cfg.get("auto_rename_tv_library"))
          lib_label = "TV"
      else:
          lib_set = bool(self._movie_root(job.get("resolution")))
          lib_label = "Movie"
      fname, dest = _naming.build_target(
          meta, movie_root=self._movie_root(job.get("resolution")),
          tv_root=self._cfg.get("auto_rename_tv_library", ""),
          template=self._template_for(mtype))
      warning = None
      if not lib_set:
          dest = None
          warning = (f"{lib_label} library not configured — set it in "
                     f"Settings → Renaming before applying")
      return {"new_filename": fname, "destination_path": dest,
              "library_configured": lib_set, "warning": warning}
  ```

- [ ] Add the route + model to `X:\Docker Apps\ScanHound\backend\api\routes\rename.py`. Add the model near `RematchRequest`:
  ```python
  class RematchPreviewRequest(BaseModel):
      tmdb_id: int
      media_type: Optional[str] = None
      season: Optional[int] = None
      episode: Optional[int] = None
  ```
  And the route after `rematch_job`:
  ```python
  @router.post("/jobs/{job_id}/rematch-preview")
  def rematch_preview(job_id: int, body: RematchPreviewRequest,
                      reg: ServiceRegistry = Depends(get_registry)):
      return _service(reg).rematch_preview(
          job_id, body.tmdb_id, body.media_type,
          season=body.season, episode=body.episode)
  ```

- [ ] Run the preview tests — expect GREEN:
  ```
  pytest tests/test_api_rename.py -v -k rematch_preview
  ```
  Expected output: `2 passed`.

- [ ] Run the full rename suites — no regression:
  ```
  pytest tests/test_api_rename.py tests/test_rename_service.py -v
  ```
  Expected output: all `passed`.

- [ ] Commit:
  ```
  git add backend/rename/service.py backend/api/routes/rename.py tests/test_api_rename.py
  git commit -m "Add non-persisting POST /rename/jobs/{id}/rematch-preview"
  ```

---

### Task 6: `GET /rename/search-tmdb` + `service.search_tmdb_public()`

**Goal:** A search endpoint wrapping a new `service.search_tmdb_public()` over `tmdb_client.search()`; the route serializes each result's `poster_path` into `poster_url`. Empty query / no client → `[]`, no error.

**Files:**
- Modify `X:\Docker Apps\ScanHound\backend\rename\service.py` — add `search_tmdb_public()` method.
- Modify `X:\Docker Apps\ScanHound\backend\api\routes\rename.py` — add `search_tmdb` route (reuses `_poster_url` from Task 3).
- Modify `X:\Docker Apps\ScanHound\tests\test_api_rename.py` — search tests.

**Interfaces:**
- Consumes: `self._tmdb_client().search(query, media_type)` → list of raw TMDB results (`id`, `title`/`name`, `release_date`/`first_air_date`, `poster_path`); `_normalize_candidate()` for title/year extraction.
- Produces: `search_tmdb_public(query, media_type="movie") -> [{tmdb_id, title, year, media_type, poster_path}]`; route `GET /rename/search-tmdb?query=&media_type=` returning `{results: [{..., poster_url}]}`.

**Steps:**

- [ ] Add failing tests to `X:\Docker Apps\ScanHound\tests\test_api_rename.py`:
  ```python
  def test_search_tmdb_results_include_poster_url(self, client, monkeypatch):
      import backend.rename.service as svc_mod
      monkeypatch.setattr(
          svc_mod.RenameService, "_tmdb_client",
          lambda self: type("T", (), {"search": staticmethod(
              lambda query, media_type="movie", year=None, language="en-US": [
                  {"id": 603, "title": "The Matrix",
                   "release_date": "1999-03-31", "poster_path": "/m.jpg"}])})())
      r = client.get("/rename/search-tmdb?query=matrix&media_type=movie").json()
      assert len(r["results"]) == 1
      res = r["results"][0]
      assert res["tmdb_id"] == 603
      assert res["title"] == "The Matrix"
      assert res["year"] == 1999
      assert res["media_type"] == "movie"
      assert res["poster_url"].endswith("/m.jpg")

  def test_search_tmdb_empty_query_returns_empty(self, client):
      r = client.get("/rename/search-tmdb?query=&media_type=movie").json()
      assert r["results"] == []

  def test_search_tmdb_no_client_returns_empty(self, client, monkeypatch):
      import backend.rename.service as svc_mod
      monkeypatch.setattr(svc_mod.RenameService, "_tmdb_client", lambda self: None)
      r = client.get("/rename/search-tmdb?query=matrix&media_type=movie").json()
      assert r["results"] == []
  ```

- [ ] Run the new tests — expect RED:
  ```
  pytest tests/test_api_rename.py -v -k search_tmdb
  ```
  Expected output: `3 failed` with `404 Not Found`.

- [ ] Add `search_tmdb_public()` to `X:\Docker Apps\ScanHound\backend\rename\service.py` (place near `rematch_preview`):
  ```python
  def search_tmdb_public(self, query: str, media_type: str = "movie") -> list:
      """Search TMDB for the rematch picker; fail-safe → [] on any problem."""
      if not query or not query.strip():
          return []
      mtype = "tv" if media_type == "tv" else "movie"
      client = self._tmdb_client()
      if not client:
          return []
      try:
          raw = client.search(query.strip(), media_type=mtype) or []
      except Exception:
          return []
      out = []
      for r in raw:
          cand = self._normalize_candidate(r, mtype)
          if not cand:
              continue
          out.append({"tmdb_id": cand.get("tmdb_id"),
                      "title": cand.get("title"),
                      "year": cand.get("year"),
                      "media_type": mtype,
                      "poster_path": cand.get("poster_path")})
      return out
  ```

- [ ] Add the route to `X:\Docker Apps\ScanHound\backend\api\routes\rename.py` (reuses `_poster_url` from Task 3):
  ```python
  @router.get("/search-tmdb")
  def search_tmdb(query: str = "", media_type: str = "movie",
                  reg: ServiceRegistry = Depends(get_registry)):
      """TMDB search for the rematch picker; serializes poster_url."""
      results = _service(reg).search_tmdb_public(query, media_type)
      for r in results:
          r["poster_url"] = _poster_url(r.pop("poster_path", None))
      return {"results": results}
  ```

- [ ] Run the search tests — expect GREEN:
  ```
  pytest tests/test_api_rename.py -v -k search_tmdb
  ```
  Expected output: `3 passed`.

- [ ] Run the full rename suites — no regression:
  ```
  pytest tests/test_api_rename.py tests/test_rename_service.py -v
  ```
  Expected output: all `passed`.

- [ ] Commit:
  ```
  git add backend/rename/service.py backend/api/routes/rename.py tests/test_api_rename.py
  git commit -m "Add GET /rename/search-tmdb wrapping service.search_tmdb_public()"
  ```

---

### Task 7: Bulk endpoints + `set-destination`

**Goal:** `POST /rename/jobs/bulk/{apply,reidentify,delete,set-destination}` backed by thin service helpers single-flighted via `_bulk_lock`, reusing single-job methods. `set-destination` rebuilds `destination_path` under the chosen root and re-runs the per-job library guard. Per-id results with partial-failure reporting.

**Files:**
- Modify `X:\Docker Apps\ScanHound\backend\rename\service.py` — add `bulk_apply()`, `bulk_reidentify()`, `bulk_delete()`, `set_destination()` (single-job) + `bulk_set_destination()`.
- Modify `X:\Docker Apps\ScanHound\backend\api\routes\rename.py` — add `BulkIdsRequest` / `BulkSetDestRequest` models + four routes.
- Modify `X:\Docker Apps\ScanHound\tests\test_api_rename.py` — bulk tests.

**Interfaces:**
- Consumes: `self.apply(id)`, `self.reidentify(id)`, `db.delete_rename_job(id)`, `db.get_rename_job(id)`, `_naming.build_target`, `_movie_root`, `_bulk_lock`.
- Produces:
  - `bulk_apply(ids) -> {results:[{id,ok,error}], applied, failed}`
  - `bulk_reidentify(ids) -> {ok, queued}`
  - `bulk_delete(ids) -> {deleted}`
  - `set_destination(id, root) -> {id, ok, destination_path, error}`
  - `bulk_set_destination(ids, root) -> {results:[...], updated}`
  - Routes `POST /rename/jobs/bulk/{apply,reidentify,delete,set-destination}`.

**Steps:**

- [ ] Add failing tests to `X:\Docker Apps\ScanHound\tests\test_api_rename.py`:
  ```python
  def test_bulk_apply_partial_failure(self, client, tmp_path):
      dest = tmp_path / "lib"
      src = tmp_path / "ok.mkv"; src.write_text("x")
      ok = _seed_job(status="matched", title="Ok", original_path=str(src),
                     destination_path=str(dest), new_filename="Ok (2020).mkv")
      bad = _seed_job(status="matched", title="Bad",
                      original_path=str(tmp_path / "missing.mkv"),
                      destination_path=str(dest), new_filename="Bad (2020).mkv")
      r = client.post("/rename/jobs/bulk/apply",
                      json={"ids": [ok, bad]}).json()
      by = {x["id"]: x for x in r["results"]}
      assert by[ok]["ok"] is True
      assert by[bad]["ok"] is False and by[bad]["error"]
      assert r["applied"] == 1 and r["failed"] == 1

  def test_bulk_delete_counts(self, client):
      a = _seed_job(status="needs_review", title="A")
      b = _seed_job(status="needs_review", title="B")
      r = client.post("/rename/jobs/bulk/delete", json={"ids": [a, b]}).json()
      assert r["deleted"] == 2
      assert client.get("/rename/jobs").json()["jobs"] == []

  def test_bulk_reidentify_queues(self, client):
      a = _seed_job(status="needs_review", title="A")
      r = client.post("/rename/jobs/bulk/reidentify", json={"ids": [a]}).json()
      assert r["ok"] is True and r["queued"] == 1

  def test_bulk_set_destination_guard_enforced(self, client, tmp_path):
      # Movie job, valid root → rebuilt destination under root.
      jid = _seed_job(status="matched", title="The Matrix", year=1999,
                      media_type="movie", resolution="1080p",
                      new_filename="The Matrix (1999) [1080p].mkv")
      root = str(tmp_path / "movies")
      r = client.post("/rename/jobs/bulk/set-destination",
                      json={"ids": [jid], "destination_root": root}).json()
      res = r["results"][0]
      assert res["ok"] is True
      assert res["destination_path"].startswith(root)
      assert r["updated"] == 1

  def test_bulk_set_destination_empty_root_blocks(self, client):
      jid = _seed_job(status="matched", title="M", media_type="movie")
      r = client.post("/rename/jobs/bulk/set-destination",
                      json={"ids": [jid], "destination_root": ""}).json()
      res = r["results"][0]
      assert res["ok"] is False
      assert res["destination_path"] is None
  ```

- [ ] Run the new tests — expect RED:
  ```
  pytest tests/test_api_rename.py -v -k "bulk"
  ```
  Expected output: `5 failed` with `404 Not Found`.

- [ ] Add the bulk service helpers to `X:\Docker Apps\ScanHound\backend\rename\service.py`. Place after `set destination`/`search_tmdb_public`:
  ```python
  def bulk_apply(self, ids: list) -> dict:
      if not self._bulk_lock.acquire(blocking=False):
          return {"results": [], "applied": 0, "failed": 0, "busy": True}
      try:
          results, applied, failed = [], 0, 0
          for jid in ids or []:
              try:
                  out = self.apply(int(jid))
              except Exception as e:
                  out = {"ok": False, "error": str(e)}
              ok = bool(out.get("ok"))
              results.append({"id": int(jid), "ok": ok,
                              "error": out.get("error")})
              applied += 1 if ok else 0
              failed += 0 if ok else 1
          return {"results": results, "applied": applied, "failed": failed}
      finally:
          self._bulk_lock.release()

  def bulk_reidentify(self, ids: list) -> dict:
      if not self._bulk_lock.acquire(blocking=False):
          return {"ok": False, "queued": 0, "busy": True}
      try:
          queued = 0
          for jid in ids or []:
              try:
                  self.reidentify(int(jid))
                  queued += 1
              except Exception:
                  logger.exception("bulk_reidentify: job %s failed", jid)
          return {"ok": True, "queued": queued}
      finally:
          self._bulk_lock.release()

  def bulk_delete(self, ids: list) -> dict:
      db = self._db
      if db is None:
          return {"deleted": 0}
      deleted = 0
      for jid in ids or []:
          try:
              db.delete_rename_job(int(jid))
              deleted += 1
          except Exception:
              logger.exception("bulk_delete: job %s failed", jid)
      return {"deleted": deleted}

  def set_destination(self, job_id: int, root: str) -> dict:
      """Rebuild one job's destination_path under ``root``; re-run guard."""
      db = self._db
      job = db.get_rename_job(job_id) if db else None
      if not job:
          return {"id": int(job_id), "ok": False,
                  "destination_path": None, "error": "Job not found"}
      if not root or not str(root).strip():
          db.update_rename_job(job_id, status="needs_review",
                               destination_path=None,
                               warning_message="Destination library not configured")
          self._broadcast(job_id)
          return {"id": int(job_id), "ok": False, "destination_path": None,
                  "error": "Destination library not configured"}
      mtype = job.get("media_type") or "movie"
      meta = {**job, "media_type": mtype}
      if mtype == "tv":
          fname, dest = _naming.build_target(
              meta, tv_root=root, movie_root=self._movie_root(job.get("resolution")),
              template=self._template_for(mtype))
      else:
          fname, dest = _naming.build_target(
              meta, movie_root=root, tv_root=self._cfg.get("auto_rename_tv_library", ""),
              template=self._template_for(mtype))
      db.update_rename_job(job_id, new_filename=fname, destination_path=dest,
                           status="matched", warning_message=None)
      self._broadcast(job_id)
      return {"id": int(job_id), "ok": True, "destination_path": dest,
              "error": None}

  def bulk_set_destination(self, ids: list, root: str) -> dict:
      if not self._bulk_lock.acquire(blocking=False):
          return {"results": [], "updated": 0, "busy": True}
      try:
          results, updated = [], 0
          for jid in ids or []:
              try:
                  out = self.set_destination(int(jid), root)
              except Exception as e:
                  out = {"id": int(jid), "ok": False,
                         "destination_path": None, "error": str(e)}
              results.append(out)
              updated += 1 if out.get("ok") else 0
          return {"results": results, "updated": updated}
      finally:
          self._bulk_lock.release()
  ```

- [ ] Add the route models + routes to `X:\Docker Apps\ScanHound\backend\api\routes\rename.py`. Add models near the others:
  ```python
  class BulkIdsRequest(BaseModel):
      ids: list[int] = []


  class BulkSetDestRequest(BaseModel):
      ids: list[int] = []
      destination_root: str = ""
  ```
  And the routes:
  ```python
  @router.post("/jobs/bulk/apply")
  def bulk_apply(body: BulkIdsRequest, reg: ServiceRegistry = Depends(get_registry)):
      return _service(reg).bulk_apply(body.ids)


  @router.post("/jobs/bulk/reidentify")
  def bulk_reidentify(body: BulkIdsRequest,
                      reg: ServiceRegistry = Depends(get_registry)):
      return _service(reg).bulk_reidentify(body.ids)


  @router.post("/jobs/bulk/delete")
  def bulk_delete(body: BulkIdsRequest, reg: ServiceRegistry = Depends(get_registry)):
      if reg.db is None:
          raise HTTPException(status_code=503, detail="Database unavailable")
      return _service(reg).bulk_delete(body.ids)


  @router.post("/jobs/bulk/set-destination")
  def bulk_set_destination(body: BulkSetDestRequest,
                           reg: ServiceRegistry = Depends(get_registry)):
      return _service(reg).bulk_set_destination(body.ids, body.destination_root)
  ```

- [ ] Run the bulk tests — expect GREEN:
  ```
  pytest tests/test_api_rename.py -v -k "bulk"
  ```
  Expected output: `5 passed`. If `test_bulk_set_destination_guard_enforced` fails on the `startswith(root)` check, confirm `build_target()` joins the title-folder under `movie_root` (the reference shows movie layout `movie_root/Title (Year)/...`), so the returned `dest` begins with `root`.

- [ ] Run the full rename suites — no regression:
  ```
  pytest tests/test_api_rename.py tests/test_rename_service.py -v
  ```
  Expected output: all `passed`.

- [ ] Commit:
  ```
  git add backend/rename/service.py backend/api/routes/rename.py tests/test_api_rename.py
  git commit -m "Add bulk apply/reidentify/delete/set-destination endpoints with guard"
  ```

---

### Task 8: `POST /rename/jobs/apply-confident`

**Goal:** Apply only jobs with `status == "matched"` AND `match_confidence >= 95`, server-enforced. Optional `ids` scopes to a selection; no `ids` → all matched. Report `applied`, `skipped` (gated out), `failed`.

**Files:**
- Modify `X:\Docker Apps\ScanHound\backend\rename\service.py` — add `apply_confident()`.
- Modify `X:\Docker Apps\ScanHound\backend\api\routes\rename.py` — add `ApplyConfidentRequest` model + route.
- Modify `X:\Docker Apps\ScanHound\tests\test_api_rename.py` — threshold tests.

**Interfaces:**
- Consumes: `db.list_rename_jobs(limit=...)`, `db.get_rename_job(id)`, `self.apply(id)`, `_bulk_lock`.
- Produces: `apply_confident(ids=None) -> {results:[{id,ok,error}], applied, skipped, failed}`; route `POST /rename/jobs/apply-confident`.

**Steps:**

- [ ] Add failing tests to `X:\Docker Apps\ScanHound\tests\test_api_rename.py`:
  ```python
  def test_apply_confident_applies_matched_96(self, client, tmp_path):
      dest = tmp_path / "lib"
      src = tmp_path / "ok.mkv"; src.write_text("x")
      jid = _seed_job(status="matched", title="Ok", match_confidence=96,
                      original_path=str(src), destination_path=str(dest),
                      new_filename="Ok (2020).mkv")
      r = client.post("/rename/jobs/apply-confident", json={}).json()
      assert r["applied"] == 1 and r["skipped"] == 0
      assert (dest / "Ok (2020).mkv").exists()

  def test_apply_confident_skips_matched_94(self, client):
      jid = _seed_job(status="matched", title="Low", match_confidence=94)
      r = client.post("/rename/jobs/apply-confident", json={}).json()
      assert r["applied"] == 0 and r["skipped"] == 1

  def test_apply_confident_skips_needs_review_99(self, client):
      jid = _seed_job(status="needs_review", title="NR", match_confidence=99)
      r = client.post("/rename/jobs/apply-confident", json={}).json()
      assert r["applied"] == 0 and r["skipped"] == 1

  def test_apply_confident_scoped_to_ids(self, client, tmp_path):
      dest = tmp_path / "lib"
      s1 = tmp_path / "a.mkv"; s1.write_text("x")
      a = _seed_job(status="matched", title="A", match_confidence=96,
                    original_path=str(s1), destination_path=str(dest),
                    new_filename="A (2020).mkv")
      b = _seed_job(status="matched", title="B", match_confidence=96,
                    original_path=str(tmp_path / "b.mkv"),
                    destination_path=str(dest), new_filename="B (2020).mkv")
      # Scope to only A; B (also confident) must be untouched.
      r = client.post("/rename/jobs/apply-confident", json={"ids": [a]}).json()
      assert r["applied"] == 1
      assert all(x["id"] == a for x in r["results"])
  ```

- [ ] Run the new tests — expect RED:
  ```
  pytest tests/test_api_rename.py -v -k apply_confident
  ```
  Expected output: `4 failed` with `404 Not Found`.

- [ ] Add `apply_confident()` to `X:\Docker Apps\ScanHound\backend\rename\service.py` (place near the bulk helpers):
  ```python
  def apply_confident(self, ids: Optional[list] = None) -> dict:
      """Apply only matched jobs at confidence >= 95. Server-enforced gate."""
      db = self._db
      if db is None:
          return {"results": [], "applied": 0, "skipped": 0, "failed": 0}
      if ids:
          candidates = []
          for jid in ids:
              job = db.get_rename_job(int(jid))
              if job:
                  candidates.append(job)
      else:
          candidates = db.list_rename_jobs(limit=100000) or []
      if not self._bulk_lock.acquire(blocking=False):
          return {"results": [], "applied": 0, "skipped": 0, "failed": 0,
                  "busy": True}
      try:
          results, applied, skipped, failed = [], 0, 0, 0
          for job in candidates:
              conf = job.get("match_confidence") or 0.0
              if job.get("status") != "matched" or conf < 95:
                  skipped += 1
                  continue
              jid = job["id"]
              try:
                  out = self.apply(int(jid))
              except Exception as e:
                  out = {"ok": False, "error": str(e)}
              ok = bool(out.get("ok"))
              results.append({"id": int(jid), "ok": ok,
                              "error": out.get("error")})
              applied += 1 if ok else 0
              failed += 0 if ok else 1
          return {"results": results, "applied": applied,
                  "skipped": skipped, "failed": failed}
      finally:
          self._bulk_lock.release()
  ```

- [ ] Add the route + model to `X:\Docker Apps\ScanHound\backend\api\routes\rename.py`. Add the model:
  ```python
  class ApplyConfidentRequest(BaseModel):
      ids: Optional[list[int]] = None
  ```
  And the route:
  ```python
  @router.post("/jobs/apply-confident")
  def apply_confident(body: ApplyConfidentRequest,
                      reg: ServiceRegistry = Depends(get_registry)):
      return _service(reg).apply_confident(body.ids)
  ```

- [ ] Run the apply-confident tests — expect GREEN:
  ```
  pytest tests/test_api_rename.py -v -k apply_confident
  ```
  Expected output: `4 passed`.

- [ ] Run the entire rename test surface — no regression across all 8 tasks:
  ```
  pytest tests/test_api_rename.py tests/test_rename_service.py tests/test_rename_poster_migration.py -v
  ```
  Expected output: all `passed`.

- [ ] Commit:
  ```
  git add backend/rename/service.py backend/api/routes/rename.py tests/test_api_rename.py
  git commit -m "Add POST /rename/jobs/apply-confident with server-side matched+95 gate"
  ```

---

**Backend plan files of record:** `X:\Docker Apps\ScanHound\backend\database.py`, `X:\Docker Apps\ScanHound\backend\rename\service.py`, `X:\Docker Apps\ScanHound\backend\api\routes\rename.py`; tests under `X:\Docker Apps\ScanHound\tests\test_api_rename.py`, `X:\Docker Apps\ScanHound\tests\test_rename_service.py`, and new `X:\Docker Apps\ScanHound\tests\test_rename_poster_migration.py`. After all eight tasks land and tests pass on the host, deploy via `docker compose up -d --build` from `X:\Docker Apps\ScanHound` (frontend is baked into the image; `docker restart` deploys nothing).

---

### Task 9: API types + client methods

**Files**
- Modify `frontend/src/lib/api/types.ts` — add `poster_url`/`dv_layer` to `RenameJob` (after line 114 `keep_reason`); add `TmdbSearchResult`, bulk-response, rematch-response interfaces.
- Modify `frontend/src/lib/api/client.ts` — add 8 methods to the `api` object after the existing `deleteRenameJob` (line 359).

**Interfaces**
- Produces (types): `RenameJob.poster_url?: string | null`, `RenameJob.dv_layer?: string | null`; `TmdbSearchResult`; `BulkApplyResult`, `BulkApplyResponse`; `BulkReidentifyResponse`; `BulkDeleteResponse`; `BulkSetDestResult`, `BulkSetDestResponse`; `ApplyConfidentResponse`; `RematchPreviewResponse`; `RematchConfirmResponse`.
- Produces (client): `api.bulkApply(ids:number[]):Promise<BulkApplyResponse>`, `api.bulkReidentify(ids:number[]):Promise<BulkReidentifyResponse>`, `api.bulkDelete(ids:number[]):Promise<BulkDeleteResponse>`, `api.bulkSetDestination(ids:number[],destinationRoot:string):Promise<BulkSetDestResponse>`, `api.applyConfident(ids?:number[]):Promise<ApplyConfidentResponse>`, `api.searchTmdb(query:string,mediaType:string):Promise<{results:TmdbSearchResult[]}>`, `api.rematchPreview(id:number,body:{tmdb_id:number;media_type:string;season?:number;episode?:number}):Promise<RematchPreviewResponse>`, extended `api.rematchRename(id,tmdbId,mediaType?,season?,episode?)`.
- Consumes: existing `request<T>` wrapper (`client.ts:48-74`).

**Steps**
- [ ] Read `frontend/src/lib/api/types.ts` to confirm the `RenameJob` block ends at line 114 (`keep_reason?: string | null;` then `}`).
- [ ] In `types.ts`, edit the tail of `RenameJob` to add the two read-only fields. Replace:
  ```ts
    keep_recommended?: boolean;
    keep_reason?: string | null;
  }
  ```
  with:
  ```ts
    keep_recommended?: boolean;
    keep_reason?: string | null;
    /** Fully-formed TMDB poster URL built server-side from poster_path. Empty/null = no poster. */
    poster_url?: string | null;
    /** Read-only DV layer joined from dv_scan by path at serialize time (FEL/MEL/P8/P5). Null = unknown. */
    dv_layer?: string | null;
  }
  ```
- [ ] Append the new response/search types to the end of `types.ts`:
  ```ts
  export interface TmdbSearchResult {
    tmdb_id: number;
    title: string;
    year: number | null;
    media_type: 'movie' | 'tv';
    poster_url: string | null;
  }

  export interface BulkApplyResult {
    id: number;
    ok: boolean;
    error: string | null;
  }
  export interface BulkApplyResponse {
    results: BulkApplyResult[];
    applied: number;
    failed: number;
  }

  export interface BulkReidentifyResponse {
    ok: boolean;
    queued: number;
  }

  export interface BulkDeleteResponse {
    deleted: number;
  }

  export interface BulkSetDestResult {
    id: number;
    ok: boolean;
    destination_path: string | null;
    error: string | null;
  }
  export interface BulkSetDestResponse {
    results: BulkSetDestResult[];
    updated: number;
  }

  export interface ApplyConfidentResponse {
    results: BulkApplyResult[];
    applied: number;
    skipped: number;
    failed: number;
  }

  export interface RematchPreviewResponse {
    new_filename: string;
    destination_path: string | null;
    library_configured: boolean;
    warning: string | null;
  }

  export interface RematchConfirmResponse {
    ok: boolean;
    status: string;
    new_filename: string;
    destination_path: string | null;
    warning: string | null;
  }
  ```
- [ ] Read `frontend/src/lib/api/client.ts` lines 1-20 to confirm the import line for types, and lines 313-360 to confirm the existing rename methods and the trailing comma/brace style.
- [ ] In `client.ts`, extend the type import to include the new types. Find the import that brings in `RenameJob` (e.g. `import type { ..., RenameJob, RenameStatus, DvScan } from './types';`) and add the new names: `BulkApplyResponse, BulkReidentifyResponse, BulkDeleteResponse, BulkSetDestResponse, ApplyConfidentResponse, TmdbSearchResult, RematchPreviewResponse, RematchConfirmResponse`.
- [ ] In `client.ts`, replace the existing `rematchRename` method (lines 316-320) to accept optional `season`/`episode`:
  ```ts
  rematchRename: (id: number, tmdbId: number, mediaType?: string, season?: number, episode?: number) =>
    request<RematchConfirmResponse>(`/rename/jobs/${id}/rematch`, {
      method: 'POST',
      body: JSON.stringify({
        tmdb_id: tmdbId,
        media_type: mediaType ?? null,
        season: season ?? null,
        episode: episode ?? null
      })
    }),
  ```
- [ ] In `client.ts`, add the new methods immediately after `deleteRenameJob` (line 359):
  ```ts
  bulkApply: (ids: number[]) =>
    request<BulkApplyResponse>('/rename/jobs/bulk/apply', {
      method: 'POST',
      body: JSON.stringify({ ids })
    }),
  bulkReidentify: (ids: number[]) =>
    request<BulkReidentifyResponse>('/rename/jobs/bulk/reidentify', {
      method: 'POST',
      body: JSON.stringify({ ids })
    }),
  bulkDelete: (ids: number[]) =>
    request<BulkDeleteResponse>('/rename/jobs/bulk/delete', {
      method: 'POST',
      body: JSON.stringify({ ids })
    }),
  bulkSetDestination: (ids: number[], destinationRoot: string) =>
    request<BulkSetDestResponse>('/rename/jobs/bulk/set-destination', {
      method: 'POST',
      body: JSON.stringify({ ids, destination_root: destinationRoot })
    }),
  applyConfident: (ids?: number[]) =>
    request<ApplyConfidentResponse>('/rename/jobs/apply-confident', {
      method: 'POST',
      body: JSON.stringify(ids ? { ids } : {})
    }),
  searchTmdb: (query: string, mediaType: string) => {
    const qs = '?' + new URLSearchParams({ query, media_type: mediaType }).toString();
    return request<{ results: TmdbSearchResult[] }>(`/rename/search-tmdb${qs}`);
  },
  rematchPreview: (
    id: number,
    body: { tmdb_id: number; media_type: string; season?: number; episode?: number }
  ) =>
    request<RematchPreviewResponse>(`/rename/jobs/${id}/rematch-preview`, {
      method: 'POST',
      body: JSON.stringify({
        tmdb_id: body.tmdb_id,
        media_type: body.media_type,
        season: body.season ?? null,
        episode: body.episode ?? null
      })
    }),
  ```
- [ ] Run `cd frontend && npm run check`. Expected output: `svelte-check found 0 errors and 0 warnings` (or unchanged pre-existing warning count; **zero new errors**). If errors reference the new types, fix the offending interface/import before proceeding.
- [ ] Commit: `git add frontend/src/lib/api/types.ts frontend/src/lib/api/client.ts && git commit -m "renames: add poster_url/dv_layer types + bulk/search/rematch client methods"` (end body with `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`).

---

### Task 10: Rename status/DV/confidence variant maps + `categoryOf` helper (TDD)

**Files**
- Modify `frontend/src/lib/constants.ts` — append `RENAME_STATUS_VARIANTS`, `DV_LAYER_VARIANTS`, `renameStatusVariant()`, `confidenceVariant()`, `renameStatusBorderColor()` after line 57.
- Create `frontend/src/lib/renames/category.ts` — pure `categoryOf(job)` + `RENAME_CATEGORIES`.
- Create `frontend/src/lib/renames/category.test.ts` — Vitest unit tests for `categoryOf`.
- Create `frontend/src/lib/constants.test.ts` — Vitest unit tests for `confidenceVariant`.

**Interfaces**
- Produces: `RENAME_STATUS_VARIANTS: Record<string,BadgeVariant>`, `DV_LAYER_VARIANTS: Record<string,BadgeVariant>`, `renameStatusVariant(status:string|null|undefined):BadgeVariant`, `confidenceVariant(pct:number|null|undefined):BadgeVariant`, `renameStatusBorderColor(status:string|null|undefined):string`; `type RenameCategory = 'all'|'movies'|'tv'|'4k'|'1080p'|'remux'`; `RENAME_CATEGORIES: readonly RenameCategory[]`; `categoryOf(job:RenameJob):Set<RenameCategory>`.
- Consumes: `BadgeVariant` (`./components/Badge.svelte`), `RenameJob` (`../api/types`).

**Steps**
- [ ] Create the failing test `frontend/src/lib/renames/category.test.ts`:
  ```ts
  import { describe, it, expect } from 'vitest';
  import { categoryOf } from './category';
  import type { RenameJob } from '$lib/api/types';

  function job(over: Partial<RenameJob>): RenameJob {
    return {
      id: 1, package_name: null, original_path: '/x', original_filename: null,
      new_filename: null, destination_path: null, status: 'matched',
      media_type: null, title: null, year: null, season: null, episode: null,
      tmdb_id: null, imdb_id: null, resolution: null, match_confidence: null,
      match_source: null, move_method: null, warning_message: null,
      error_message: null, plex_sort_title: null, detected_at: null,
      processed_at: null, reverted_at: null, ...over
    } as RenameJob;
  }

  describe('categoryOf', () => {
    it('classifies a 4K movie under both movies and 4k', () => {
      const c = categoryOf(job({ media_type: 'movie', resolution: '2160p' }));
      expect(c.has('movies')).toBe(true);
      expect(c.has('4k')).toBe(true);
      expect(c.has('tv')).toBe(false);
    });
    it('treats media_type "show" as tv', () => {
      expect(categoryOf(job({ media_type: 'show' })).has('tv')).toBe(true);
    });
    it('matches uhd/4k/2160p case-insensitively for 4k', () => {
      expect(categoryOf(job({ resolution: 'UHD' })).has('4k')).toBe(true);
      expect(categoryOf(job({ resolution: '4K' })).has('4k')).toBe(true);
    });
    it('classifies 1080p', () => {
      expect(categoryOf(job({ resolution: '1080p' })).has('1080p')).toBe(true);
    });
    it('detects remux from filename when resolution lacks it', () => {
      const c = categoryOf(job({ resolution: '2160p', new_filename: 'Movie.2024.REMUX.mkv' }));
      expect(c.has('remux')).toBe(true);
    });
    it('returns an empty set for a job with no media_type/resolution', () => {
      expect(categoryOf(job({})).size).toBe(0);
    });
  });
  ```
- [ ] Run `cd frontend && npm run test:unit -- category`. Expected: failure — `Failed to resolve import "./category"` (module does not exist yet). This confirms the test runs and the impl is missing.
- [ ] Create the minimal implementation `frontend/src/lib/renames/category.ts`:
  ```ts
  import type { RenameJob } from '$lib/api/types';

  export type RenameCategory = 'all' | 'movies' | 'tv' | '4k' | '1080p' | 'remux';
  export const RENAME_CATEGORIES: readonly RenameCategory[] = [
    'all', 'movies', 'tv', '4k', '1080p', 'remux'
  ];

  /** Membership set for a job across filter chips (a job can belong to several). */
  export function categoryOf(job: RenameJob): Set<RenameCategory> {
    const cats = new Set<RenameCategory>();
    const mt = (job.media_type ?? '').toLowerCase();
    const res = (job.resolution ?? '').toLowerCase();
    const names = `${job.new_filename ?? ''} ${job.original_filename ?? ''}`.toLowerCase();

    if (mt === 'movie') cats.add('movies');
    if (mt === 'tv' || mt === 'show') cats.add('tv');
    if (/2160p|4k|uhd/.test(res)) cats.add('4k');
    if (res.includes('1080p')) cats.add('1080p');
    if (res.includes('remux') || names.includes('remux')) cats.add('remux');
    return cats;
  }
  ```
- [ ] Run `cd frontend && npm run test:unit -- category`. Expected: all 6 `categoryOf` assertions pass (`6 passed`).
- [ ] Create the failing test `frontend/src/lib/constants.test.ts`:
  ```ts
  import { describe, it, expect } from 'vitest';
  import { confidenceVariant, renameStatusVariant } from './constants';

  describe('confidenceVariant', () => {
    it('is success at and above 95', () => {
      expect(confidenceVariant(95)).toBe('success');
      expect(confidenceVariant(100)).toBe('success');
    });
    it('is warning in 70..94', () => {
      expect(confidenceVariant(70)).toBe('warning');
      expect(confidenceVariant(94)).toBe('warning');
    });
    it('is error below 70', () => {
      expect(confidenceVariant(69)).toBe('error');
      expect(confidenceVariant(0)).toBe('error');
    });
    it('is default for null/undefined', () => {
      expect(confidenceVariant(null)).toBe('default');
      expect(confidenceVariant(undefined)).toBe('default');
    });
  });

  describe('renameStatusVariant', () => {
    it('maps known rename statuses', () => {
      expect(renameStatusVariant('needs_review')).toBe('warning');
      expect(renameStatusVariant('matched')).toBe('accent');
      expect(renameStatusVariant('applied')).toBe('success');
      expect(renameStatusVariant('failed')).toBe('error');
    });
    it('falls back to default for unknown', () => {
      expect(renameStatusVariant('zzz')).toBe('default');
      expect(renameStatusVariant(null)).toBe('default');
    });
  });
  ```
- [ ] Run `cd frontend && npm run test:unit -- constants`. Expected: failure — `confidenceVariant` / `renameStatusVariant` are not exported (`No "confidenceVariant" export is defined`).
- [ ] Append the maps and helpers to the end of `frontend/src/lib/constants.ts`:
  ```ts
  /** Rename-pipeline status → Badge variant (distinct from scan STATUS_VARIANTS). */
  export const RENAME_STATUS_VARIANTS: Record<string, BadgeVariant> = {
    needs_review: 'warning',
    matched: 'accent',
    applied: 'success',
    reverted: 'default',
    failed: 'error',
    pending: 'info',
  };

  /** Dolby Vision layer → Badge variant. */
  export const DV_LAYER_VARIANTS: Record<string, BadgeVariant> = {
    fel: 'error',
    mel: 'orange',
    p8: 'accent',
    p5: 'info',
  };

  export function renameStatusVariant(status: string | null | undefined): BadgeVariant {
    if (!status) return 'default';
    return RENAME_STATUS_VARIANTS[status.toLowerCase()] ?? 'default';
  }

  export function dvLayerVariant(layer: string | null | undefined): BadgeVariant {
    if (!layer) return 'default';
    return DV_LAYER_VARIANTS[layer.toLowerCase()] ?? 'default';
  }

  /** Confidence %→ variant: ≥95 success, 70..94 warning, <70 error, null default. */
  export function confidenceVariant(pct: number | null | undefined): BadgeVariant {
    if (pct == null || Number.isNaN(pct)) return 'default';
    if (pct >= 95) return 'success';
    if (pct >= 70) return 'warning';
    return 'error';
  }

  export function renameStatusBorderColor(status: string | null | undefined): string {
    switch (renameStatusVariant(status)) {
      case 'error': return 'var(--error)';
      case 'warning': return 'var(--warning)';
      case 'success': return 'var(--success)';
      case 'accent': return 'var(--accent)';
      case 'info': return '#3b82f6';
      case 'orange': return '#f97316';
      default: return 'var(--border)';
    }
  }
  ```
- [ ] Run `cd frontend && npm run test:unit -- constants category`. Expected: both suites green (`10 passed`).
- [ ] Run `cd frontend && npm run check`. Expected: `0 errors` (no new errors).
- [ ] Commit: `git add frontend/src/lib/constants.ts frontend/src/lib/constants.test.ts frontend/src/lib/renames && git commit -m "renames: add RENAME_STATUS/DV_LAYER variants, confidenceVariant, categoryOf (unit-tested)"`.

---

### Task 11: Store additions — multi-select, view prefs, bulk actions

**Files**
- Modify `frontend/src/lib/stores/renames.ts` — add the persisted-prefs import from results, the select/pref/query stores, the bulk actions, and export `refresh` for orchestrator use. Insert after the existing imports (line 4) and after `rematchJob` (line 67).

**Interfaces**
- Produces: `selectedJobIds: Writable<Set<number>>`; `toggleSelect(id:number):void`, `selectAll(ids:number[]):void`, `clearSelection():void`; `viewMode: Writable<'list'|'grid'>`; `renameSort: Writable<'detected_desc'|'detected_asc'|'confidence_desc'|'title_asc'>`; `renameCategory: Writable<RenameCategory>`; `renameQuery: Writable<string>`; `bulkBusy: Writable<boolean>`; `bulkApply():Promise<void>`, `bulkReidentify():Promise<void>`, `bulkDelete():Promise<void>`, `bulkSetDestination(root:string):Promise<void>`, `applyConfident(ids?:number[]):Promise<void>`; re-export `refreshRenames()`.
- Consumes: `persisted` (`$lib/stores/results`), `api.*` bulk/applyConfident methods (Task 9), `addToast` (`$lib/stores/notifications`), `RenameCategory` (Task 10), private `refresh()` (existing, `renames.ts`).

**Steps**
- [ ] Read `frontend/src/lib/stores/renames.ts` lines 1-67 to confirm import block, `refresh()` signature, and `rematchJob` location.
- [ ] Add imports at the top of `renames.ts` (after the existing `import type { RenameJob, RenameStatus, DvScan } ...` line):
  ```ts
  import { persisted } from '$lib/stores/results';
  import { addToast } from '$lib/stores/notifications';
  import type { RenameCategory } from '$lib/renames/category';
  ```
- [ ] In `renames.ts`, immediately after `export const renameStatus = writable<RenameStatus | null>(null);` add the select + pref + query stores:
  ```ts
  // --- Multi-select ---
  export const selectedJobIds = writable<Set<number>>(new Set());
  export function toggleSelect(id: number) {
    selectedJobIds.update((s) => {
      const next = new Set(s);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }
  export function selectAll(ids: number[]) {
    selectedJobIds.set(new Set(ids));
  }
  export function clearSelection() {
    selectedJobIds.set(new Set());
  }

  // --- View / sort / category / search prefs ---
  export const viewMode = persisted<'list' | 'grid'>('sh-renames-view', 'list');
  export const renameSort = persisted<
    'detected_desc' | 'detected_asc' | 'confidence_desc' | 'title_asc'
  >('sh-renames-sort', 'detected_desc');
  export const renameCategory = persisted<RenameCategory>('sh-renames-category', 'all');
  export const renameQuery = writable<string>('');

  // --- Bulk in-flight flag (disables BulkBar during a run) ---
  export const bulkBusy = writable<boolean>(false);
  ```
- [ ] In `renames.ts`, export a public refresh wrapper so the orchestrator and bulk actions share one path. Add directly below the private `refresh()` definition:
  ```ts
  export async function refreshRenames() {
    await refresh();
  }
  ```
- [ ] In `renames.ts`, after `rematchJob` (line 67), add the bulk actions. Each reads the current selection, calls the client, toasts a summary, then refreshes + clears selection inside `finally`:
  ```ts
  import { get } from 'svelte/store';
  import { api } from '$lib/api/client'; // ensure 'api' is imported (it already is at top)

  async function runBulk(label: string, fn: (ids: number[]) => Promise<void>) {
    const ids = [...get(selectedJobIds)];
    if (ids.length === 0) return;
    bulkBusy.set(true);
    try {
      await fn(ids);
    } catch (e) {
      addToast(`${label} failed: ${e instanceof Error ? e.message : String(e)}`, 'error');
    } finally {
      await refresh();
      clearSelection();
      bulkBusy.set(false);
    }
  }

  export function bulkApply() {
    return runBulk('Apply', async (ids) => {
      const r = await api.bulkApply(ids);
      addToast(`Applied ${r.applied}, ${r.failed} failed`, r.failed ? 'warning' : 'success');
    });
  }

  export function bulkReidentify() {
    return runBulk('Re-identify', async (ids) => {
      const r = await api.bulkReidentify(ids);
      addToast(`Queued ${r.queued} for re-identify`, 'info');
    });
  }

  export function bulkDelete() {
    return runBulk('Delete', async (ids) => {
      const r = await api.bulkDelete(ids);
      addToast(`Deleted ${r.deleted}`, 'success');
    });
  }

  export function bulkSetDestination(root: string) {
    return runBulk('Set destination', async (ids) => {
      const r = await api.bulkSetDestination(ids, root);
      addToast(`Updated destination for ${r.updated}`, 'success');
    });
  }

  /** Apply-confident. ids omitted = all matched jobs on the page (Matched-card shortcut). */
  export async function applyConfident(ids?: number[]) {
    bulkBusy.set(true);
    try {
      const r = await api.applyConfident(ids);
      addToast(
        `Applied ${r.applied} confident (${r.skipped} skipped, ${r.failed} failed)`,
        r.failed ? 'warning' : 'success'
      );
    } catch (e) {
      addToast(`Apply confident failed: ${e instanceof Error ? e.message : String(e)}`, 'error');
    } finally {
      await refresh();
      clearSelection();
      bulkBusy.set(false);
    }
  }
  ```
  Note: if `get` and `api` are already imported at the top of `renames.ts`, do NOT re-import them — drop the inline `import` lines and rely on the existing top-level imports. Confirm during the edit.
- [ ] Read the top of `renames.ts` again to verify there is exactly one `import { get }` and one `import { api }`. If the inline imports duplicate them, delete the inline ones; if `get` was not imported, add `import { get } from 'svelte/store';` to the top import block.
- [ ] Run `cd frontend && npm run check`. Expected: `0 errors`. Resolve any "duplicate import" or "addToast signature" mismatch (check `notifications.ts` for the real `addToast(message, level)` signature and adapt the level strings to its actual union).
- [ ] Read `frontend/src/lib/stores/notifications.ts` to confirm `addToast`'s exact signature/levels; adjust the `'error'|'warning'|'success'|'info'` arguments above to match the real union if different.
- [ ] Commit: `git add frontend/src/lib/stores/renames.ts && git commit -m "renames store: multi-select, persisted view/sort/category prefs, bulk actions + applyConfident"`.

---

### Task 12: `RenamePoster.svelte` + `BadgeCluster.svelte`

**Files**
- Create `frontend/src/lib/components/renames/RenamePoster.svelte` — poster `<img>` + "No poster" placeholder, mirroring `ResultTile.svelte:135-158`.
- Create `frontend/src/lib/components/renames/BadgeCluster.svelte` — status/confidence/media·resolution/DV/keep·dup badges.

**Interfaces**
- Produces: `RenamePoster` props `{ posterUrl?: string | null; alt?: string; class?: string }`; `BadgeCluster` props `{ job: RenameJob; compact?: boolean }`.
- Consumes: `posterAspect`, `POSTER_ASPECT_CLASS` (`$lib/stores/results`); `Badge` (`$lib/components/Badge.svelte`); `formatStatus` (`$lib/constants`), `renameStatusVariant`, `confidenceVariant`, `dvLayerVariant` (Task 10); `RenameJob` (`$lib/api/types`).

**Steps**
- [ ] Create `frontend/src/lib/components/renames/RenamePoster.svelte`:
  ```svelte
  <script lang="ts">
    import { posterAspect, POSTER_ASPECT_CLASS } from '$lib/stores/results';

    let {
      posterUrl = null,
      alt = '',
      class: klass = ''
    }: { posterUrl?: string | null; alt?: string; class?: string } = $props();
  </script>

  <div
    class="{POSTER_ASPECT_CLASS[$posterAspect]} bg-[var(--bg-tertiary)] relative overflow-hidden {klass}"
  >
    {#if posterUrl}
      <img
        src={posterUrl}
        {alt}
        class="w-full h-full object-cover"
        loading="lazy"
      />
    {:else}
      <div class="flex items-center justify-center h-full text-[var(--text-secondary)] text-xs">
        No poster
      </div>
    {/if}
  </div>
  ```
- [ ] Create `frontend/src/lib/components/renames/BadgeCluster.svelte`:
  ```svelte
  <script lang="ts">
    import Badge from '$lib/components/Badge.svelte';
    import { formatStatus, renameStatusVariant, confidenceVariant, dvLayerVariant } from '$lib/constants';
    import type { RenameJob } from '$lib/api/types';

    let { job, compact = false }: { job: RenameJob; compact?: boolean } = $props();

    let confidence = $derived(
      job.match_confidence == null ? null : Math.round(job.match_confidence)
    );
    let mediaRes = $derived(
      [job.media_type ? job.media_type.toUpperCase() : null, job.resolution]
        .filter(Boolean)
        .join(' · ')
    );
  </script>

  <div class="flex flex-wrap items-center gap-1">
    <Badge variant={renameStatusVariant(job.status)}>{formatStatus(job.status)}</Badge>

    {#if confidence != null}
      <Badge variant={confidenceVariant(confidence)}>{confidence}%</Badge>
    {/if}

    {#if mediaRes && !compact}
      <Badge variant="info">{mediaRes}</Badge>
    {/if}

    {#if job.dv_layer}
      <Badge variant={dvLayerVariant(job.dv_layer)}>{job.dv_layer.toUpperCase()}</Badge>
    {/if}

    {#if job.keep_recommended}
      <Badge variant="success">★ Keep</Badge>
    {/if}

    {#if job.destination_conflict}
      <Badge variant="orange">⚠ Duplicate</Badge>
    {/if}
  </div>
  ```
- [ ] Read `frontend/src/lib/components/Badge.svelte` to confirm it accepts a default slot for label text and a `variant` prop (the module block exports `BadgeVariant`; confirm the prop name is `variant`). If the label is passed as a prop instead of a slot, adapt both components accordingly.
- [ ] Run `cd frontend && npm run check`. Expected: `0 errors`. Resolve any Badge prop/slot mismatch found in the previous step.
- [ ] Commit: `git add frontend/src/lib/components/renames/RenamePoster.svelte frontend/src/lib/components/renames/BadgeCluster.svelte && git commit -m "renames: RenamePoster + BadgeCluster shared primitives"`.

---

### Task 13: `RenameRow.svelte` (dense list row)

**Files**
- Create `frontend/src/lib/components/renames/RenameRow.svelte` — checkbox, poster thumb, title+diff, `BadgeCluster`, per-row Apply/Rematch.

**Interfaces**
- Produces: `RenameRow` props `{ job: RenameJob; onRematch: (job: RenameJob) => void }`.
- Consumes: `selectedJobIds`, `toggleSelect`, `applyJob` (existing single-job action in `renames.ts`); `RenamePoster`, `BadgeCluster` (Task 12); `RenameJob` (`$lib/api/types`).

**Steps**
- [ ] Read `frontend/src/lib/stores/renames.ts` to confirm the exported single-job action name for apply (`applyJob`) used by the old page; if it is `applyJob(id)` use that, otherwise note the exact name.
- [ ] Create `frontend/src/lib/components/renames/RenameRow.svelte`:
  ```svelte
  <script lang="ts">
    import RenamePoster from './RenamePoster.svelte';
    import BadgeCluster from './BadgeCluster.svelte';
    import { selectedJobIds, toggleSelect, applyJob } from '$lib/stores/renames';
    import type { RenameJob } from '$lib/api/types';

    let { job, onRematch }: { job: RenameJob; onRematch: (job: RenameJob) => void } = $props();

    let selected = $derived($selectedJobIds.has(job.id));
    let busy = $state(false);
    let titleLine = $derived(
      [job.title ?? job.package_name ?? job.original_filename ?? `Job ${job.id}`, job.year ? `(${job.year})` : null]
        .filter(Boolean)
        .join(' ')
    );
    let canApply = $derived(job.status === 'matched' || job.status === 'needs_review');

    async function apply() {
      busy = true;
      try {
        await applyJob(job.id);
      } finally {
        busy = false;
      }
    }
  </script>

  <li class="flex items-center gap-3 px-3 py-2 hover:bg-[var(--bg-tertiary)]/40 transition-colors min-w-0">
    <input
      type="checkbox"
      class="shrink-0 accent-[var(--accent)]"
      checked={selected}
      onchange={(e) => { e.stopPropagation(); toggleSelect(job.id); }}
      aria-label="Select {titleLine}"
    />

    <div class="w-10 shrink-0">
      <RenamePoster posterUrl={job.poster_url} alt={job.title ?? ''} />
    </div>

    <div class="flex-1 min-w-0">
      <div class="font-medium text-sm truncate">{titleLine}</div>
      <div class="text-xs text-[var(--text-secondary)] truncate" title={job.original_filename ?? ''}>
        {job.original_filename ?? '—'}
      </div>
      {#if job.new_filename}
        <div class="text-xs text-[var(--accent)] truncate" title={job.new_filename}>
          → {job.new_filename}
        </div>
      {/if}
      {#if job.error_message}
        <div class="text-xs text-[var(--error)] truncate" title={job.error_message}>
          {job.error_message}
        </div>
      {:else if job.warning_message}
        <div class="text-xs text-[var(--warning)] truncate" title={job.warning_message}>
          {job.warning_message}
        </div>
      {/if}
    </div>

    <div class="shrink-0 hidden sm:block">
      <BadgeCluster {job} />
    </div>

    <div class="shrink-0 flex items-center gap-1">
      {#if canApply}
        <button
          class="px-2 py-1 rounded text-[11px] font-medium bg-[var(--accent)] text-white disabled:opacity-50"
          disabled={busy}
          onclick={apply}
        >
          Apply
        </button>
      {/if}
      <button
        class="px-2 py-1 rounded text-[11px] font-medium text-[var(--text-secondary)] hover:bg-[var(--bg-tertiary)]"
        onclick={() => onRematch(job)}
      >
        Rematch
      </button>
    </div>
  </li>
  ```
  Note: if the store's single-apply action is not `applyJob`, replace the import and call with the verified name from the prior step.
- [ ] Run `cd frontend && npm run check`. Expected: `0 errors`.
- [ ] Commit: `git add frontend/src/lib/components/renames/RenameRow.svelte && git commit -m "renames: RenameRow dense list row with checkbox, poster, diff, per-row actions"`.

---

### Task 14: `RenameCard.svelte` (poster-grid tile)

**Files**
- Create `frontend/src/lib/components/renames/RenameCard.svelte` — poster tile reusing grid prefs, checkbox top-left, compact `BadgeCluster`.

**Interfaces**
- Produces: `RenameCard` props `{ job: RenameJob; onRematch: (job: RenameJob) => void }`.
- Consumes: `tileShowMeta` (`$lib/stores/results`); `selectedJobIds`, `toggleSelect` (`$lib/stores/renames`); `RenamePoster`, `BadgeCluster` (Task 12); `RenameJob`.

**Steps**
- [ ] Create `frontend/src/lib/components/renames/RenameCard.svelte`:
  ```svelte
  <script lang="ts">
    import RenamePoster from './RenamePoster.svelte';
    import BadgeCluster from './BadgeCluster.svelte';
    import { tileShowMeta } from '$lib/stores/results';
    import { selectedJobIds, toggleSelect } from '$lib/stores/renames';
    import type { RenameJob } from '$lib/api/types';

    let { job, onRematch }: { job: RenameJob; onRematch: (job: RenameJob) => void } = $props();

    let selected = $derived($selectedJobIds.has(job.id));
    let titleLine = $derived(
      [job.title ?? job.package_name ?? job.original_filename ?? `Job ${job.id}`, job.year ? `(${job.year})` : null]
        .filter(Boolean)
        .join(' ')
    );
  </script>

  <div
    class="group min-w-0 rounded-lg overflow-hidden border transition-colors cursor-pointer
      {selected ? 'border-[var(--accent)]' : 'border-[var(--border)] hover:border-[var(--accent)]/60'}"
    onclick={() => onRematch(job)}
    role="button"
    tabindex="0"
    onkeydown={(e) => { if (e.key === 'Enter') onRematch(job); }}
  >
    <div class="relative">
      <RenamePoster posterUrl={job.poster_url} alt={job.title ?? ''} />
      <input
        type="checkbox"
        class="absolute top-1.5 left-1.5 accent-[var(--accent)] z-10"
        checked={selected}
        onclick={(e) => e.stopPropagation()}
        onchange={(e) => { e.stopPropagation(); toggleSelect(job.id); }}
        aria-label="Select {titleLine}"
      />
      <div class="absolute bottom-1.5 left-1.5 right-1.5">
        <BadgeCluster {job} compact />
      </div>
    </div>

    {#if $tileShowMeta}
      <div class="p-2 min-w-0">
        <div class="text-xs font-medium truncate" title={titleLine}>{titleLine}</div>
        {#if job.new_filename}
          <div class="text-[10px] text-[var(--text-secondary)] truncate" title={job.new_filename}>
            {job.new_filename}
          </div>
        {/if}
      </div>
    {/if}
  </div>
  ```
- [ ] Run `cd frontend && npm run check`. Expected: `0 errors`.
- [ ] Commit: `git add frontend/src/lib/components/renames/RenameCard.svelte && git commit -m "renames: RenameCard poster-grid tile with selection + compact badges"`.

---

### Task 15: `StatCard.svelte` + `StatusDashboard.svelte`

**Files**
- Create `frontend/src/lib/components/renames/StatCard.svelte` — one clickable colored count card.
- Create `frontend/src/lib/components/renames/StatusDashboard.svelte` — 4 status cards (click-to-filter) + DV inventory card; Matched card carries "Apply all confident".

**Interfaces**
- Produces: `StatCard` props `{ label: string; count: number; variant: BadgeVariant; active?: boolean; onclick: () => void }`; `StatusDashboard` props `{ statusFilter: string; onFilter: (status: string) => void }`.
- Consumes: `renameStatus`, `dvCounts`, `applyConfident` (`$lib/stores/renames`); `renameStatusBorderColor` (`$lib/constants`); `BadgeVariant` (`$lib/components/Badge.svelte`).

**Steps**
- [ ] Read `frontend/src/lib/stores/renames.ts` to confirm `dvCounts` exists and its shape (the old page imported `dvCounts`, `loadDvScans`). Confirm whether it is `{ fel: number; mel: number; ... }` or a `Record<string,number>`; adapt the DV card accordingly.
- [ ] Create `frontend/src/lib/components/renames/StatCard.svelte`:
  ```svelte
  <script lang="ts">
    import { renameStatusBorderColor } from '$lib/constants';
    import type { BadgeVariant } from '$lib/components/Badge.svelte';

    let {
      label,
      count,
      variant,
      active = false,
      borderStatus = null,
      onclick
    }: {
      label: string;
      count: number;
      variant: BadgeVariant;
      active?: boolean;
      borderStatus?: string | null;
      onclick: () => void;
    } = $props();

    const tints: Record<BadgeVariant, string> = {
      default: 'var(--border)',
      success: 'var(--success)',
      warning: 'var(--warning)',
      error: 'var(--error)',
      accent: 'var(--accent)',
      info: '#3b82f6',
      orange: '#f97316'
    };
    let color = $derived(borderStatus ? renameStatusBorderColor(borderStatus) : tints[variant]);
  </script>

  <button
    {onclick}
    class="flex-1 min-w-0 text-left rounded-lg border-2 px-3 py-2 transition-colors hover:bg-[var(--bg-tertiary)]/40"
    style="border-color: {active ? color : 'var(--border)'}"
  >
    <div class="text-2xl font-bold" style="color: {color}">{count}</div>
    <div class="text-xs text-[var(--text-secondary)] truncate">{label}</div>
  </button>
  ```
- [ ] Create `frontend/src/lib/components/renames/StatusDashboard.svelte`:
  ```svelte
  <script lang="ts">
    import StatCard from './StatCard.svelte';
    import { renameStatus, dvCounts, applyConfident } from '$lib/stores/renames';

    let { statusFilter, onFilter }: { statusFilter: string; onFilter: (status: string) => void } =
      $props();

    let counts = $derived($renameStatus?.counts ?? {});
    function n(key: string): number {
      const v = (counts as Record<string, number>)[key];
      return typeof v === 'number' ? v : 0;
    }

    // dvCounts is keyed by layer (e.g. { fel, mel, ... }); fall back to 0.
    let fel = $derived(($dvCounts as Record<string, number>)?.fel ?? 0);
    let mel = $derived(($dvCounts as Record<string, number>)?.mel ?? 0);

    function toggle(status: string) {
      onFilter(statusFilter === status ? 'all' : status);
    }
  </script>

  <div class="flex flex-wrap gap-3">
    <StatCard
      label="Needs review"
      count={n('needs_review')}
      variant="warning"
      borderStatus="needs_review"
      active={statusFilter === 'needs_review'}
      onclick={() => toggle('needs_review')}
    />

    <div class="flex-1 min-w-0 flex flex-col gap-1">
      <StatCard
        label="Matched"
        count={n('matched')}
        variant="accent"
        borderStatus="matched"
        active={statusFilter === 'matched'}
        onclick={() => toggle('matched')}
      />
      <button
        class="text-[11px] font-medium text-[var(--accent)] hover:underline px-1 text-left"
        onclick={() => applyConfident()}
        title="Apply every matched job with confidence ≥ 95% across the page"
      >
        Apply all confident
      </button>
    </div>

    <StatCard
      label="Applied"
      count={n('applied')}
      variant="success"
      borderStatus="applied"
      active={statusFilter === 'applied'}
      onclick={() => toggle('applied')}
    />
    <StatCard
      label="Failed"
      count={n('failed')}
      variant="error"
      borderStatus="failed"
      active={statusFilter === 'failed'}
      onclick={() => toggle('failed')}
    />

    <button
      class="flex-1 min-w-0 text-left rounded-lg border-2 border-[var(--border)] px-3 py-2 hover:bg-[var(--bg-tertiary)]/40"
      onclick={() => document.getElementById('dv-scan-surface')?.scrollIntoView({ behavior: 'smooth' })}
      title="Dolby Vision inventory (read-only)"
    >
      <div class="text-sm font-bold flex gap-2">
        <span style="color: var(--error)">FEL {fel}</span>
        <span style="color: #f97316">MEL {mel}</span>
      </div>
      <div class="text-xs text-[var(--text-secondary)]">Dolby Vision</div>
    </button>
  </div>
  ```
  Note: adjust the `$dvCounts` access (`.fel`/`.mel`) to the verified shape from the first step. If `dvCounts` is not a store of layer counts, derive FEL/MEL from `$dvScans` by layer instead.
- [ ] Run `cd frontend && npm run check`. Expected: `0 errors`. Fix any `dvCounts` shape mismatch surfaced here.
- [ ] Commit: `git add frontend/src/lib/components/renames/StatCard.svelte frontend/src/lib/components/renames/StatusDashboard.svelte && git commit -m "renames: StatusDashboard stat cards (click-to-filter) + DV inventory + apply-all-confident shortcut"`.

---

### Task 16: `RenameFilterBar.svelte`

**Files**
- Create `frontend/src/lib/components/renames/RenameFilterBar.svelte` — category chips with live counts, title search bound to `renameQuery`, sort `<select>` bound to `renameSort`.

**Interfaces**
- Produces: `RenameFilterBar` props `{}` (drives shared stores directly).
- Consumes: `renameJobs`, `renameCategory`, `renameQuery`, `renameSort` (`$lib/stores/renames`); `categoryOf`, `RENAME_CATEGORIES`, `RenameCategory` (`$lib/renames/category`).

**Steps**
- [ ] Create `frontend/src/lib/components/renames/RenameFilterBar.svelte`:
  ```svelte
  <script lang="ts">
    import { renameJobs, renameCategory, renameQuery, renameSort } from '$lib/stores/renames';
    import { categoryOf, RENAME_CATEGORIES, type RenameCategory } from '$lib/renames/category';

    const LABELS: Record<RenameCategory, string> = {
      all: 'All',
      movies: 'Movies',
      tv: 'TV',
      '4k': '4K',
      '1080p': '1080p',
      remux: 'Remux'
    };

    // Per-category live counts derived from the full job list.
    let counts = $derived.by(() => {
      const c: Record<RenameCategory, number> = {
        all: $renameJobs.length, movies: 0, tv: 0, '4k': 0, '1080p': 0, remux: 0
      };
      for (const job of $renameJobs) {
        for (const cat of categoryOf(job)) c[cat] += 1;
      }
      return c;
    });

    const SORTS: { value: typeof $renameSort; label: string }[] = [
      { value: 'detected_desc', label: 'Newest' },
      { value: 'detected_asc', label: 'Oldest' },
      { value: 'confidence_desc', label: 'Confidence' },
      { value: 'title_asc', label: 'Title A–Z' }
    ];
  </script>

  <div class="flex flex-wrap items-center gap-2">
    <div class="flex flex-wrap items-center gap-1">
      {#each RENAME_CATEGORIES as cat (cat)}
        <button
          onclick={() => renameCategory.set(cat)}
          class="px-2 py-1 rounded text-[11px] font-medium transition-colors border
            {$renameCategory === cat
              ? 'bg-[var(--accent)]/15 border-[var(--accent)] text-[var(--accent)]'
              : 'border-transparent text-[var(--text-secondary)] hover:bg-[var(--bg-tertiary)]'}"
        >
          {LABELS[cat]} <span class="opacity-70">({counts[cat]})</span>
        </button>
      {/each}
    </div>

    <input
      type="search"
      placeholder="Search title / filename…"
      value={$renameQuery}
      oninput={(e) => renameQuery.set((e.target as HTMLInputElement).value)}
      class="flex-1 min-w-[160px] px-2 py-1 rounded text-xs bg-[var(--bg-tertiary)] border border-[var(--border)] focus:border-[var(--accent)] outline-none"
    />

    <select
      value={$renameSort}
      onchange={(e) => renameSort.set((e.target as HTMLSelectElement).value as typeof $renameSort)}
      class="px-2 py-1 rounded text-xs bg-[var(--bg-tertiary)] border border-[var(--border)]"
    >
      {#each SORTS as s (s.value)}
        <option value={s.value}>{s.label}</option>
      {/each}
    </select>
  </div>
  ```
- [ ] Run `cd frontend && npm run check`. Expected: `0 errors`.
- [ ] Manual check: `cd frontend && npm run dev`, open `/renames`, confirm category chips show live counts, typing in search updates `renameQuery` (rows filter live in Task 19 once wired), and the sort select persists across reload (localStorage `sh-renames-sort`). Expected: chip counts equal `$renameJobs` membership; active chip uses accent styling. Stop dev server.
- [ ] Commit: `git add frontend/src/lib/components/renames/RenameFilterBar.svelte && git commit -m "renames: RenameFilterBar category chips + live counts + search + sort"`.

---

### Task 17: `BulkBar.svelte`

**Files**
- Create `frontend/src/lib/components/renames/BulkBar.svelte` — sticky bar with Apply / Re-identify / Set destination (friendly-label root picker) / Apply confident / Delete, plus a select-all checkbox over the shown set.

**Interfaces**
- Produces: `BulkBar` props `{ shownIds: number[] }`.
- Consumes: `selectedJobIds`, `selectAll`, `clearSelection`, `bulkBusy`, `bulkApply`, `bulkReidentify`, `bulkDelete`, `bulkSetDestination`, `applyConfident` (`$lib/stores/renames`).

**Steps**
- [ ] Create `frontend/src/lib/components/renames/BulkBar.svelte`:
  ```svelte
  <script lang="ts">
    import {
      selectedJobIds, selectAll, clearSelection, bulkBusy,
      bulkApply, bulkReidentify, bulkDelete, bulkSetDestination, applyConfident
    } from '$lib/stores/renames';

    let { shownIds }: { shownIds: number[] } = $props();

    // Friendly labels → backend root keys; backend rebuilds the real path + re-runs the guard.
    const ROOTS: { label: string; value: string }[] = [
      { label: 'TV', value: 'tv' },
      { label: 'Movies 4K', value: 'movies_4k' },
      { label: 'Movies 1080p', value: 'movies_1080p' }
    ];

    let selectedCount = $derived($selectedJobIds.size);
    let allShownSelected = $derived(
      shownIds.length > 0 && shownIds.every((id) => $selectedJobIds.has(id))
    );
    let destOpen = $state(false);
    let destRoot = $state(ROOTS[0].value);

    function toggleAll() {
      if (allShownSelected) clearSelection();
      else selectAll(shownIds);
    }

    function confirmDelete() {
      if (confirm(`Delete ${selectedCount} job(s)? This cannot be undone.`)) bulkDelete();
    }

    function applyDest() {
      bulkSetDestination(destRoot);
      destOpen = false;
    }
  </script>

  {#if selectedCount > 0}
    <div
      class="sticky top-0 z-20 flex flex-wrap items-center gap-2 px-3 py-2 rounded-lg
        bg-[var(--bg-secondary)] border border-[var(--accent)] shadow"
    >
      <label class="flex items-center gap-1 text-xs">
        <input type="checkbox" class="accent-[var(--accent)]" checked={allShownSelected} onchange={toggleAll} />
        Select all
      </label>
      <span class="text-xs text-[var(--text-secondary)]">{selectedCount} selected</span>

      <div class="flex flex-wrap items-center gap-1 ml-auto">
        <button class="px-2 py-1 rounded text-[11px] font-medium bg-[var(--accent)] text-white disabled:opacity-50"
          disabled={$bulkBusy} onclick={bulkApply}>Apply</button>
        <button class="px-2 py-1 rounded text-[11px] font-medium bg-[var(--bg-tertiary)] disabled:opacity-50"
          disabled={$bulkBusy} onclick={bulkReidentify}>Re-identify</button>

        <div class="relative">
          <button class="px-2 py-1 rounded text-[11px] font-medium bg-[var(--bg-tertiary)] disabled:opacity-50"
            disabled={$bulkBusy} onclick={() => (destOpen = !destOpen)}>Set destination ▾</button>
          {#if destOpen}
            <div class="absolute right-0 mt-1 z-30 flex flex-col gap-1 p-2 rounded-lg bg-[var(--bg-secondary)] border border-[var(--border)] shadow">
              <select bind:value={destRoot} class="px-2 py-1 rounded text-xs bg-[var(--bg-tertiary)] border border-[var(--border)]">
                {#each ROOTS as r (r.value)}
                  <option value={r.value}>{r.label}</option>
                {/each}
              </select>
              <button class="px-2 py-1 rounded text-[11px] font-medium bg-[var(--accent)] text-white" onclick={applyDest}>Apply destination</button>
            </div>
          {/if}
        </div>

        <button class="px-2 py-1 rounded text-[11px] font-medium bg-[var(--success)]/15 text-[var(--success)] disabled:opacity-50"
          disabled={$bulkBusy}
          title="Server applies only matched jobs with confidence ≥ 95%; needs_review / low-confidence are skipped"
          onclick={() => applyConfident([...$selectedJobIds])}>Apply confident</button>

        <button class="px-2 py-1 rounded text-[11px] font-medium bg-[var(--error)]/15 text-[var(--error)] disabled:opacity-50"
          disabled={$bulkBusy} onclick={confirmDelete}>Delete</button>
      </div>
    </div>
  {/if}
  ```
- [ ] Run `cd frontend && npm run check`. Expected: `0 errors`.
- [ ] Manual check: in `npm run dev` on `/renames` (after Task 19 wiring), select rows → BulkBar appears; "Select all" toggles the shown set; "Set destination ▾" opens the friendly-label picker; buttons disable while `$bulkBusy`. (Pre-Task-19 this component is not yet mounted; defer the live check to Task 19's manual step.)
- [ ] Commit: `git add frontend/src/lib/components/renames/BulkBar.svelte && git commit -m "renames: BulkBar with Apply/Re-identify/Set-destination/Apply-confident/Delete + select-all"`.

---

### Task 18: `RematchModal.svelte`

**Files**
- Create `frontend/src/lib/components/renames/RematchModal.svelte` — debounced TMDB search, Movie/TV toggle, pasted id direct pick, TV season/episode override, live preview, Confirm.

**Interfaces**
- Produces: `RematchModal` props `{ job: RenameJob; onClose: () => void }`.
- Consumes: `api.searchTmdb`, `api.rematchPreview`, `api.rematchRename` (Task 9); `refreshRenames` (Task 11), `addToast` (`$lib/stores/notifications`); `TmdbSearchResult`, `RematchPreviewResponse`, `RenameJob` (`$lib/api/types`).

**Steps**
- [ ] Create `frontend/src/lib/components/renames/RematchModal.svelte`:
  ```svelte
  <script lang="ts">
    import { api } from '$lib/api/client';
    import { refreshRenames } from '$lib/stores/renames';
    import { addToast } from '$lib/stores/notifications';
    import type { RenameJob, TmdbSearchResult, RematchPreviewResponse } from '$lib/api/types';

    let { job, onClose }: { job: RenameJob; onClose: () => void } = $props();

    let mediaType = $state<'movie' | 'tv'>(job.media_type === 'tv' || job.media_type === 'show' ? 'tv' : 'movie');
    let query = $state(job.title ?? '');
    let results = $state<TmdbSearchResult[]>([]);
    let searchBusy = $state(false);
    let selected = $state<TmdbSearchResult | null>(null);
    let season = $state<number | null>(job.season);
    let episode = $state<number | null>(job.episode);
    let preview = $state<RematchPreviewResponse | null>(null);
    let confirmBusy = $state(false);

    let debounceTimer: ReturnType<typeof setTimeout> | null = null;

    function parsePastedId(q: string): { tmdb_id?: number; imdb?: string } {
      const t = q.trim();
      if (/^\d+$/.test(t)) return { tmdb_id: parseInt(t, 10) };
      if (/^tt\d+$/i.test(t)) return { imdb: t.toLowerCase() };
      return {};
    }

    async function runSearch() {
      const q = query.trim();
      if (!q) { results = []; return; }
      // Pasted numeric TMDB id → direct pick, skip search.
      const pasted = parsePastedId(q);
      if (pasted.tmdb_id != null) {
        selected = { tmdb_id: pasted.tmdb_id, title: `TMDB ${pasted.tmdb_id}`, year: null, media_type: mediaType, poster_url: null };
        results = [];
        await loadPreview();
        return;
      }
      searchBusy = true;
      try {
        const r = await api.searchTmdb(q, mediaType);
        results = r.results;
      } catch (e) {
        addToast(`Search failed: ${e instanceof Error ? e.message : String(e)}`, 'error');
        results = [];
      } finally {
        searchBusy = false;
      }
    }

    function onQueryInput(v: string) {
      query = v;
      if (debounceTimer) clearTimeout(debounceTimer);
      debounceTimer = setTimeout(runSearch, 350);
    }

    function pick(r: TmdbSearchResult) {
      selected = r;
      loadPreview();
    }

    async function loadPreview() {
      if (!selected) return;
      try {
        preview = await api.rematchPreview(job.id, {
          tmdb_id: selected.tmdb_id,
          media_type: mediaType,
          season: mediaType === 'tv' ? (season ?? undefined) : undefined,
          episode: mediaType === 'tv' ? (episode ?? undefined) : undefined
        });
      } catch (e) {
        addToast(`Preview failed: ${e instanceof Error ? e.message : String(e)}`, 'error');
        preview = null;
      }
    }

    function setMediaType(mt: 'movie' | 'tv') {
      if (mt === mediaType) return;
      mediaType = mt;
      selected = null;
      preview = null;
      runSearch();
    }

    async function confirm() {
      if (!selected) return;
      confirmBusy = true;
      try {
        const r = await api.rematchRename(
          job.id, selected.tmdb_id, mediaType,
          mediaType === 'tv' ? (season ?? undefined) : undefined,
          mediaType === 'tv' ? (episode ?? undefined) : undefined
        );
        addToast(
          r.status === 'matched' ? 'Rematched and ready to apply' : `Rematched → ${r.status}${r.warning ? ': ' + r.warning : ''}`,
          r.status === 'matched' ? 'success' : 'warning'
        );
        await refreshRenames();
        onClose();
      } catch (e) {
        addToast(`Rematch failed: ${e instanceof Error ? e.message : String(e)}`, 'error');
      } finally {
        confirmBusy = false;
      }
    }
  </script>

  <div class="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-4" onclick={onClose} role="presentation">
    <div class="w-full max-w-lg max-h-[85vh] overflow-auto rounded-xl bg-[var(--bg-secondary)] border border-[var(--border)] p-4 flex flex-col gap-3"
      onclick={(e) => e.stopPropagation()} role="dialog" aria-modal="true">
      <div class="flex items-center justify-between">
        <h3 class="font-semibold text-sm">Rematch: {job.title ?? job.original_filename ?? `Job ${job.id}`}</h3>
        <button class="text-[var(--text-secondary)] hover:text-[var(--text)]" onclick={onClose} aria-label="Close">✕</button>
      </div>

      <div class="flex gap-1">
        {#each (['movie', 'tv'] as const) as mt (mt)}
          <button onclick={() => setMediaType(mt)}
            class="px-2 py-1 rounded text-[11px] font-medium transition-colors
              {mediaType === mt ? 'bg-[var(--accent)] text-white' : 'text-[var(--text-secondary)] hover:bg-[var(--bg-tertiary)]'}">
            {mt === 'movie' ? 'Movie' : 'TV'}
          </button>
        {/each}
      </div>

      <input type="search" placeholder="Search TMDB, or paste a TMDB id / tt-IMDB id…"
        value={query} oninput={(e) => onQueryInput((e.target as HTMLInputElement).value)}
        class="w-full px-2 py-1.5 rounded text-sm bg-[var(--bg-tertiary)] border border-[var(--border)] focus:border-[var(--accent)] outline-none" />

      {#if mediaType === 'tv'}
        <div class="flex gap-2">
          <label class="flex items-center gap-1 text-xs">S
            <input type="number" min="0" class="w-16 px-1 py-0.5 rounded bg-[var(--bg-tertiary)] border border-[var(--border)]"
              value={season ?? ''} oninput={(e) => { season = (e.target as HTMLInputElement).value === '' ? null : +(e.target as HTMLInputElement).value; loadPreview(); }} />
          </label>
          <label class="flex items-center gap-1 text-xs">E
            <input type="number" min="0" class="w-16 px-1 py-0.5 rounded bg-[var(--bg-tertiary)] border border-[var(--border)]"
              value={episode ?? ''} oninput={(e) => { episode = (e.target as HTMLInputElement).value === '' ? null : +(e.target as HTMLInputElement).value; loadPreview(); }} />
          </label>
        </div>
      {/if}

      {#if searchBusy}
        <div class="text-xs text-[var(--text-secondary)]">Searching…</div>
      {:else if results.length > 0}
        <ul class="flex flex-col gap-1 max-h-52 overflow-auto">
          {#each results as r (r.tmdb_id)}
            <li>
              <button onclick={() => pick(r)}
                class="w-full flex items-center gap-2 p-1.5 rounded text-left transition-colors
                  {selected?.tmdb_id === r.tmdb_id ? 'bg-[var(--accent)]/15' : 'hover:bg-[var(--bg-tertiary)]'}">
                <div class="w-8 shrink-0 aspect-[2/3] bg-[var(--bg-tertiary)] rounded overflow-hidden">
                  {#if r.poster_url}<img src={r.poster_url} alt="" class="w-full h-full object-cover" loading="lazy" />{/if}
                </div>
                <span class="text-xs truncate">{r.title}{r.year ? ` (${r.year})` : ''}</span>
                <span class="ml-auto text-[10px] uppercase text-[var(--text-secondary)]">{r.media_type}</span>
              </button>
            </li>
          {/each}
        </ul>
      {/if}

      {#if preview}
        {#if preview.library_configured === false}
          <div class="text-xs p-2 rounded bg-[var(--warning)]/15 text-[var(--warning)]">
            ⚠ Library not configured — {preview.warning ?? 'this will be queued as needs_review, not placed.'}
          </div>
        {/if}
        <div class="text-xs p-2 rounded bg-[var(--bg-tertiary)] flex flex-col gap-1">
          <div><span class="text-[var(--text-secondary)]">New name:</span> {preview.new_filename}</div>
          <div><span class="text-[var(--text-secondary)]">Destination:</span> {preview.destination_path ?? '—'}</div>
        </div>
      {/if}

      <div class="flex justify-end gap-2 pt-1">
        <button class="px-3 py-1.5 rounded text-xs font-medium text-[var(--text-secondary)] hover:bg-[var(--bg-tertiary)]" onclick={onClose}>Cancel</button>
        <button class="px-3 py-1.5 rounded text-xs font-medium bg-[var(--accent)] text-white disabled:opacity-50"
          disabled={!selected || confirmBusy} onclick={confirm}>Confirm</button>
      </div>
    </div>
  </div>
  ```
- [ ] Run `cd frontend && npm run check`. Expected: `0 errors`. Verify `addToast` level strings match `notifications.ts`'s union (adjust `'warning'` if its union differs).
- [ ] Manual check (after Task 19 wiring): open Rematch on a job → typing debounces a search; Movie/TV toggle re-queries; pasting `tt1234567` or a numeric id short-circuits to a direct pick + preview; for TV, editing season/episode re-runs preview; an unconfigured library shows the warning banner and Confirm still routes to `needs_review`. Defer live run to Task 19.
- [ ] Commit: `git add frontend/src/lib/components/renames/RematchModal.svelte && git commit -m "renames: RematchModal search picker with debounce, id-paste, TV S/E override, live preview"`.

---

### Task 19: `RenamesHeader.svelte` + `ProcessMenu.svelte` + orchestrator rewire (final verify)

**Files**
- Create `frontend/src/lib/components/renames/ProcessMenu.svelte` — `Process ▾` split-button (folder / files / paste-path) wrapping the existing `process-folder` call.
- Create `frontend/src/lib/components/renames/RenamesHeader.svelte` — title + `ProcessMenu` + Dolby Vision + Re-identify all + list/grid view toggle + Refresh.
- Modify `frontend/src/routes/renames/+page.svelte` — replace the monolith body with a thin orchestrator wiring all components, the list⟷grid switch (reusing `gridStyle`/`min-w-0`), status-card filtering, and the derived `shown` set.

**Interfaces**
- Produces: `ProcessMenu` props `{}`; `RenamesHeader` props `{ statusFilter: string }` (or none; emits via stores); orchestrator (route, no exports).
- Consumes: `viewMode`, `renameJobs`, `renameStatus`, `renameCategory`, `renameQuery`, `renameSort`, `selectedJobIds`, `loadRenameJobs`, `loadRenameStatus`, `loadDvScans`, `reidentifyAll` (or existing name), `refreshRenames` (`$lib/stores/renames`); grid prefs `tileSize`, `gridColumns`, `gridGap`, `TILE_MIN_PX`, `GRID_GAP_CLASS` (`$lib/stores/results`); `categoryOf` (`$lib/renames/category`); all Task 12–18 components; `api.processFolder`/existing folder call.

**Steps**
- [ ] Read `frontend/src/routes/renames/+page.svelte` in full (497 lines) to capture: the existing `process-folder` call site + `folderPreview` usage, the `reidentifyAll()` definition, the DV-scan trigger markup (to give the `id="dv-scan-surface"` anchor), and any imports to preserve.
- [ ] Create `frontend/src/lib/components/renames/ProcessMenu.svelte` (port the existing folder-path input + preview + `process-folder` call from the old page; modes = folder / files / paste-path):
  ```svelte
  <script lang="ts">
    import { api } from '$lib/api/client';
    import { folderPreview, refreshRenames } from '$lib/stores/renames';
    import { addToast } from '$lib/stores/notifications';

    let open = $state(false);
    let path = $state('');
    let busy = $state(false);

    async function process(recursive: boolean) {
      const p = path.trim();
      if (!p) return;
      busy = true;
      try {
        await api.processFolder(p, recursive); // existing client method; confirm exact signature
        addToast('Folder queued for processing', 'success');
        await refreshRenames();
        open = false;
      } catch (e) {
        addToast(`Process failed: ${e instanceof Error ? e.message : String(e)}`, 'error');
      } finally {
        busy = false;
      }
    }
  </script>

  <div class="relative">
    <button class="px-3 py-1.5 rounded text-xs font-medium bg-[var(--accent)] text-white" onclick={() => (open = !open)}>
      Process ▾
    </button>
    {#if open}
      <div class="absolute right-0 mt-1 z-30 w-72 p-3 rounded-lg bg-[var(--bg-secondary)] border border-[var(--border)] shadow flex flex-col gap-2">
        <input type="text" placeholder="Folder or file path…" bind:value={path}
          class="w-full px-2 py-1 rounded text-xs bg-[var(--bg-tertiary)] border border-[var(--border)]" />
        <div class="flex gap-1">
          <button class="flex-1 px-2 py-1 rounded text-[11px] bg-[var(--bg-tertiary)] disabled:opacity-50" disabled={busy} onclick={() => process(true)}>Folder (recursive)</button>
          <button class="flex-1 px-2 py-1 rounded text-[11px] bg-[var(--bg-tertiary)] disabled:opacity-50" disabled={busy} onclick={() => process(false)}>Files only</button>
        </div>
        {#if $folderPreview}
          <div class="text-[11px] text-[var(--text-secondary)]">{$folderPreview.count ?? 0} item(s) detected</div>
        {/if}
      </div>
    {/if}
  </div>
  ```
  Note: replace `api.processFolder(p, recursive)` with the verified existing client method/signature found in the page-read step (the old page already calls process-folder — reuse that exact call). Adapt the `$folderPreview` field read to its real shape.
- [ ] Create `frontend/src/lib/components/renames/RenamesHeader.svelte`:
  ```svelte
  <script lang="ts">
    import ProcessMenu from './ProcessMenu.svelte';
    import { viewMode, refreshRenames } from '$lib/stores/renames';

    let { onDolbyVision, onReidentifyAll }: { onDolbyVision: () => void; onReidentifyAll: () => void } = $props();
  </script>

  <div class="flex flex-wrap items-center gap-2">
    <h1 class="text-lg font-semibold mr-auto">Renames</h1>

    <ProcessMenu />

    <button class="px-3 py-1.5 rounded text-xs font-medium bg-[var(--bg-tertiary)]" onclick={onDolbyVision}>Dolby Vision</button>
    <button class="px-3 py-1.5 rounded text-xs font-medium bg-[var(--bg-tertiary)]" onclick={onReidentifyAll}>Re-identify all</button>

    <div class="flex rounded overflow-hidden border border-[var(--border)]">
      <button onclick={() => viewMode.set('list')}
        class="px-2 py-1.5 text-xs {$viewMode === 'list' ? 'bg-[var(--accent)] text-white' : 'text-[var(--text-secondary)] hover:bg-[var(--bg-tertiary)]'}"
        aria-label="List view">☰</button>
      <button onclick={() => viewMode.set('grid')}
        class="px-2 py-1.5 text-xs {$viewMode === 'grid' ? 'bg-[var(--accent)] text-white' : 'text-[var(--text-secondary)] hover:bg-[var(--bg-tertiary)]'}"
        aria-label="Grid view">▦</button>
    </div>

    <button class="px-3 py-1.5 rounded text-xs font-medium bg-[var(--bg-tertiary)]" onclick={() => refreshRenames()}>Refresh</button>
  </div>
  ```
- [ ] Rewrite `frontend/src/routes/renames/+page.svelte` as the orchestrator. Preserve the on-mount loads, the existing DV-scan trigger surface (wrap it in `<div id="dv-scan-surface">`), and `reidentifyAll`/Dolby-Vision handlers ported from the old page:
  ```svelte
  <script lang="ts">
    import { onMount } from 'svelte';
    import {
      renameJobs, renameStatus, renameCategory, renameQuery, renameSort,
      selectedJobIds, viewMode,
      loadRenameJobs, loadRenameStatus, loadDvScans
    } from '$lib/stores/renames';
    import { categoryOf } from '$lib/renames/category';
    import {
      tileSize, gridColumns, gridGap, TILE_MIN_PX, GRID_GAP_CLASS
    } from '$lib/stores/results';
    import { api } from '$lib/api/client';
    import { addToast } from '$lib/stores/notifications';
    import type { RenameJob } from '$lib/api/types';

    import RenamesHeader from '$lib/components/renames/RenamesHeader.svelte';
    import StatusDashboard from '$lib/components/renames/StatusDashboard.svelte';
    import RenameFilterBar from '$lib/components/renames/RenameFilterBar.svelte';
    import BulkBar from '$lib/components/renames/BulkBar.svelte';
    import RenameRow from '$lib/components/renames/RenameRow.svelte';
    import RenameCard from '$lib/components/renames/RenameCard.svelte';
    import RematchModal from '$lib/components/renames/RematchModal.svelte';

    // Status filter is local orchestrator state (surfaced via stat cards).
    let statusFilter = $state<string>('all');
    let rematchJob = $state<RenameJob | null>(null);

    onMount(() => {
      loadRenameJobs();
      loadRenameStatus();
      loadDvScans();
    });

    async function reidentifyAll() {
      try {
        await api.reidentifyAll(); // existing client method; confirm exact name from old page
        addToast('Re-identify all queued', 'info');
      } catch (e) {
        addToast(`Re-identify failed: ${e instanceof Error ? e.message : String(e)}`, 'error');
      }
    }

    function dolbyVision() {
      document.getElementById('dv-scan-surface')?.scrollIntoView({ behavior: 'smooth' });
    }

    // --- Derived visible set: status → category → query → sort ---
    function matchesQuery(j: RenameJob, q: string): boolean {
      if (!q) return true;
      const hay = `${j.title ?? ''} ${j.original_filename ?? ''} ${j.new_filename ?? ''}`.toLowerCase();
      return hay.includes(q.toLowerCase());
    }
    function sortJobs(arr: RenameJob[], mode: typeof $renameSort): RenameJob[] {
      const a = [...arr];
      switch (mode) {
        case 'detected_asc': return a.sort((x, y) => (x.detected_at ?? '').localeCompare(y.detected_at ?? ''));
        case 'confidence_desc': return a.sort((x, y) => (y.match_confidence ?? -1) - (x.match_confidence ?? -1));
        case 'title_asc': return a.sort((x, y) => (x.title ?? '').localeCompare(y.title ?? ''));
        default: return a.sort((x, y) => (y.detected_at ?? '').localeCompare(x.detected_at ?? ''));
      }
    }
    let shown = $derived(
      sortJobs(
        $renameJobs
          .filter((j) => statusFilter === 'all' || j.status === statusFilter)
          .filter((j) => $renameCategory === 'all' || categoryOf(j).has($renameCategory))
          .filter((j) => matchesQuery(j, $renameQuery)),
        $renameSort
      )
    );
    let shownIds = $derived(shown.map((j) => j.id));
    let hasFilters = $derived(statusFilter !== 'all' || $renameCategory !== 'all' || $renameQuery.trim() !== '');

    // Grid prefs (reuse Scan page machinery verbatim).
    let effectiveColumns = $derived($gridColumns !== 'auto' ? $gridColumns : 0);
    let gridStyle = $derived(effectiveColumns > 0
      ? `grid-template-columns: repeat(${effectiveColumns}, 1fr)`
      : `grid-template-columns: repeat(auto-fill, minmax(${TILE_MIN_PX[$tileSize]}px, 1fr))`);
    let gridGapClass = $derived(GRID_GAP_CLASS[$gridGap]);

    function clearFilters() {
      statusFilter = 'all';
      renameCategory.set('all');
      renameQuery.set('');
    }
  </script>

  <div class="flex flex-col gap-4 p-4">
    <RenamesHeader onDolbyVision={dolbyVision} onReidentifyAll={reidentifyAll} />

    <StatusDashboard {statusFilter} onFilter={(s) => (statusFilter = s)} />

    <RenameFilterBar />

    <BulkBar {shownIds} />

    {#if shown.length === 0}
      <div class="text-center py-12 text-[var(--text-secondary)]">
        {#if $renameJobs.length === 0}
          <p>No rename jobs yet. Use <strong>Process ▾</strong> to scan a folder.</p>
        {:else if hasFilters}
          <p>No jobs match these filters.</p>
          <button class="mt-2 px-3 py-1.5 rounded text-xs bg-[var(--bg-tertiary)]" onclick={clearFilters}>Clear filters</button>
        {/if}
      </div>
    {:else if $viewMode === 'grid'}
      <div class="grid {gridGapClass}" style={gridStyle}>
        {#each shown as job (job.id)}
          <RenameCard {job} onRematch={(j) => (rematchJob = j)} />
        {/each}
      </div>
    {:else}
      <ul class="divide-y divide-[var(--border)] rounded-lg border border-[var(--border)] overflow-hidden">
        {#each shown as job (job.id)}
          <RenameRow {job} onRematch={(j) => (rematchJob = j)} />
        {/each}
      </ul>
    {/if}

    <div id="dv-scan-surface">
      <!-- Port the existing Dolby Vision scan trigger markup from the old page here verbatim. -->
    </div>
  </div>

  {#if rematchJob}
    <RematchModal job={rematchJob} onClose={() => (rematchJob = null)} />
  {/if}
  ```
  Note: paste the old page's Dolby-Vision scan UI (the `dvScan*` stores + trigger button it already used) into the `#dv-scan-surface` div, and replace `api.reidentifyAll()` / `api.processFolder()` with the verified existing client methods from the page-read step. Do NOT introduce new backend calls.
- [ ] Run `cd frontend && npm run check`. Expected: `svelte-check found 0 errors`. Fix any unresolved import (`reidentifyAll`, `processFolder`, `folderPreview` shape) by matching the verified names from the old page.
- [ ] Run `cd frontend && npm run test:unit`. Expected: all suites pass (the Task 10 unit tests, `10 passed`).
- [ ] Run `cd frontend && npm run build`. Expected: `vite build` completes with `✓ built in …` and no errors (exit code 0).
- [ ] Manual check: `cd frontend && npm run dev`, open `/renames`. Verify: stat cards filter the list; category chips + counts work; search narrows rows; sort reorders; the list⟷grid toggle switches views and grid columns/gap match Scan prefs with no horizontal overflow (`min-w-0`); selecting rows shows `BulkBar`; opening Rematch shows the search modal with live preview; posters fall back to "No poster" placeholders. Stop dev server.
- [ ] Commit: `git add frontend/src/lib/components/renames/ProcessMenu.svelte frontend/src/lib/components/renames/RenamesHeader.svelte frontend/src/routes/renames/+page.svelte && git commit -m "renames: thin orchestrator + RenamesHeader/ProcessMenu, list<->grid switch, status-card filtering"`.