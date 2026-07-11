# Auto-Analyzed Duplicate Quality Comparison Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Every duplicate (exact-destination-path collision or same-title-different-path library copy) gets automatically probed and ranked, so the Renames row shows a concise real quality diff instead of a raw-byte tooltip, without waiting for the user to open the Compare modal.

**Architecture:** Two new caches (`media_probe` for ffprobe results, keyed like the existing `dv_scan`; `rename_jobs.conflict_analysis` for the computed diff) feed a standalone analyzer module (`backend/rename/conflict_analyzer.py`) that mirrors the existing `pipeline_service.reconcile_batch` pattern — a plain function taking `db`, callable both from the `GET /rename/jobs` route (fire-and-forget, for near-immediate feedback) and from `AppService`'s hourly maintenance pass (backfill safety net). A new pure function `find_library_duplicate()` extends duplicate detection beyond exact-path collisions using a new `plex_cache.file_path` column (populated for free from data `plex_service.py` already computes). Frontend gets a `conflictSummary()` formatter and kind-aware action buttons.

**Tech Stack:** Python/FastAPI/SQLite backend, Svelte 5 (runes) frontend, ffprobe (already a dependency), `dovi_tool` (already a dependency, existing `dv_detect.py` wrapper).

## Global Constraints

- Recommendations are advice only — no task in this plan may auto-trigger Overwrite/Apply/Skip without a human click (spec §1, §7).
- The smart FEL/MEL gate must fire `dovi_tool` **only** when it's the sole possible tiebreaker (spec §4.d) — this is a hard cost-control property, not a nice-to-have; its test must assert zero `detect_layer` calls in the decisive-tier case.
- TV jobs are out of scope for `find_library_duplicate` — always return `None` (spec §5, §7).
- `rank_conflict`'s scoring weights are reused verbatim — no task may change `_quality_score`'s tuple ordering or weights (spec §7).
- The three legacy `conflict_same_size`/`conflict_existing_size`/`conflict_incoming_size` columns keep being written by existing `service.py` code (unrelated execution-time logic) — no task deletes them; the frontend just stops reading them for display (spec §6.3).
- Every backend test file in this plan lives in `X:\Docker Apps\ScanHound\tests\` and runs via the project's established throwaway-container pytest pattern (prod image lacks pytest) — `docker cp` the changed `backend/`/`tests/` dirs into a `scanhound:latest`-based container and run there. Every frontend test runs via `npm run check`, `npm run build`, `npx vitest run` on the host (frontend deps are installed there).
- No Svelte component in this codebase has a colocated render test (verified: zero `*.svelte.test.ts` files, no `@testing-library/svelte` dependency) — `.svelte` changes are verified via `npm run check` + `npm run build`, not new render-test infrastructure. All new *decision logic* that a `.svelte` file needs must live in a plain, unit-tested `.ts` helper — never embed untestable conditionals directly in template markup.

---

### Task 1: `media_probe` cache table + DB helpers

**Files:**
- Modify: `backend/database.py` (add `CREATE TABLE` in the schema block near `dv_scan`, ~line 527; add 3 new helper methods near `dv_scan`'s helpers, ~line 1900)
- Test: `tests/test_database_media_probe.py` (new)

**Interfaces:**
- Produces: `DatabaseManager.upsert_media_probe(path, probe_json, *, sig_mtime=None, sig_size=None) -> bool`, `DatabaseManager.get_media_probe(path) -> dict | None` (returns `{"path", "sig_mtime", "sig_size", "probe_json", "probed_at"}` — `probe_json` is the **raw string**, not decoded; the caller in Task 2 decodes it), `DatabaseManager.media_probe_is_current(path, sig_mtime, sig_size) -> bool`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_database_media_probe.py
import json
from backend.database import DatabaseManager


def _db(tmp_path):
    return DatabaseManager(db_path=str(tmp_path / "t.db"))


def test_upsert_and_get_media_probe(tmp_path):
    db = _db(tmp_path)
    payload = {"present": True, "resolution": "2160p", "size_bytes": 40000000000}
    assert db.upsert_media_probe("/m.mkv", json.dumps(payload), sig_mtime=100.0, sig_size=40000000000)
    row = db.get_media_probe("/m.mkv")
    assert row is not None
    assert json.loads(row["probe_json"]) == payload
    assert row["sig_mtime"] == 100.0
    assert row["sig_size"] == 40000000000


def test_get_media_probe_missing_returns_none(tmp_path):
    db = _db(tmp_path)
    assert db.get_media_probe("/no/such.mkv") is None


def test_media_probe_is_current_matches_within_1s_mtime_tolerance(tmp_path):
    db = _db(tmp_path)
    db.upsert_media_probe("/m.mkv", "{}", sig_mtime=100.0, sig_size=1000)
    assert db.media_probe_is_current("/m.mkv", 100.5, 1000) is True   # within 1s
    assert db.media_probe_is_current("/m.mkv", 102.0, 1000) is False  # outside 1s
    assert db.media_probe_is_current("/m.mkv", 100.0, 999) is False   # size changed


def test_media_probe_is_current_no_row_is_false(tmp_path):
    db = _db(tmp_path)
    assert db.media_probe_is_current("/no/such.mkv", 100.0, 1000) is False


def test_upsert_media_probe_overwrites_on_reprobe(tmp_path):
    db = _db(tmp_path)
    db.upsert_media_probe("/m.mkv", '{"v": 1}', sig_mtime=1.0, sig_size=10)
    db.upsert_media_probe("/m.mkv", '{"v": 2}', sig_mtime=2.0, sig_size=20)
    row = db.get_media_probe("/m.mkv")
    assert json.loads(row["probe_json"]) == {"v": 2}
    assert row["sig_mtime"] == 2.0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `docker rm -f sh-plan-t1 >/dev/null 2>&1; docker run -d --name sh-plan-t1 --entrypoint sleep scanhound:latest infinity >/dev/null && docker cp backend/. sh-plan-t1:/app/backend && docker cp tests/. sh-plan-t1:/app/tests && docker exec sh-plan-t1 pip install -q pytest && MSYS_NO_PATHCONV=1 docker exec sh-plan-t1 sh -c "cd /app && python3 -m pytest tests/test_database_media_probe.py -q"`
Expected: FAIL — `AttributeError: 'DatabaseManager' object has no attribute 'upsert_media_probe'`

- [ ] **Step 3: Add the table to the schema block**

In `backend/database.py`, immediately after the `dv_scan` `CREATE TABLE` block (ends ~line 527, right before the `# ── Performance indexes` comment), add:

```python
                # ffprobe result cache, keyed by path with a (mtime, size)
                # change-signal — mirrors dv_scan's invalidation shape exactly.
                # A cache MISS or STALE row means re-probe; a probe FAILURE is
                # never written here (the caller retries next time rather than
                # wedging a file into permanent "unknown").
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS media_probe (
                        path TEXT PRIMARY KEY,
                        sig_mtime REAL,
                        sig_size INTEGER,
                        probe_json TEXT,
                        probed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                ''')
```

- [ ] **Step 4: Add the three helper methods**

In `backend/database.py`, immediately after `clear_dv_scans` (the last method in the `# ── Dolby Vision layer inventory (dv_scan) ──` section, ends ~line 1899), add a new section:

```python
    # ── ffprobe result cache (media_probe) ─────────────────────────────

    def upsert_media_probe(self, path, probe_json, *, sig_mtime=None, sig_size=None):
        """Insert/update the cached ffprobe result for ``path``. Returns True on success."""
        if not path:
            return False
        return self._mutate('''
            INSERT INTO media_probe (path, sig_mtime, sig_size, probe_json, probed_at)
            VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(path) DO UPDATE SET
                sig_mtime = excluded.sig_mtime,
                sig_size = excluded.sig_size,
                probe_json = excluded.probe_json,
                probed_at = CURRENT_TIMESTAMP
        ''', (path, sig_mtime, sig_size, probe_json), label="upsert_media_probe") is not None

    def get_media_probe(self, path):
        """Return the cached probe row for ``path`` (dict, probe_json still a raw
        JSON string) or None."""
        rows = self._query_dicts(
            'SELECT path, sig_mtime, sig_size, probe_json, probed_at '
            'FROM media_probe WHERE path = ?', (path,))
        return rows[0] if rows else None

    def media_probe_is_current(self, path, sig_mtime, sig_size):
        """Whether ``path``'s cached probe still matches the on-disk signature —
        mirrors dv_scan_is_current's 1s mtime tolerance / exact size match."""
        row = self.get_media_probe(path)
        if not row or row.get("sig_mtime") is None or row.get("sig_size") is None:
            return False
        try:
            return (abs(float(row["sig_mtime"]) - float(sig_mtime)) < 1.0
                    and int(row["sig_size"]) == int(sig_size))
        except (TypeError, ValueError):
            return False
```

- [ ] **Step 5: Run test to verify it passes**

Run: `docker cp backend/. sh-plan-t1:/app/backend && MSYS_NO_PATHCONV=1 docker exec sh-plan-t1 sh -c "cd /app && python3 -m pytest tests/test_database_media_probe.py -q"`
Expected: `5 passed`

- [ ] **Step 6: Clean up the throwaway container and commit**

```bash
docker rm -f sh-plan-t1
git add backend/database.py tests/test_database_media_probe.py
git commit -m "feat(database): add media_probe ffprobe result cache"
```

---

### Task 2: `probe_specs()` cache integration

**Files:**
- Modify: `backend/rename/mediainfo.py` (`probe_specs`, ~line 69)
- Test: `tests/test_mediainfo.py` (add cases)

**Interfaces:**
- Consumes: Task 1's `db.get_media_probe`, `db.media_probe_is_current`, `db.upsert_media_probe`.
- Produces: `probe_specs(path, timeout=30, db=None)` unchanged signature/return shape — callers (Task 6/7, and the existing `conflict_preview`) get the same dict, just faster on a cache hit. **No new caller-visible behavior** — this task is pure performance, verified by asserting `subprocess.run` is NOT called on a cache hit.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_mediainfo.py`:

```python
def test_probe_specs_cache_hit_skips_ffprobe(tmp_path):
    f = tmp_path / "m.mkv"; f.write_bytes(b"x")
    st = f.stat()
    db = MagicMock()
    cached = {"present": True, "path": str(f), "size_bytes": 1, "container": "matroska",
              "duration_min": 1, "bitrate": 1, "resolution": "2160p", "video_codec": "HEVC",
              "hdr": None, "dv_layer": None, "audio": None}
    db.media_probe_is_current.return_value = True
    db.get_media_probe.return_value = {"probe_json": json.dumps(cached)}
    with patch("subprocess.run") as run_spy:
        s = mediainfo.probe_specs(str(f), db=db)
    run_spy.assert_not_called()
    assert s["resolution"] == "2160p"


def test_probe_specs_cache_miss_probes_and_caches(tmp_path):
    f = tmp_path / "m.mkv"; f.write_bytes(b"x")
    db = MagicMock()
    db.media_probe_is_current.return_value = False
    fake = MagicMock(returncode=0, stdout=FFPROBE_JSON)
    with patch("shutil.which", return_value="/usr/bin/ffprobe"), \
         patch("subprocess.run", return_value=fake) as run_spy:
        s = mediainfo.probe_specs(str(f), db=db)
    run_spy.assert_called_once()
    assert s["present"] is True
    db.upsert_media_probe.assert_called_once()
    args, kwargs = db.upsert_media_probe.call_args
    assert args[0] == str(f)
    assert json.loads(args[1])["resolution"] == "2160p"


