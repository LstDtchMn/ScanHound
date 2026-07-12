# Plex Library Path Translation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Translate `plex_cache.file_path` (Plex's own reported paths — drive letters, NTFS junction aliases, or NAS UNC paths) into the container-local paths ScanHound's own docker-compose mounts actually expose, so `probe_specs()`/`dv_detect` can find and read real files instead of silently failing on every call.

**Architecture:** A new pure helper `translate_plex_path(raw_path, mappings_text)` implements the same longest-prefix-match logic `RenameService._translate_path()` already has, factored out so both share it instead of duplicating the algorithm. A new `plex_library_path_mappings` config key (same `host => container` textarea format as the existing `auto_rename_path_mappings`) is seeded with the 23 mappings confirmed working this session. Two call sites — `conflict_analyzer.py`'s existing-path resolution and `plex_metadata_scan.py`'s target-builder — translate before probing. A maintenance-loop check (daily) plus an on-demand Settings button surface any `plex_cache` path prefix with no matching mapping.

**Tech Stack:** Python (FastAPI), pytest; SvelteKit 5 (runes), vitest.

## Global Constraints

- The shared helper is a **pure function**: `translate_plex_path(raw_path: str, mappings_text: str) -> str`. No config/DB access inside it — callers pass the mappings text in.
- Longest-prefix-wins, path-boundary-safe matching (a mapping for `F:/Downloads` must not also match `F:/Downloads2/...`) — copy `RenameService._translate_path()`'s exact matching logic (`backend/rename/service.py:567-593`), don't reinvent it.
- No match → return the input **unchanged** (passthrough), exactly like the existing behavior. Malformed lines (no `=>`, empty host/container) are skipped, not errors.
- `plex_library_path_mappings` is a **separate** config key from `auto_rename_path_mappings` — never merge them into one list or one Settings field.
- The 23-mapping seed value (verified working end-to-end this session — every one resolves to a real, readable file):
```
C:\1080p Drives\1080p Bismark => /library/plex-source/l-1080p-bismark
C:\1080p Drives\1080p Eastwood & Gengis Khan => /library/plex-source/b-1080p-eastwood-gengis-khan
C:\1080p Drives\1080p Kennedy & Van Buren => /library/plex-source/k-1080p-kennedy-van-buren
C:\1080p Drives\1080p Nixon & Maclom => /library/plex-source/m-1080p-nixon-maclom
C:\1080p Drives\1080p Tony Montana => /library/plex-source/f-1080p-tony-montana
C:\1080p Drives\1080p Walter White => /library/plex-source/w-1080p-walter-white
C:\1080p Drives\1080p Zepplin => /library/plex-source/h-1080p-zepplin
C:\4K Drives\4K Columbo => /library/plex-source/e-4k-hdr-columbo
C:\4K Drives\4K Gambino => /library/plex-source/a-4k-gambino
C:\4K Drives\4K Jefferson & Truman BU => /library/plex-source/j-4k-jefferson-truman-bu
C:\4K Drives\4K Quantum => /library/plex-source/q-4k-quantum
C:\4K Drives\4K Rickover => /library/plex-source/r-4k-rickover
C:\4K Drives\4K Ulysses & Yuri Gagarin BU => /library/plex-source/u-4k-ulysses-yuri-gagarin-bu
C:\4K Drives\4k HDR Arnold => /library/plex-source/i-4k-hdr-arnold
G:\Movies 1 => /library/plex-source/g-movies-1
\\TURTLELANDSRV2\1080p John Paul Jones => /library/plex-source/nas-1080p-john-paul-jones
\\TURTLELANDSRV2\1080p Lincoln => /library/plex-source/nas-1080p-lincoln
\\TURTLELANDSRV2\1080p Faraday => /library/plex-source/nas-1080p-faraday
\\TURTLELANDSRV2\1080p Icarus => /library/plex-source/nas-1080p-icarus
\\TURTLELANDSRV2\1080p Nathan Hale => /library/plex-source/nas-1080p-nathan-hale
\\TURTLELANDSRV2\1080p Picasso aka Newton => /library/plex-source/nas-1080p-picasso-aka-newton
\\TURTLELANDSRV2\4K HDR Geronimo => /library/plex-source/nas-4k-hdr-geronimo
\\TURTLELANDSRV2\4K Magellan => /library/plex-source/nas-4k-magellan
```
- Backend tests: throwaway container pattern (`docker run -d --name <c> --entrypoint sleep scanhound:latest infinity`, `docker cp backend/. tests/. <c>:/app/...`, `pip install -q pytest httpx`, run, `docker rm -f`). Frontend tests: host node (`cd frontend && npm run check && npm run build && npx vitest run`).
- Work directly on `main`. Commit only when genuinely green.
- Smart/curly-quote hazard: plain ASCII quotes only in all new/changed source; grep before committing.

