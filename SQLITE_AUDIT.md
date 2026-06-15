# SQLite Audit — Combined Report

Date: March 8, 2026
Sources: Codex (live DB inspection) + Claude (code analysis) + cross-review

## Scope

This audit reviewed:

- SQLite usage in `backend/database.py`, `backend/watchlist.py`, and `backend/analytics.py`
- The configured live database path from `backend/config.py`
- Live database files found in the workspace and data directory
- All callers: `backend/scanner_service.py`, `backend/app_service.py`, `backend/plex_service.py`, `backend/download_service.py`, `ui/controllers/`

## Active Database Location

The application currently uses:

- `%LOCALAPPDATA%\ScanHound\crawler.db`

This is set in `backend/config.py` via `DB_PATH = _migrate_db("crawler.db")`.

Repo-root database files such as `crawler.db`, `watchlist.db`, and `library.db` are not necessarily the active files the app is using.

## Database Health

### `%LOCALAPPDATA%\ScanHound\crawler.db`

- integrity check: ok
- quick check: ok
- journal mode: `wal`
- foreign keys: `0`
- row counts:
  - `downloads`: `103`
  - `plex_cache`: `34,157`
  - `scan_history`: `301`
  - `scanned_urls`: `442`
  - `watchlist`: `0`

### Repo-root `crawler.db`

- integrity check: ok
- quick check: ok
- journal mode: `delete`
- appears stale relative to the active DB
- large freelist count, consistent with old/deleted content

## Summary

The SQLite files are structurally intact (`PRAGMA integrity_check` returns `ok`). No SQL injection vulnerabilities were found — all queries use parameterized `?` placeholders.

The main problems fall into five categories:

1. **Schema drift** — The live watchlist table doesn't match current code; legacy tables persist. **(Critical — feature broken)**
2. **Cache pollution** — `plex_cache` accumulates 14K+ stale rows that inflate matching and analytics. **(Critical — data quality)**
3. **API mismatch** — Analytics caller uses a method name that doesn't exist on the class. **(High — feature broken)**
4. **Connection fragmentation** — Three independent SQLite connections (`DatabaseManager`, `WatchlistManager`, `StatsDashboard`) with inconsistent locking, timeouts, and WAL configuration. **(High)**
5. **Missing infrastructure** — No migration versioning, timestamp-resetting upserts, unused feature paths. **(High/Medium)**

---

## Findings

### 1. Watchlist schema is incompatible with current code

**Severity: Critical** | Source: Codex (live DB) + Claude (code)
**Files:** `backend/watchlist.py`, `backend/config.py`, `backend/app_service.py`

The app initializes `WatchlistManager` against the same `crawler.db` file used by the rest of the app. Current code expects a `watchlist` table with columns:

- `imdb_id`, `tmdb_id`, `item_type`, `status`, `season`, `min_resolution`, `prefer_dovi`, `found_date`, `found_url`

However, the live `watchlist` table has a legacy schema:

- `tmdb_id`, `media_type`, `title`, `year`, `poster_path`, `overview`, `rating`, `genres`, `added_date`, `priority`, `notes`
- unique constraint on `(tmdb_id, media_type)`

`WatchlistManager._init_db()` creates indexes on columns like `status` and `imdb_id`, which fails:

```
sqlite3.OperationalError: no such column: status
```

**Additionally** (Claude finding): `_init_db()` has **zero error handling** — no try/except around schema creation. `app_service.py` catches the failure at `_init_optional_subsystems()` but only logs at `DEBUG` level.

Impact:
- Watchlist feature is **silently broken** in production
- UI calls to add/remove/list watchlist items are no-ops or fail quietly
- Users get no feedback that the feature is disabled

### 2. Plex cache refresh does not remove stale rows

**Severity: Critical** | Source: Codex (live DB evidence) + Claude (code)
**Files:** `backend/database.py` (lines 342-384), `backend/plex_service.py`

`save_plex_cache()` uses `INSERT OR REPLACE`, but there is no pruning step for rows that no longer belong in the current cache. `load_plex_cache()` loads all rows for a content type with `SELECT * FROM plex_cache WHERE content_type = ?`.

Stale rows accumulate after library assignment changes, cache key strategy changes, or content removal from Plex.

**Live database evidence:**

- `plex_cache` total rows: **34,157**
- `Movies`: 29,521 — of which **14,294 have `library_name IS NULL`** (older cache format)
- `TV Shows`: 4,636
- 15,227 rows belong to named libraries
- 11,926 logical movie keys exist in **both** the null-library and named-library sets
- Only 3 null-library keys are unique leftovers