def test_probe_specs_failed_probe_not_cached(tmp_path):
    f = tmp_path / "m.mkv"; f.write_bytes(b"x")
    db = MagicMock()
    db.media_probe_is_current.return_value = False
    with patch("shutil.which", return_value=None):
        s = mediainfo.probe_specs(str(f), db=db)
    assert s is None
    db.upsert_media_probe.assert_not_called()


def test_probe_specs_no_db_still_works_uncached(tmp_path):
    f = tmp_path / "m.mkv"; f.write_bytes(b"x")
    fake = MagicMock(returncode=0, stdout=FFPROBE_JSON)
    with patch("shutil.which", return_value="/usr/bin/ffprobe"), \
         patch("subprocess.run", return_value=fake):
        s = mediainfo.probe_specs(str(f))  # db=None, existing default
    assert s["present"] is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `docker rm -f sh-plan-t2 >/dev/null 2>&1; docker run -d --name sh-plan-t2 --entrypoint sleep scanhound:latest infinity >/dev/null && docker cp backend/. sh-plan-t2:/app/backend && docker cp tests/. sh-plan-t2:/app/tests && docker exec sh-plan-t2 pip install -q pytest && MSYS_NO_PATHCONV=1 docker exec sh-plan-t2 sh -c "cd /app && python3 -m pytest tests/test_mediainfo.py -q"`
Expected: FAIL — `test_probe_specs_cache_hit_skips_ffprobe` fails because `subprocess.run` IS called (no caching yet).

- [ ] **Step 3: Add the cache check + write-through to `probe_specs`**

In `backend/rename/mediainfo.py`, modify `probe_specs` (currently starts `def probe_specs(path: str, timeout: int = 30, db=None) -> Optional[dict]:` at line 69). Insert a cache-check right after the existing `{"present": False, ...}` early return (so a missing file is never probed OR cached — matches current behavior), and before the `ffprobe = shutil.which("ffprobe")` line:

```python
def probe_specs(path: str, timeout: int = 30, db=None) -> Optional[dict]:
    if not path:
        return None
    if not os.path.exists(path):
        return {"present": False, "path": path, "size_bytes": None,
                "container": None, "duration_min": None, "bitrate": None,
                "resolution": None, "video_codec": None, "hdr": None,
                "dv_layer": None, "audio": None}
    try:
        _st = os.stat(path)
        disk_mtime, disk_size = _st.st_mtime, _st.st_size
    except OSError:
        disk_mtime, disk_size = None, None
    # Cache check: a signature-matching prior probe is reused verbatim,
    # skipping the ffprobe subprocess entirely. A probe FAILURE (None) is
    # never cached (see the bottom of this function), so there's nothing to
    # hit here for a file that previously failed to probe.
    if db is not None and disk_mtime is not None and db.media_probe_is_current(path, disk_mtime, disk_size):
        cached_row = db.get_media_probe(path)
        if cached_row and cached_row.get("probe_json"):
            try:
                return json.loads(cached_row["probe_json"])
            except (json.JSONDecodeError, TypeError):
                pass  # corrupt cache row — fall through and re-probe
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return None
    try:
        r = subprocess.run(
            [ffprobe, "-v", "quiet", "-print_format", "json",
             "-show_format", "-show_streams", path],
            capture_output=True, text=True, timeout=timeout)
        if r.returncode != 0:
            return None
        data = json.loads(r.stdout)
    except Exception:
        return None

    fmt = data.get("format") or {}
    streams = data.get("streams") or []
    video = next((s for s in streams if s.get("codec_type") == "video"), {})
    audio = next((s for s in streams if s.get("codec_type") == "audio"), {})

    try:
        size = int(fmt.get("size")) if fmt.get("size") else os.path.getsize(path)
    except (TypeError, ValueError, OSError):
        size = None
    try:
        dur = float(fmt.get("duration")) if fmt.get("duration") else None
        duration_min = round(dur / 60) if dur and dur > 0 else None
    except (TypeError, ValueError):
        duration_min = None
    try:
        bitrate = int(fmt.get("bit_rate")) if fmt.get("bit_rate") else None
    except (TypeError, ValueError):
        bitrate = None

    vcodec = _CODEC_LABEL.get(str(video.get("codec_name") or "").lower(),
                              (video.get("codec_name") or None))
    resolution = _res_label(video.get("width"), video.get("height"))

    hdr = None
    sd = video.get("side_data_list") or []
    if any("dovi" in str(x.get("side_data_type", "")).lower()
           or "dolby vision" in str(x.get("side_data_type", "")).lower() for x in sd):
        hdr = "Dolby Vision"
    else:
        ct = str(video.get("color_transfer") or "").lower()
        if ct == "smpte2084":
            hdr = "HDR10"
        elif ct in ("arib-std-b67", "bt2020-10", "bt2020-12"):
            hdr = "HLG"

    acodec = _AUDIO_LABEL.get(str(audio.get("codec_name") or "").lower(),
                              (audio.get("codec_name") or None))
    chans = audio.get("channel_layout") or (
        f"{audio.get('channels')}ch" if audio.get("channels") else None)
    audio_label = f"{acodec} {chans}".strip() if acodec else None

    result = {
        "present": True, "path": path, "size_bytes": size,
        "container": (fmt.get("format_name") or None),
        "duration_min": duration_min, "bitrate": bitrate,
        "resolution": resolution, "video_codec": vcodec, "hdr": hdr,
        "dv_layer": _cached_dv_layer(path, disk_mtime, disk_size, db),
        "audio": audio_label,
    }
    if db is not None and disk_mtime is not None:
        try:
            db.upsert_media_probe(path, json.dumps(result),
                                   sig_mtime=disk_mtime, sig_size=disk_size)
        except Exception:
            pass  # cache write failure must never fail the probe itself
    return result
```

Note: `disk_mtime`/`disk_size` were previously computed *after* the ffprobe call (for `_cached_dv_layer` only) — this task moves that `os.stat` call earlier (before the cache check) since it's now needed twice. The existing `_cached_dv_layer(path, disk_mtime, disk_size, db)` call at the bottom is unchanged, just now reuses the already-computed `disk_mtime`/`disk_size` from the top instead of re-statting.

- [ ] **Step 4: Run test to verify it passes**

Run: `docker cp backend/. sh-plan-t2:/app/backend && MSYS_NO_PATHCONV=1 docker exec sh-plan-t2 sh -c "cd /app && python3 -m pytest tests/test_mediainfo.py -q"`
Expected: `14 passed` (10 pre-existing + 4 new)

- [ ] **Step 5: Clean up and commit**

```bash
docker rm -f sh-plan-t2
git add backend/rename/mediainfo.py tests/test_mediainfo.py
git commit -m "feat(mediainfo): cache probe_specs() results in media_probe"
```

---

### Task 3: New columns — `plex_cache.file_path` + `rename_jobs.conflict_analysis`

**Files:**
- Modify: `backend/database.py` (migration list ~line 590-598; `plex_cache` `CREATE TABLE` — find via `grep -n "CREATE TABLE IF NOT EXISTS plex_cache" backend/database.py`; `save_plex_cache` ~line 749-800; `_RENAME_FIELDS`/`_JSON_RENAME_FIELDS` ~line 2006-2019)
- Test: `tests/test_database_media_probe.py` (add plex_cache case), `tests/test_rename_service.py` (add conflict_analysis round-trip case, alongside existing `_RENAME_FIELDS` coverage)

**Interfaces:**
- Produces: `plex_cache` rows carry `file_path` (nullable TEXT); `rename_jobs.conflict_analysis` round-trips a Python dict transparently through `update_rename_job`/`get_rename_job` (same as `suggested_correction`/`match_reasons` already do).

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_database_media_probe.py`:

```python
def test_plex_cache_stores_file_path(tmp_path):
    db = _db(tmp_path)
    db.save_plex_cache([{
        "clean_title": "Movie", "original_title": "Movie", "year": 2024,
        "res": "4K", "size": 40.0, "imdb_id": "tt1", "rating_key": "1",
        "media_id": "1", "file": "/library/movies/Movie (2024)/Movie.mkv",
        "key": "1_1_0",
    }], "Movies")
    row = db._query_dicts("SELECT file_path FROM plex_cache WHERE key = ?", ("1_1_0",))
    assert row[0]["file_path"] == "/library/movies/Movie (2024)/Movie.mkv"


def test_plex_cache_file_path_defaults_null_when_absent(tmp_path):
    db = _db(tmp_path)
    db.save_plex_cache([{
        "clean_title": "Movie", "original_title": "Movie", "year": 2024,
        "res": "4K", "size": 40.0, "imdb_id": "tt1", "rating_key": "1",
        "media_id": "1", "key": "1_1_0",
    }], "Movies")
    row = db._query_dicts("SELECT file_path FROM plex_cache WHERE key = ?", ("1_1_0",))
    assert row[0]["file_path"] is None
```

Add to `tests/test_rename_service.py` (near the other `_RENAME_FIELDS` round-trip coverage — search `grep -n "suggested_correction" tests/test_rename_service.py` for a sibling example to place this beside):

```python
def test_conflict_analysis_round_trips_as_dict(db, tmp_path):
    svc = _service(db, _weak_search)
    jid, _ = _matched_job(db, tmp_path, "MovieCA")
    analysis = {"kind": "same_path", "existing": {"resolution": "1080p"},
                "incoming": {"resolution": "2160p"}, "recommended": "incoming",
                "reason": "2160p", "degraded": False, "analyzed_at": "2026-07-11T00:00:00+00:00"}
    db.update_rename_job(jid, conflict_analysis=analysis)
    job = db.get_rename_job(jid)
    assert job["conflict_analysis"] == analysis


def test_conflict_analysis_null_by_default(db, tmp_path):
    svc = _service(db, _weak_search)
    jid, _ = _matched_job(db, tmp_path, "MovieCA2")
    job = db.get_rename_job(jid)
    assert job["conflict_analysis"] is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `docker rm -f sh-plan-t3 >/dev/null 2>&1; docker run -d --name sh-plan-t3 --entrypoint sleep scanhound:latest infinity >/dev/null && docker cp backend/. sh-plan-t3:/app/backend && docker cp tests/. sh-plan-t3:/app/tests && docker exec sh-plan-t3 pip install -q pytest && MSYS_NO_PATHCONV=1 docker exec sh-plan-t3 sh -c "cd /app && python3 -m pytest tests/test_database_media_probe.py tests/test_rename_service.py -k 'file_path or conflict_analysis' -q"`
Expected: FAIL — `sqlite3.OperationalError: no such column: file_path` / `conflict_analysis`

- [ ] **Step 3: Add the migrations**

In `backend/database.py`'s `_column_migrations` list (ends at `'ALTER TABLE rename_jobs ADD COLUMN conflict_incoming_size INTEGER',` ~line 597), add two more entries right before the closing `]`:

```python
                    'ALTER TABLE rename_jobs ADD COLUMN conflict_incoming_size INTEGER',
                    # Duplicate-quality-comparison feature: the full computed
                    # diff (existing vs incoming specs, recommendation) for
                    # BOTH same-path and library-wide duplicates — supersedes
                    # the three conflict_*_size columns above for row display
                    # (they're still written by service.py's execution-time
                    # collision handling, just no longer read by the UI).
                    'ALTER TABLE rename_jobs ADD COLUMN conflict_analysis TEXT',
                    # The served path Plex reports for a movie (part.file) —
                    # plex_service.py already computes this per item; this
                    # column just stops discarding it, so a library-wide
                    # duplicate match (a different path than the incoming
                    # job's own destination) can be ffprobed directly.
                    'ALTER TABLE plex_cache ADD COLUMN file_path TEXT',
                ]
```