---

### Task 1: Shared `translate_plex_path()` helper + `RenameService` refactor

**Files:**
- Create: `backend/rename/path_translation.py`
- Modify: `backend/rename/service.py:567-593`
- Test: `tests/test_path_translation.py`

**Interfaces:**
- Produces: `translate_plex_path(raw_path: str, mappings_text: str) -> str` (pure function, `backend/rename/path_translation.py`) — matches the exact matching semantics of the existing `RenameService._translate_path()` implementation.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_path_translation.py`:

```python
from backend.rename.path_translation import translate_plex_path


def test_exact_match_translates():
    mappings = "F:\\Downloads => /library/movies"
    assert translate_plex_path("F:\\Downloads\\Movie.mkv", mappings) == "/library/movies/Movie.mkv"


def test_no_match_returns_unchanged():
    mappings = "F:\\Downloads => /library/movies"
    assert translate_plex_path("Z:\\Somewhere\\X.mkv", mappings) == "Z:\\Somewhere\\X.mkv"


def test_empty_mappings_returns_unchanged():
    assert translate_plex_path("F:\\Downloads\\X.mkv", "") == "F:\\Downloads\\X.mkv"
    assert translate_plex_path("F:\\Downloads\\X.mkv", None) == "F:\\Downloads\\X.mkv"


def test_empty_path_returns_unchanged():
    assert translate_plex_path("", "F:\\Downloads => /library/movies") == ""


def test_longest_prefix_wins():
    mappings = (
        "F:\\Downloads => /library/movies\n"
        "F:\\Downloads\\Sub => /library/movies-sub"
    )
    result = translate_plex_path("F:\\Downloads\\Sub\\Movie.mkv", mappings)
    assert result == "/library/movies-sub/Movie.mkv"


def test_path_boundary_safe_no_false_prefix_match():
    # A mapping for 'F:/Downloads' must not also match 'F:/Downloads2/...'
    mappings = "F:\\Downloads => /library/movies"
    assert translate_plex_path("F:\\Downloads2\\Other.mkv", mappings) == "F:\\Downloads2\\Other.mkv"


def test_exact_path_with_no_remainder():
    mappings = "F:\\Downloads => /library/movies"
    assert translate_plex_path("F:\\Downloads", mappings) == "/library/movies"


def test_malformed_line_no_arrow_is_skipped():
    mappings = "F:\\Downloads /library/movies\nG:\\Downloads => /library/movies-4k"
    assert translate_plex_path("G:\\Downloads\\X.mkv", mappings) == "/library/movies-4k/X.mkv"


def test_malformed_line_empty_side_is_skipped():
    mappings = " => /library/movies\nG:\\Downloads => /library/movies-4k"
    assert translate_plex_path("G:\\Downloads\\X.mkv", mappings) == "/library/movies-4k/X.mkv"


def test_junction_alias_mapping_translates():
    mappings = "C:\\1080p Drives\\1080p Bismark => /library/plex-source/l-1080p-bismark"
    raw = "C:\\1080p Drives\\1080p Bismark\\Movie.mkv"
    assert translate_plex_path(raw, mappings) == "/library/plex-source/l-1080p-bismark/Movie.mkv"


def test_unc_share_mapping_translates():
    mappings = "\\\\TURTLELANDSRV2\\1080p Lincoln => /library/plex-source/nas-1080p-lincoln"
    raw = "\\\\TURTLELANDSRV2\\1080p Lincoln\\Movie.mkv"
    assert translate_plex_path(raw, mappings) == "/library/plex-source/nas-1080p-lincoln/Movie.mkv"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_path_translation.py -v`
Expected: FAIL — `backend.rename.path_translation` doesn't exist yet.

- [ ] **Step 3: Write the implementation**

Create `backend/rename/path_translation.py`:

```python
"""Shared longest-prefix-match path translation, used by both
RenameService._translate_path() (JD download-path -> container path) and
the Plex-library file_path -> container path translator. A pure function --
no config/DB access -- so both callers can plug in their own mappings text
and this stays trivially unit-testable.
"""
from __future__ import annotations

from typing import Optional


