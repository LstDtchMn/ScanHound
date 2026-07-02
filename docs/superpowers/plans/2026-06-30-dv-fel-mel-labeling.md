# DV FEL/MEL Host-Side Detection + Plex Labeling — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Detect each 4K movie's Dolby Vision layer host-side and label the exact copy Plex serves so Kometa badges `DV FEL` / `DV MEL` / `DV P8` / `DV P5` on the poster.

**Architecture:** A host-side detector on .170 (`dovi_tool.exe` + `dv_host_scan.py`) walks the DV library, classifies via the reused `dv_detect` recipe, and writes its OWN `dv_host.db`; a new `POST /rename/dv-import` ingests it into `dv_scan` (the container is the sole `crawler.db` owner). An in-app labeler resolves each movie's served `part.file`, normalizes the path, matches `dv_scan`, and reconciles a closed managed label set. Kometa overlays the labels.

**Tech Stack:** FastAPI + SQLite (backend), python-plexapi (Plex), SvelteKit 5 (frontend), pytest, Docker; host detector = standalone Python + `dovi_tool.exe`/`mkvpropedit.exe` + Windows Task Scheduler. Full spec: `docs/superpowers/specs/2026-06-30-dv-fel-mel-labeling-design.md`.

## Global Constraints

- In-app changes deploy ONLY via `docker compose up -d --build` (frontend baked into the image). The host detector is a HOST artifact (`dovi_tool.exe` + `dv_host_scan.py` + Task Scheduler) — NOT in the image, NOT built by compose.
- The host detector writes its OWN `dv_host.db` and MUST NEVER open `crawler.db` or import `DatabaseManager` (its `__init__` runs DDL — a second DDL-running process already corrupted the DB once). The container is the SOLE `crawler.db` owner; `POST /rename/dv-import` is the only bridge.
- The labeler reconciles a CLOSED managed set `{DV FEL, DV MEL, DV P8, DV P5}` ONLY — it must NEVER remove a label by a `"DV "` prefix wildcard (that deletes user labels like `DV Cut`).
- Path matching uses a dedicated, tested `normalize_path()` — NOT `PlexManager.translate_path` (dead code at runtime).
- `count_dv_scans_by_layer` + `GET /rename/dv-scans` take a `source` filter; the DV panel shows `source='scan'` only (the ~3729 seed rows must not pollute counts).
- Resolve served paths by iterating ALL `media`/`parts` (never `media[0].parts[0]`) with a multi-copy tie-break `fel > mel > profile8 > profile5`.
- mtime signature tolerance is `>= 2.0s` (clears FAT/exFAT granularity).
- Enumerate movies via the bulk `lib.all()` objects already in memory — NO per-movie `fetchItem` for path resolution (reserve `fetchItem` for the O(1) `rating_key` back-write only).
- The sync worker wraps each title in try/except, throttles Plex writes, and ALWAYS broadcasts `dv:sync_done` in a `finally` (so the UI never sticks).
- On settings save the container writes `data/dv_host.json` with the DV keys; the host detector reads THAT file (never `config.py`, whose `%APPDATA%` path differs from the container's `/data/.config`).
- Verified: `part.file` is available via python-plexapi; `addLabel`/`removeLabel` exist on the movie object; `dv_scan.path` is the PK (no `year`); the DB is WAL. Also fix: `auto_rename_movie_library_4k` is missing from `SettingsUpdate` (currently 422s) — add it.

---

### Task 1: `dv_scan` count/list source filter (default DV-panel to `source='scan'`)

Add a `source` filter to `count_dv_scans_by_layer` and `get_dv_scans` so the seed rows stop dominating the inventory, and default the DV-panel counts to `source='scan'` only.

**Files**
- `X:\Docker Apps\ScanHound\backend\database.py` (edit `get_dv_scans`, `count_dv_scans_by_layer`)
- `X:\Docker Apps\ScanHound\backend\api\routes\rename.py` (edit `GET /dv-scans`)
- `X:\Docker Apps\ScanHound\tests\test_dv_scan_source_filter.py` (new)

**Interfaces**
- Consumes: `DatabaseManager.upsert_dv_scan(path, dv_layer, *, source=...)`, `clear_dv_scans` (existing).
- Produces: `DatabaseManager.count_dv_scans_by_layer(source=None) -> dict[str,int]`, `DatabaseManager.get_dv_scans(dv_layer=None, limit=100000, source=None) -> list[dict]`; `GET /rename/dv-scans` defaulting to scan-source counts/scans.

Steps:

- [ ] **Write the failing test.** Create `X:\Docker Apps\ScanHound\tests\test_dv_scan_source_filter.py`:
  ```python
  from backend.database import DatabaseManager


  def test_count_and_list_exclude_seed_by_default(tmp_path):
      dm = DatabaseManager(db_path=str(tmp_path / "t.db"))
      # seed rows (dead bootstrap) + real scan rows
      dm.upsert_dv_scan("/media/seed_a.mkv", "fel", title="Seed A", source="seed")
      dm.upsert_dv_scan("/media/seed_b.mkv", "mel", title="Seed B", source="seed")
      dm.upsert_dv_scan("/media/scan_a.mkv", "fel", title="Scan A", source="scan")
      dm.upsert_dv_scan("/media/scan_b.mkv", "fel", title="Scan B", source="scan")
      dm.upsert_dv_scan("/media/scan_c.mkv", "mel", title="Scan C", source="scan")

      # default (no filter) = every row, backward-compatible
      assert dm.count_dv_scans_by_layer() == {"fel": 3, "mel": 2}

      # scan-only = the real detected counts
      assert dm.count_dv_scans_by_layer(source="scan") == {"fel": 2, "mel": 1}

      scan_rows = dm.get_dv_scans(source="scan")
      assert {r["path"] for r in scan_rows} == {
          "/media/scan_a.mkv", "/media/scan_b.mkv", "/media/scan_c.mkv"}

      # layer + source compose
      fel_scan = dm.get_dv_scans(dv_layer="fel", source="scan")
      assert {r["path"] for r in fel_scan} == {"/media/scan_a.mkv", "/media/scan_b.mkv"}

      dm.close()
  ```

- [ ] **Run it — expect failure.** From `X:\Docker Apps\ScanHound`:
  ```
  python -m pytest tests/test_dv_scan_source_filter.py -v
  ```
  Expected: `TypeError: count_dv_scans_by_layer() got an unexpected keyword argument 'source'` (and same for `get_dv_scans`).

- [ ] **Minimal impl — `count_dv_scans_by_layer`.** In `X:\Docker Apps\ScanHound\backend\database.py` replace the method:
  ```python
  def count_dv_scans_by_layer(self, source=None):
      """Return ``{layer: count}`` over the dv_scan table.

      ``source`` (e.g. 'scan') restricts the count to that origin, so the DV
      panel can show real detected counts instead of dead seed rows.
      """
      if source is not None:
          rows = self._query(
              'SELECT dv_layer, COUNT(*) FROM dv_scan WHERE source = ? '
              'GROUP BY dv_layer', (source,), default=[])
      else:
          rows = self._query(
              'SELECT dv_layer, COUNT(*) FROM dv_scan GROUP BY dv_layer', default=[])
      return {r[0]: r[1] for r in (rows or [])}
  ```

- [ ] **Minimal impl — `get_dv_scans`.** Replace the method:
  ```python
  def get_dv_scans(self, dv_layer=None, limit=100000, source=None):
      """Return DV-scan rows, optionally filtered by layer and/or source."""
      clauses = []
      params = []
      if dv_layer:
          clauses.append("dv_layer = ?")
          params.append(dv_layer)
      if source is not None:
          clauses.append("source = ?")
          params.append(source)
      where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
      params.append(limit)
      return self._query_dicts(
          'SELECT path, title, dv_layer, rating_key, imdb_id, '
          'scanned_at, last_seen_at FROM dv_scan'
          f'{where} ORDER BY last_seen_at DESC LIMIT ?', tuple(params), default=[])
  ```

- [ ] **Run it — expect pass.**
  ```
  python -m pytest tests/test_dv_scan_source_filter.py -v
  ```
  Expected: `1 passed`.

- [ ] **Wire the endpoint to scan-source counts.** In `X:\Docker Apps\ScanHound\backend\api\routes\rename.py`, change the `GET /dv-scans` return to default the DV panel to scan-source data:
  ```python
      return {
          "scans": reg.db.get_dv_scans(dv_layer=layer, limit=limit, source="scan"),
          "counts": reg.db.count_dv_scans_by_layer(source="scan"),
      }
  ```

- [ ] **Guard the endpoint with a route test.** Append to the new test file:
  ```python
  def test_dv_scans_endpoint_scan_source_only(client, monkeypatch):
      from backend.api.dependencies import registry
      from backend.database import DatabaseManager
      import backend.api.routes.rename as rename_mod
      dm = DatabaseManager()
      dm.clear_dv_scans()
      dm.upsert_dv_scan("/m/seed.mkv", "fel", source="seed")
      dm.upsert_dv_scan("/m/scan.mkv", "fel", source="scan")
      registry.db = dm
      r = client.get("/api/rename/dv-scans")
      assert r.status_code == 200
      body = r.json()
      assert body["counts"] == {"fel": 1}
      assert [s["path"] for s in body["scans"]] == ["/m/scan.mkv"]
      dm.clear_dv_scans()
  ```
  (Reuses the existing `client` fixture from `tests/test_api_rename.py`; if running this file standalone, copy the `client` fixture from `tests/test_api_rename.py:19-23` and the `_reset_jobs` autouse fixture into it.)

- [ ] **Run the whole file — expect pass.**
  ```
  python -m pytest tests/test_dv_scan_source_filter.py -v
  ```
  Expected: `2 passed`.

- [ ] **Commit.**
  ```
  git add backend/database.py backend/api/routes/rename.py tests/test_dv_scan_source_filter.py
  git commit -m "dv_scan: add source filter to counts/list; DV panel shows scan-source only"
  ```

**Deliverable:** `count_dv_scans_by_layer(source=...)` + `get_dv_scans(source=...)` exclude seed rows; `GET /rename/dv-scans` shows real detected counts.

---

### Task 2: `plex_service` label write-path + `part.file` capture (all parts)

Add `add_label`/`remove_label` and capture the served file path for **every** `(media, part)` in `_extract_movie_data`.

**Files**
- `X:\Docker Apps\ScanHound\backend\plex_manager.py` (add `add_label`, `remove_label`)
- `X:\Docker Apps\ScanHound\backend\plex_service.py` (edit `_extract_movie_data`)
- `X:\Docker Apps\ScanHound\tests\test_dv_label_writepath.py` (new)

**Interfaces**
- Consumes: `PlexManager._server` (the connected `PlexServer` from `_connect_direct`/`connect_via_account`), `movie.media[*].parts[*].file`.
- Produces: `PlexManager.add_label(rating_key, label)`, `PlexManager.remove_label(rating_key, label)`; each per-part movie dict in `_extract_movie_data` carries `'file'` (the served path, or `None`).

Steps:

- [ ] **Write the failing test.** Create `X:\Docker Apps\ScanHound\tests\test_dv_label_writepath.py`:
  ```python
  from unittest.mock import MagicMock
  from backend.plex_manager import PlexManager
  from backend.plex_service import PlexService


  def _pm_with_server():
      pm = PlexManager.__new__(PlexManager)
      pm._server = MagicMock()
      return pm


  def _make_service(config=None, pm=None):
      return PlexService(config=config or {}, db=MagicMock(),
                         plex_manager=pm or MagicMock())


  def _part(file, size=1_000_000_000):
      p = MagicMock()
      p.size = size
      p.file = file
      p.videoStreams.return_value = []
      return p


  def _media(parts, res="4k"):
      m = MagicMock()
      m.videoResolution = res
      m.id = 42
      m.parts = parts
      return m


  def _movie(media_list, title="M", year=2024, rk=7):
      mv = MagicMock()
      mv.title = title
      mv.year = year
      mv.ratingKey = rk
      mv.originalLanguage = "en"
      g = MagicMock(); g.id = "imdb://tt1"
      mv.guids = [g]
      mv.media = media_list
      return mv


  def test_add_and_remove_label_fetch_and_call():
      pm = _pm_with_server()
      item = MagicMock()
      pm._server.fetchItem.return_value = item

      pm.add_label("123", "DV FEL")
      pm._server.fetchItem.assert_called_with(123)   # str -> int
      item.addLabel.assert_called_once_with("DV FEL")

      pm.remove_label("123", "DV MEL")
      item.removeLabel.assert_called_once_with("DV MEL")


  def test_extract_captures_file_for_all_parts():
      svc = _make_service()
      movie = _movie([
          _media([_part("Y:/A/edition1.mkv"), _part("Y:/A/edition2.mkv")]),
          _media([_part("Z:/B/optimized.mp4")], res="1080"),
      ])
      rows = svc._extract_movie_data(movie)
      files = [r["file"] for r in rows]
      assert files == ["Y:/A/edition1.mkv", "Y:/A/edition2.mkv", "Z:/B/optimized.mp4"]


  def test_extract_guards_empty_parts_and_none_file():
      svc = _make_service()
      empty_media = _media([])          # no parts
      none_part = _part(None)           # part with no file
      movie = _movie([empty_media, _media([none_part])])
      rows = svc._extract_movie_data(movie)
      # empty-parts media yields no row; None file is preserved (not a crash)
      assert [r["file"] for r in rows] == [None]
  ```

- [ ] **Run it — expect failure.**
  ```
  python -m pytest tests/test_dv_label_writepath.py -v
  ```
  Expected: `AttributeError: 'PlexManager' object has no attribute 'add_label'` and `KeyError: 'file'`.

- [ ] **Minimal impl — label methods.** In `X:\Docker Apps\ScanHound\backend\plex_manager.py`, add right after `get_library_section` (ends ~line 419):
  ```python
      def add_label(self, rating_key, label):
          """Add a Plex label to the item with ``rating_key`` (TEXT-safe)."""
          self._server.fetchItem(int(rating_key)).addLabel(label)

      def remove_label(self, rating_key, label):
          """Remove a Plex label from the item with ``rating_key`` (TEXT-safe)."""
          self._server.fetchItem(int(rating_key)).removeLabel(label)
  ```

- [ ] **Minimal impl — iterate all parts + capture `file`.** In `X:\Docker Apps\ScanHound\backend\plex_service.py`, replace the `for media in movie.media:` body (lines 431-475) so it loops parts and appends one row per part:
  ```python
              results = []
              for media in movie.media:
                  parts = media.parts or []
                  if not parts:
                      continue
                  for part in parts:
                      size_gb = round(part.size / (1024**3), 2) if part and part.size else 0

                      res = "?"
                      if media.videoResolution:
                          if media.videoResolution in ("4k", "2160"):
                              res = "4K"
                          elif media.videoResolution == "1080":
                              res = "1080p"
                          elif media.videoResolution == "720":
                              res = "720p"

                      dovi = False
                      hdr = False
                      for stream in part.videoStreams():
                          dovi_found = self._check_dovi(stream)
                          if dovi_found:
                              dovi = True
                              break
                          if hasattr(stream, 'colorPrimaries') and stream.colorPrimaries:
                              if 'bt2020' in stream.colorPrimaries.lower():
                                  hdr = True

                      imdb_id = None
                      for guid in movie.guids:
                          if 'imdb://' in guid.id:
                              imdb_id = guid.id.replace('imdb://', '')
                              break

                      results.append({
                          'clean_title': _clean_string(movie.title),
                          'original_title': movie.title,
                          'year': movie.year or 0,
                          'res': res,
                          'size': size_gb,
                          'dovi': dovi,
                          'hdr': hdr,
                          'imdb_id': imdb_id,
                          'rating_key': movie.ratingKey,
                          'media_id': media.id,  # unique per version — prevents DB key collision
                          'file': part.file if part else None,  # served path (may be None)
                          'language': getattr(movie, 'originalLanguage', '') or "",
                      })

              return results if results else None
  ```
  Leave the `needs_reload` block (lines 419-428) and the outer `try/except` (lines 415-480) unchanged.

- [ ] **Run it — expect pass.**
  ```
  python -m pytest tests/test_dv_label_writepath.py -v
  ```
  Expected: `3 passed`.

- [ ] **Regression — existing plex_service tests still green.**
  ```
  python -m pytest tests/test_plex_service.py -v
  ```
  Expected: all pass (the extractor still returns one dict per version-part; `media_id` unchanged; multi-part titles now emit multiple rows).

- [ ] **Commit.**
  ```
  git add backend/plex_manager.py backend/plex_service.py tests/test_dv_label_writepath.py
  git commit -m "plex: add add_label/remove_label; capture part.file for every (media,part)"
  ```

**Deliverable:** `PlexManager.add_label/remove_label` and a per-part `'file'` key on every extracted movie dict.

---

### Task 3: `normalize_path()` helper + drive↔UNC mapping table

A dedicated, unit-tested module (NOT `PlexManager.translate_path`, which is dead) that canonicalizes a path for cross-machine matching.

**Files**
- `X:\Docker Apps\ScanHound\backend\rename\dv_paths.py` (new)
- `X:\Docker Apps\ScanHound\tests\test_dv_paths.py` (new)

**Interfaces**
- Consumes: raw path strings + a mapping table `list[tuple[str, str]]` of `(drive_root, unc_root)`.
- Produces: `normalize_path(p, mappings=None) -> str` (sep-unified `/`, casefolded, longest-prefix drive↔UNC rewrite, trimmed); `DEFAULT_DV_MAPPINGS` (empty default); `same_target(a, b, mappings=None) -> bool` guard for two different roots colliding.

Steps:

- [ ] **Write the failing test.** Create `X:\Docker Apps\ScanHound\tests\test_dv_paths.py`:
  ```python
  import pytest
  from backend.rename.dv_paths import normalize_path, same_target

  MAP = [
      (r"Y:", r"\\TURTLELANDSRV2\Share"),
      (r"P:", r"\\TURTLELANDSRV2\Plex\4K Magellan"),
  ]


  @pytest.mark.parametrize("raw,expected", [
      # separator unify + casefold
      (r"E:\4K\Movie (2020)\file.MKV", "e:/4k/movie (2020)/file.mkv"),
      ("E:/4K/Movie (2020)/file.mkv", "e:/4k/movie (2020)/file.mkv"),
      # trailing junk trimmed, dup separators collapsed
      (r"E:\4K\\Movie\\ ", "e:/4k/movie"),
      (r"E:\4K\Movie\.", "e:/4k/movie"),
  ])
  def test_sep_case_trim(raw, expected):
      assert normalize_path(raw) == expected


  @pytest.mark.parametrize("raw", [
      r"Y:\Movies\A\f.mkv",
      r"\\TURTLELANDSRV2\Share\Movies\A\f.mkv",
      "y:/movies/a/f.mkv",
      r"\\turtlelandsrv2\share\Movies\A\f.MKV",
  ])
  def test_drive_and_unc_collapse_to_one_canonical(raw):
      # every variant of the same file maps to a single canonical string
      assert normalize_path(raw, MAP) == normalize_path(r"Y:\Movies\A\f.mkv", MAP)


  def test_longest_prefix_wins():
      # P: is a deeper UNC root than Y:; must not be shadowed by a shorter match
      a = normalize_path(r"P:\Film\x.mkv", MAP)
      b = normalize_path(r"\\TURTLELANDSRV2\Plex\4K Magellan\Film\x.mkv", MAP)
      assert a == b


  def test_two_different_roots_do_not_collide():
      a = normalize_path(r"Y:\Movies\A\f.mkv", MAP)
      b = normalize_path(r"Z:\Movies\A\f.mkv", MAP)
      assert a != b


  def test_same_target_guard():
      assert same_target(r"Y:\Movies\A\f.mkv",
                         r"\\TURTLELANDSRV2\Share\Movies\A\f.mkv", MAP) is True
      assert same_target(r"Y:\Movies\A\f.mkv",
                         r"Z:\Movies\A\f.mkv", MAP) is False
  ```

- [ ] **Run it — expect failure.**
  ```
  python -m pytest tests/test_dv_paths.py -v
  ```
  Expected: `ModuleNotFoundError: No module named 'backend.rename.dv_paths'`.

- [ ] **Minimal impl.** Create `X:\Docker Apps\ScanHound\backend\rename\dv_paths.py`:
  ```python
  """Path canonicalization for cross-machine DV matching.

  Standalone and dependency-free: does NOT use PlexManager.translate_path (which
  is dead — _path_mappings is never populated and PathMapping.translate is a bare
  str.replace with no case/separator handling). The host detector records
  host-native paths (drive letters or UNC); Plex serves whatever letter/case/UNC
  it stored. normalize_path() collapses both into one comparable string.
  """
  from typing import List, Optional, Tuple

  # (drive_root, unc_root) pairs, e.g. ("Y:", r"\\SRV\Share"). Both roots must
  # point at the SAME physical storage. Empty by default — populated from
  # dv_label_vocab/config or the dry-run sampling gate (design §7.4).
  DEFAULT_DV_MAPPINGS: List[Tuple[str, str]] = []


  def _unify(s: str) -> str:
      """Backslashes -> forward slashes, casefold."""
      return s.replace("\\", "/").casefold()


  def _trim(s: str) -> str:
      """Collapse duplicate separators; strip trailing slashes/dots/spaces."""
      while "//" in s:
          s = s.replace("//", "/")
      return s.rstrip("/. ")


  def normalize_path(p: str, mappings: Optional[List[Tuple[str, str]]] = None) -> str:
      """Canonicalize *p* for cross-machine equality.

      Steps: (1) unify separators to '/'; (2) casefold; (3) rewrite each mapped
      drive/UNC root to a single canonical form (longest matching prefix wins so a
      deeper UNC share isn't shadowed by a shorter one); (4) trim trailing junk.
      Returns '' for a falsy input.
      """
      if not p:
          return ""
      s = _unify(p)
      table = mappings if mappings is not None else DEFAULT_DV_MAPPINGS
      # Canonical target = the drive form (short + stable). Build (variant, drive)
      # rewrite pairs from BOTH the drive and UNC roots, longest-prefix first.
      rewrites: List[Tuple[str, str]] = []
      for drive_root, unc_root in table:
          canon = _unify(drive_root)
          rewrites.append((_unify(unc_root), canon))
          rewrites.append((canon, canon))
      rewrites.sort(key=lambda pair: len(pair[0]), reverse=True)
      for variant, canon in rewrites:
          if s == variant or s.startswith(variant + "/"):
              s = canon + s[len(variant):]
              break
      return _trim(s)


  def same_target(a: str, b: str,
                  mappings: Optional[List[Tuple[str, str]]] = None) -> bool:
      """True iff *a* and *b* normalize to the same canonical path."""
      return normalize_path(a, mappings) == normalize_path(b, mappings)
  ```

- [ ] **Run it — expect pass.**
  ```
  python -m pytest tests/test_dv_paths.py -v
  ```
  Expected: `10 passed` (7 param cases + 3 named tests: adjust to actual count reported).

- [ ] **Commit.**
  ```
  git add backend/rename/dv_paths.py tests/test_dv_paths.py
  git commit -m "dv_paths: dedicated normalize_path() with drive<->UNC mapping table"
  ```

**Deliverable:** `normalize_path()` collapsing drive/UNC/case/separator variants to one canonical string, with a two-root collision guard.

---

### Task 4: Config keys + `SettingsUpdate` (and fix the missing `auto_rename_movie_library_4k` 422)

Add the four DV keys to `AppConfig`/`_DEFAULT_CONFIG` and `SettingsUpdate`; also add the already-missing `auto_rename_movie_library_4k` to `SettingsUpdate`.

**Files**
- `X:\Docker Apps\ScanHound\backend\config.py` (edit `AppConfig`, `_DEFAULT_CONFIG`)
- `X:\Docker Apps\ScanHound\backend\api\routes\settings.py` (edit `SettingsUpdate`)
- `X:\Docker Apps\ScanHound\tests\test_dv_settings.py` (new)

**Interfaces**
- Consumes: existing `PUT /settings` handler (`update_settings`) + `AppService.save_config`.
- Produces: config keys `dv_library_roots: str`, `dv_detection: bool`, `dv_file_tagging: bool`, `dv_label_vocab: str`; `SettingsUpdate` fields for those four + `auto_rename_movie_library_4k`.

Steps:

- [ ] **Write the failing test.** Create `X:\Docker Apps\ScanHound\tests\test_dv_settings.py`:
  ```python
  import json
  from backend.api.routes.settings import SettingsUpdate


  def test_settings_model_accepts_dv_keys_and_4k():
      m = SettingsUpdate(
          dv_library_roots="Y:\\Movies;E:\\4K",
          dv_detection=True,
          dv_file_tagging=False,
          dv_label_vocab=json.dumps({"fel": "DV FEL", "mel": "DV MEL"}),
          auto_rename_movie_library_4k="Movies 4K",
      )
      dumped = m.model_dump(exclude_unset=True)
      assert dumped["dv_library_roots"] == "Y:\\Movies;E:\\4K"
      assert dumped["dv_detection"] is True
      assert dumped["auto_rename_movie_library_4k"] == "Movies 4K"


  def test_defaults_have_dv_keys():
      from backend.config import _DEFAULT_CONFIG
      assert _DEFAULT_CONFIG["dv_detection"] is False
      assert _DEFAULT_CONFIG["dv_file_tagging"] is False
      assert _DEFAULT_CONFIG["dv_library_roots"] == ""
      assert isinstance(_DEFAULT_CONFIG["dv_label_vocab"], str)


  def test_put_settings_round_trips_dv_and_4k(client):
      from backend.api.dependencies import registry
      registry.config = {}

      class _Backend:
          _cleared_keys = set()
          def save_config(self):  # no-op; config isolated by conftest
              pass
      registry.backend = _Backend()

      payload = {
          "dv_library_roots": "Y:\\M",
          "dv_detection": True,
          "auto_rename_movie_library_4k": "Movies 4K",
      }
      r = client.put("/api/settings", json=payload)
      assert r.status_code == 200, r.text  # was 422 for the 4k key before the fix
      updated = set(r.json()["updated_keys"])
      assert {"dv_library_roots", "dv_detection",
              "auto_rename_movie_library_4k"} <= updated
      assert registry.config["auto_rename_movie_library_4k"] == "Movies 4K"
  ```
  (Add the `client` fixture — copy from `tests/test_api_routes.py:74-78` — and the `_reset_registry` autouse fixture from `tests/test_api_routes.py:10-25` into this file if it is not picked up from a shared conftest.)

- [ ] **Run it — expect failure.**
  ```
  python -m pytest tests/test_dv_settings.py -v
  ```
  Expected: `TypeError`/`ValidationError` for unknown `dv_*` fields (extra=forbid) and `KeyError` on `_DEFAULT_CONFIG["dv_detection"]`; the PUT test gets `422`.

- [ ] **Minimal impl — `AppConfig`.** In `X:\Docker Apps\ScanHound\backend\config.py`, add to the `AppConfig` TypedDict (after the auto-rename/ollama block, ~line 129):
  ```python
      # Dolby Vision host-detector + labeler
      dv_library_roots: str      # host-native roots, ';' or newline separated
      dv_detection: bool
      dv_file_tagging: bool
      dv_label_vocab: str        # JSON: {layer: label}
  ```

- [ ] **Minimal impl — `_DEFAULT_CONFIG`.** Add to `_DEFAULT_CONFIG` (after `ollama_model`, ~line 367):
  ```python
      "dv_library_roots": "",
      "dv_detection": False,
      "dv_file_tagging": False,
      "dv_label_vocab": '{"fel": "DV FEL", "mel": "DV MEL", "profile8": "DV P8", "profile5": "DV P5"}',
  ```

- [ ] **Minimal impl — `SettingsUpdate`.** In `X:\Docker Apps\ScanHound\backend\api\routes\settings.py`, add the missing 4K field right after `auto_rename_movie_library` (line 116) and the DV fields after `ollama_model` (line 123):
  ```python
      auto_rename_movie_library_4k: Optional[str] = None
  ```
  and:
  ```python
      dv_library_roots: Optional[str] = None
      dv_detection: Optional[bool] = None
      dv_file_tagging: Optional[bool] = None
      dv_label_vocab: Optional[str] = None
  ```

- [ ] **Run it — expect pass.**
  ```
  python -m pytest tests/test_dv_settings.py -v
  ```
  Expected: `3 passed`.

- [ ] **Regression — settings route tests still green.**
  ```
  python -m pytest tests/test_api_routes.py -v -k settings
  ```
  Expected: all pass.

- [ ] **Commit.**
  ```
  git add backend/config.py backend/api/routes/settings.py tests/test_dv_settings.py
  git commit -m "config: add dv_* settings; fix missing auto_rename_movie_library_4k (was 422)"
  ```

**Deliverable:** DV settings round-trip through `PUT /settings`; `auto_rename_movie_library_4k` no longer 422s.

---

### Task 5: Export `data/dv_host.json` on settings save

When settings are saved, write the DV keys to `<repo>\data\dv_host.json` (the host detector reads that, NOT `config.py`).

**Files**
- `X:\Docker Apps\ScanHound\backend\app_service.py` (edit `save_config` to also export)
- `X:\Docker Apps\ScanHound\tests\test_dv_host_export.py` (new)

**Interfaces**
- Consumes: `self.config` dict (holds the DV keys), a `DV_HOST_JSON` path constant.
- Produces: a helper `export_dv_host_config(config, dest) -> dict` (pure, testable) + a call from `AppService.save_config`; writes `{dv_library_roots, dv_detection, dv_file_tagging, dv_label_vocab}` JSON.

Steps:

- [ ] **Write the failing test.** Create `X:\Docker Apps\ScanHound\tests\test_dv_host_export.py`:
  ```python
  import json
  from backend.app_service import export_dv_host_config


  def test_export_writes_only_dv_keys(tmp_path):
      dest = tmp_path / "dv_host.json"
      cfg = {
          "plex_token": "SECRET",            # must NOT leak
          "dv_library_roots": "Y:\\M;E:\\4K",
          "dv_detection": True,
          "dv_file_tagging": False,
          "dv_label_vocab": '{"fel": "DV FEL"}',
      }
      written = export_dv_host_config(cfg, str(dest))
      assert set(written) == {
          "dv_library_roots", "dv_detection", "dv_file_tagging", "dv_label_vocab"}
      on_disk = json.loads(dest.read_text(encoding="utf-8"))
      assert on_disk == written
      assert "plex_token" not in on_disk


  def test_export_uses_defaults_for_missing_keys(tmp_path):
      dest = tmp_path / "dv_host.json"
      written = export_dv_host_config({}, str(dest))
      assert written["dv_detection"] is False
      assert written["dv_library_roots"] == ""


  def test_save_config_exports_dv_host_json(tmp_path, monkeypatch):
      import backend.app_service as app_service

      class _Svc:
          config = {
              "dv_library_roots": "Y:\\M",
              "dv_detection": True,
              "dv_file_tagging": False,
              "dv_label_vocab": '{"fel": "DV FEL"}',
          }
          _cleared_keys = set()
      dest = tmp_path / "data" / "dv_host.json"
      monkeypatch.setattr(app_service, "DV_HOST_JSON", str(dest), raising=False)
      # exercise just the export hook (avoid the full CONFIG_FILE write path)
      app_service.export_dv_host_config(_Svc.config, app_service.DV_HOST_JSON)
      assert json.loads(dest.read_text(encoding="utf-8"))["dv_detection"] is True
  ```

- [ ] **Run it — expect failure.**
  ```
  python -m pytest tests/test_dv_host_export.py -v
  ```
  Expected: `ImportError: cannot import name 'export_dv_host_config'`.

- [ ] **Minimal impl — path constant + helper.** In `X:\Docker Apps\ScanHound\backend\app_service.py`, add near the module-level path constants (top of file, alongside `CONFIG_FILE`):
  ```python
  import os

  # Bind-mounted data dir the host detector reads (design §5/§9). Fixed path so the
  # host script never needs config.py's %APPDATA% resolution.
  _DV_DATA_DIR = os.environ.get("SCANHOUND_DATA_DIR", "/data")
  DV_HOST_JSON = os.path.join(_DV_DATA_DIR, "dv_host.json")

  _DV_EXPORT_DEFAULTS = {
      "dv_library_roots": "",
      "dv_detection": False,
      "dv_file_tagging": False,
      "dv_label_vocab": '{"fel": "DV FEL", "mel": "DV MEL", "profile8": "DV P8", "profile5": "DV P5"}',
  }


  def export_dv_host_config(config, dest):
      """Write the DV subset of *config* to *dest* (JSON) for the host detector.

      Only the four DV keys are exported — never secrets. Missing keys fall back to
      defaults. Returns the dict written. Atomic replace; parent dir auto-created.
      """
      import json
      payload = {k: config.get(k, default) for k, default in _DV_EXPORT_DEFAULTS.items()}
      os.makedirs(os.path.dirname(dest), exist_ok=True)
      tmp = f"{dest}.tmp"
      with open(tmp, "w", encoding="utf-8") as f:
          json.dump(payload, f, indent=2)
          f.flush()
          os.fsync(f.fileno())
      os.replace(tmp, dest)
      return payload
  ```

- [ ] **Minimal impl — call from `save_config`.** In `AppService.save_config` (after the `os.replace(temp_file, CONFIG_FILE)` at line 733), add:
  ```python
              try:
                  export_dv_host_config(self.config, DV_HOST_JSON)
              except Exception as e:
                  logger.warning("dv_host.json export failed: %s", e)
  ```

- [ ] **Run it — expect pass.**
  ```
  python -m pytest tests/test_dv_host_export.py -v
  ```
  Expected: `3 passed`.

- [ ] **Regression — save_config path still works.**
  ```
  python -m pytest tests/test_api_routes.py -v -k settings
  ```
  Expected: all pass (export failure is swallowed; config isolation from conftest keeps writes off the real FS).

- [ ] **Commit.**
  ```
  git add backend/app_service.py tests/test_dv_host_export.py
  git commit -m "app_service: export data/dv_host.json (DV keys only) on settings save"
  ```

**Deliverable:** saving settings writes `data/dv_host.json` containing exactly the four DV keys, secrets excluded.

---

### Task 6: `POST /rename/dv-import` — ingest `dv_host.db` into `dv_scan`

Read a host SQLite (`dv_host.db`) of `{path, dv_layer, sig_mtime, sig_size, title}` rows and upsert each with `source='scan'`; idempotent; overwrites a same-path seed row.

**Files**
- `X:\Docker Apps\ScanHound\backend\rename\dv_import.py` (new — pure reader/importer)
- `X:\Docker Apps\ScanHound\backend\api\routes\rename.py` (add endpoint + request model)
- `X:\Docker Apps\ScanHound\tests\test_dv_import.py` (new)

**Interfaces**
- Consumes: a host `dv_host.db` (raw `sqlite3`, read-only), `DatabaseManager.upsert_dv_scan(..., source='scan')`, `DatabaseManager.get_dv_scan`.
- Produces: `import_dv_host_db(db, host_db_path) -> {"imported": int, "updated": int}`; `POST /rename/dv-import {host_db_path?}` returning that dict.

Steps:

- [ ] **Write the failing test.** Create `X:\Docker Apps\ScanHound\tests\test_dv_import.py`:
  ```python
  import sqlite3
  from backend.database import DatabaseManager
  from backend.rename.dv_import import import_dv_host_db


  def _make_host_db(path, rows):
      conn = sqlite3.connect(str(path))
      conn.execute(
          "CREATE TABLE dv_host (path TEXT PRIMARY KEY, dv_layer TEXT, "
          "sig_mtime REAL, sig_size INTEGER, title TEXT, scanned_at TIMESTAMP)")
      conn.executemany(
          "INSERT INTO dv_host (path, dv_layer, sig_mtime, sig_size, title) "
          "VALUES (?,?,?,?,?)", rows)
      conn.commit(); conn.close()


  def test_import_creates_scan_rows(tmp_path):
      host = tmp_path / "dv_host.db"
      _make_host_db(host, [
          ("Y:/M/a.mkv", "fel", 111.0, 1000, "A"),
          ("Y:/M/b.mkv", "mel", 222.0, 2000, "B"),
      ])
      dm = DatabaseManager(db_path=str(tmp_path / "c.db"))
      res = import_dv_host_db(dm, str(host))
      assert res == {"imported": 2, "updated": 0}
      row = dm.get_dv_scan("Y:/M/a.mkv")
      assert row["dv_layer"] == "fel" and row["source"] == "scan"
      assert row["sig_mtime"] == 111.0 and row["sig_size"] == 1000
      dm.close()


  def test_reimport_is_idempotent_update(tmp_path):
      host = tmp_path / "dv_host.db"
      _make_host_db(host, [("Y:/M/a.mkv", "fel", 111.0, 1000, "A")])
      dm = DatabaseManager(db_path=str(tmp_path / "c.db"))
      import_dv_host_db(dm, str(host))
      res2 = import_dv_host_db(dm, str(host))
      assert res2 == {"imported": 0, "updated": 1}
      assert dm.count_dv_scans_by_layer(source="scan") == {"fel": 1}
      dm.close()


  def test_import_overwrites_seed_row(tmp_path):
      host = tmp_path / "dv_host.db"
      _make_host_db(host, [("Y:/M/a.mkv", "fel", 111.0, 1000, "A")])
      dm = DatabaseManager(db_path=str(tmp_path / "c.db"))
      dm.upsert_dv_scan("Y:/M/a.mkv", "unknown", title="A", source="seed")
      import_dv_host_db(dm, str(host))
      row = dm.get_dv_scan("Y:/M/a.mkv")
      assert row["source"] == "scan" and row["dv_layer"] == "fel"
      dm.close()


  def test_missing_host_db_returns_zero(tmp_path):
      dm = DatabaseManager(db_path=str(tmp_path / "c.db"))
      res = import_dv_host_db(dm, str(tmp_path / "nope.db"))
      assert res == {"imported": 0, "updated": 0}
      dm.close()
  ```

- [ ] **Run it — expect failure.**
  ```
  python -m pytest tests/test_dv_import.py -v
  ```
  Expected: `ModuleNotFoundError: No module named 'backend.rename.dv_import'`.

- [ ] **Minimal impl.** Create `X:\Docker Apps\ScanHound\backend\rename\dv_import.py`:
  ```python
  """Ingest the host detector's dv_host.db into crawler.db's dv_scan table.

  The container is the SOLE owner of crawler.db. This reads the host store
  read-only (raw sqlite3 — it must NOT construct a second DatabaseManager on the
  host DB, which would run DDL) and upserts every row as source='scan', which the
  upsert's ON CONFLICT supersedes any existing 'seed' row for the same path.
  """
  import logging
  import os
  import sqlite3

  logger = logging.getLogger(__name__)


  def import_dv_host_db(db, host_db_path):
      """Upsert every dv_host.db row into *db*.dv_scan as source='scan'.

      Returns ``{"imported": <new paths>, "updated": <existing paths>}``.
      A missing/unreadable host DB is a no-op returning zeros.
      """
      if not host_db_path or not os.path.exists(host_db_path):
          logger.warning("dv-import: host db not found: %s", host_db_path)
          return {"imported": 0, "updated": 0}

      imported = 0
      updated = 0
      try:
          conn = sqlite3.connect(f"file:{host_db_path}?mode=ro", uri=True)
          conn.row_factory = sqlite3.Row
          rows = conn.execute(
              "SELECT path, dv_layer, sig_mtime, sig_size, title FROM dv_host").fetchall()
          conn.close()
      except sqlite3.Error as e:
          logger.error("dv-import: reading host db failed: %s", e)
          return {"imported": 0, "updated": 0}

      for r in rows:
          path = r["path"]
          if not path:
              continue
          existed = db.get_dv_scan(path) is not None
          ok = db.upsert_dv_scan(
              path, r["dv_layer"], title=r["title"],
              sig_mtime=r["sig_mtime"], sig_size=r["sig_size"], source="scan")
          if not ok:
              continue
          if existed:
              updated += 1
          else:
              imported += 1
      return {"imported": imported, "updated": updated}
  ```

- [ ] **Run it — expect pass.**
  ```
  python -m pytest tests/test_dv_import.py -v
  ```
  Expected: `4 passed`.

- [ ] **Add the endpoint.** In `X:\Docker Apps\ScanHound\backend\api\routes\rename.py`, add a request model near `DvScanRequest`:
  ```python
  class DvImportRequest(BaseModel):
      host_db_path: Optional[str] = None
  ```
  and the endpoint (import the helper at top: `from backend.rename.dv_import import import_dv_host_db`):
  ```python
  # default host store path inside the container's bind-mounted data dir
  _DEFAULT_DV_HOST_DB = os.environ.get(
      "SCANHOUND_DV_HOST_DB", "/data/dv_host.db")


  @router.post("/dv-import")
  def dv_import(req: DvImportRequest, reg: ServiceRegistry = Depends(get_registry)):
      """Ingest the host detector's dv_host.db into dv_scan (source='scan')."""
      if reg.db is None:
          raise HTTPException(status_code=503, detail="DB not initialized")
      path = (req.host_db_path or _DEFAULT_DV_HOST_DB)
      return import_dv_host_db(reg.db, path)
  ```

- [ ] **Add the endpoint test.** Append to `tests/test_dv_import.py`:
  ```python
  def test_dv_import_endpoint(client, tmp_path):
      from backend.api.dependencies import registry
      from backend.database import DatabaseManager
      host = tmp_path / "dv_host.db"
      _make_host_db(host, [("Y:/M/a.mkv", "fel", 1.0, 10, "A")])
      dm = DatabaseManager(); dm.clear_dv_scans()
      registry.db = dm
      r = client.post("/api/rename/dv-import", json={"host_db_path": str(host)})
      assert r.status_code == 200
      assert r.json() == {"imported": 1, "updated": 0}
      assert dm.get_dv_scan("Y:/M/a.mkv")["source"] == "scan"
      dm.clear_dv_scans()
  ```
  (Bring in the `client` fixture + `_reset_jobs` autouse from `tests/test_api_rename.py:9-23`.)

- [ ] **Run the file — expect pass.**
  ```
  python -m pytest tests/test_dv_import.py -v
  ```
  Expected: `5 passed`.

- [ ] **Commit.**
  ```
  git add backend/rename/dv_import.py backend/api/routes/rename.py tests/test_dv_import.py
  git commit -m "rename: POST /dv-import ingests host dv_host.db into dv_scan (source=scan)"
  ```

**Deliverable:** `POST /rename/dv-import` upserts host rows as `source='scan'`, idempotent, overwriting seed rows.

---

### Task 7: Labeler + `POST /rename/dv-sync-labels`

Daemon-thread worker (mirrors `dv-scan-folder`): build a normalized index from `source='scan'` rows, enumerate movies via bulk `lib.all()`, resolve served paths from the already-fetched objects, tie-break `fel>mel>profile8>profile5`, reconcile ONLY within the managed set, per-title try/except, throttle, broadcast progress, ALWAYS `dv:sync_done` in `finally`, back-write `rating_key`, support `dry_run`.

**Files**
- `X:\Docker Apps\ScanHound\backend\rename\dv_labeler.py` (new — pure reconciliation core)
- `X:\Docker Apps\ScanHound\backend\api\routes\rename.py` (add endpoint + request model)
- `X:\Docker Apps\ScanHound\tests\test_dv_labeler.py` (new)

**Interfaces**
- Consumes: `DatabaseManager.get_dv_scans(source='scan')`, `normalize_path` (Task 3), `PlexManager.get_library_section`/`add_label`/`remove_label` (Task 2), `movie.media[*].parts[*].file` (Task 2), `movie.labels`.
- Produces: `MANAGED` set; `desired_label(layer, vocab)`; `pick_layer(paths, index)` (tie-break); `reconcile_movie(movie, index, vocab, pm, *, dry_run) -> {"added":[...],"removed":[...],"matched":bool}`; `sync_labels(db, pm, config, *, dry_run, progress_cb) -> summary`; `POST /rename/dv-sync-labels {dry_run?}`.

Steps:

- [ ] **Write the failing test.** Create `X:\Docker Apps\ScanHound\tests\test_dv_labeler.py`:
  ```python
  from unittest.mock import MagicMock
  from backend.rename.dv_labeler import (
      MANAGED, desired_label, pick_layer, reconcile_movie, build_index)

  VOCAB = {"fel": "DV FEL", "mel": "DV MEL", "profile8": "DV P8", "profile5": "DV P5"}


  def _movie(rk, files, labels):
      mv = MagicMock()
      mv.ratingKey = rk
      lab_objs = []
      for t in labels:
          lo = MagicMock(); lo.tag = t; lab_objs.append(lo)
      mv.labels = lab_objs
      medias = []
      for f in files:
          part = MagicMock(); part.file = f
          m = MagicMock(); m.parts = [part]; medias.append(m)
      mv.media = medias
      return mv


  def test_desired_label_maps_and_ignores_none():
      assert desired_label("fel", VOCAB) == "DV FEL"
      assert desired_label("none", VOCAB) is None
      assert desired_label("unknown", VOCAB) is None
      assert desired_label(None, VOCAB) is None


  def test_pick_layer_tie_break_rank():
      idx = {"y:/a.mkv": "profile5", "y:/b.mkv": "fel", "y:/c.mkv": "mel"}
      assert pick_layer(["y:/a.mkv", "y:/b.mkv", "y:/c.mkv"], idx) == "fel"
      assert pick_layer(["y:/a.mkv", "y:/c.mkv"], idx) == "mel"
      assert pick_layer(["y:/a.mkv"], idx) == "profile5"
      assert pick_layer(["y:/none.mkv"], idx) is None


  def test_reconcile_add_when_none():
      idx = {"y:/a.mkv": "fel"}
      pm = MagicMock()
      mv = _movie(1, ["Y:/a.mkv"], [])
      res = reconcile_movie(mv, idx, VOCAB, pm, dry_run=False)
      assert res["added"] == ["DV FEL"] and res["removed"] == []
      pm.add_label.assert_called_once_with(1, "DV FEL")


  def test_reconcile_swaps_stale_managed():
      idx = {"y:/a.mkv": "fel"}
      pm = MagicMock()
      mv = _movie(1, ["Y:/a.mkv"], ["DV MEL"])
      res = reconcile_movie(mv, idx, VOCAB, pm, dry_run=False)
      assert res["added"] == ["DV FEL"] and res["removed"] == ["DV MEL"]


  def test_reconcile_never_touches_non_managed():
      idx = {"y:/a.mkv": "fel"}
      pm = MagicMock()
      mv = _movie(1, ["Y:/a.mkv"], ["DV Cut", "DV FEL"])  # already correct
      res = reconcile_movie(mv, idx, VOCAB, pm, dry_run=False)
      assert res["added"] == [] and res["removed"] == []   # idempotent
      pm.remove_label.assert_not_called()                  # DV Cut survives


  def test_reconcile_unmatched_removes_stale_managed_only():
      idx = {}  # movie's path not in index
      pm = MagicMock()
      mv = _movie(1, ["Y:/a.mkv"], ["DV FEL", "DV Cut"])
      res = reconcile_movie(mv, idx, VOCAB, pm, dry_run=False)
      assert res["removed"] == ["DV FEL"] and res["added"] == []
      pm.remove_label.assert_called_once_with(1, "DV FEL")  # DV Cut untouched


  def test_reconcile_multipart_tie_break():
      idx = {"y:/a.mkv": "mel", "y:/b.mkv": "fel"}
      pm = MagicMock()
      mv = _movie(1, ["Y:/a.mkv", "Y:/b.mkv"], [])
      res = reconcile_movie(mv, idx, VOCAB, pm, dry_run=False)
      assert res["added"] == ["DV FEL"]  # fel outranks mel


  def test_dry_run_writes_nothing():
      idx = {"y:/a.mkv": "fel"}
      pm = MagicMock()
      mv = _movie(1, ["Y:/a.mkv"], ["DV MEL"])
      res = reconcile_movie(mv, idx, VOCAB, pm, dry_run=True)
      assert res["added"] == ["DV FEL"] and res["removed"] == ["DV MEL"]
      pm.add_label.assert_not_called()
      pm.remove_label.assert_not_called()


  def test_build_index_normalizes():
      rows = [{"path": r"Y:\Movies\A\f.mkv", "dv_layer": "fel"}]
      idx = build_index(rows, mappings=[])
      assert idx == {"y:/movies/a/f.mkv": "fel"}
  ```

- [ ] **Run it — expect failure.**
  ```
  python -m pytest tests/test_dv_labeler.py -v
  ```
  Expected: `ModuleNotFoundError: No module named 'backend.rename.dv_labeler'`.

- [ ] **Minimal impl — reconciliation core.** Create `X:\Docker Apps\ScanHound\backend\rename\dv_labeler.py`:
  ```python
  """DV Plex labeler: reconcile a CLOSED managed label set against dv_scan.

  Reconciles ONLY within {DV FEL, DV MEL, DV P8, DV P5} — never a 'DV ' prefix
  wildcard (that deleted user labels like 'DV Cut'). Uses the bulk lib.all()
  objects already in memory; no per-movie fetchItem for path resolution.
  """
  import json
  import logging
  import time

  from backend.rename.dv_paths import normalize_path

  logger = logging.getLogger(__name__)

  MANAGED = {"DV FEL", "DV MEL", "DV P8", "DV P5"}

  # highest-first preference when a title's parts disagree
  _LAYER_RANK = ["fel", "mel", "profile8", "profile5"]

  _THROTTLE_S = 0.05  # inter-write pause so a big library can't hammer Plex


  def desired_label(layer, vocab):
      """Map a dv_layer to its managed label, or None for none/unknown/NULL."""
      if not layer or layer in ("none", "unknown"):
          return None
      label = vocab.get(layer)
      return label if label in MANAGED else None


  def pick_layer(norm_paths, index):
      """Best layer among a movie's candidate normalized paths (rank fel>mel>p8>p5)."""
      found = [index[p] for p in norm_paths if p in index]
      for rank in _LAYER_RANK:
          if rank in found:
              return rank
      return found[0] if found else None


  def build_index(rows, mappings=None):
      """{normalize_path(path) -> dv_layer} from scan-source rows."""
      idx = {}
      for r in rows:
          p = normalize_path(r.get("path"), mappings)
          if p:
              idx[p] = r.get("dv_layer")
      return idx


  def _movie_norm_paths(movie, mappings):
      paths = []
      for media in (movie.media or []):
          for part in (media.parts or []):
              f = getattr(part, "file", None)
              if f:
                  paths.append(normalize_path(f, mappings))
      return paths


  def _existing_labels(movie):
      out = set()
      for lab in (getattr(movie, "labels", None) or []):
          tag = getattr(lab, "tag", None) or (lab if isinstance(lab, str) else None)
          if tag:
              out.add(tag)
      return out


  def reconcile_movie(movie, index, vocab, pm, *, dry_run=False, mappings=None):
      """Reconcile one movie's managed label. Returns {added, removed, matched}."""
      norm_paths = _movie_norm_paths(movie, mappings)
      layer = pick_layer(norm_paths, index)
      desired = desired_label(layer, vocab)
      existing_managed = _existing_labels(movie) & MANAGED

      added, removed = [], []
      if desired and desired not in existing_managed:
          added.append(desired)
      for stale in existing_managed - ({desired} if desired else set()):
          removed.append(stale)

      if not dry_run:
          for lbl in added:
              try:
                  pm.add_label(movie.ratingKey, lbl)
              except Exception as e:
                  logger.warning("add_label %s on %s failed: %s", lbl, movie.ratingKey, e)
          for lbl in removed:
              try:
                  pm.remove_label(movie.ratingKey, lbl)
              except Exception as e:
                  logger.warning("remove_label %s on %s failed: %s", lbl, movie.ratingKey, e)
          if added or removed:
              time.sleep(_THROTTLE_S)

      return {"added": added, "removed": removed, "matched": layer is not None}


  def _vocab_from_config(config):
      raw = config.get("dv_label_vocab") or "{}"
      try:
          v = json.loads(raw)
          return {k: val for k, val in v.items() if val in MANAGED}
      except (ValueError, TypeError):
          return {"fel": "DV FEL", "mel": "DV MEL", "profile8": "DV P8", "profile5": "DV P5"}


  def sync_labels(db, pm, config, *, dry_run=False, progress_cb=None, mappings=None):
      """Reconcile every movie against dv_scan (source='scan'). Returns a summary."""
      vocab = _vocab_from_config(config)
      rows = db.get_dv_scans(source="scan", limit=1000000)
      index = build_index(rows, mappings)

      movie_libs = (config.get("movie_libs")
                    or config.get("known_movie_libraries") or [])
      seen = set()
      movies = []
      for lib_name in movie_libs:
          lib = pm.get_library_section(lib_name)
          if not lib:
              continue
          for mv in lib.all():
              if mv.ratingKey in seen:
                  continue
              seen.add(mv.ratingKey)
              movies.append(mv)

      total = len(movies)
      added_n = removed_n = matched_n = 0
      for i, mv in enumerate(movies):
          try:
              res = reconcile_movie(mv, index, vocab, pm,
                                    dry_run=dry_run, mappings=mappings)
              added_n += len(res["added"])
              removed_n += len(res["removed"])
              if res["matched"]:
                  matched_n += 1
                  if not dry_run:
                      # O(1) rating_key back-write for the matched copy
                      for p in _movie_norm_paths(mv, mappings):
                          if p in index:
                              db.upsert_dv_scan(
                                  _row_path_for(rows, p, mappings) or p,
                                  index[p], rating_key=str(mv.ratingKey),
                                  source="scan")
                              break
          except Exception as e:
              logger.warning("dv sync: title %s failed: %s",
                             getattr(mv, "title", "?"), e)
          if progress_cb:
              progress_cb(i + 1, total)

      return {"total": total, "added": added_n, "removed": removed_n,
              "matched": matched_n, "dry_run": dry_run}


  def _row_path_for(rows, norm, mappings):
      """Recover the original dv_scan path whose normalize == *norm* (back-write key)."""
      for r in rows:
          if normalize_path(r.get("path"), mappings) == norm:
              return r.get("path")
      return None
  ```

- [ ] **Run it — expect pass.**
  ```
  python -m pytest tests/test_dv_labeler.py -v
  ```
  Expected: `9 passed`.

- [ ] **Add the endpoint (daemon thread + finally-guaranteed done).** In `X:\Docker Apps\ScanHound\backend\api\routes\rename.py`, import `from backend.rename import dv_labeler` at top, add a request model:
  ```python
  class DvSyncRequest(BaseModel):
      dry_run: bool = False
  ```
  and the endpoint (mirrors `dv-scan-folder`):
  ```python
  @router.post("/dv-sync-labels")
  def dv_sync_labels(req: DvSyncRequest, reg: ServiceRegistry = Depends(get_registry)):
      """Reconcile managed DV labels on every movie against dv_scan (source='scan').
      Runs in the background; streams dv:sync_progress and ALWAYS emits dv:sync_done."""
      svc = _service(reg)
      dry_run = bool(req.dry_run)
      if reg.db is None:
          raise HTTPException(status_code=503, detail="DB not initialized")
      pm = getattr(svc, "plex_manager", None) or getattr(reg, "_plex_service", None)
      plex_manager = getattr(reg._plex_service, "plex_manager", None) if reg._plex_service else None
      if plex_manager is None:
          raise HTTPException(status_code=503, detail="Plex not initialized")

      def _run():
          try:
              def _progress(done, total):
                  ws_manager.broadcast_sync({"type": "dv:sync_progress", "data": {
                      "done": done, "total": total}})
              result = dv_labeler.sync_labels(
                  reg.db, plex_manager, reg.config,
                  dry_run=dry_run, progress_cb=_progress)
              ws_manager.broadcast_sync({"type": "notification", "data": {
                  "title": "Dolby Vision label sync",
                  "body": (f"{result['matched']} matched, "
                           f"{result['added']} added, {result['removed']} removed"
                           f"{' (dry run)' if dry_run else ''}"),
                  "priority": "normal"}})
          except Exception as e:
              logger.exception("dv-sync-labels failed")
              ws_manager.broadcast_sync({"type": "notification", "data": {
                  "title": "Dolby Vision label sync failed",
                  "body": str(e), "priority": "high"}})
              result = {"error": str(e)}
          finally:
              ws_manager.broadcast_sync({"type": "dv:sync_done", "data": result})

      threading.Thread(target=_run, name="dv-sync-labels", daemon=True).start()
      return {"status": "started", "dry_run": dry_run}
  ```

- [ ] **Add an endpoint + finally test.** Append to `tests/test_dv_labeler.py`:
  ```python
  def test_sync_labels_finally_emits_done_on_plex_failure(monkeypatch):
      from backend.rename import dv_labeler as L

      class _DB:
          def get_dv_scans(self, **kw): return [{"path": "Y:/a.mkv", "dv_layer": "fel"}]
          def upsert_dv_scan(self, *a, **k): return True

      class _PM:
          def get_library_section(self, name):
              raise RuntimeError("plex dropped")

      # should NOT raise; per-lib failure is swallowed -> empty movie set
      res = L.sync_labels(_DB(), _PM(), {"movie_libs": ["Movies"]}, dry_run=True)
      assert res["total"] == 0 and res["matched"] == 0


  def test_sync_labels_dry_run_no_writes():
      from backend.rename import dv_labeler as L
      from unittest.mock import MagicMock

      class _DB:
          def get_dv_scans(self, **kw): return [{"path": "Y:/a.mkv", "dv_layer": "fel"}]
          upsert_dv_scan = MagicMock(return_value=True)

      pm = MagicMock()
      lib = MagicMock()
      mv = _movie(1, ["Y:/a.mkv"], ["DV MEL"])
      lib.all.return_value = [mv]
      pm.get_library_section.return_value = lib
      db = _DB()
      res = L.sync_labels(db, pm, {"movie_libs": ["Movies"]}, dry_run=True)
      assert res["added"] == 1 and res["removed"] == 1
      pm.add_label.assert_not_called()
      db.upsert_dv_scan.assert_not_called()  # no back-write in dry_run
  ```

- [ ] **Run the file — expect pass.**
  ```
  python -m pytest tests/test_dv_labeler.py -v
  ```
  Expected: `11 passed`.

- [ ] **Commit.**
  ```
  git add backend/rename/dv_labeler.py backend/api/routes/rename.py tests/test_dv_labeler.py
  git commit -m "rename: DV labeler + POST /dv-sync-labels (managed-set reconcile, dry_run, finally done)"
  ```

**Deliverable:** `POST /rename/dv-sync-labels` reconciles the closed managed set with tie-break, dry-run, per-title isolation, and a `finally`-guaranteed `dv:sync_done`.

---

### Task 8: Host detector script `scripts/host-detector/dv_host_scan.py`

A HOST artifact (NOT in the image): reads `data/dv_host.json` (NOT `config.py`), opens its OWN `dv_host.db` (standalone sqlite, no `DatabaseManager`), walks roots, signature-skips at tolerance `>=2.0s`, reuses `dv_detect.detect_layer`, upserts, optional `mkvpropedit` tag + post-tag re-stat/re-upsert, then POSTs `/rename/dv-import`. Tests exercise its pure functions.

**Files**
- `X:\Docker Apps\ScanHound\scripts\host-detector\dv_host_scan.py` (new)
- `X:\Docker Apps\ScanHound\tests\test_dv_host_scan.py` (new)

**Interfaces**
- Consumes: `data/dv_host.json` (`{dv_library_roots, dv_detection, dv_file_tagging, dv_label_vocab}`), `dv_detect.detect_layer`, `dovi_tool.exe`/`mkvpropedit.exe` on PATH, `os.stat`.
- Produces (pure, tested): `DV_MTIME_TOL=2.0`; `sig_is_current(stored_mtime, stored_size, st_mtime, st_size, tol=DV_MTIME_TOL) -> bool`; `classify_to_row(path, layer, st) -> dict`; `tag_name_for(layer) -> str|None`; `load_host_config(path) -> dict`; `should_run(cfg) -> bool`; `parse_roots(cfg) -> list[str]`. Plus a `main()` that walks, upserts `dv_host.db`, tags, and POSTs `/rename/dv-import`.

Steps:

- [ ] **Write the failing test.** Create `X:\Docker Apps\ScanHound\tests\test_dv_host_scan.py`:
  ```python
  import importlib.util
  import os
  import types

  HERE = os.path.dirname(__file__)
  SCRIPT = os.path.abspath(os.path.join(
      HERE, "..", "scripts", "host-detector", "dv_host_scan.py"))


  def _load():
      spec = importlib.util.spec_from_file_location("dv_host_scan", SCRIPT)
      mod = importlib.util.module_from_spec(spec)
      spec.loader.exec_module(mod)
      return mod


  def _stat(mtime, size):
      s = types.SimpleNamespace()
      s.st_mtime = mtime
      s.st_size = size
      return s


  def test_signature_skip_2s_boundary():
      m = _load()
      assert m.DV_MTIME_TOL >= 2.0
      # within tolerance + same size -> current (skip)
      assert m.sig_is_current(100.0, 5000, 101.9, 5000) is True
      # 2.0s exactly is within (<=)
      assert m.sig_is_current(100.0, 5000, 102.0, 5000) is True
      # beyond tolerance -> not current
      assert m.sig_is_current(100.0, 5000, 103.0, 5000) is False
      # size mismatch always rescans
      assert m.sig_is_current(100.0, 5000, 100.0, 5001) is False
      # NULL stored signature always rescans
      assert m.sig_is_current(None, 5000, 100.0, 5000) is False
      assert m.sig_is_current(100.0, None, 100.0, 5000) is False


  def test_classify_to_row():
      m = _load()
      st = _stat(123.5, 9999)
      row = m.classify_to_row("Y:/M/a.mkv", "fel", st)
      assert row["path"] == "Y:/M/a.mkv"
      assert row["dv_layer"] == "fel"
      assert row["sig_mtime"] == 123.5
      assert row["sig_size"] == 9999
      # unknown -> NULL mtime so the next run retries
      row2 = m.classify_to_row("Y:/M/b.mkv", "unknown", st)
      assert row2["sig_mtime"] is None


  def test_tag_name_map():
      m = _load()
      assert m.tag_name_for("fel") == "Dolby Vision Profile 7 FEL"
      assert m.tag_name_for("mel") == "Dolby Vision Profile 7 MEL"
      assert m.tag_name_for("profile8") == "Dolby Vision Profile 8"
      assert m.tag_name_for("profile5") == "Dolby Vision Profile 5"
      assert m.tag_name_for("none") is None
      assert m.tag_name_for("unknown") is None


  def test_should_run_config_gates(tmp_path):
      m = _load()
      # detection off -> no-op
      assert m.should_run({"dv_detection": False, "dv_library_roots": "Y:/M"}) is False
      # detection on but no roots -> no-op
      assert m.should_run({"dv_detection": True, "dv_library_roots": ""}) is False
      # detection on + roots -> run
      assert m.should_run({"dv_detection": True, "dv_library_roots": "Y:/M"}) is True


  def test_load_host_config_missing(tmp_path):
      m = _load()
      cfg = m.load_host_config(str(tmp_path / "nope.json"))
      assert cfg == {}


  def test_parse_roots_splits_semicolon_and_newline():
      m = _load()
      cfg = {"dv_library_roots": "Y:\\M ; E:\\4K\n\\\\SRV\\Share"}
      roots = m.parse_roots(cfg)
      assert roots == ["Y:\\M", "E:\\4K", "\\\\SRV\\Share"]
  ```

- [ ] **Run it — expect failure.**
  ```
  python -m pytest tests/test_dv_host_scan.py -v
  ```
  Expected: `FileNotFoundError`/`spec.loader` error — the script doesn't exist yet.

- [ ] **Minimal impl.** Create `X:\Docker Apps\ScanHound\scripts\host-detector\dv_host_scan.py`:
  ```python
  """ScanHound DV host detector (HOST artifact — NOT in the Docker image).

  Runs on TurtleLandSRVR (.170) where dovi_tool.exe reaches both local drives and
  the .180 SMB media. Reads data/dv_host.json (NOT config.py), keeps its OWN
  standalone dv_host.db (raw sqlite3 — it must NEVER open crawler.db or construct
  DatabaseManager, which runs DDL), reuses dv_detect.detect_layer, optionally tags
  MKVs with mkvpropedit, then POSTs /rename/dv-import so the container ingests it.

  Usage (Task Scheduler action, with dovi_tool.exe's dir on PATH):
      python scripts\\host-detector\\dv_host_scan.py \\
          --config data\\dv_host.json --db scripts\\host-detector\\dv_host.db \\
          --api http://localhost:9721
  """
  import argparse
  import json
  import logging
  import os
  import re
  import shutil
  import sqlite3
  import subprocess
  import sys
  import urllib.request

  # Make backend.rename.dv_detect importable when run from repo root.
  sys.path.insert(0, os.path.abspath(
      os.path.join(os.path.dirname(__file__), "..", "..")))
  from backend.rename import dv_detect  # noqa: E402

  logging.basicConfig(level=logging.INFO,
                      format="%(asctime)s %(levelname)s %(message)s")
  logger = logging.getLogger("dv_host_scan")

  DV_MTIME_TOL = 2.0  # >= FAT/exFAT 2s granularity — below this = endless rescans

  _TAG_NAMES = {
      "fel": "Dolby Vision Profile 7 FEL",
      "mel": "Dolby Vision Profile 7 MEL",
      "profile8": "Dolby Vision Profile 8",
      "profile5": "Dolby Vision Profile 5",
  }


  # ── pure helpers (unit-tested) ──────────────────────────────────────────
  def load_host_config(path):
      """Read data/dv_host.json. Missing/invalid -> {} (caller no-ops)."""
      try:
          with open(path, encoding="utf-8") as f:
              return json.load(f)
      except (OSError, ValueError):
          return {}


  def parse_roots(cfg):
      """Split dv_library_roots on ';' and newlines; trim; drop empties."""
      raw = cfg.get("dv_library_roots") or ""
      parts = re.split(r"[;\n]", raw)
      return [p.strip() for p in parts if p.strip()]


  def should_run(cfg):
      """True only when detection is enabled AND at least one root is configured."""
      return bool(cfg.get("dv_detection")) and bool(parse_roots(cfg))


  def sig_is_current(stored_mtime, stored_size, st_mtime, st_size,
                     tol=DV_MTIME_TOL):
      """Whether a stored signature still matches the file (skip re-scan).

      A NULL stored component never matches. Size must match exactly; mtime within
      *tol* (>=2.0s to absorb FAT/exFAT granularity)."""
      if stored_mtime is None or stored_size is None:
          return False
      try:
          return (abs(float(stored_mtime) - float(st_mtime)) <= tol
                  and int(stored_size) == int(st_size))
      except (TypeError, ValueError):
          return False


  def classify_to_row(path, layer, st):
      """Build a dv_host.db row. 'unknown' stores NULL mtime so the next run retries."""
      unknown = layer in ("unknown", None)
      return {
          "path": path,
          "dv_layer": layer,
          "sig_mtime": None if unknown else float(st.st_mtime),
          "sig_size": None if unknown else int(st.st_size),
      }


  def tag_name_for(layer):
      """MKV track-name string for a layer, or None when no tag applies."""
      return _TAG_NAMES.get(layer)


  # ── db (own standalone sqlite — NOT DatabaseManager) ────────────────────
  def _open_db(db_path):
      conn = sqlite3.connect(db_path)
      conn.row_factory = sqlite3.Row
      conn.execute("PRAGMA journal_mode=WAL")
      conn.execute('''
          CREATE TABLE IF NOT EXISTS dv_host (
              path TEXT PRIMARY KEY,
              dv_layer TEXT,
              sig_mtime REAL,
              sig_size INTEGER,
              title TEXT,
              scanned_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
          )''')
      conn.commit()
      return conn


  def _get_sig(conn, path):
      row = conn.execute(
          "SELECT sig_mtime, sig_size FROM dv_host WHERE path = ?", (path,)).fetchone()
      return (row["sig_mtime"], row["sig_size"]) if row else (None, None)


  def _upsert(conn, row):
      conn.execute('''
          INSERT INTO dv_host (path, dv_layer, sig_mtime, sig_size, scanned_at)
          VALUES (:path, :dv_layer, :sig_mtime, :sig_size, CURRENT_TIMESTAMP)
          ON CONFLICT(path) DO UPDATE SET
              dv_layer = excluded.dv_layer,
              sig_mtime = excluded.sig_mtime,
              sig_size = excluded.sig_size,
              scanned_at = CURRENT_TIMESTAMP
      ''', row)
      conn.commit()


  def _tag_file(path, layer):
      """mkvpropedit track-name tag for MKV. Returns True on a successful write."""
      name = tag_name_for(layer)
      if not name or not path.lower().endswith(".mkv"):
          return False
      exe = shutil.which("mkvpropedit")
      if not exe:
          logger.warning("mkvpropedit not on PATH — skipping tag for %s", path)
          return False
      try:
          subprocess.run(
              [exe, path, "--edit", "track:v1", "--set", f"name={name}"],
              check=True, capture_output=True, timeout=300)
          return True
      except (subprocess.SubprocessError, OSError) as e:
          logger.warning("mkvpropedit failed on %s: %s", path, e)
          return False


  def _post_import(api_base):
      url = api_base.rstrip("/") + "/api/rename/dv-import"
      req = urllib.request.Request(url, data=b"{}",
                                   headers={"Content-Type": "application/json"},
                                   method="POST")
      try:
          with urllib.request.urlopen(req, timeout=120) as resp:
              logger.info("dv-import -> %s", resp.read().decode("utf-8", "replace"))
      except OSError as e:
          logger.error("dv-import POST failed: %s", e)


  def _iter_files(roots):
      exts = dv_detect._SUPPORTED_EXTS
      for root in roots:
          for dirpath, _dirs, files in os.walk(root):
              for fn in files:
                  if os.path.splitext(fn)[1].lower() in exts:
                      yield os.path.join(dirpath, fn)


  def main(argv=None):
      ap = argparse.ArgumentParser()
      ap.add_argument("--config", default="data/dv_host.json")
      ap.add_argument("--db", default="scripts/host-detector/dv_host.db")
      ap.add_argument("--api", default="http://localhost:9721")
      args = ap.parse_args(argv)

      cfg = load_host_config(args.config)
      if not should_run(cfg):
          logger.info("dv_detection off or no roots — nothing to do")
          return 0
      if not dv_detect.available():
          logger.error("dovi_tool not on PATH — aborting (nothing written)")
          return 1

      tagging = bool(cfg.get("dv_file_tagging"))
      conn = _open_db(args.db)
      scanned = 0
      for path in _iter_files(parse_roots(cfg)):
          try:
              st = os.stat(path)
          except OSError:
              continue
          stored_m, stored_s = _get_sig(conn, path)
          if sig_is_current(stored_m, stored_s, st.st_mtime, st.st_size):
              continue
          layer = dv_detect.detect_layer(path).get("layer")
          _upsert(conn, classify_to_row(path, layer, st))
          scanned += 1
          if tagging and _tag_file(path, layer):
              st2 = os.stat(path)  # header rewrite bumped mtime/size
              _upsert(conn, classify_to_row(path, layer, st2))
      conn.close()
      logger.info("scanned %d file(s); posting dv-import", scanned)
      _post_import(args.api)
      return 0


  if __name__ == "__main__":
      raise SystemExit(main())
  ```

- [ ] **Run it — expect pass.**
  ```
  python -m pytest tests/test_dv_host_scan.py -v
  ```
  Expected: `6 passed`.

- [ ] **Guard: script does NOT import `DatabaseManager`.** Append to `tests/test_dv_host_scan.py`:
  ```python
  def test_script_never_imports_database_manager():
      with open(SCRIPT, encoding="utf-8") as f:
          src = f.read()
      assert "DatabaseManager" not in src
      assert "crawler.db" not in src
  ```

- [ ] **Run the file — expect pass.**
  ```
  python -m pytest tests/test_dv_host_scan.py -v
  ```
  Expected: `7 passed`.

- [ ] **Full DV-suite regression.**
  ```
  python -m pytest tests/test_dv_scan_source_filter.py tests/test_dv_label_writepath.py tests/test_dv_paths.py tests/test_dv_settings.py tests/test_dv_host_export.py tests/test_dv_import.py tests/test_dv_labeler.py tests/test_dv_host_scan.py -v
  ```
  Expected: all pass.

- [ ] **Commit.**
  ```
  git add scripts/host-detector/dv_host_scan.py tests/test_dv_host_scan.py
  git commit -m "host-detector: standalone dv_host_scan.py (own dv_host.db, sig-skip>=2s, tag, POST /dv-import)"
  ```

**Deliverable:** a standalone host detector script with tested pure functions (2s signature boundary, classify→row, tag map, config gates) that owns its `dv_host.db`, never touches `crawler.db`/`DatabaseManager`, and POSTs `/rename/dv-import`.

---

### Task 9: Frontend API client + DV sync stores

Add the two new client methods (`dvImport`, `dvSyncLabels`) that consume `POST /rename/dv-import` and `POST /rename/dv-sync-labels`, plus the `dvSyncRunning`/`dvSyncProgress`/`dvSyncResult` stores wired to the `dv:sync_progress`/`dv:sync_done` WebSocket events — mirroring the existing `dvScan*` stores exactly.

**Files**
- `X:\Docker Apps\ScanHound\frontend\src\lib\api\client.ts` (edit — add two methods)
- `X:\Docker Apps\ScanHound\frontend\src\lib\stores\renames.ts` (edit — add stores + WS handlers)
- `X:\Docker Apps\ScanHound\frontend\src\lib\stores\renames.test.ts` (new — vitest unit test for the store wiring)

**Interfaces**
- Consumes: `POST /rename/dv-import` → `{ imported: number; updated: number }`; `POST /rename/dv-sync-labels` body `{ dry_run?: boolean }` → `{ status: string }`; WS events `dv:sync_progress` (`{ done, total, title }`) and `dv:sync_done` (`{ added, removed, unmatched, dry_run?, error? }`).
- Produces: `api.dvImport()`, `api.dvSyncLabels(dry_run?)`; stores `dvSyncRunning`, `dvSyncProgress`, `dvSyncResult`; types `DvSyncProgress`, `DvSyncResult`.

**Steps**

- [ ] Write a failing vitest that imports the store module and asserts the new stores exist and that a simulated `dv:sync_done` event resets `dvSyncRunning` to `false` and populates `dvSyncResult`. Create `X:\Docker Apps\ScanHound\frontend\src\lib\stores\renames.test.ts`:

```ts
import { describe, it, expect, vi, beforeEach } from 'vitest';

// The store module registers connection.on(...) handlers at import time, so we
// must stub ./connection BEFORE importing the store. We capture every handler
// keyed by event name so the test can invoke them directly.
const handlers: Record<string, (data: unknown) => void> = {};
vi.mock('$lib/stores/connection', () => ({
  connection: { on: (event: string, cb: (data: unknown) => void) => { handlers[event] = cb; } }
}));
vi.mock('$lib/api/client', () => ({
  api: { getDvScans: vi.fn().mockResolvedValue({ scans: [], counts: {} }) }
}));

describe('DV sync stores', () => {
  beforeEach(() => { for (const k of Object.keys(handlers)) delete handlers[k]; });

  it('registers dv:sync_progress and dv:sync_done handlers', async () => {
    await import('./renames');
    expect(typeof handlers['dv:sync_progress']).toBe('function');
    expect(typeof handlers['dv:sync_done']).toBe('function');
  });

  it('dv:sync_done clears dvSyncRunning and stores the result', async () => {
    const mod = await import('./renames');
    const { get } = await import('svelte/store');
    mod.dvSyncRunning.set(true);
    handlers['dv:sync_done']({ added: 1, removed: 0, unmatched: 2 });
    expect(get(mod.dvSyncRunning)).toBe(false);
    expect(get(mod.dvSyncResult)).toEqual({ added: 1, removed: 0, unmatched: 2 });
    expect(get(mod.dvSyncProgress)).toBeNull();
  });

  it('dv:sync_progress updates dvSyncProgress', async () => {
    const mod = await import('./renames');
    const { get } = await import('svelte/store');
    handlers['dv:sync_progress']({ done: 3, total: 10, title: 'Dune' });
    expect(get(mod.dvSyncProgress)).toEqual({ done: 3, total: 10, title: 'Dune' });
  });
});
```

- [ ] Run it and confirm it fails (module has no `dvSyncRunning`/`dvSyncResult`/`dvSyncProgress` exports yet). From `X:\Docker Apps\ScanHound\frontend`:

```
npm run test:unit -- src/lib/stores/renames.test.ts
```

Expected: vitest reports the file with failing tests, e.g. `dvSyncRunning` is `undefined` → `TypeError: Cannot read properties of undefined (reading 'set')` and the handler-registration assertions fail (`expected 'undefined' to be 'function'`). Overall: `Test Files 1 failed`.

- [ ] Add the client methods. In `X:\Docker Apps\ScanHound\frontend\src\lib\api\client.ts`, insert immediately after the `getDvScans` method (after the block ending at line 406, before the closing `};` of the object):

```ts
  dvImport: () =>
    request<{ imported: number; updated: number }>('/rename/dv-import', {
      method: 'POST',
      body: JSON.stringify({})
    }),
  dvSyncLabels: (dryRun = false) =>
    request<{ status: string }>('/rename/dv-sync-labels', {
      method: 'POST',
      body: JSON.stringify({ dry_run: dryRun })
    }),
```

- [ ] Add the stores + WS wiring. In `X:\Docker Apps\ScanHound\frontend\src\lib\stores\renames.ts`, insert immediately after the existing `dv:scan_done` handler block (after line 248):

```ts
// ── Dolby Vision label sync (mirrors the DV scan stores above) ────────
export interface DvSyncProgress { done: number; total: number; title: string; }
export interface DvSyncResult {
  added: number; removed: number; unmatched: number;
  dry_run?: boolean; error?: string;
}
/** True from dispatch of a label sync until its dv:sync_done arrives —
 *  drives the "Sync Plex labels" button's disabled state. */
export const dvSyncRunning = writable<boolean>(false);
/** Live per-title progress of a running label sync (null when idle). */
export const dvSyncProgress = writable<DvSyncProgress | null>(null);
/** Summary of the last completed label sync. */
export const dvSyncResult = writable<DvSyncResult | null>(null);

connection.on('dv:sync_progress', (data) => {
  dvSyncProgress.set(data as unknown as DvSyncProgress);
});
connection.on('dv:sync_done', (data) => {
  dvSyncResult.set(data as unknown as DvSyncResult);
  dvSyncProgress.set(null);
  dvSyncRunning.set(false);
});
```

- [ ] Run the unit test again and confirm it passes. From `X:\Docker Apps\ScanHound\frontend`:

```
npm run test:unit -- src/lib/stores/renames.test.ts
```

Expected: `Test Files 1 passed`, `Tests 3 passed`.

- [ ] Run the type-check to confirm the new client methods and store types compile. From `X:\Docker Apps\ScanHound\frontend`:

```
npm run check
```

Expected: `svelte-check` completes with `0 errors` (warnings unchanged from baseline).

- [ ] Commit.

```
git add frontend/src/lib/api/client.ts frontend/src/lib/stores/renames.ts frontend/src/lib/stores/renames.test.ts
git commit -m "$(cat <<'EOF'
DV: add dvImport/dvSyncLabels client + dv sync stores

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
EOF
)"
```

---

### Task 10: DV panel UI — inventory + Sync Plex labels button

Turn the collapsed `#dv-scan-surface` panel into an inventory-first surface: it already shows the folder scan; add a "Sync Plex labels" section wired to the Task 9 stores that dispatches a sync, disables while running, streams progress, and on completion renders the `added/removed/unmatched` summary. The inventory counts already come from `dvCounts` (scan-source per the backend contract).

**Files**
- `X:\Docker Apps\ScanHound\frontend\src\routes\renames\+page.svelte` (edit — imports, a `dvSync()` handler, markup block)

**Interfaces**
- Consumes: stores `dvSyncRunning`, `dvSyncProgress`, `dvSyncResult`, `dvCounts` (from `$lib/stores/renames`); `api.dvSyncLabels()` (from `$lib/api/client`); `addToast` (existing import in the file).
- Produces: user-visible "Sync Plex labels" button + progress/result UI inside `#dv-scan-surface`.

**Steps**

- [ ] Extend the store import. In `X:\Docker Apps\ScanHound\frontend\src\routes\renames\+page.svelte`, replace the import member list on line 9 (`dvScanProgress, dvScanResult, dvScans, dvCounts, dvScanRunning`) with:

```svelte
    dvScanProgress, dvScanResult, dvScans, dvCounts, dvScanRunning,
    dvSyncRunning, dvSyncProgress, dvSyncResult
```

- [ ] Add the `dvSync()` handler. In the same file, immediately after the `dvScan()` function (after its closing brace on line 104), insert:

```svelte
  // --- Dolby Vision Plex label sync ---
  // The POST returns immediately; we stay "running" until dv:sync_done arrives
  // (the store's WS handler flips dvSyncRunning back to false — success or error).
  async function dvSync() {
    if ($dvSyncRunning) return; // guard: one sync at a time
    dvSyncRunning.set(true);
    dvSyncResult.set(null);
    try {
      await api.dvSyncLabels(false);
      addToast('Dolby Vision', 'Syncing Plex labels — matching detected layers to the copy Plex serves.');
    } catch (e) {
      dvSyncRunning.set(false);
      addToast('Error', e instanceof Error ? e.message : 'Failed to start label sync', 'error');
    }
  }
```

- [ ] Add the sync UI. In the same file, inside the `{#if dvOpen}` body, immediately before its closing `</div>` that ends the panel body (i.e. after the inventory/scan-list block ending on line 377, before line 378's `</div>`), insert:

```svelte
        <div class="mt-3 pt-3 border-t border-[var(--border)]">
          <div class="flex items-center gap-2 flex-wrap">
            <button
              onclick={dvSync}
              disabled={$dvSyncRunning}
              class="px-3 py-1.5 text-sm rounded-lg bg-[var(--accent)] hover:opacity-90 text-white font-medium transition disabled:opacity-50"
            >{$dvSyncRunning ? 'Syncing…' : 'Sync Plex labels'}</button>
            <span class="text-xs text-[var(--text-secondary)]">
              Applies <code>DV FEL/MEL/P8/P5</code> to the exact copy Plex serves. Only these four labels are managed — your own labels are never touched.
            </span>
          </div>
          {#if $dvSyncProgress}
            <div class="mt-2 text-xs text-[var(--text-secondary)]">
              Matching {$dvSyncProgress.done}/{$dvSyncProgress.total}:
              <span class="font-mono">{$dvSyncProgress.title}</span>
            </div>
          {/if}
          {#if $dvSyncResult && !$dvSyncProgress}
            <div class="mt-2 text-xs">
              {#if $dvSyncResult.error}
                <span class="text-[var(--error)]">{$dvSyncResult.error}</span>
              {:else}
                Labeled: <strong>{$dvSyncResult.added}</strong> added, {$dvSyncResult.removed} removed, {$dvSyncResult.unmatched} unmatched.
              {/if}
            </div>
          {/if}
        </div>
```

- [ ] Type-check. From `X:\Docker Apps\ScanHound\frontend`:

```
npm run check
```

Expected: `0 errors` (the `$dvSync*` auto-subscriptions and `api.dvSyncLabels` resolve against the Task 9 additions).

- [ ] Build to confirm the route compiles end-to-end. From `X:\Docker Apps\ScanHound\frontend`:

```
npm run build
```

Expected: `vite build` finishes with `✓ built in …` and no errors (the `renames` route chunk emits).

- [ ] Commit.

```
git add frontend/src/routes/renames/+page.svelte
git commit -m "$(cat <<'EOF'
DV: add Sync Plex labels button + progress/result to DV panel

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
EOF
)"
```

---

### Task 11: Settings UI + Settings type — the four DV settings (and the missing 4K field)

Add the four DV settings inputs (`dv_library_roots`, `dv_detection`, `dv_file_tagging`, `dv_label_vocab`) in a new "Dolby Vision" group on the Renaming tab, bound to the `settings` store with the existing `settings.update(...)` idiom. Add the corresponding optional fields to the `Settings` TypeScript interface — including `auto_rename_movie_library_4k`, which the type already has (line 359) but which is the front-of-house counterpart to the backend `SettingsUpdate` fix in the backend half.

**Files**
- `X:\Docker Apps\ScanHound\frontend\src\lib\api\types.ts` (edit — add DV fields to `Settings`)
- `X:\Docker Apps\ScanHound\frontend\src\routes\settings\+page.svelte` (edit — add the "Dolby Vision" group)

**Interfaces**
- Consumes: `settings` store (`$lib/stores/settings`); shared `inputClass` / `inputSmClass` (declared at `settings/+page.svelte:204-205`).
- Produces: `Settings.dv_library_roots?`, `Settings.dv_detection?`, `Settings.dv_file_tagging?`, `Settings.dv_label_vocab?`; four bound inputs. These feed `PUT /settings` → the backend `SettingsUpdate` keys `dv_library_roots`/`dv_detection`/`dv_file_tagging`/`dv_label_vocab`.

**Steps**

- [ ] Add the types. In `X:\Docker Apps\ScanHound\frontend\src\lib\api\types.ts`, immediately after the `ollama_model?: string;` line (line 367), insert:

```ts
  // Dolby Vision host detector + Plex labeling
  dv_library_roots?: string;
  dv_detection?: boolean;
  dv_file_tagging?: boolean;
  dv_label_vocab?: string;
```

- [ ] Add the "Dolby Vision" settings group. In `X:\Docker Apps\ScanHound\frontend\src\routes\settings\+page.svelte`, on the Renaming tab, insert a new group immediately after the auto-rename group's closing markup and before the tab's closing container. Locate the last `</label>` block on the Renaming tab (the one that closes the auto-rename fields, in the region beginning at line 1381) and insert this block right after it:

```svelte
          <div class="mt-6 pt-4 border-t border-[var(--border)]">
            <h3 class="text-sm font-semibold mb-1">Dolby Vision</h3>
            <p class="text-xs text-[var(--text-secondary)] mb-3">
              Host-side FEL/MEL detection feeding per-copy Plex labels (DV FEL / DV MEL / DV P8 / DV P5) for Kometa badges.
            </p>

            <label class="flex items-center gap-3">
              <input type="checkbox" checked={$settings.dv_detection ?? false}
                onchange={(e) => settings.update((s) => ({ ...s, dv_detection: e.currentTarget.checked }))}
                class="accent-[var(--accent)]" />
              <span class="text-sm font-medium">Enable Dolby Vision detection</span>
            </label>

            <label class="flex items-center gap-3 mt-3">
              <input type="checkbox" checked={$settings.dv_file_tagging ?? false}
                onchange={(e) => settings.update((s) => ({ ...s, dv_file_tagging: e.currentTarget.checked }))}
                class="accent-[var(--accent)]" />
              <span class="text-sm font-medium">Tag MKV track name with the detected layer</span>
            </label>

            <label class="block mt-3">
              <span class="text-sm text-[var(--text-secondary)]">Library roots (host-native, one per line)</span>
              <textarea rows="3" value={$settings.dv_library_roots ?? ''}
                oninput={(e) => settings.update((s) => ({ ...s, dv_library_roots: e.currentTarget.value }))}
                placeholder={'Y:\\Movies\nE:\\4K\n\\\\TURTLELANDSRV2\\Share\\Movies'}
                class={inputClass + ' font-mono'}></textarea>
            </label>

            <label class="block mt-3">
              <span class="text-sm text-[var(--text-secondary)]">Label vocabulary (JSON: layer → label)</span>
              <input type="text" value={$settings.dv_label_vocab ?? ''}
                oninput={(e) => settings.update((s) => ({ ...s, dv_label_vocab: e.currentTarget.value }))}
                placeholder={'{"fel":"DV FEL","mel":"DV MEL","profile8":"DV P8","profile5":"DV P5"}'}
                class={inputClass + ' font-mono'} />
            </label>
          </div>
```

- [ ] Type-check. From `X:\Docker Apps\ScanHound\frontend`:

```
npm run check
```

Expected: `0 errors` (the four `$settings.dv_*` reads resolve against the new optional `Settings` fields).

- [ ] Build. From `X:\Docker Apps\ScanHound\frontend`:

```
npm run build
```

Expected: `vite build` finishes with `✓ built in …` and no errors (the `settings` route chunk emits).

- [ ] Commit.

```
git add frontend/src/lib/api/types.ts frontend/src/routes/settings/+page.svelte
git commit -m "$(cat <<'EOF'
DV: add Dolby Vision settings group + Settings type fields

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
EOF
)"
```

---

### Task 12: Kometa overlay asset + host-detector setup docs

Ship the two external, non-image artifacts: a label-gated Kometa overlay config (`dv_badges.yml`) that badges exactly the four managed labels, and a `README.md` for the host detector documenting `dovi_tool.exe` placement, Task Scheduler setup, and the walk → import ordering. Neither is in `docker build`; the overlay is dropped into the user's Kometa config, and the README documents the host artifact.

**Files**
- `X:\Docker Apps\ScanHound\docs\kometa\dv_badges.yml` (new)
- `X:\Docker Apps\ScanHound\scripts\host-detector\README.md` (new)

**Interfaces**
- Consumes: the closed managed label set `{DV FEL, DV MEL, DV P8, DV P5}` (must equal `dv_label_vocab` values exactly, §7.5/§8); endpoint `POST /rename/dv-import` (README documents the walk→import call, §5); endpoint `POST /rename/dv-sync-labels` (README documents ordering, §8).
- Produces: a Kometa `overlays` config block per managed label; host-detector operator docs.

**Steps**

- [ ] Create the Kometa overlay asset. Write `X:\Docker Apps\ScanHound\docs\kometa\dv_badges.yml`:

```yaml
# ScanHound — Dolby Vision layer badges for Kometa.
#
# Label-gated overlays: each block badges movies carrying exactly one of the four
# labels ScanHound manages ("DV FEL", "DV MEL", "DV P8", "DV P5"). These label
# strings MUST match ScanHound's dv_label_vocab values exactly, or nothing badges.
#
# Drop this file into your Kometa config and reference it from a Movies library, e.g.:
#
#   libraries:
#     Movies:
#       overlay_files:
#         - file: config/dv_badges.yml
#
# Kometa applies overlays on its own schedule; run it AFTER ScanHound's
# host detector walk -> POST /rename/dv-import -> POST /rename/dv-sync-labels.

overlays:
  DV FEL:
    overlay:
      name: text(DV FEL)
      horizontal_offset: 15
      horizontal_align: left
      vertical_offset: 15
      vertical_align: top
      font_color: "#FFFFFF"
      back_color: "#00000099"
      back_width: 200
      back_height: 80
      back_radius: 30
    plex_search:
      all:
        label: DV FEL

  DV MEL:
    overlay:
      name: text(DV MEL)
      horizontal_offset: 15
      horizontal_align: left
      vertical_offset: 15
      vertical_align: top
      font_color: "#FFFFFF"
      back_color: "#00000099"
      back_width: 200
      back_height: 80
      back_radius: 30
    plex_search:
      all:
        label: DV MEL

  DV P8:
    overlay:
      name: text(DV P8)
      horizontal_offset: 15
      horizontal_align: left
      vertical_offset: 15
      vertical_align: top
      font_color: "#FFFFFF"
      back_color: "#00000099"
      back_width: 200
      back_height: 80
      back_radius: 30
    plex_search:
      all:
        label: DV P8

  DV P5:
    overlay:
      name: text(DV P5)
      horizontal_offset: 15
      horizontal_align: left
      vertical_offset: 15
      vertical_align: top
      font_color: "#FFFFFF"
      back_color: "#00000099"
      back_width: 200
      back_height: 80
      back_radius: 30
    plex_search:
      all:
        label: DV P5
```

- [ ] Create the host-detector README. Write `X:\Docker Apps\ScanHound\scripts\host-detector\README.md`:

```markdown
# ScanHound Host Detector (Dolby Vision FEL/MEL)

Runs on the Docker **host** (TurtleLandSRVR, 192.168.1.170), NOT inside the container.
Detection is host-side because FEL vs MEL requires `dovi_tool` to read the full RPU
stream, and the container cannot reach the `.180` SMB media. This artifact is **not**
part of `docker build` — the container image never contains it.

## Contents

| File | Role |
|---|---|
| `dv_host_scan.py` | Walks `dv_library_roots`, classifies each file, writes `dv_host.db`, optionally tags MKVs. |
| `dovi_tool.exe` | quietvoid **v2.3.2** (must match the image's Linux `dovi_tool` for identical classification). |
| `mkvpropedit.exe` | MKVToolNix; only needed when `dv_file_tagging` is enabled. |
| `dv_host.db` | The detector's OWN SQLite store. Created by the script. NEVER opens `crawler.db`. |

## Placement

1. Put `dovi_tool.exe` and `mkvpropedit.exe` in this folder (or anywhere), and ensure
   their directory is on `PATH`. `detect_layer` resolves the binary with
   `shutil.which("dovi_tool")`, which honors `PATHEXT` so `dovi_tool.exe` resolves.
2. Do **not** rely on your interactive user `PATH` for scheduled runs — a Windows
   Task Scheduler action runs with a stripped environment. Set the binary directory
   on `PATH` inside the scheduled action itself (see below).

## Config source

The container writes `<repo>\data\dv_host.json` on every settings save, containing
`{dv_library_roots, dv_detection, dv_file_tagging, dv_label_vocab}`. The host script
reads THAT file (a fixed bind-mounted path) — it does not import `config.py`.
If `dv_detection` is false or the roots are empty, the script logs and exits.

## Ordering (the walk -> import -> sync -> Kometa chain)

The nightly run must happen in this exact order:

1. **Walk + tag** — `python dv_host_scan.py` recurses each root, skips files whose
   signature is unchanged (mtime within `DV_MTIME_TOL` = 2.0s AND same size), runs
   `dovi_tool` on the rest, upserts `dv_host.db`, and (if `dv_file_tagging`) writes the
   MKV track name then re-stats + re-upserts the post-tag signature.
2. **Import** — the action then bridges the store into the container:
   `curl -X POST http://localhost:9721/rename/dv-import`
   (the container is the sole `crawler.db` owner; this upserts `dv_scan` `source='scan'`).
3. **Sync labels** — trigger from the ScanHound UI ("Sync Plex labels") or
   `curl -X POST http://localhost:9721/rename/dv-sync-labels -H "Content-Type: application/json" -d "{}"`.
4. **Kometa** — runs on its own schedule; it badges the labels applied in step 3.
   A mis-ordered Kometa run overlays stale labels until the next pass.

## Task Scheduler setup

Create a nightly task (Task Scheduler > Create Task):

- **General:** Run whether user is logged on or not.
- **Triggers:** Daily, e.g. 03:00.
- **Actions:** Start a program — `powershell.exe` with arguments:
  ```
  -NoProfile -Command "$env:PATH = 'C:\path\to\host-detector;' + $env:PATH; python 'C:\path\to\host-detector\dv_host_scan.py'; Invoke-WebRequest -Method POST -Uri http://localhost:9721/rename/dv-import"
  ```
  The `$env:PATH` prefix is what makes `dovi_tool.exe` resolvable under the stripped
  scheduled environment.

## Never touches `crawler.db`

The script opens only `dv_host.db`. It must **not** import ScanHound's
`DatabaseManager` (its `__init__` runs DDL/`user_version` writes; a second
DDL-running process is what corrupted the DB previously). It reuses only
`dv_detect.detect_layer` for classification.
```

- [ ] Verify file presence and YAML validity. From `X:\Docker Apps\ScanHound`:

```
python -c "import yaml,sys; d=yaml.safe_load(open('docs/kometa/dv_badges.yml')); assert set(d['overlays'])=={'DV FEL','DV MEL','DV P8','DV P5'}; print('dv_badges.yml OK:', list(d['overlays']))"
```

Expected: `dv_badges.yml OK: ['DV FEL', 'DV MEL', 'DV P8', 'DV P5']` (confirms valid YAML and exactly the four managed gates). Then confirm the README exists:

```
test -f scripts/host-detector/README.md && echo "README present"
```

Expected: `README present`.

- [ ] Commit.

```
git add docs/kometa/dv_badges.yml scripts/host-detector/README.md
git commit -m "$(cat <<'EOF'
DV: add Kometa dv_badges overlay + host-detector README

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
EOF
)"
```

---

### Task 13: End-to-end acceptance test (merge gate)

A single backend pytest that exercises the whole in-container chain: enable `dv_detection` + set a root, seed a `dv_host.db` row for a known movie path, call `POST /rename/dv-import`, monkeypatch the Plex client so `lib.all()` returns one movie whose served `part.file` normalizes to that path, run the sync worker (not `dry_run`), and assert **exactly one `DV FEL` `addLabel`** occurred and **no non-managed label** was touched. This is the merge gate — units can all pass while the feature labels nothing.

**Files**
- `X:\Docker Apps\ScanHound\tests\test_dv_acceptance.py` (new)

**Interfaces**
- Consumes: `create_app(config_override=...)` + `TestClient` (from `backend.api.main`); `DatabaseManager` (`clear_dv_scans`, `upsert_dv_scan`); `POST /rename/dv-import`; `POST /rename/dv-sync-labels`; the sync worker + `add_label`/`remove_label` on `PlexService`/`PlexManager`; `normalize_path` (Task 6/7 of the backend half).
- Produces: `test_dv_acceptance.py::test_end_to_end_fel_labels_exactly_once` — the merge gate.

**Steps**

- [ ] Write the failing acceptance test. Create `X:\Docker Apps\ScanHound\tests\test_dv_acceptance.py`:

```python
"""End-to-end DV acceptance gate.

host dv_host.db row -> POST /rename/dv-import -> dv_scan(source='scan')
-> POST /rename/dv-sync-labels (real worker, not dry_run) -> exactly one
'DV FEL' addLabel on the target movie, and no non-managed label touched.

Units can all pass while the feature labels nothing; this test is the gate.
"""
import sqlite3
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from backend.api.main import create_app
from backend.api.dependencies import registry
from backend.database import DatabaseManager

# The host-native path the detector would have recorded, and the exact string
# Plex serves as part.file. normalize_path() must equate the two (drive->UNC /
# case / separator). Here we use the SAME path so the test exercises the wiring,
# not the mapping table (mapping variants are covered by the normalize unit tests).
MOVIE_PATH = r"Y:\Movies\Dune (2021)\Dune (2021) 2160p.mkv"


@pytest.fixture(autouse=True)
def _reset_dv():
    def _clear():
        try:
            dm = DatabaseManager(); dm.clear_dv_scans(); dm.close()
        except Exception:
            pass
    _clear(); yield; _clear()


def _seed_host_db(path: str) -> str:
    """Create a standalone dv_host.db with one FEL row, return its path."""
    import tempfile, os
    fd, host_db = tempfile.mkstemp(prefix="dv_host_", suffix=".db"); os.close(fd)
    con = sqlite3.connect(host_db)
    con.execute(
        "CREATE TABLE dv_host (path TEXT PRIMARY KEY, dv_layer TEXT, "
        "sig_mtime REAL, sig_size INTEGER, title TEXT, scanned_at TEXT)"
    )
    con.execute(
        "INSERT INTO dv_host VALUES (?,?,?,?,?,?)",
        (path, "fel", 1000.0, 42, "Dune", "2026-06-30T00:00:00"),
    )
    con.commit(); con.close()
    return host_db


def _make_plex_movie(path: str):
    """A Plex movie MagicMock whose single part.file == path, tracking labels."""
    part = MagicMock(); part.file = path; part.size = 42
    media = MagicMock(); media.parts = [part]; media.videoResolution = "2160"
    movie = MagicMock()
    movie.title = "Dune"; movie.year = 2021; movie.ratingKey = 555
    movie.media = [media]; movie.guids = []
    # Track label mutations on a plain attribute so we can assert exact calls.
    movie._labels = ["Favorites"]  # a NON-managed label that must survive.
    movie.labels = [MagicMock(tag="Favorites")]
    movie.addLabel = MagicMock(side_effect=lambda l: movie._labels.append(l))
    movie.removeLabel = MagicMock(side_effect=lambda l: movie._labels.remove(l))
    return movie


def test_end_to_end_fel_labels_exactly_once(monkeypatch, tmp_path):
    # 1. App with DV enabled + a root; DB is the container's sole crawler.db.
    app = create_app(config_override={
        "plex_url": "http://x", "plex_token": "t",
        "movie_libs": ["Movies"],
        "dv_detection": True,
        "dv_library_roots": r"Y:\Movies",
        "dv_label_vocab": '{"fel":"DV FEL","mel":"DV MEL","profile8":"DV P8","profile5":"DV P5"}',
    })

    # 2. Seed the host store and point dv-import at it.
    host_db = _seed_host_db(MOVIE_PATH)
    monkeypatch.setenv("DV_HOST_DB", host_db)  # import endpoint reads this env (§4/§5)

    # 3. Monkeypatch the Plex client: lib.all() serves our one movie; label
    #    writes go through fetchItem(rating_key) -> the same movie object.
    movie = _make_plex_movie(MOVIE_PATH)
    fake_lib = MagicMock(); fake_lib.all.return_value = [movie]
    fake_server = MagicMock()
    fake_server.library.section.return_value = fake_lib
    fake_server.fetchItem.return_value = movie

    pm = registry.plex_service.plex_manager
    monkeypatch.setattr(pm, "_server", fake_server, raising=False)
    monkeypatch.setattr(pm, "is_connected", True, raising=False)

    with TestClient(app) as client:
        # 4. Import host rows into dv_scan(source='scan').
        r_imp = client.post("/rename/dv-import")
        assert r_imp.status_code == 200, r_imp.text
        assert r_imp.json()["imported"] == 1

        # 5. Run the real sync worker synchronously (not dry_run). The route
        #    dispatches a daemon thread; the sync helper is exposed for tests.
        from backend.api.routes.rename import _run_dv_sync
        result = _run_dv_sync(dry_run=False)

    # 6. Assertions — the merge gate.
    movie.addLabel.assert_called_once_with("DV FEL")   # exactly one add
    movie.removeLabel.assert_not_called()              # nothing to remove
    assert "Favorites" in movie._labels                # non-managed untouched
    assert "DV FEL" in movie._labels
    assert result["added"] == 1
    assert result["unmatched"] == 0
```

- [ ] Run it and confirm it fails for the right reason (the endpoints/`_run_dv_sync` don't exist yet, or the sync labels nothing). From `X:\Docker Apps\ScanHound`:

```
python -m pytest tests/test_dv_acceptance.py -q
```

Expected (before the backend half is wired): failure such as `404` from `POST /rename/dv-import`, or `ImportError: cannot import name '_run_dv_sync'`, or the assertion `movie.addLabel.assert_called_once_with("DV FEL")` failing with `Expected 'addLabel' to be called once. Called 0 times.` Overall: `1 failed`.

- [ ] Once the backend half (import endpoint, `normalize_path`, sync worker `_run_dv_sync`, `add_label`/`remove_label`, `part.file` capture) is implemented, run it again and confirm it passes. From `X:\Docker Apps\ScanHound`:

```
python -m pytest tests/test_dv_acceptance.py -q
```

Expected: `1 passed`.

- [ ] Run the full suite to confirm no regression, and that the acceptance gate is included. From `X:\Docker Apps\ScanHound`:

```
python -m pytest tests/ --tb=short -q
```

Expected: all tests pass (`… passed`), including `tests/test_dv_acceptance.py::test_end_to_end_fel_labels_exactly_once`.

- [ ] Commit.

```
git add tests/test_dv_acceptance.py
git commit -m "$(cat <<'EOF'
DV: end-to-end acceptance gate (host row -> import -> sync -> one DV FEL)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
EOF
)"
```