Also add `file_path TEXT` to the `plex_cache` `CREATE TABLE IF NOT EXISTS` block (for fresh installs — find it via `grep -n "CREATE TABLE IF NOT EXISTS plex_cache" backend/database.py`, add the column to the column list, e.g. right after `library_name TEXT`).

- [ ] **Step 4: Register `conflict_analysis` in the field/JSON registries**

In `backend/database.py`, modify `_RENAME_FIELDS` (~line 2006) and `_JSON_RENAME_FIELDS` (~line 2018):

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
    )

    # Fields stored as JSON TEXT in SQLite — auto-serialized/deserialized.
    _JSON_RENAME_FIELDS = frozenset({"suggested_correction", "combined_episode",
                                     "split_file", "match_reasons", "conflict_analysis"})
```

- [ ] **Step 5: Add `file_path` to `save_plex_cache`'s write**

In `backend/database.py`'s `save_plex_cache` (~line 776-800), add the column to the `INSERT OR REPLACE`:

```python
                    cursor.execute('''
                        INSERT OR REPLACE INTO plex_cache (
                            key, title, original_title, year, res, size, imdb_id,
                            rating_key, media_id, is_tv, season, episode_count,
                            content_type, dovi, hdr, last_updated, library_name,
                            file_path
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ''', (
                        item['key'],
                        item.get('clean_title'),
                        item.get('original_title'),
                        item.get('year'),
                        item.get('res'),
                        item.get('size'),
                        item.get('imdb_id'),
                        item.get('rating_key'),
                        item.get('media_id'),
                        1 if is_tv else 0,
                        item.get('season', 0),
                        item.get('episode_count', 0),
                        mode,
                        1 if item.get('dovi') else 0,
                        1 if item.get('hdr') else 0,
                        timestamp,
                        item.get('library_name') or library_name,
                        item.get('file'),
                    ))
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `docker cp backend/. sh-plan-t3:/app/backend && MSYS_NO_PATHCONV=1 docker exec sh-plan-t3 sh -c "cd /app && python3 -m pytest tests/test_database_media_probe.py tests/test_rename_service.py -q"`
Expected: all pass, including the 4 new tests (7 total in `test_database_media_probe.py`, +2 in `test_rename_service.py`)

- [ ] **Step 7: Clean up and commit**

```bash
docker rm -f sh-plan-t3
git add backend/database.py tests/test_database_media_probe.py tests/test_rename_service.py
git commit -m "feat(database): add plex_cache.file_path + rename_jobs.conflict_analysis columns"
```

---

### Task 4: `find_library_duplicate()` pure function

**Files:**
- Modify: `backend/rename/conflicts.py` (add function)
- Test: `tests/test_conflicts_rank.py` (add cases)

**Interfaces:**
- Consumes: nothing new — pure dicts in, matching `plex_cache` row shape (`{key, title, original_title, year, res, size, imdb_id, rating_key, media_id, is_tv, season, file_path, ...}`) and `rename_jobs` row shape (`{id, media_type, imdb_id, title, year, destination_path, new_filename, ...}`).
- Produces: `find_library_duplicate(job: dict, plex_cache_rows: list[dict]) -> dict | None` — returns the matched `plex_cache` row, or `None` if there's no match, the job is TV, the job's status isn't in `_ACTIVE_STATUSES` (an applied/failed/reverted job has nothing left to resolve), or the match is at the job's own destination path. Consumed by Task 6 (analyzer) and Task 7 (route wiring).

- [ ] **Step 1: Write the failing test**

Add to `tests/test_conflicts_rank.py`:

```python
def test_find_library_duplicate_matches_by_imdb_id():
    job = {"id": 1, "status": "matched", "media_type": "movie", "imdb_id": "tt123", "title": "X", "year": 2020,
           "destination_path": "/library/movies-4k/X (2020)", "new_filename": "X (2020).mkv"}
    rows = [{"key": "k1", "imdb_id": "tt999", "title": "y", "year": 2019, "is_tv": 0, "file_path": "/a"},
            {"key": "k2", "imdb_id": "tt123", "title": "x", "year": 2020, "is_tv": 0, "file_path": "/library/movies/X (2020)/X.mkv"}]
    match = conflicts.find_library_duplicate(job, rows)
    assert match is not None
    assert match["key"] == "k2"


def test_find_library_duplicate_falls_back_to_title_year_when_no_imdb_match():
    job = {"id": 1, "status": "matched", "media_type": "movie", "imdb_id": None, "title": "The Movie", "year": 2020,
           "destination_path": "/library/movies-4k/The Movie (2020)", "new_filename": "The Movie (2020).mkv"}
    rows = [{"key": "k1", "imdb_id": None, "title": "the movie", "year": 2020, "is_tv": 0, "file_path": "/library/movies/The Movie (2020)/f.mkv"}]
    match = conflicts.find_library_duplicate(job, rows)
    assert match is not None and match["key"] == "k1"


def test_find_library_duplicate_no_match_returns_none():
    job = {"id": 1, "status": "matched", "media_type": "movie", "imdb_id": "tt1", "title": "X", "year": 2020,
           "destination_path": "/library/movies-4k/X (2020)", "new_filename": "X (2020).mkv"}
    rows = [{"key": "k1", "imdb_id": "tt2", "title": "y", "year": 2021, "is_tv": 0, "file_path": "/a"}]
    assert conflicts.find_library_duplicate(job, rows) is None


def test_find_library_duplicate_excludes_same_path_match():
    # If the matched Plex row's file_path IS the job's own would-be
    # destination, that's the exact-path case (already covered by
    # destination_conflict) — not a library-wide duplicate.
    job = {"id": 1, "status": "matched", "media_type": "movie", "imdb_id": "tt1", "title": "X", "year": 2020,
           "destination_path": "/library/movies-4k/X (2020)", "new_filename": "X (2020).mkv"}
    rows = [{"key": "k1", "imdb_id": "tt1", "title": "x", "year": 2020, "is_tv": 0,
             "file_path": "/library/movies-4k/X (2020)/X (2020).mkv"}]
    assert conflicts.find_library_duplicate(job, rows) is None


def test_find_library_duplicate_tv_job_always_none():
    job = {"id": 1, "status": "matched", "media_type": "tv", "imdb_id": "tt1", "title": "X", "year": 2020,
           "destination_path": "/library/tv/X", "new_filename": "X S01E01.mkv"}
    rows = [{"key": "k1", "imdb_id": "tt1", "title": "x", "year": 2020, "is_tv": 0, "file_path": "/a"}]
    assert conflicts.find_library_duplicate(job, rows) is None


def test_find_library_duplicate_applied_job_always_none():
    # An already-applied job has nothing left to resolve — must never be
    # flagged (would waste an analysis cycle on a completed job).
    job = {"id": 1, "status": "applied", "media_type": "movie", "imdb_id": "tt1", "title": "X", "year": 2020,
           "destination_path": "/library/movies-4k/X (2020)", "new_filename": "X (2020).mkv"}
    rows = [{"key": "k1", "imdb_id": "tt1", "title": "x", "year": 2020, "is_tv": 0, "file_path": "/library/movies/X (2020)/X.mkv"}]
    assert conflicts.find_library_duplicate(job, rows) is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `docker rm -f sh-plan-t4 >/dev/null 2>&1; docker run -d --name sh-plan-t4 --entrypoint sleep scanhound:latest infinity >/dev/null && docker cp backend/. sh-plan-t4:/app/backend && docker cp tests/. sh-plan-t4:/app/tests && docker exec sh-plan-t4 pip install -q pytest && MSYS_NO_PATHCONV=1 docker exec sh-plan-t4 sh -c "cd /app && python3 -m pytest tests/test_conflicts_rank.py -k find_library_duplicate -q"`
Expected: FAIL — `AttributeError: module 'backend.rename.conflicts' has no attribute 'find_library_duplicate'`

- [ ] **Step 3: Implement the function**

In `backend/rename/conflicts.py`, add near the bottom (after `rank_conflict`, which currently ends the file):

```python
def _full_dest_path(job: dict) -> Optional[str]:
    """Job's would-be full destination path, or None if not yet targeted."""
    dest = (job.get("destination_path") or "").rstrip("/\\")
    name = job.get("new_filename") or ""
    if not dest or not name:
        return None
    return f"{dest}/{name}".replace("\\", "/").casefold()


def find_library_duplicate(job: dict, plex_cache_rows: list) -> Optional[dict]:
    """Match *job* against the Plex library by imdb_id (exact) or normalized
    title+year (fallback), for movies only. Returns the matched plex_cache
    row, or None if there's no match, the job is TV, the job isn't in an
    ACTIVE status (an applied/failed/reverted job has nothing left to
    resolve — matches _ACTIVE_STATUSES' semantics, same statuses
    destination_conflict is restricted to), or the only match is at the
    job's own destination path (that's the exact-path case, already covered
    by destination_conflict — never double-flag it here).

    Pure, DB-free — plex_cache_rows is whatever the caller already fetched."""
    if (job.get("media_type") or "movie") != "movie":
        return None
    if job.get("status") not in _ACTIVE_STATUSES:
        return None
    imdb_id = job.get("imdb_id")
    candidates = [r for r in plex_cache_rows if not r.get("is_tv")]
    match = None
    if imdb_id:
        match = next((r for r in candidates if r.get("imdb_id") == imdb_id), None)
    if not match:
        from backend.app_service import normalize_title
        job_key = (normalize_title(job.get("title") or ""), job.get("year"))
        if job_key[0]:
            match = next(
                (r for r in candidates
                 if (normalize_title(r.get("title") or ""), r.get("year")) == job_key),
                None)
    if not match:
        return None
    job_dest = _full_dest_path(job)
    match_path = (match.get("file_path") or "").replace("\\", "/").casefold()
    if job_dest and match_path and job_dest == match_path:
        return None  # same-path — the exact-path collision case, not this one
    return match
```

- [ ] **Step 4: Run test to verify it passes**

Run: `docker cp backend/. sh-plan-t4:/app/backend && MSYS_NO_PATHCONV=1 docker exec sh-plan-t4 sh -c "cd /app && python3 -m pytest tests/test_conflicts_rank.py -q"`
Expected: all pass (pre-existing + 5 new)

- [ ] **Step 5: Clean up and commit**

```bash
docker rm -f sh-plan-t4
git add backend/rename/conflicts.py tests/test_conflicts_rank.py
git commit -m "feat(conflicts): add find_library_duplicate() for cross-path duplicate detection"
```

---

### Task 5: Smart FEL/MEL gate — `needs_dv_layer_scan()`

**Files:**
- Modify: `backend/rename/conflicts.py` (add function)
- Test: `tests/test_conflicts_rank.py` (add cases)

**Interfaces:**
- Consumes: `_quality_score()` (already in this file).
- Produces: `needs_dv_layer_scan(existing: dict, incoming: dict) -> bool`. Consumed by Task 6 (analyzer) to gate the `dovi_tool` call.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_conflicts_rank.py`:

```python
def test_needs_dv_layer_scan_true_when_both_dv_and_tied_on_everything_else():
    existing = {"resolution": "2160p", "hdr": "Dolby Vision", "dv_layer": None,
                "original_filename": "a.mkv"}
    incoming = {"resolution": "2160p", "hdr": "Dolby Vision", "dv_layer": None,
                "original_filename": "a.mkv"}
    assert conflicts.needs_dv_layer_scan(existing, incoming) is True


def test_needs_dv_layer_scan_false_when_resolution_already_decides_it():
    existing = {"resolution": "2160p", "hdr": "Dolby Vision", "dv_layer": None,
                "original_filename": "a.mkv"}
    incoming = {"resolution": "1080p", "hdr": "Dolby Vision", "dv_layer": None,
                "original_filename": "a.mkv"}
    assert conflicts.needs_dv_layer_scan(existing, incoming) is False


def test_needs_dv_layer_scan_false_when_neither_side_is_dv():
    existing = {"resolution": "2160p", "hdr": None, "dv_layer": None, "original_filename": "a.mkv"}
    incoming = {"resolution": "2160p", "hdr": None, "dv_layer": None, "original_filename": "a.mkv"}
    assert conflicts.needs_dv_layer_scan(existing, incoming) is False


def test_needs_dv_layer_scan_false_when_only_one_side_is_dv():
    existing = {"resolution": "2160p", "hdr": "Dolby Vision", "dv_layer": None, "original_filename": "a.mkv"}
    incoming = {"resolution": "2160p", "hdr": None, "dv_layer": None, "original_filename": "b.mkv"}
    assert conflicts.needs_dv_layer_scan(existing, incoming) is False


def test_needs_dv_layer_scan_false_when_dv_layer_already_known_on_both():
    # Already resolved (e.g. a prior scan) — nothing left to gain by re-scanning.
    existing = {"resolution": "2160p", "hdr": "Dolby Vision", "dv_layer": "mel", "original_filename": "a.mkv"}
    incoming = {"resolution": "2160p", "hdr": "Dolby Vision", "dv_layer": "fel", "original_filename": "a.mkv"}
    assert conflicts.needs_dv_layer_scan(existing, incoming) is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `docker cp backend/. sh-plan-t4:/app/backend && docker cp tests/. sh-plan-t4:/app/tests && MSYS_NO_PATHCONV=1 docker exec sh-plan-t4 sh -c "cd /app && python3 -m pytest tests/test_conflicts_rank.py -k needs_dv_layer_scan -q"`
Expected: FAIL — `AttributeError: module 'backend.rename.conflicts' has no attribute 'needs_dv_layer_scan'`

- [ ] **Step 3: Implement the function**

In `backend/rename/conflicts.py`, add after `find_library_duplicate`:

```python
def needs_dv_layer_scan(existing: dict, incoming: dict) -> bool:
    """Whether the FEL/MEL layer is the SOLE remaining tiebreaker between two
    probed specs, i.e. whether a dovi_tool scan is actually worth its
    multi-minute cost.

    _quality_score()'s comparison tuple is (res_rank, dv, dv_layer_rank, hdr,
    source, audio, edition) — dv_layer_rank (index 2) is exactly the field a
    scan would resolve. Recompute both tuples with index 2 forced to 0 (as if
    unscanned): if those modified tuples are equal AND both sides are
    Dolby Vision (dv == 1), the real tuples can only differ, if at all, on
    dv_layer_rank — worth resolving. Any other outcome means some other tier
    already decides it, or one/both sides aren't DV — skip the scan."""
    se = list(_quality_score(existing))
    si = list(_quality_score(incoming))
    both_dv = se[1] == 1 and si[1] == 1
    se[2] = 0
    si[2] = 0
    return both_dv and tuple(se) == tuple(si)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `docker cp backend/. sh-plan-t4:/app/backend && MSYS_NO_PATHCONV=1 docker exec sh-plan-t4 sh -c "cd /app && python3 -m pytest tests/test_conflicts_rank.py -q"`
Expected: all pass (previous + 5 new)

- [ ] **Step 5: Clean up and commit**

```bash
docker rm -f sh-plan-t4
git add backend/rename/conflicts.py tests/test_conflicts_rank.py
git commit -m "feat(conflicts): add needs_dv_layer_scan() cost-control gate"
```

---

### Task 6: Background analyzer module

**Files:**
- Create: `backend/rename/conflict_analyzer.py`
- Test: `tests/test_conflict_analyzer.py` (new)

**Interfaces:**
- Consumes: `mediainfo.probe_specs` (Task 2, cache-backed), `conflicts.rank_conflict`/`conflicts.conflict_annotations` (existing)/`conflicts.find_library_duplicate` (Task 4)/`conflicts.needs_dv_layer_scan` (Task 5), `dv_detect.detect_layer`/`dv_detect.available` (existing), `db.get_rename_job`/`db.update_rename_job` (existing, now carries `conflict_analysis` per Task 3), a new `db.list_plex_cache_movies() -> list[dict]` helper (added in this task — needed by `find_library_duplicate`).
- Produces: `has_active_duplicate(job, annotations, plex_cache_rows) -> bool` (shared "does this job actually have a duplicate" definition), `analyze_job_conflict(db, job: dict, plex_cache_rows: list | None = None) -> dict | None` (the core single-job analysis; returns the `conflict_analysis` dict written to the job, or `None` if the job has no active duplicate to analyze) and `analyze_pending_conflicts(db, limit: int = 50) -> int` (the maintenance-loop-callable sweep, pre-filtered by `has_active_duplicate` so the limit budget is never wasted on non-duplicate jobs; returns count analyzed). All are plain functions — no class, no `self` — mirrors `pipeline_service.reconcile_batch(db, ...)`'s existing shape so `AppService` (which has no `RenameService` reference) can call `analyze_pending_conflicts` directly, and `RenameService`/the route layer (Task 7) can call it too by passing `self._db`.

- [ ] **Step 1: Add the `list_plex_cache_movies` DB helper first (small prerequisite)**

In `backend/database.py`, add near `save_plex_cache` (e.g. right after it):

```python
    def list_plex_cache_movies(self):
        """Return every plex_cache row for content_type='Movies' (dicts) — the
        candidate pool for find_library_duplicate()."""
        return self._query_dicts(
            "SELECT key, title, original_title, year, res, size, imdb_id, "
            "rating_key, media_id, is_tv, dovi, hdr, file_path "
            "FROM plex_cache WHERE content_type = 'Movies'", default=[])
```

Test (add to `tests/test_database_media_probe.py`):

```python
def test_list_plex_cache_movies_returns_movie_rows_only(tmp_path):
    db = _db(tmp_path)
    db.save_plex_cache([{"clean_title": "M", "original_title": "M", "year": 2020,
                          "res": "4K", "size": 1.0, "imdb_id": "tt1", "rating_key": "1",
                          "media_id": "1", "key": "1_1_0", "file": "/m.mkv"}], "Movies")
    db.save_plex_cache([{"clean_title": "S", "original_title": "S", "year": 2020,
                          "res": "1080p", "size": 1.0, "imdb_id": "tt2",
                          "rating_key": "2", "key": "s2", "season": 1}], "TV Shows")
    rows = db.list_plex_cache_movies()
    assert len(rows) == 1
    assert rows[0]["imdb_id"] == "tt1"
```

Run: `docker rm -f sh-plan-t6 >/dev/null 2>&1; docker run -d --name sh-plan-t6 --entrypoint sleep scanhound:latest infinity >/dev/null && docker cp backend/. sh-plan-t6:/app/backend && docker cp tests/. sh-plan-t6:/app/tests && docker exec sh-plan-t6 pip install -q pytest && MSYS_NO_PATHCONV=1 docker exec sh-plan-t6 sh -c "cd /app && python3 -m pytest tests/test_database_media_probe.py -k list_plex_cache_movies -q"` — verify it FAILS first (AttributeError), then add the method above, re-run, verify PASS.

- [ ] **Step 2: Write the failing tests for the analyzer module**