def translate_plex_path(raw_path: str, mappings_text: Optional[str]) -> str:
    """Translate `raw_path` using `mappings_text` (one `host => container`
    line per line). Longest host prefix wins; a mapping only matches at a
    path boundary (exact match or the next character is '/'), so a mapping
    for 'F:/Downloads' never also matches 'F:/Downloads2/...'. Malformed
    lines (no '=>', empty host or container) are skipped. Returns
    `raw_path` unchanged if nothing matches or `raw_path`/`mappings_text`
    is empty."""
    if not raw_path:
        return raw_path
    norm = raw_path.replace("\\", "/")
    best = None  # (host_prefix_len, translated)
    for line in str(mappings_text or "").splitlines():
        if "=>" not in line:
            continue
        host, container = (p.strip() for p in line.split("=>", 1))
        if not host or not container:
            continue
        hp = host.replace("\\", "/").rstrip("/")
        nl, hl = norm.lower(), hp.lower()
        if hp and (nl == hl or nl.startswith(hl + "/")):
            rest = norm[len(hp):].lstrip("/")
            translated = container.rstrip("/") + ("/" + rest if rest else "")
            if best is None or len(hp) > best[0]:
                best = (len(hp), translated)
    return best[1] if best else raw_path
```

Refactor `RenameService._translate_path()` (`backend/rename/service.py:567-593`) to delegate to it:

```python
    def _translate_path(self, path: str) -> str:
        """Map a host (JDownloader/Windows) path into a container path using the
        configured ``auto_rename_path_mappings`` (one ``host => container`` per
        line). JDownloader runs on the host and reports e.g. ``F:\\Downloads\\X``;
        the container sees that bind-mounted at ``/library/movies/X``. Longest
        host prefix wins. Returns the path unchanged if nothing matches."""
        from backend.rename.path_translation import translate_plex_path
        return translate_plex_path(path, self._cfg.get("auto_rename_path_mappings"))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_path_translation.py -v`
Expected: PASS, all 10 tests green. Also run the existing rename-service test suite to confirm the refactor didn't change `_translate_path()`'s behavior: `pytest tests/test_rename_service.py -v -k translate_path` (adjust the `-k` filter to whatever the existing tests are actually named — check first with `pytest tests/test_rename_service.py -v -k path --collect-only`).

- [ ] **Step 5: Commit**

```bash
git add backend/rename/path_translation.py backend/rename/service.py tests/test_path_translation.py
git commit -m "feat(paths): shared translate_plex_path() helper; refactor RenameService to use it"
```

---

### Task 2: Wire translation into both probe consumers

**Files:**
- Modify: `backend/rename/conflict_analyzer.py:65,84-98,147,176`
- Modify: `backend/app_service.py:609` (caller update)
- Modify: `backend/api/routes/rename.py:203` (caller update)
- Modify: `backend/api/routes/plex.py:167-185` (`_movie_targets_for_scope`)
- Test: `tests/test_conflict_analyzer.py`
- Test: `tests/test_plex_metadata_scan.py` (or wherever `_movie_targets_for_scope`'s existing tests live — check `ls tests/ | grep plex` first)

**Interfaces:**
- Consumes: `translate_plex_path(raw_path, mappings_text)` (Task 1, `backend.rename.path_translation`).
- Produces (signature changes, both `path_mappings` params default to `None` for backward compatibility): `analyze_job_conflict(db, job, plex_cache_rows=None, path_mappings=None)`, `analyze_pending_conflicts(db, limit=50, path_mappings=None)`.

- [ ] **Step 1: Write the failing tests**

Read the current state of `backend/rename/conflict_analyzer.py` around lines 74-98 first (line numbers may have shifted slightly since this plan was written) to confirm `existing_path = match["file_path"]` is still there. Add to `tests/test_conflict_analyzer.py`:

```python
def test_analyze_job_conflict_translates_existing_path_before_probing():
    # Regression test: existing_path comes straight from plex_cache.file_path
    # (Plex's own reported path, e.g. a Windows drive/junction path) and must
    # be translated to a container-local path before reaching probe_specs(),
    # or every library-side probe silently fails to find the file.
    db = MagicMock()
    path_mappings = "C:\\1080p Drives\\1080p Bismark => /library/plex-source/l-1080p-bismark"
    plex_cache_rows = [{
        "is_tv": False, "imdb_id": "tt1234567", "title": "Some Movie", "year": 2020,
        "file_path": "C:\\1080p Drives\\1080p Bismark\\Some Movie (2020).mkv",
    }]
    job = {
        "media_type": "movie", "status": "matched", "imdb_id": "tt1234567",
        "title": "Some Movie", "year": 2020, "original_path": "/library/movies/incoming.mkv",
        "destination_path": "/library/movies/Some Movie (2020).mkv", "new_filename": "Some Movie (2020).mkv",
    }
    with patch("backend.rename.conflict_analyzer.probe_specs") as mock_probe:
        mock_probe.return_value = {"present": False, "path": None, "size_bytes": None,
                                    "container": None, "duration_min": None, "bitrate": None,
                                    "resolution": None, "video_codec": None, "hdr": None,
                                    "dv_layer": None, "audio": None, "audio_profile": None}
        analyze_job_conflict(db, job, plex_cache_rows=plex_cache_rows, path_mappings=path_mappings)
        existing_call = mock_probe.call_args_list[0]
        assert existing_call.args[0] == "/library/plex-source/l-1080p-bismark/Some Movie (2020).mkv"