**Additional code-level issue** (Claude):
- No UNIQUE constraint on the natural business key `(rating_key, media_id, content_type)` — only on the application-generated `key TEXT PRIMARY KEY`

Impact:
- Cache-backed Plex loads are inflated (~2x for movies)
- Duplicate match candidates in in-memory indexes
- Analytics overcounts the library
- Removed/stale library state persists across scans

### 3. Analytics API mismatch — `get_summary()` does not exist

**Severity: High** | Source: Codex (code analysis)
**Files:** `ui/controllers/scanner_controller.py` (lines 903-904), `backend/analytics.py` (line 503)

The scanner controller calls:

```python
stats = self._backend.stats_dashboard.get_summary()
```

But `StatsDashboard` only defines `get_dashboard_summary()` — `get_summary()` does not exist. This means analytics is **already broken** at runtime, raising `AttributeError` before any lock/WAL discussion matters. The `if self._backend.stats_dashboard:` guard prevents a crash, but analytics data is silently unavailable.

Impact:
- Analytics feature is broken — dashboard summary never populates
- Error is silently swallowed

### 4. StatsDashboard bypasses DatabaseManager with weaker connection handling

**Severity: Medium** | Source: Claude (code analysis), reviewed by Codex
**Files:** `backend/analytics.py` (lines 126-131)

`StatsDashboard._get_connection()` opens its own connection without the safeguards that `DatabaseManager` applies:

```python
def _get_connection(self) -> sqlite3.Connection:
    if self._conn is None:
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
    return self._conn
```