```python
# tests/test_conflict_analyzer.py
from unittest.mock import MagicMock, patch
from backend.rename import conflict_analyzer


def _job(**over):
    base = {"id": 1, "status": "matched", "media_type": "movie", "title": "X", "year": 2020,
            "imdb_id": "tt1", "original_path": "/incoming/X.mkv",
            "destination_path": "/library/movies/X (2020)",
            "new_filename": "X (2020).mkv", "conflict_analysis": None,
            "detected_at": "2026-07-11T00:00:00+00:00"}
    base.update(over)
    return base


def test_analyze_job_conflict_same_path_writes_analysis():
    db = MagicMock()
    db.get_rename_job.return_value = _job()
    with patch("os.path.lexists", return_value=True), \
         patch("backend.rename.conflict_analyzer.probe_specs") as probe_mock:
        probe_mock.side_effect = [
            {"present": True, "resolution": "2160p", "hdr": None, "dv_layer": None,
             "audio": "AC3", "size_bytes": 10, "path": "/library/movies/X (2020)/X (2020).mkv"},
            {"present": True, "resolution": "1080p", "hdr": None, "dv_layer": None,
             "audio": "AC3", "size_bytes": 5, "path": "/incoming/X.mkv"},
        ]
        result = conflict_analyzer.analyze_job_conflict(db, _job(), plex_cache_rows=[])
    assert result["kind"] == "same_path"
    assert result["recommended"] == "existing"
    db.update_rename_job.assert_called_once()
    args, kwargs = db.update_rename_job.call_args
    assert args[0] == 1
    assert kwargs["conflict_analysis"]["kind"] == "same_path"


def test_analyze_job_conflict_library_duplicate_writes_analysis():
    db = MagicMock()
    plex_rows = [{"key": "k1", "imdb_id": "tt1", "title": "x", "year": 2020, "is_tv": 0,
                  "file_path": "/library/movies-other/X (2020)/X.mkv", "rating_key": "99"}]
    with patch("os.path.lexists", return_value=False), \
         patch("backend.rename.conflict_analyzer.probe_specs") as probe_mock:
        probe_mock.side_effect = [
            {"present": True, "resolution": "1080p", "hdr": None, "dv_layer": None,
             "audio": "AC3", "size_bytes": 5, "path": "/library/movies-other/X (2020)/X.mkv"},
            {"present": True, "resolution": "2160p", "hdr": None, "dv_layer": None,
             "audio": "AC3", "size_bytes": 10, "path": "/incoming/X.mkv"},
        ]
        result = conflict_analyzer.analyze_job_conflict(db, _job(), plex_cache_rows=plex_rows)
    assert result["kind"] == "library_duplicate"
    assert result["recommended"] == "incoming"


def test_analyze_job_conflict_no_duplicate_returns_none():
    db = MagicMock()
    with patch("os.path.lexists", return_value=False):
        result = conflict_analyzer.analyze_job_conflict(db, _job(), plex_cache_rows=[])
    assert result is None
    db.update_rename_job.assert_not_called()


def test_analyze_job_conflict_degraded_when_probe_fails():
    # probe_specs returns None only on a genuine ffprobe FAILURE (missing
    # binary/timeout/error) — never for a merely-absent file, which it
    # already reports as a full {"present": False, ...} dict itself. This
    # test exercises that genuine-failure path.
    db = MagicMock()
    with patch("os.path.lexists", return_value=True), \
         patch("backend.rename.conflict_analyzer.probe_specs", return_value=None):
        result = conflict_analyzer.analyze_job_conflict(db, _job(), plex_cache_rows=[])
    assert result["degraded"] is True
    assert result["recommended"] is None
    # The fallback dict must carry every FileSpec field (as null), matching
    # probe_specs' OWN not-present shape exactly — not an abbreviated dict —
    # so the frontend's FileSpec type never sees a field silently missing.
    assert set(result["existing"].keys()) == {
        "present", "path", "size_bytes", "container", "duration_min",
        "bitrate", "resolution", "video_codec", "hdr", "dv_layer", "audio"}


def test_analyze_job_conflict_fires_detect_layer_when_gate_says_yes():
    db = MagicMock()
    with patch("os.path.lexists", return_value=True), \
         patch("backend.rename.conflict_analyzer.probe_specs") as probe_mock, \
         patch("backend.rename.conflict_analyzer._dv.available", return_value=True), \
         patch("backend.rename.conflict_analyzer._dv.detect_layer") as detect_mock:
        probe_mock.side_effect = [
            {"present": True, "resolution": "2160p", "hdr": "Dolby Vision", "dv_layer": None,
             "audio": "AC3", "size_bytes": 10, "path": "/library/movies/X (2020)/X (2020).mkv"},
            {"present": True, "resolution": "2160p", "hdr": "Dolby Vision", "dv_layer": None,
             "audio": "AC3", "size_bytes": 10, "path": "/incoming/X.mkv"},
        ]
        detect_mock.return_value = {"layer": "fel", "tool": True, "error": None}
        conflict_analyzer.analyze_job_conflict(db, _job(), plex_cache_rows=[])
    assert detect_mock.call_count == 2  # both sides scanned


def test_analyze_job_conflict_skips_detect_layer_when_gate_says_no():
    db = MagicMock()
    with patch("os.path.lexists", return_value=True), \
         patch("backend.rename.conflict_analyzer.probe_specs") as probe_mock, \
         patch("backend.rename.conflict_analyzer._dv.detect_layer") as detect_mock:
        probe_mock.side_effect = [
            {"present": True, "resolution": "1080p", "hdr": None, "dv_layer": None,
             "audio": "AC3", "size_bytes": 10, "path": "/library/movies/X (2020)/X (2020).mkv"},
            {"present": True, "resolution": "2160p", "hdr": "Dolby Vision", "dv_layer": None,
             "audio": "AC3", "size_bytes": 10, "path": "/incoming/X.mkv"},
        ]
        conflict_analyzer.analyze_job_conflict(db, _job(), plex_cache_rows=[])
    detect_mock.assert_not_called()


def test_analyze_pending_conflicts_only_counts_jobs_with_an_active_duplicate_flag():
    # 3 jobs total, but only 1 has an actual duplicate (destination_conflict
    # via conflict_annotations) — the other 2 are plain matched jobs with no
    # conflict at all. The 50(here: 2)-per-pass limit must apply to the
    # FILTERED set, not the raw job list, or the budget is mostly wasted on
    # non-duplicates every pass.
    db = MagicMock()
    dup_job = _job(id=1, destination_path="/library/movies/Dup (2020)",
                   new_filename="Dup (2020).mkv")
    other_dup_job = _job(id=2, destination_path="/library/movies/Dup (2020)",
                         new_filename="Dup (2020).mkv")  # same dest as dup_job -> conflict pair
    plain_job = _job(id=3, imdb_id="tt999", destination_path="/library/movies/Plain (2020)",
                     new_filename="Plain (2020).mkv")
    db.list_rename_jobs.return_value = [dup_job, other_dup_job, plain_job]
    db.list_plex_cache_movies.return_value = []
    with patch("backend.rename.conflict_analyzer.analyze_job_conflict", return_value=None) as analyze_mock:
        n = conflict_analyzer.analyze_pending_conflicts(db, limit=50)
    analyzed_ids = {call.args[1]["id"] for call in analyze_mock.call_args_list}
    assert analyzed_ids == {1, 2}  # the conflicting pair only — plain_job excluded
    assert n == 2


def test_analyze_pending_conflicts_respects_limit_within_the_filtered_set():
    db = MagicMock()
    # 4 jobs all sharing one destination -> all 4 flagged destination_conflict.
    jobs = [_job(id=i, destination_path="/library/movies/Dup (2020)",
                new_filename="Dup (2020).mkv") for i in range(4)]
    db.list_rename_jobs.return_value = jobs
    db.list_plex_cache_movies.return_value = []
    with patch("backend.rename.conflict_analyzer.analyze_job_conflict", return_value=None) as analyze_mock:
        n = conflict_analyzer.analyze_pending_conflicts(db, limit=2)
    assert analyze_mock.call_count == 2
    assert n == 2
```

- [ ] **Step 3: Run test to verify it fails**

Run: `docker cp backend/. sh-plan-t6:/app/backend && docker cp tests/. sh-plan-t6:/app/tests && MSYS_NO_PATHCONV=1 docker exec sh-plan-t6 sh -c "cd /app && python3 -m pytest tests/test_conflict_analyzer.py -q"`
Expected: FAIL — `ModuleNotFoundError: No module named 'backend.rename.conflict_analyzer'`

- [ ] **Step 4: Implement `backend/rename/conflict_analyzer.py`**

```python
"""Background duplicate-quality analysis — fills rename_jobs.conflict_analysis
for every active duplicate (same-destination-path collision or a library-wide
match at a different path), so the Renames row can show a real quality diff
without the user opening the Compare modal.

A plain module of functions, not a class — mirrors pipeline_service's
reconcile_batch(db, ...) shape so both the route layer (RenameService has a
db) and AppService's maintenance loop (no RenameService reference at all) can
call analyze_pending_conflicts(db) directly.
"""
from __future__ import annotations

import logging
import os
from typing import Optional

from backend.rename import dv_detect as _dv
from backend.rename.conflicts import (
    conflict_annotations, find_library_duplicate, needs_dv_layer_scan, rank_conflict,
)
from backend.rename.mediainfo import probe_specs

logger = logging.getLogger(__name__)

# The full not-present FileSpec shape probe_specs() itself returns for a
# missing file — reused here for the "no path to probe at all" case so every
# conflict_analysis.existing/incoming is ALWAYS the same full shape (every
# key present, unknowns as null) regardless of which branch produced it.
# Never abbreviate this dict — the frontend's FileSpec type expects every key.
_ABSENT_SPEC_FIELDS = ("size_bytes", "container", "duration_min", "bitrate",
                       "resolution", "video_codec", "hdr", "dv_layer", "audio")


def _absent_spec(path: Optional[str]) -> dict:
    return {"present": False, "path": path, **{k: None for k in _ABSENT_SPEC_FIELDS}}


def _now_iso() -> str:
    import datetime
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _job_dest(job: dict) -> Optional[str]:
    dest = (job.get("destination_path") or "").rstrip("/\\")
    name = job.get("new_filename") or os.path.basename(job.get("original_path") or "")
    if not dest or not name:
        return None
    return os.path.join(dest, name)


def has_active_duplicate(job: dict, annotations: dict, plex_cache_rows: list) -> bool:
    """Whether *job* has an active duplicate worth analyzing — either an
    exact-destination-path collision (from conflict_annotations, computed
    over the whole active job list) or a library-wide match at a different
    path. Shared by analyze_pending_conflicts' pre-filter and (indirectly,
    via the route's own equivalent check) the list route's trigger — kept
    here so both use the identical definition of "active duplicate"."""
    ann = annotations.get(job.get("id")) or {}
    if ann.get("destination_conflict"):
        return True
    return find_library_duplicate(job, plex_cache_rows) is not None


def analyze_job_conflict(db, job: dict, plex_cache_rows: Optional[list] = None) -> Optional[dict]:
    """Analyze one job's active duplicate (if any) and persist the result.

    Resolution order: an exact-destination-path collision (a file already on
    disk at the job's would-be destination) takes priority over a
    library-wide match — they're mutually exclusive by construction
    (find_library_duplicate excludes same-path matches). Returns the written
    conflict_analysis dict, or None if this job has no active duplicate to
    analyze (nothing is written in that case)."""
    incoming_path = job.get("original_path")
    dest = _job_dest(job)
    kind = None
    existing_path = None

    if dest and os.path.lexists(dest):
        kind = "same_path"
        existing_path = dest
    else:
        rows = plex_cache_rows if plex_cache_rows is not None else db.list_plex_cache_movies()
        match = find_library_duplicate(job, rows)
        if match and match.get("file_path"):
            kind = "library_duplicate"
            existing_path = match["file_path"]

    if kind is None:
        return None

    # probe_specs() already returns a full not-present dict for a missing
    # file (checked internally via os.path.exists) — never pre-check
    # existence here, or the fallback shape drifts from probe_specs' own.
    # It returns None only for a genuine ffprobe FAILURE (missing binary,
    # timeout, bad output) — THAT is what "degraded" means.
    existing = probe_specs(existing_path, db=db) if existing_path else _absent_spec(existing_path)
    incoming = probe_specs(incoming_path, db=db) if incoming_path else _absent_spec(incoming_path)

    degraded = existing is None or incoming is None
    existing = existing or _absent_spec(existing_path)
    incoming = incoming or _absent_spec(incoming_path)

    if not degraded and existing.get("present") and incoming.get("present") \
            and _dv.available() and needs_dv_layer_scan(existing, incoming):
        try:
            e_layer = _dv.detect_layer(existing_path).get("layer")
            i_layer = _dv.detect_layer(incoming_path).get("layer")
            if e_layer and e_layer != _dv.LAYER_UNKNOWN:
                existing = {**existing, "dv_layer": e_layer}
            if i_layer and i_layer != _dv.LAYER_UNKNOWN:
                incoming = {**incoming, "dv_layer": i_layer}
        except Exception:
            logger.exception("conflict_analyzer: DV layer scan failed for job %s", job.get("id"))

    if degraded:
        rec = {"recommended": None, "reason": None}
    else:
        rec = rank_conflict(existing if existing.get("present") else None,
                            {**incoming, "id": job.get("id")})

    analysis = {
        "kind": kind,
        "existing": existing,
        "incoming": incoming,
        "recommended": rec["recommended"],
        "reason": rec["reason"],
        "degraded": degraded,
        "analyzed_at": _now_iso(),
    }
    try:
        db.update_rename_job(job["id"], conflict_analysis=analysis)
    except Exception:
        logger.exception("conflict_analyzer: could not write analysis for job %s", job.get("id"))
    return analysis


def analyze_pending_conflicts(db, limit: int = 50) -> int:
    """Maintenance-loop sweep: find active jobs that ACTUALLY have a duplicate
    (has_active_duplicate) with missing/stale conflict_analysis (older than
    detected_at — a duplicate that only became detectable after the job's
    own creation), analyze up to *limit* of them.

    The has_active_duplicate pre-filter matters: without it, *limit* would
    apply to every matched/needs_review job (the vast majority of which have
    no duplicate at all), starving genuine duplicates of the expensive
    ffprobe/dovi_tool budget behind a wall of cheap non-duplicate jobs ahead
    of them in list order. Per-job try/except — one bad job never stops the
    sweep. Returns the count actually processed."""
    if db is None:
        return 0
    jobs = db.list_rename_jobs(limit=100000) or []
    active = [j for j in jobs if j.get("status") in ("matched", "needs_review")]
    # conflict_annotations() needs the WHOLE active-job list (incl. applied)
    # to correctly group same-destination collisions — same as the route.
    annotations = conflict_annotations(jobs)
    plex_rows = db.list_plex_cache_movies()
    candidates = [
        j for j in active
        if has_active_duplicate(j, annotations, plex_rows)
        and (j.get("conflict_analysis") is None
             or (j.get("conflict_analysis") or {}).get("analyzed_at", "") < (j.get("detected_at") or ""))
    ][:limit]
    n = 0
    for job in candidates:
        try:
            analyze_job_conflict(db, job, plex_cache_rows=plex_rows)
        except Exception:
            logger.exception("analyze_pending_conflicts: job %s failed", job.get("id"))
        n += 1
    return n
```