def test_analyze_job_conflict_no_path_mappings_uses_raw_path():
    # Backward-compat: path_mappings defaults to None, so an existing caller
    # that doesn't pass it still works exactly as before (raw path through,
    # same as translate_plex_path's own no-mapping passthrough behavior).
    db = MagicMock()
    plex_cache_rows = [{
        "is_tv": False, "imdb_id": "tt1234567", "title": "Some Movie", "year": 2020,
        "file_path": "C:\\1080p Drives\\1080p Bismark\\Some Movie (2020).mkv",
    }]
    job = {
        "media_type": "movie", "status": "matched", "imdb_id": "tt1234567",
        "title": "Some Movie", "year": 2020, "original_path": "/library/movies/incoming.mkv",
        "destination_path": "/library/movies/Some Movie (2020).mkv", "new_filename": "Some Movie (2020).mkv",
    }
    with patch("backend.rename.conflict_analyzer.probe_specs") as mock_probe:
        mock_probe.return_value = {"present": False, "path": None, "size_bytes": None,
                                    "container": None, "duration_min": None, "bitrate": None,
                                    "resolution": None, "video_codec": None, "hdr": None,
                                    "dv_layer": None, "audio": None, "audio_profile": None}
        analyze_job_conflict(db, job, plex_cache_rows=plex_cache_rows)
        existing_call = mock_probe.call_args_list[0]
        assert existing_call.args[0] == "C:\\1080p Drives\\1080p Bismark\\Some Movie (2020).mkv"
```

(Read this test file's existing imports/fixtures first — `MagicMock`, `patch`, `analyze_job_conflict` should already be imported; adapt the job/plex_cache_rows dict shape to exactly match what `find_library_duplicate()` and `_job_dest()` expect, per the existing tests in this file, if this literal shape doesn't match.)

Read `backend/api/routes/plex.py`'s current `_movie_targets_for_scope` (lines 167-185, may have shifted) and whatever test file already covers it, then add:

```python
def test_movie_targets_translates_file_path(monkeypatch):
    reg = MagicMock()
    reg.config = {"plex_library_path_mappings": "G:\\Movies 1 => /library/plex-source/g-movies-1"}
    reg.db.list_plex_cache_movies.return_value = [{
        "key": "1", "title": "X", "rating_key": "1", "imdb_id": "tt1",
        "file_path": "G:\\Movies 1\\X.mkv",
    }]
    targets = _movie_targets_for_scope(reg, "all", None)
    assert targets[0]["path"] == "/library/plex-source/g-movies-1/X.mkv"