Issues:
- No lock protecting the `_conn is None` check — theoretically a TOCTOU race, though no concrete corruption or guaranteed failure has been reproduced locally
- No `busy_timeout` set (defaults to 0ms vs DatabaseManager's 5000ms)
- No WAL mode pragma
- `get_dashboard_summary()` makes 6+ sequential DB round-trips on the main thread, risking UI freezing
- Reads polluted `plex_cache` data (see finding #2)
- `codec_counts` is defined in the stats model but never populated

Impact:
- Dashboard totals overstated from polluted cache
- Potential UI freeze on large libraries
- Weaker than the main DB layer and should be unified with `DatabaseManager`

### 5. WatchlistManager connection bypasses DatabaseManager

**Severity: High** | Source: Codex + Claude, corrected by Codex review
**Files:** `backend/watchlist.py` (lines 113-162)

`WatchlistManager` opens a **second independent connection** to the same `crawler.db`:

- No `busy_timeout` set (0ms default — writes fail instantly vs DatabaseManager's 5000ms)
- WAL mode is not explicitly set on this connection
- `_init_db()` calls `conn.commit()` outside the lock scope of `_get_connection()`
- Writes bypass `DatabaseManager._lock` entirely — serialized only by SQLite's internal locking

**Important context:** WatchlistManager methods (`add`, `update`, `remove`, `get`, `get_all`, `search`, `find_by_imdb`, `get_stats`, `clear`) do properly acquire `self._lock` for their operations. The WatchlistManager is not broadly unsafe — the concern is specifically the missing `busy_timeout`/WAL setup and the lack of cross-manager coordination with `DatabaseManager`, not a blanket locking absence.

Impact:
- Without `busy_timeout`, `WatchlistManager` writes fail with `database is locked` under any write contention from `DatabaseManager`
- Two independent lock domains for the same DB file

### 6. Migration/version handling is incomplete

**Severity: High** | Source: Codex (live DB) + Claude (code)
**Files:** `backend/database.py` (lines 173-290)

`DatabaseManager.init_db()` creates base tables and applies `ALTER TABLE` statements, but has no robust migration system.

**Observed issues:**

- No use of `PRAGMA user_version`
- No active migration logic for reshaping incompatible tables (e.g., watchlist)
- Legacy `schema_version` table exists in the live DB but current code doesn't use it
- Legacy `file_manager` table persists from a removed subsystem (may contain user data — export before dropping)
- `app_config` table is created in schema (lines 238-243) but **never read or written** — dead schema
- Migration catches **all** `OperationalError` (lines 203-212), masking genuinely broken migrations (misspelled columns, wrong types) alongside "column already exists"

Impact:
- App cannot detect whether DB is compatible with current code
- Old tables and schemas survive upgrades
- Feature breakage appears only at runtime
- Broken migrations are silently swallowed

### 7. `INSERT OR REPLACE` on downloads resets `date_added`

**Severity: High** | Source: Claude (code analysis)
**Files:** `backend/database.py` (lines 194-199, 510-515)

The `downloads` table has `date_added TIMESTAMP DEFAULT CURRENT_TIMESTAMP`. The `add_to_history` method uses `INSERT OR REPLACE`, which on a collision **deletes the old row and inserts a new one**, resetting `date_added` to now.

Re-downloading an item loses its original download timestamp. If preserving history timing matters, this should use `INSERT ... ON CONFLICT DO UPDATE` or `INSERT OR IGNORE`.

### 8. `scanned_urls` feature path appears unused in production

**Severity: Medium** | Source: Claude (code analysis), reframed by Codex review
**Files:** `backend/database.py` (lines 263-270, 662-670), `backend/scanner_service.py`

`scanned_urls` is written to by `scanner_service.py` (`add_scanned_urls_batch`) and cleared for deep scans (`clear_scanned_urls`), but **no non-test code reads it back**:

- `get_scanned_urls()` — only called from tests, never from production code
- `is_url_scanned()` — only called from tests, never from production code

This means the table accumulates data that nothing consumes. The bigger question is whether this feature path should be restored (e.g., for incremental scan deduplication) or removed entirely. Adding a TTL retention policy first would optimize logic that may be dead.

Current live DB has 442 rows — not a performance concern yet.

### 9. DatabaseManager shares one connection across all threads

**Severity: Medium** | Source: Claude (code analysis)
**Files:** `backend/database.py` (lines 69-84)

Single shared `sqlite3.Connection` with `check_same_thread=False` protected by `threading.RLock`. The reentrant lock design means a nested `_mutate` inside a `transaction()` block would call `conn.commit()` mid-transaction, ending the outer transaction prematurely. No current call site does this, but it is a latent hazard.

`transaction()` is also used for **read-only queries** in 3 callers (`app_service.py:771`, `scanner_service.py:1009`, `download_service.py:113`) — semantically wrong and wastes the commit, though not harmful.

### 10. Missing rollback in `add_scanned_urls_batch`

**Severity: Medium** | Source: Claude (code analysis), corrected by Codex review
**Files:** `backend/database.py` (lines 684-697)

`add_scanned_urls_batch` has no rollback on exception:

```python
conn.cursor().executemany(...)
conn.commit()
```

If `executemany` raises, `conn.commit()` is never reached and the connection is left with uncommitted state. The lock is released and the next operation sees the connection in a dirty state. Under WAL mode the implicit rollback happens on the next operation, but there is a brief window of inconsistency.

**Note:** `save_plex_cache()` already has a rollback on exception (`database.py:378`). An earlier version of this audit incorrectly listed it as missing — only `add_scanned_urls_batch` lacks one.

### 11. Silent error handling in plex_cache stat methods

**Severity: Medium** | Source: Claude (code analysis)
**Files:** `backend/database.py` (lines 448-449, 470-471, 498-499)

`plex_cache_counts`, `get_plex_cache_max_timestamp`, and `plex_cache_counts_per_library` all have bare `except Exception: return {...}` with **no logging**. Schema problems or connection issues are silently swallowed, causing the UI to show stale/zero counts with no indication of a problem.

### 12. Watchlist deduplication gap

**Severity: Medium** | Source: Claude (code analysis)
**Files:** `backend/watchlist.py` (lines 182-188)

`WatchlistManager.add()` deduplicates by `imdb_id`, but when `imdb_id` is absent, no duplicate check is done. Importing the same CSV twice via `import_from_letterboxd` or `import_from_imdb_list` produces duplicates for any item without an IMDb ID.

### 13. `scan_history.timestamp` is TEXT — fragile ordering

**Severity: Low** | Source: Claude (code analysis)
**Files:** `backend/database.py` (line 249)

`timestamp TEXT NOT NULL` relies on ISO 8601 strings sorting lexicographically. The index `idx_scan_history_timestamp ON scan_history(timestamp DESC)` reinforces this. This works as long as all timestamps are in the same format, but there is no enforcement. Using `REAL` (Unix epoch) or `INTEGER` would be more robust.

### 14. Multiple SQLite copies create operational confusion

**Severity: Low** | Source: Codex (file inspection)

Multiple SQLite files exist:

- repo root `crawler.db`, `watchlist.db`, `library.db`
- `%LOCALAPPDATA%\ScanHound\crawler.db`
- `%LOCALAPPDATA%\ScanHound\library.db`

The active `crawler.db` is the `%LOCALAPPDATA%` copy.

Impact:
- Easy to inspect or back up the wrong file
- Easy to misdiagnose issues using stale local copies

### 15. Dead code

**Severity: Low** | Source: Claude (code analysis), corrected by Codex review

| Dead code | Location |
|-----------|----------|
| `is_url_scanned()` — never called from production code | `database.py:662-665` |
| `get_scanned_urls()` — never called from production code | `database.py:667-670` |
| `clear_history()` / `clear_download_history()` — exact duplicates | `database.py:503-505, 542-544` |
| `app_config` table — created but never read/written | `database.py:238-243` |

**Correction:** An earlier version of this audit listed `get_downloaded_titles()` as dead/duplicated code. This is **incorrect** — it is actively called by `source_search_controller.py:483` for download cross-referencing in source search. The inline copy in `scanner_service.py` exists for transaction consistency, not because the DB method is unused.

---

## Open Questions

Before implementing fixes, these decisions should be made explicitly:

1. **Watchlist schema:** Is the current watchlist model (imdb_id/status/season) or the legacy TMDB-only model (tmdb_id/media_type/poster_path) the source of truth? The migration strategy depends on this.
2. **Plex cache model:** Should the refresh be "full replace per content type" (delete all then insert) or "incremental refresh with tombstoning"? The document assumes full replace, which is probably right, but should be explicit.
3. **Legacy `file_manager` table:** Contains user data in the live DB. Decide whether to export this data before dropping the table during migration.
4. **`scanned_urls` feature path:** Is incremental scan deduplication via scanned URLs intended to work? If so, the read path needs to be restored. If not, the write path and table can be removed.

---

## Recommended Fixes (Priority Order)

### 1. Watchlist schema migration (Critical)

Add a real migration that detects the legacy schema and either reshapes or recreates the table. Decide the source-of-truth schema first (see Open Questions). Make initialization failures log at WARNING, not DEBUG.

### 2. Plex cache prune/rebuild on refresh (Critical)

Change `save_plex_cache()` to replace the cache set for a content type transactionally instead of only upserting. Run a one-time cleanup of 14K stale null-library rows. Decide the refresh model first (see Open Questions).

### 3. Fix analytics API mismatch (High)

Either rename `get_dashboard_summary()` to `get_summary()` in `analytics.py`, or update the call site in `scanner_controller.py:904` to use the correct method name. This is a one-line fix that unblocks analytics entirely.

### 4. Unify connection handling (High)

Consolidate `WatchlistManager` and `StatsDashboard` to use `DatabaseManager` semantics — shared lock, `busy_timeout=5000`, explicit WAL mode. This addresses findings #4 and #5 together. Alternatively, at minimum add `busy_timeout` and WAL pragmas to both independent connections.

### 5. Add real schema versioning/migrations (High)

Implement `PRAGMA user_version` or a migration table. Guard `ALTER TABLE` catches to only match "duplicate column name", not all `OperationalError`. Handle legacy table cleanup as part of versioned migrations (checking Open Questions first).

### 6. Downloads upsert and scanned_urls decision (Medium)

- Fix `INSERT OR REPLACE` on downloads to use `INSERT ... ON CONFLICT(url) DO UPDATE SET ...` preserving `date_added`.
- Decide whether `scanned_urls` should be a live feature or removed (see Open Questions). If kept, restore the read path; if removed, drop the write path and table.

### 7. Cleanup (Low)

- Add rollback guard to `add_scanned_urls_batch`.
- Add logging to silent `except Exception` blocks in plex_cache stat methods.
- Remove dead code: `is_url_scanned()`, `get_scanned_urls()`, duplicate `clear_history()`, unused `app_config` table.
- Remove or `.gitignore` stale repo-root database files.
- Consider enabling `PRAGMA foreign_keys`.

---

## Conclusion

The SQLite databases are not corrupted, but the persistence layer has significant issues across schema compatibility, cache hygiene, and connection management.

The most impactful production bugs are the **broken watchlist schema** (feature silently disabled) and the **analytics API mismatch** (`get_summary()` doesn't exist on `StatsDashboard`). The most impactful data-quality issue is **34K plex_cache rows with ~14K stale duplicates** inflating matches and analytics. The most important infrastructure gap is **three independent SQLite connections with inconsistent timeout and WAL configuration**, which should be unified behind `DatabaseManager`.