- [ ] **Step 5: Run test to verify it passes**

Run: `docker cp backend/. sh-plan-t6:/app/backend && MSYS_NO_PATHCONV=1 docker exec sh-plan-t6 sh -c "cd /app && python3 -m pytest tests/test_conflict_analyzer.py tests/test_database_media_probe.py -q"`
Expected: all pass (7 analyzer tests + prior)

- [ ] **Step 6: Clean up and commit**

```bash
docker rm -f sh-plan-t6
git add backend/database.py backend/rename/conflict_analyzer.py tests/test_conflict_analyzer.py tests/test_database_media_probe.py
git commit -m "feat(rename): add background conflict analyzer (analyze_job_conflict/analyze_pending_conflicts)"
```

---

### Task 7: Wire into `GET /rename/jobs` route + maintenance loop

**Files:**
- Modify: `backend/api/routes/rename.py` (`list_jobs`, ~line 130-160)
- Modify: `backend/app_service.py` (`_run_maintenance_pass`, ~line 569-604)
- Test: `tests/test_api_rename.py` (add route-level case), `tests/test_app_service_maintenance.py` (new, small)

**Interfaces:**
- Consumes: Task 4's `find_library_duplicate`, Task 6's `analyze_job_conflict`/`analyze_pending_conflicts`.
- Produces: `GET /rename/jobs` response jobs carry `library_duplicate: bool`; a job newly flagged (`destination_conflict` or `library_duplicate` True) with no `conflict_analysis` gets a background analysis thread fired, de-duplicated by an in-process "currently analyzing" set so rapid repeat polls of the same job don't spawn redundant threads.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_api_rename.py` (check `grep -n "^from\|^import\|def _client\|TestClient" tests/test_api_rename.py | head -10` first for the existing client-fixture pattern and mirror it exactly):

```python
def test_list_jobs_annotates_library_duplicate(client, db, tmp_path):
    # A movie job whose destination is free, but a same-title/year Plex row
    # exists at a DIFFERENT path — must be flagged library_duplicate.
    db.save_plex_cache([{
        "clean_title": "Dup Movie", "original_title": "Dup Movie", "year": 2021,
        "res": "1080p", "size": 5.0, "imdb_id": "tt777", "rating_key": "5",
        "media_id": "5", "key": "5_5_0", "file": "/library/movies/Dup Movie (2021)/f.mkv",
    }], "Movies")
    job_id = db.create_rename_job({
        "original_path": str(tmp_path / "src.mkv"), "original_filename": "src.mkv",
        "status": "matched", "media_type": "movie", "title": "Dup Movie", "year": 2021,
        "imdb_id": "tt777", "destination_path": "/library/movies-4k/Dup Movie (2021)",
        "new_filename": "Dup Movie (2021) [2160p].mkv",
    })
    resp = client.get("/rename/jobs")
    body = resp.json()
    job = next(j for j in body["jobs"] if j["id"] == job_id)
    assert job["library_duplicate"] is True
```

```python
# tests/test_app_service_maintenance.py
from unittest.mock import MagicMock, patch
from backend.app_service import AppService


def test_maintenance_pass_calls_analyze_pending_conflicts():
    svc = AppService.__new__(AppService)  # bypass __init__'s heavy service wiring
    svc.db = MagicMock()
    svc.config = MagicMock()
    svc.config.get.side_effect = lambda k, d=None: d
    # _run_maintenance_pass also runs trash-sweep and pipeline-reconcile in
    # their own try/except blocks — explicitly no-op them so this test only
    # exercises (and can't be accidentally affected by) the conflict-analysis
    # block, and never touches the real filesystem via fileops.sweep_trash.
    with patch("backend.rename.fileops.sweep_trash", return_value={}), \
         patch("backend.pipeline_service.reconcile_batch", return_value=0), \
         patch("backend.rename.conflict_analyzer.analyze_pending_conflicts", return_value=3) as analyze_mock:
        svc._run_maintenance_pass()
    analyze_mock.assert_called_once_with(svc.db, limit=50)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `docker rm -f sh-plan-t7 >/dev/null 2>&1; docker run -d --name sh-plan-t7 --entrypoint sleep scanhound:latest infinity >/dev/null && docker cp backend/. sh-plan-t7:/app/backend && docker cp tests/. sh-plan-t7:/app/tests && docker exec sh-plan-t7 pip install -q pytest httpx && MSYS_NO_PATHCONV=1 docker exec sh-plan-t7 sh -c "cd /app && python3 -m pytest tests/test_api_rename.py -k library_duplicate tests/test_app_service_maintenance.py -q"`
Expected: FAIL — `KeyError: 'library_duplicate'` / `AssertionError` (analyze_pending_conflicts not called)

- [ ] **Step 3: Wire the route**

In `backend/api/routes/rename.py`, modify `list_jobs` (~line 130-160). Add the import at the top of the file (near the existing `from backend.rename.conflicts import ... destination_conflict_ids` or `conflict_annotations` import line):

```python
from backend.rename.conflicts import conflict_annotations, find_library_duplicate
from backend.rename.conflict_analyzer import analyze_job_conflict, has_active_duplicate
import threading
```

(If `threading` is already imported at module level — check `grep -n "^import threading" backend/api/routes/rename.py` — don't duplicate the import.)

Add a module-level in-flight guard right after the imports:

```python
# Jobs currently being background-analyzed — prevents the list route (polled
# frequently) from spawning a redundant analysis thread for the same job on
# every request while one's already running.
_analyzing_job_ids: set = set()
_analyzing_lock = threading.Lock()
```

Then modify `list_jobs`:

```python
@router.get("/jobs")
def list_jobs(status: Optional[str] = None, limit: int = 200,
              reg: ServiceRegistry = Depends(get_registry)):
    """List tracked rename jobs (optionally filtered by status) + status counts.

    Each job is annotated with ``destination_conflict`` (another job targets the
    same destination file), ``library_duplicate`` (a same-title/year movie
    already exists in Plex at a DIFFERENT path) and, for the best release in a
    duplicate group, ``keep_recommended`` + ``keep_reason`` — so the UI can flag
    the duplicate and suggest which copy to keep before either is applied.

    A job newly flagged by either signal, with no conflict_analysis yet, gets a
    background analysis thread fired (fire-and-forget, de-duplicated by
    _analyzing_job_ids so rapid repeat polls don't pile up redundant threads)."""
    if reg.db is None:
        return {"jobs": [], "counts": {}}
    limit = max(1, min(int(limit), 2000))  # clamp: never let a client OOM the box
    jobs = reg.db.list_rename_jobs(status=status, limit=limit)
    all_active_jobs = reg.db.list_rename_jobs(limit=100000) or []
    # Annotations are computed over ALL active jobs (not just this filtered page),
    # so a duplicate is still flagged when the two halves land on different pages
    # or under a status filter.
    annotations = conflict_annotations(all_active_jobs)
    plex_movie_rows = reg.db.list_plex_cache_movies()
    paths = [j.get("original_path") for j in jobs if j.get("original_path")]
    dv_map = reg.db.get_dv_scans_by_paths(paths)
    to_analyze = []
    for j in jobs:
        ann = annotations.get(j.get("id")) or {}
        j["destination_conflict"] = ann.get("destination_conflict", False)
        j["keep_recommended"] = ann.get("keep_recommended", False)
        j["keep_reason"] = ann.get("keep_reason")
        j["poster_url"] = _poster_url(j.get("poster_path"))
        dv = dv_map.get(j.get("original_path"))
        j["dv_layer"] = (dv or {}).get("dv_layer")
        lib_dup = find_library_duplicate(j, plex_movie_rows) is not None
        j["library_duplicate"] = lib_dup
        # has_active_duplicate re-derives destination_conflict from
        # `annotations` itself rather than reusing j["destination_conflict"]
        # above — same source, just kept as the ONE shared definition of
        # "active duplicate" also used by the maintenance-loop sweep
        # (Task 6's analyze_pending_conflicts), so the two never drift.
        if has_active_duplicate(j, annotations, plex_movie_rows) and not j.get("conflict_analysis"):
            to_analyze.append(j["id"])

    if to_analyze:
        with _analyzing_lock:
            fresh = [jid for jid in to_analyze if jid not in _analyzing_job_ids]
            _analyzing_job_ids.update(fresh)
        if fresh:
            def _run(job_ids):
                try:
                    for jid in job_ids:
                        try:
                            job = reg.db.get_rename_job(jid)
                            if job:
                                analyze_job_conflict(reg.db, job, plex_cache_rows=plex_movie_rows)
                        except Exception:
                            logger.exception("list_jobs: background analysis failed for job %s", jid)
                finally:
                    with _analyzing_lock:
                        _analyzing_job_ids.difference_update(job_ids)
            threading.Thread(target=_run, args=(fresh,), name="conflict-analyze", daemon=True).start()

    return {
        "jobs": jobs,
        "counts": reg.db.count_rename_jobs_by_status(),
    }
```

(`logger` should already be defined at module level in this file — check `grep -n "^logger = " backend/api/routes/rename.py`; if not, add `logger = logging.getLogger(__name__)` near the top imports, matching every other route file's convention.)

- [ ] **Step 4: Wire the maintenance loop**

In `backend/app_service.py`, modify `_run_maintenance_pass` (~line 569-604), adding one more try/except block after the existing pipeline-reconcile block:

```python
        try:
            if self.db is not None:
                from backend.rename.conflict_analyzer import analyze_pending_conflicts
                n = analyze_pending_conflicts(self.db, limit=50)
                if n:
                    logger.info("Conflict analysis backfill: processed %d job(s)", n)
        except Exception:
            logger.exception("Conflict analysis backfill failed (non-fatal)")
```

- [ ] **Step 5: Run test to verify it passes**

Run: `docker cp backend/. sh-plan-t7:/app/backend && MSYS_NO_PATHCONV=1 docker exec sh-plan-t7 sh -c "cd /app && python3 -m pytest tests/test_api_rename.py tests/test_app_service_maintenance.py -q"`
Expected: all pass

- [ ] **Step 6: Run the FULL backend suite to catch regressions**

Run: `MSYS_NO_PATHCONV=1 docker exec sh-plan-t7 sh -c "cd /app && python3 -m pytest tests/ --timeout=90 -q -x --ignore=tests/test_api_routes.py"` (the pre-existing `test_api_routes.py` hangs on network tests per this project's established testing notes — skip it, matching prior session practice)
Expected: all pass, no new failures

- [ ] **Step 7: Clean up and commit**

```bash
docker rm -f sh-plan-t7
git add backend/api/routes/rename.py backend/app_service.py tests/test_api_rename.py tests/test_app_service_maintenance.py
git commit -m "feat(rename): trigger conflict analysis from the jobs list route + maintenance loop"
```

---

### Task 8: Frontend types + `conflictSummary()` + row redesign

**Files:**
- Modify: `frontend/src/lib/api/types.ts` (`RenameJob` interface, ~line 73-130)
- Modify: `frontend/src/lib/renames/conflictView.ts` (add `conflictSummary`, add `ConflictAnalysis` type)
- Modify: `frontend/src/lib/components/renames/RenameRow.svelte`
- Test: `frontend/src/lib/renames/conflictView.test.ts` (add cases)

**Interfaces:**
- Consumes: Task 6/7's `conflict_analysis` JSON shape (`{kind, existing, incoming, recommended, reason, degraded, analyzed_at}`), Task 7's `library_duplicate` field.
- Produces: `conflictSummary(analysis: ConflictAnalysis | null | undefined): string` — pure, used by `RenameRow.svelte`.

- [ ] **Step 1: Write the failing tests**

Add to `frontend/src/lib/renames/conflictView.test.ts`:

```typescript
import { conflictSummary } from './conflictView';
import type { ConflictAnalysis } from '$lib/api/types';