```

(Adapt to this file's existing test fixtures/mocking style for `ServiceRegistry` — check the file for an existing `reg`-style fixture before writing a new one from scratch.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_conflict_analyzer.py -v -k translates_existing_path` and `pytest tests/test_plex_routes.py -v -k translates_file_path` (adjust the second file name per Step 1's investigation)
Expected: FAIL — the raw, untranslated path reaches `probe_specs()`/the target list.

- [ ] **Step 3: Write the implementation**

`DatabaseManager` (the `db` parameter both functions receive) has **no config accessor at all** (confirmed: no `get_config`-style method anywhere on it) — `analyze_job_conflict`/`analyze_pending_conflicts` must instead take the mappings text as an explicit new parameter, threaded from each of their two real call sites, both of which already have a config dict in scope under a different name.

In `backend/rename/conflict_analyzer.py`, add the import near the top, add a `path_mappings` parameter to both functions, and translate right after the assignment (line ~87):

```python
from backend.rename.path_translation import translate_plex_path
```

```python
def analyze_job_conflict(db, job: dict, plex_cache_rows: Optional[list] = None,
                          path_mappings: Optional[str] = None) -> Optional[dict]:
```

```python
        match = find_library_duplicate(job, rows)
        if match and match.get("file_path"):
            kind = "library_duplicate"
            existing_path = translate_plex_path(match["file_path"], path_mappings)
```

```python
def analyze_pending_conflicts(db, limit: int = 50, path_mappings: Optional[str] = None) -> int:
```

And thread it into `analyze_pending_conflicts`'s own internal call to `analyze_job_conflict` (line ~176):

```python
            analyze_job_conflict(db, job, plex_cache_rows=plex_rows, path_mappings=path_mappings)
```

Update both real call sites to pass it:

`backend/app_service.py:609` (inside `_run_maintenance_pass`):

```python
                n = analyze_pending_conflicts(
                    self.db, limit=50,
                    path_mappings=self.config.get("plex_library_path_mappings"))
```

`backend/api/routes/rename.py:203` (inside `list_jobs`'s background-analysis dispatch — read the surrounding function first, since this call is inside a closure passed to a background thread; confirm `reg` is captured in that closure's scope already, which it should be since `reg.db`/`plex_movie_rows` are already used there):

```python
                                analyze_job_conflict(
                                    reg.db, job, plex_cache_rows=plex_movie_rows,
                                    path_mappings=reg.config.get("plex_library_path_mappings"))
```

In `backend/api/routes/plex.py`'s `_movie_targets_for_scope`:

```python
from backend.rename.path_translation import translate_plex_path
```

```python
def _movie_targets_for_scope(reg: ServiceRegistry, scope: str, ids: Optional[List[str]]) -> list:
    """Resolve a scan scope into a list of {path, title, rating_key, imdb_id}
    dicts, movies only, skipping rows with no known file_path. Each path is
    translated from Plex's own reported form into the container-local path
    the docker-compose mounts actually expose."""
    movies = reg.db.list_plex_cache_movies() if reg.db else []
    if scope == "selected":
        wanted = set(ids or [])
        movies = [m for m in movies if m.get("key") in wanted]
    mappings = reg.config.get("plex_library_path_mappings") if reg.config else None
    targets = []
    for m in movies:
        path = m.get("file_path")
        if not path:
            continue
        targets.append({
            "path": translate_plex_path(path, mappings),
            "title": m.get("title"),
            "rating_key": m.get("rating_key"),
            "imdb_id": m.get("imdb_id"),
        })
    return targets
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_conflict_analyzer.py -v` and `pytest tests/test_plex_routes.py -v` (full files, confirm no regressions)
Expected: PASS, all tests green including the new ones.

- [ ] **Step 5: Commit**

```bash
git add backend/rename/conflict_analyzer.py backend/api/routes/plex.py tests/test_conflict_analyzer.py tests/test_plex_routes.py
git commit -m "feat(paths): translate plex_cache.file_path before probing in both consumers"
```

(Adjust the second test file path in `git add` to whatever Step 1's investigation actually found.)

---

### Task 3: Settings UI panel + config plumbing (seeded with the 23-mapping default)

**Files:**
- Modify: `backend/api/routes/settings.py:124`
- Modify: `backend/config.py` (default config values — check for a `DEFAULT_CONFIG`-style dict near where other defaults live, e.g. near `trash_retention_days`'s default of 30 seen earlier this session)
- Modify: `frontend/src/lib/api/types.ts` (the `Settings` interface)
- Modify: `frontend/src/routes/settings/+page.svelte`
- Test: `tests/test_settings_routes.py` (check `ls tests/ | grep settings` first for the correct existing file)

**Interfaces:**
- Produces: config key `plex_library_path_mappings: str`, default-seeded with the 23-line mapping set from this plan's Global Constraints.

- [ ] **Step 1: Write the failing test**

First find this codebase's default-config mechanism (grep `trash_retention_days.*30` or similar in `backend/config.py` to find the defaults dict/function) and read a neighboring string-default entry to match its exact style. Add to whichever test file covers settings defaults/round-trip:

```python
def test_plex_library_path_mappings_has_seeded_default():
    from backend.config import DEFAULT_CONFIG  # adjust import to the real name found above
    assert "plex_library_path_mappings" in DEFAULT_CONFIG
    seeded = DEFAULT_CONFIG["plex_library_path_mappings"]
    assert "C:\\1080p Drives\\1080p Bismark => /library/plex-source/l-1080p-bismark" in seeded
    assert "\\\\TURTLELANDSRV2\\4K Magellan => /library/plex-source/nas-4k-magellan" in seeded


def test_settings_update_accepts_plex_library_path_mappings(client):
    resp = client.put("/settings", json={"plex_library_path_mappings": "A: => /library/plex-source/a"})
    assert resp.status_code == 200
```

(Adapt `client`/fixture names to whatever this test file already uses for the settings PUT endpoint.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_settings_routes.py -v -k plex_library_path_mappings` (adjust filename per Step 1)
Expected: FAIL — key doesn't exist in defaults or the `SettingsUpdate` model yet.

- [ ] **Step 3: Write the implementation**

In `backend/api/routes/settings.py`, add near line 124 (`auto_rename_path_mappings: Optional[str] = None`):

```python
    plex_library_path_mappings: Optional[str] = None
```

In `backend/config.py`, add the seeded default to the defaults dict/structure found in Step 1, using this exact 23-line value (matching the plan's Global Constraints verbatim):

```python
    "plex_library_path_mappings": (
        "C:\\1080p Drives\\1080p Bismark => /library/plex-source/l-1080p-bismark\n"
        "C:\\1080p Drives\\1080p Eastwood & Gengis Khan => /library/plex-source/b-1080p-eastwood-gengis-khan\n"
        "C:\\1080p Drives\\1080p Kennedy & Van Buren => /library/plex-source/k-1080p-kennedy-van-buren\n"
        "C:\\1080p Drives\\1080p Nixon & Maclom => /library/plex-source/m-1080p-nixon-maclom\n"
        "C:\\1080p Drives\\1080p Tony Montana => /library/plex-source/f-1080p-tony-montana\n"
        "C:\\1080p Drives\\1080p Walter White => /library/plex-source/w-1080p-walter-white\n"
        "C:\\1080p Drives\\1080p Zepplin => /library/plex-source/h-1080p-zepplin\n"
        "C:\\4K Drives\\4K Columbo => /library/plex-source/e-4k-hdr-columbo\n"
        "C:\\4K Drives\\4K Gambino => /library/plex-source/a-4k-gambino\n"
        "C:\\4K Drives\\4K Jefferson & Truman BU => /library/plex-source/j-4k-jefferson-truman-bu\n"
        "C:\\4K Drives\\4K Quantum => /library/plex-source/q-4k-quantum\n"
        "C:\\4K Drives\\4K Rickover => /library/plex-source/r-4k-rickover\n"
        "C:\\4K Drives\\4K Ulysses & Yuri Gagarin BU => /library/plex-source/u-4k-ulysses-yuri-gagarin-bu\n"
        "C:\\4K Drives\\4k HDR Arnold => /library/plex-source/i-4k-hdr-arnold\n"
        "G:\\Movies 1 => /library/plex-source/g-movies-1\n"
        "\\\\TURTLELANDSRV2\\1080p John Paul Jones => /library/plex-source/nas-1080p-john-paul-jones\n"
        "\\\\TURTLELANDSRV2\\1080p Lincoln => /library/plex-source/nas-1080p-lincoln\n"
        "\\\\TURTLELANDSRV2\\1080p Faraday => /library/plex-source/nas-1080p-faraday\n"
        "\\\\TURTLELANDSRV2\\1080p Icarus => /library/plex-source/nas-1080p-icarus\n"
        "\\\\TURTLELANDSRV2\\1080p Nathan Hale => /library/plex-source/nas-1080p-nathan-hale\n"
        "\\\\TURTLELANDSRV2\\1080p Picasso aka Newton => /library/plex-source/nas-1080p-picasso-aka-newton\n"
        "\\\\TURTLELANDSRV2\\4K HDR Geronimo => /library/plex-source/nas-4k-hdr-geronimo\n"
        "\\\\TURTLELANDSRV2\\4K Magellan => /library/plex-source/nas-4k-magellan"
    ),
```

In `frontend/src/lib/api/types.ts`, add to the `Settings` interface (near `auto_rename_path_mappings`):

```typescript
  plex_library_path_mappings?: string;
```

In `frontend/src/routes/settings/+page.svelte`, add a new panel near the existing "Download path mappings" field (around line 1519-1526), matching its exact structure:

```svelte
          <label class="block">
            <Tooltip text={'Plex reports library files using ITS OWN path form (a drive letter, an NTFS junction-folder alias, or a NAS share path) which is usually different from where ScanHound sees that same file mounted in its own container. Map each Plex-reported path prefix to its container path, one per line, as: host => container. Seeded with the mappings already confirmed working for this library -- edit if a drive is renamed or a new one is added (needs a matching docker-compose mount too).'}>
              <span class="text-sm text-[var(--text-secondary)] cursor-help underline decoration-dotted">Plex library path mappings (host ⇒ container) ⓘ</span>
            </Tooltip>
            <textarea rows="6" value={$settings.plex_library_path_mappings ?? ''}
              oninput={(e) => settings.update((s) => ({ ...s, plex_library_path_mappings: e.currentTarget.value }))}
              placeholder={'C:\\1080p Drives\\Example => /library/plex-source/example'} class="{inputClass} font-mono text-xs"></textarea>
          </label>
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_settings_routes.py -v -k plex_library_path_mappings` (adjust filename per Step 1). Frontend: `cd frontend && npm run check && npm run build && npx vitest run`.
Expected: all green.

- [ ] **Step 5: Commit**

```bash
git add backend/api/routes/settings.py backend/config.py frontend/src/lib/api/types.ts frontend/src/routes/settings/+page.svelte tests/test_settings_routes.py
git commit -m "feat(settings): Plex library path mappings panel, seeded with 23 verified mappings"
```

(Adjust the test filename in `git add` to whatever Step 1's investigation actually found.)

---

### Task 4: Auto-detection (unmapped-path finder) + on-demand check + maintenance hook

**Files:**
- Create: `backend/rename/path_translation.py` additions (same file as Task 1)
- Modify: `backend/api/routes/plex.py` (new endpoint)
- Modify: `backend/app_service.py:606-613` (`_run_maintenance_pass`)
- Modify: `frontend/src/lib/api/client.ts`
- Modify: `frontend/src/routes/settings/+page.svelte`
- Test: `tests/test_path_translation.py` (same file as Task 1)
- Test: `tests/test_plex_routes.py` (or wherever Task 2 landed its plex-route tests)

**Interfaces:**
- Consumes: `translate_plex_path` (Task 1).
- Produces: `find_unmapped_plex_path_prefixes(plex_cache_rows: list[dict], mappings_text: str) -> list[str]` (`backend.rename.path_translation`). `GET /plex/unmapped-paths` (returns `{"prefixes": [...]}`).

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_path_translation.py`:

```python
from backend.rename.path_translation import find_unmapped_plex_path_prefixes


def test_finds_local_prefix_with_no_mapping():
    rows = [{"file_path": "Z:\\Something\\Movie.mkv"}]
    result = find_unmapped_plex_path_prefixes(rows, "")
    assert result == ["Z:\\"]


def test_finds_junction_alias_prefix_with_no_mapping():
    rows = [{"file_path": "C:\\1080p Drives\\1080p New Drive\\Movie.mkv"}]
    result = find_unmapped_plex_path_prefixes(rows, "")
    assert result == ["C:\\1080p Drives\\1080p New Drive"]


def test_finds_unc_share_prefix_with_no_mapping():
    rows = [{"file_path": "\\\\TURTLELANDSRV2\\New Share\\Movie.mkv"}]
    result = find_unmapped_plex_path_prefixes(rows, "")
    assert result == ["\\\\TURTLELANDSRV2\\New Share"]


def test_mapped_prefix_is_not_flagged():
    rows = [{"file_path": "G:\\Movies 1\\Movie.mkv"}]
    mappings = "G:\\Movies 1 => /library/plex-source/g-movies-1"
    assert find_unmapped_plex_path_prefixes(rows, mappings) == []


def test_returns_distinct_prefixes_only():
    rows = [
        {"file_path": "Z:\\Something\\A.mkv"},
        {"file_path": "Z:\\Something\\B.mkv"},
    ]
    assert find_unmapped_plex_path_prefixes(rows, "") == ["Z:\\"]


def test_rows_with_no_file_path_are_skipped():
    rows = [{"file_path": None}, {"file_path": ""}, {}]
    assert find_unmapped_plex_path_prefixes(rows, "") == []
```

Read the current `_movie_targets_for_scope` / plex.py route file structure from Task 2 first, then add:

```python
def test_unmapped_paths_endpoint_returns_prefixes(client):
    # Adapt to this file's existing ServiceRegistry/client mocking pattern
    # (matches Task 2's test conventions in this same file).
    ...
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_path_translation.py -v -k unmapped`
Expected: FAIL — `find_unmapped_plex_path_prefixes` doesn't exist yet.

- [ ] **Step 3: Write the implementation**

Add to `backend/rename/path_translation.py`:

```python
def _prefix_key(raw_path: str) -> Optional[str]:
    """Coarse top-level grouping key for a Plex-reported path: the drive
    letter plus its immediate subfolder for a local path (or just the drive
    root if there's no subfolder), or the server plus share name for a UNC
    path. Returns None for anything that isn't a recognizable Windows/UNC
    path at all."""
    if not raw_path:
        return None
    if raw_path.startswith("\\\\"):
        segs = raw_path[2:].split("\\")
        if len(segs) < 2 or not segs[0] or not segs[1]:
            return None
        return f"\\\\{segs[0]}\\{segs[1]}"
    if len(raw_path) > 1 and raw_path[1] == ":":
        rest = raw_path[2:]
        segs = [s for s in rest.split("\\") if s]
        if not segs:
            return raw_path[:2] + "\\"
        return raw_path[:2] + "\\" + segs[0]
    return None


def find_unmapped_plex_path_prefixes(plex_cache_rows: list, mappings_text: Optional[str]) -> list:
    """Return the distinct set of top-level path prefixes among
    `plex_cache_rows` (each a dict with a 'file_path' key) for which
    `translate_plex_path` is currently a no-op -- i.e. no configured mapping
    actually changes the path. Sorted for stable, testable output."""
    seen = set()
    unmapped = set()
    for row in plex_cache_rows:
        path = row.get("file_path") if isinstance(row, dict) else None
        if not path:
            continue
        key = _prefix_key(path)
        if not key or key in seen:
            continue
        seen.add(key)
        if translate_plex_path(path, mappings_text) == path:
            unmapped.add(key)
    return sorted(unmapped)
```

Add the endpoint in `backend/api/routes/plex.py` (near `_movie_targets_for_scope`):

```python
@router.get("/unmapped-paths")
def unmapped_plex_paths(reg: ServiceRegistry = Depends(get_registry)):
    """Distinct plex_cache path prefixes with no configured
    plex_library_path_mappings entry -- surfaces a gap before it silently
    means those files never get probed."""
    from backend.rename.path_translation import find_unmapped_plex_path_prefixes
    movies = reg.db.list_plex_cache_movies() if reg.db else []
    mappings = reg.config.get("plex_library_path_mappings") if reg.config else None
    return {"prefixes": find_unmapped_plex_path_prefixes(movies, mappings)}
```

In `backend/app_service.py`'s `_run_maintenance_pass` (around lines 606-613, after the existing conflict-analysis backfill block), add a daily-gated check:

```python
        try:
            if self.db is not None:
                import time as _time
                last_check = getattr(self, "_last_unmapped_path_check", 0.0)
                if _time.time() - last_check >= 86400.0:  # once per day, not every maintenance tick
                    self._last_unmapped_path_check = _time.time()
                    from backend.rename.path_translation import find_unmapped_plex_path_prefixes
                    movies = self.db.list_plex_cache_movies()
                    mappings = self.config.get("plex_library_path_mappings")
                    unmapped = find_unmapped_plex_path_prefixes(movies, mappings)
                    if unmapped:
                        from backend.api.ws import ws_manager
                        preview = ", ".join(unmapped[:3]) + ("..." if len(unmapped) > 3 else "")
                        ws_manager.broadcast_sync({"type": "notification", "data": {
                            "title": "Unmapped Plex library paths",
                            "body": f"{len(unmapped)} path prefix(es) have no mapping and won't be scanned: {preview}",
                            "priority": "normal"}})
                        logger.info("Unmapped Plex path check: %d prefix(es) found", len(unmapped))
        except Exception:
            logger.exception("Unmapped Plex path check failed (non-fatal)")
```

Read the actual current contents of `_run_maintenance_pass` first (line numbers/exact surrounding code may have shifted) before inserting — place this as its own independent `try`/`except` block, same pattern as the two existing blocks in that method, so a failure here never breaks the other maintenance work.

Add the client method in `frontend/src/lib/api/client.ts` (near other `plex*` methods):

```typescript
  getUnmappedPlexPaths: () => request<{ prefixes: string[] }>('/plex/unmapped-paths'),
```

Add a "Check for unmapped paths" button to the new Settings panel from Task 3, in `frontend/src/routes/settings/+page.svelte`:

```svelte
<script lang="ts">
  // ...existing script content...
  let unmappedPaths = $state<string[] | null>(null);
  let checkingUnmapped = $state(false);
  async function checkUnmappedPaths() {
    checkingUnmapped = true;
    try {
      const r = await api.getUnmappedPlexPaths();
      unmappedPaths = r.prefixes;
    } finally {
      checkingUnmapped = false;
    }
  }
</script>
```

```svelte
          <button type="button" onclick={checkUnmappedPaths} disabled={checkingUnmapped}
            class="text-xs px-2 py-1 rounded bg-[var(--bg-tertiary)] disabled:opacity-50">
            {checkingUnmapped ? 'Checking...' : 'Check for unmapped paths'}
          </button>
          {#if unmappedPaths !== null}
            {#if unmappedPaths.length === 0}
              <p class="text-xs text-[var(--success)]">All Plex library paths are mapped.</p>
            {:else}
              <p class="text-xs text-[var(--error)]">Unmapped: {unmappedPaths.join(', ')}</p>
            {/if}
          {/if}
```

Place this immediately after the textarea added in Task 3.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_path_translation.py -v` (full file) and whichever plex-routes test file covers the new endpoint.
Expected: all green, no regressions.

- [ ] **Step 5: Run the full verification suite**

```bash
cd frontend
npm run check
npm run build
npx vitest run
```
Backend (throwaway container):
```bash
pytest tests/test_path_translation.py tests/test_conflict_analyzer.py tests/test_plex_routes.py tests/test_rename_service.py tests/test_settings_routes.py -v
```
(Adjust filenames to whatever earlier tasks' investigations actually found.)
Expected: all green. Grep every file touched across Tasks 1-4 for curly/smart quotes and confirm zero matches.

- [ ] **Step 6: Commit**

```bash
git add backend/rename/path_translation.py backend/api/routes/plex.py backend/app_service.py frontend/src/lib/api/client.ts frontend/src/routes/settings/+page.svelte tests/test_path_translation.py
git commit -m "feat(paths): auto-detect unmapped Plex path prefixes (on-demand + daily maintenance check)"
```