const analysis = (o: Partial<ConflictAnalysis>): ConflictAnalysis => ({
  kind: 'same_path',
  existing: spec({}), incoming: spec({}),
  recommended: null, reason: null, degraded: false, analyzed_at: '2026-07-11T00:00:00Z',
  ...o,
});

describe('conflictSummary', () => {
  it('shows only differing axes', () => {
    const a = analysis({
      existing: spec({ resolution: '2160p', hdr: 'Dolby Vision', dv_layer: 'mel', size_bytes: 25e9 }),
      incoming: spec({ resolution: '2160p', hdr: 'Dolby Vision', dv_layer: 'fel', size_bytes: 29e9 }),
      recommended: 'incoming',
    });
    const s = conflictSummary(a);
    expect(s).toContain('MEL');
    expect(s).toContain('FEL');
    expect(s).toContain('25.0 GB');
    expect(s).toContain('29.0 GB');
    expect(s).toContain('keep Incoming');
  });

  it('identical-except-size renders no redundant repeated axis', () => {
    const a = analysis({
      existing: spec({ resolution: '2160p', hdr: null, dv_layer: null, size_bytes: 22e9 }),
      incoming: spec({ resolution: '2160p', hdr: null, dv_layer: null, size_bytes: 26e9 }),
      recommended: 'incoming',
    });
    const s = conflictSummary(a);
    expect(s).not.toContain('2160p → 2160p');
    expect(s).toContain('22.0 GB');
    expect(s).toContain('26.0 GB');
  });

  it('missing analysis returns empty string, never throws', () => {
    expect(conflictSummary(null)).toBe('');
    expect(conflictSummary(undefined)).toBe('');
  });

  it('degraded analysis omits the keep-recommendation clause', () => {
    const a = analysis({
      existing: spec({ resolution: '2160p' }), incoming: spec({ present: false }),
      recommended: null, degraded: true,
    });
    expect(conflictSummary(a)).not.toContain('keep');
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd frontend && npx vitest run src/lib/renames/conflictView.test.ts`
Expected: FAIL — `conflictSummary is not exported` / `ConflictAnalysis` type missing

- [ ] **Step 3: Add the `ConflictAnalysis` type + `library_duplicate` field**

In `frontend/src/lib/api/types.ts`, add near `FileSpec`/`ConflictComparison` (~line 583-596):

```typescript
export interface ConflictAnalysis {
  kind: 'same_path' | 'library_duplicate';
  existing: FileSpec;
  incoming: FileSpec;
  recommended: 'existing' | 'incoming' | 'tie' | null;
  reason: string | null;
  degraded: boolean;
  analyzed_at: string;
}
```

In the `RenameJob` interface, add two fields near the existing `destination_conflict`/`keep_recommended` block (~line 113-125):

```typescript
  // True when a same-title/year movie already exists in Plex at a DIFFERENT
  // path than this job's own destination. Computed server-side on the jobs
  // list, same as destination_conflict.
  library_duplicate?: boolean;
  // The full background-analyzed quality comparison for this job's active
  // duplicate (either kind). Null until the analyzer has run. Sole read path
  // for the row's diff summary — never live-probed on the frontend.
  conflict_analysis?: ConflictAnalysis | null;
```

- [ ] **Step 4: Implement `conflictSummary()`**

In `frontend/src/lib/renames/conflictView.ts`, add near the bottom (after `needsDvScan`), reusing the existing `formatBytes`/`hdrLabel`-equivalent logic already in this file:

```typescript
import type { ConflictAnalysis } from '$lib/api/types';

/** Concise one-line row diff, showing only the axes that DIFFER between
 *  existing and incoming — replaces the old raw-byte warning_message
 *  tooltip. Pure, never throws on missing/degraded input. */
export function conflictSummary(analysis: ConflictAnalysis | null | undefined): string {
  if (!analysis) return '';
  const e = analysis.existing?.present ? analysis.existing : null;
  const i = analysis.incoming?.present ? analysis.incoming : null;
  const parts: string[] = [];

  const eRes = e?.resolution ?? EM_DASH;
  const iRes = i?.resolution ?? EM_DASH;
  const eHdr = hdrLabel(e);
  const iHdr = hdrLabel(i);
  const eSize = formatBytes(e?.size_bytes ?? null);
  const iSize = formatBytes(i?.size_bytes ?? null);

  const axisDiffers = eRes !== iRes || eHdr !== iHdr;
  const existingBits = [axisDiffers ? eRes : null, axisDiffers ? eHdr : null, eSize]
    .filter((v): v is string => !!v && v !== EM_DASH);
  const incomingBits = [axisDiffers ? iRes : null, axisDiffers ? iHdr : null, iSize]
    .filter((v): v is string => !!v && v !== EM_DASH);

  parts.push(`Existing ${existingBits.join('·') || EM_DASH}`);
  parts.push(`→ Incoming ${incomingBits.join('·') || EM_DASH}`);

  let summary = parts.join(' ');
  if (!analysis.degraded && analysis.recommended && analysis.recommended !== 'tie') {
    const who = analysis.recommended === 'existing' ? 'Existing' : 'Incoming';
    summary += ` · keep ${who} ★`;
  }
  return summary;
}
```

- [ ] **Step 5: Run test to verify it passes**

Run: `cd frontend && npx vitest run src/lib/renames/conflictView.test.ts`
Expected: all pass

- [ ] **Step 6: Update `RenameRow.svelte`**

Modify `frontend/src/lib/components/renames/RenameRow.svelte`'s conflict badge block (the `{:else if isConflict}` branch that currently renders the GB-chip badges, per Task from earlier this session — search `grep -n "Already in library\|conflict_existing_size" frontend/src/lib/components/renames/RenameRow.svelte` for the exact current lines). Replace the GB-chip rendering with `conflictSummary()`, and gate `isConflict` on `job.destination_conflict || job.library_duplicate` instead of only `hasDestinationConflict(job)` (which itself already reads `job.destination_conflict` — extend `hasDestinationConflict` in `frontend/src/lib/renames/review.ts` instead, so every caller benefits):

In `frontend/src/lib/renames/review.ts`, modify `hasDestinationConflict`:

```typescript
export function hasDestinationConflict(job: RenameJob): boolean {
  if (job.conflict_kind === 'destination_exists') return true;
  if (job.destination_conflict) return true;
  if (job.library_duplicate) return true;
  return /already exists/i.test(job.warning_message ?? '');
}
```

In `RenameRow.svelte`, add the import and replace the badge block:

```svelte
  import { conflictSummary } from '$lib/renames/conflictView';
```

Replace the existing conflict-badge markup (the block rendering `Badge variant="warning" label="⚠ Already in library"` plus the GB-chip badges and Compare button) with:

```svelte
    {#if isConflict}
      <div class="flex items-center gap-1.5 text-xs" title="Click Compare for full details">
        <Badge variant="warning" label="⚠ Already in library" />
        {#if job.conflict_analysis}
          <span class="text-[var(--text-secondary)] truncate">{conflictSummary(job.conflict_analysis)}</span>
        {/if}
        <button
          type="button"
          class="ml-auto shrink-0 px-2 py-0.5 rounded text-[11px] font-medium bg-[var(--accent)] text-white hover:brightness-110"
          onclick={() => onCompare(job)}
        >
          Compare
        </button>
      </div>
```

(Keep the surrounding `{:else if job.warning_message}` fallback branch for non-conflict warnings unchanged.)

- [ ] **Step 7: Verify the frontend build**

Run: `cd frontend && npm run check && npm run build`
Expected: `0 ERRORS`, build succeeds

- [ ] **Step 8: Commit**

```bash
git add frontend/src/lib/api/types.ts frontend/src/lib/renames/conflictView.ts frontend/src/lib/renames/conflictView.test.ts frontend/src/lib/renames/review.ts frontend/src/lib/components/renames/RenameRow.svelte
git commit -m "feat(renames): replace raw-byte conflict tooltip with conflictSummary()"
```

---

### Task 9: Adaptive resolution actions (library-wide duplicate kind)

**Files:**
- Modify: `frontend/src/lib/renames/conflictView.ts` (add `actionsForKind`)
- Modify: `frontend/src/lib/components/renames/RenameReviewCard.svelte`
- Test: `frontend/src/lib/renames/conflictView.test.ts` (add cases)

**Interfaces:**
- Consumes: Task 8's `ConflictAnalysis['kind']`.
- Produces: `actionsForKind(kind: ConflictAnalysis['kind'] | undefined) -> {overwrite: boolean, keepBoth: boolean, applyAnyway: boolean}` — pure, unit-tested; `RenameReviewCard.svelte` renders buttons from this instead of an inline template conditional (per this plan's Global Constraint: untestable logic never lives directly in `.svelte` markup).

- [ ] **Step 1: Write the failing tests**

Add to `frontend/src/lib/renames/conflictView.test.ts`:

```typescript
import { actionsForKind } from './conflictView';

describe('actionsForKind', () => {
  it('same_path shows Overwrite + Keep both (today\'s behavior)', () => {
    expect(actionsForKind('same_path')).toEqual({ overwrite: true, keepBoth: true, applyAnyway: false });
  });

  it('library_duplicate shows Apply anyway, not Overwrite/Keep both', () => {
    expect(actionsForKind('library_duplicate')).toEqual({ overwrite: false, keepBoth: false, applyAnyway: true });
  });

  it('undefined kind defaults to same_path shape (no analysis yet)', () => {
    expect(actionsForKind(undefined)).toEqual({ overwrite: true, keepBoth: true, applyAnyway: false });
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd frontend && npx vitest run src/lib/renames/conflictView.test.ts`
Expected: FAIL — `actionsForKind is not exported`

- [ ] **Step 3: Implement `actionsForKind`**

In `frontend/src/lib/renames/conflictView.ts`:

```typescript
/** Which resolution buttons apply for a duplicate's analysis kind.
 *  library_duplicate has no file AT the incoming destination to overwrite,
 *  and no dedupe-naming decision to make (the incoming file was never going
 *  to collide with the existing file's actual name) — so Overwrite/Keep-both
 *  don't apply; Apply-anyway (accept two copies) replaces them. Skip always
 *  applies for both kinds (handled separately by the caller). Undefined
 *  (analysis not loaded yet) defaults to the same_path shape, matching
 *  today's behavior until the real kind is known. */
export function actionsForKind(kind: ConflictAnalysis['kind'] | undefined): {
  overwrite: boolean; keepBoth: boolean; applyAnyway: boolean;
} {
  if (kind === 'library_duplicate') {
    return { overwrite: false, keepBoth: false, applyAnyway: true };
  }
  return { overwrite: true, keepBoth: true, applyAnyway: false };
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd frontend && npx vitest run src/lib/renames/conflictView.test.ts`
Expected: all pass

- [ ] **Step 5: Wire `RenameReviewCard.svelte`**

In `frontend/src/lib/components/renames/RenameReviewCard.svelte`, add the import:

```svelte
  import { specRows, needsDvScan, actionsForKind } from '$lib/renames/conflictView';
```

Add a derived value near the existing `preview`-related derived state:

```svelte
  let actions = $derived(actionsForKind(preview?.kind));
```

Note: `preview` here is the existing `ConflictComparison` from `conflict_preview` (unchanged per spec §6.2) — it does NOT carry `kind` today. Add `kind` to the backend's `conflict_preview` response and the frontend `ConflictComparison` type as part of this step:

In `backend/rename/service.py`'s `conflict_preview` (~line 1678-1722), add a `kind` field to the returned dict — same-path when `dst and os.path.lexists(dst)`, otherwise fall back to checking `find_library_duplicate` the same way the analyzer does:

```python
    def conflict_preview(self, job_id: int) -> dict:
        db = self._db
        job = db.get_rename_job(job_id) if db else None
        if not job:
            return {"existing": None, "incoming": None,
                    "recommended": None, "reason": None, "kind": None}
        dest_dir = job.get("destination_path") or ""
        dst = (os.path.join(dest_dir, job.get("new_filename")
                            or os.path.basename(job.get("original_path") or ""))
               if dest_dir else None)
        incoming_probe = probe_specs(job.get("original_path"), db=db)
        incoming = incoming_probe or {
            "present": os.path.exists(job.get("original_path") or ""),
            "path": job.get("original_path")}
        incoming["original_filename"] = job.get("original_filename")
        incoming["resolution"] = incoming.get("resolution") or job.get("resolution")
        existing_probe_failed = False
        kind = "same_path"
        if dst and os.path.lexists(dst):
            existing_probe = probe_specs(dst, db=db)
            existing = existing_probe or {"present": True, "path": dst}
            existing["original_filename"] = os.path.basename(dst)
            existing_probe_failed = existing_probe is None
        else:
            from backend.rename.conflicts import find_library_duplicate
            match = find_library_duplicate(job, db.list_plex_cache_movies()) if db else None
            if match and match.get("file_path"):
                kind = "library_duplicate"
                existing_probe = probe_specs(match["file_path"], db=db)
                existing = existing_probe or {"present": True, "path": match["file_path"]}
                existing["original_filename"] = os.path.basename(match["file_path"])
                existing_probe_failed = existing_probe is None
            else:
                existing = {"present": False, "path": dst}
        rec = rank_conflict(existing, {**incoming, "id": job_id})
        if incoming_probe is None or existing_probe_failed:
            rec = {"recommended": None, "reason": None}
        return {"existing": existing, "incoming": incoming,
                "recommended": rec["recommended"], "reason": rec["reason"], "kind": kind}
```

Add `kind` to `ConflictComparison` in `frontend/src/lib/api/types.ts`:

```typescript
export interface ConflictComparison {
  existing: FileSpec | null;
  incoming: FileSpec | null;
  recommended: 'existing' | 'incoming' | 'tie' | null;
  reason: string | null;
  kind: 'same_path' | 'library_duplicate' | null;
}
```

Test (add to `tests/test_rename_service.py`, near any existing `conflict_preview` test — search `grep -n "def test_conflict_preview" tests/test_rename_service.py` for a sibling to place beside):

```python
def test_conflict_preview_kind_library_duplicate(db, tmp_path):
    svc = _service(db, _weak_search, movie_lib=str(tmp_path / "lib"))
    other = tmp_path / "other.mkv"; other.write_bytes(b"x")
    db.save_plex_cache([{
        "clean_title": "Dup", "original_title": "Dup", "year": 2020,
        "res": "1080p", "size": 1.0, "imdb_id": "tt55", "rating_key": "5",
        "media_id": "5", "key": "5_5_0", "file": str(other),
    }], "Movies")
    src, _ = _extracted(tmp_path, "Dup.2020.mkv")
    job_id = svc.process_package("pkg", src)[0]
    db.update_rename_job(job_id, imdb_id="tt55", title="Dup", year=2020, media_type="movie")
    out = svc.conflict_preview(job_id)
    assert out["kind"] == "library_duplicate"


def test_conflict_preview_kind_same_path_when_dest_occupied(db, tmp_path):
    svc = _service(db, _weak_search, movie_lib=str(tmp_path / "lib"))
    j0, src0 = _matched_job(db, tmp_path, "Occ")
    dst = os.path.join(str(tmp_path / "lib"), "Occ.mkv")
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    with open(dst, "wb") as f:
        f.write(b"y")
    db.update_rename_job(j0, new_filename="Occ.mkv")
    out = svc.conflict_preview(j0)
    assert out["kind"] == "same_path"
```

(Run these two against the throwaway container per Step 6 below to confirm; adjust the `_extracted`/`_matched_job` call shapes to match whatever this test file's actual helpers require — check `grep -n "^def _matched_job\|^def _extracted" tests/test_rename_service.py` first.)

Now the button markup. Replace the existing three-button block (`Overwrite` / `Keep both` / `Skip`, ~line 262-284 of `RenameReviewCard.svelte`):

```svelte
        <div class="flex flex-wrap gap-2 pt-1">
          {#if actions.overwrite}
            <button
              class="flex-1 py-2 rounded-lg bg-[var(--error)] text-white text-xs font-semibold disabled:opacity-50 hover:brightness-110 transition-all"
              disabled={busy}
              onclick={onOverwrite}
            >
              Overwrite
            </button>
          {/if}
          {#if actions.keepBoth}
            <button
              class="flex-1 py-2 rounded-lg bg-[var(--accent)] text-white text-xs font-semibold disabled:opacity-50 hover:brightness-110 transition-all"
              disabled={busy}
              onclick={onKeepBoth}
            >
              Keep both
            </button>
          {/if}
          {#if actions.applyAnyway}
            <button
              class="flex-1 py-2 rounded-lg bg-[var(--accent)] text-white text-xs font-semibold disabled:opacity-50 hover:brightness-110 transition-all"
              disabled={busy}
              onclick={onApply}
              title="This title already exists elsewhere in the library — apply anyway to keep both copies"
            >
              Apply anyway
            </button>
          {/if}
          <button
            class="flex-1 py-2 rounded-lg border border-[var(--border)] text-[var(--text-secondary)] text-xs font-semibold hover:bg-[var(--bg-tertiary)] disabled:opacity-50"
            disabled={busy}
            onclick={onSkip}
          >
            Skip
          </button>
        </div>
```

`onApply` is already a prop on `RenameReviewCard` (used in the no-conflict branch) — no new prop needed; "Apply anyway" just reuses it, matching the spec's note that this is a normal apply to the normal destination.

- [ ] **Step 6: Run the new backend tests, then verify frontend build**

Run: `docker cp backend/. sh-plan-t7:/app/backend && docker cp tests/. sh-plan-t7:/app/tests && MSYS_NO_PATHCONV=1 docker exec sh-plan-t7 sh -c "cd /app && python3 -m pytest tests/test_rename_service.py -k conflict_preview -q"` (reuse the Task 7 container if still present, or start a fresh one per the established pattern)
Expected: pass

Run: `cd frontend && npm run check && npm run build && npx vitest run`
Expected: `0 ERRORS`, build succeeds, all vitest pass

- [ ] **Step 7: Commit**

```bash
docker rm -f sh-plan-t7
git add frontend/src/lib/renames/conflictView.ts frontend/src/lib/renames/conflictView.test.ts frontend/src/lib/components/renames/RenameReviewCard.svelte frontend/src/lib/api/types.ts backend/rename/service.py tests/test_rename_service.py
git commit -m "feat(renames): adaptive Apply-anyway/Skip actions for library-wide duplicates"
```

---

### Task 10: Full verification, adversarial review, changelog

**Files:**
- Modify: `frontend/src/lib/changelog.ts` (new entry)
- No new code files — this task is verification + review + release notes.

- [ ] **Step 1: Full backend suite**

```bash
docker rm -f sh-plan-final >/dev/null 2>&1
docker run -d --name sh-plan-final --entrypoint sleep scanhound:latest infinity >/dev/null
docker cp backend/. sh-plan-final:/app/backend
docker cp tests/. sh-plan-final:/app/tests
docker exec sh-plan-final pip install -q pytest pytest-timeout httpx
MSYS_NO_PATHCONV=1 docker exec sh-plan-final sh -c "cd /app && python3 -m pytest tests/ --timeout=90 -q --ignore=tests/test_api_routes.py"
```
Expected: all pass, 0 failures.

- [ ] **Step 2: Full frontend suite**

```bash
cd frontend
npm run check
npm run build
npx vitest run
```
Expected: `0 ERRORS`, build succeeds, all vitest tests pass.

- [ ] **Step 3: Adversarial review gate (spec §9 — mandatory, not optional)**

Dispatch an adversarial "try to break this" review — a fresh reviewer given the diff across Tasks 4-9, explicitly instructed to EXECUTE the edge cases (trace concrete inputs through the real code) rather than read-through — covering exactly the three areas the spec calls out:
1. `rank_conflict` usage inside `analyze_job_conflict` (Task 6) — does any input combination produce a recommendation that would favor a lower-quality file? Concretely re-trace the `test_rank_conflict_keeps_existing_dv_remux_over_tag_rich_lower_res`-style trap against the NEW code path (degraded-probe handling, the DV-layer-merge-after-scan step). Also specifically trace: `rank_conflict`'s own pre-existing logic returns `{"recommended": "incoming"}` whenever `existing` is absent/not-present, with NO check that `incoming` is present either — so a job where BOTH files probe successfully as `present: False` (a genuine race: the existing file got deleted and the incoming source also vanished between detection and analysis) is NOT caught by this plan's `degraded` flag (both probes SUCCEEDED, they just report nothing there) and would still recommend "keep Incoming ★" for a file that doesn't exist. Confirm whether this is reachable in practice and, if so, whether `analyze_job_conflict` needs its own guard (`rank_conflict` itself is out of scope to change per this plan's Global Constraints) — e.g. treating "both sides present: False" as `degraded` too.
2. `conflictSummary()`'s differing-axes logic (Task 8) — any input where a real difference is silently hidden (e.g. both sides show `—` for a field that actually differs), or where "keep X" is shown alongside a `degraded: true` analysis.
3. `actionsForKind`/adaptive-action gating (Task 9) — any path where Overwrite could still fire for a `library_duplicate` kind (e.g. `preview.kind` stale/null while `onOverwrite` is still wired to a button that's supposed to be hidden).

If the review finds real issues, fix them, re-run the affected test suites, and commit the fix(es) before proceeding — do not defer to "known issue."

- [ ] **Step 4: Add the changelog entry**

In `frontend/src/lib/changelog.ts`, add a new entry (check the file's existing entry shape/version-numbering convention first via `grep -n "version:" frontend/src/lib/changelog.ts | tail -5`) describing: auto-analyzed duplicate quality comparison (row diff replaces byte tooltip), library-wide duplicate detection (same title, different path), and the FEL/MEL smart-scan gate.

- [ ] **Step 5: Final commit**

```bash
docker rm -f sh-plan-final
git add frontend/src/lib/changelog.ts
git commit -m "chore: changelog — auto-analyzed duplicate quality comparison"
```

**This plan does NOT push or deploy** — per this project's established practice, deployment is a separate, explicitly-requested step after the user reviews the merged work.
