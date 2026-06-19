# ScanHound — Codex Audit Review Request

> ⚠️ **LEGACY / STALE (note added 2026-06-19).** Refers to the Mar 2026 audit of
> the earlier PySide6/QML `ui/` stack and to commit hashes absent from current
> git history. Re-verify any file:line reference against the live FastAPI + Svelte
> code before acting.

> **Date**: 2026-03-08
> **Context**: Two audits were performed: a 35-pass codebase audit (AUDIT_ISSUES.md) and a SQLite-focused audit (SQLITE_AUDIT.md). 51 code changes were applied across 6 commits. This document is for Codex to verify the changes and assess remaining work.

---

## Part A: Verify Applied Changes

51 changes were applied in 6 commits (`5b2af8f`, `774765c`, `2ebc0bb`, `2e0b594`, `ad66398`, `e49fd1f`). Full descriptions are in AUDIT_CHANGELOG.md. Below is a condensed verification checklist.

### Commit 1: `5b2af8f` — Tier 1 (CRITICAL/HIGH bugs)

| # | File | Verify |
|---|------|--------|
| 1 | `backend/watchlist.py:134-148` | `ALTER TABLE ADD COLUMN` for each missing column instead of `DROP TABLE` |
| 2 | `backend/metadata_enricher.py:122` | `movie_results + tv_results` (concatenation, not `or`) |
| 3 | `backend/metadata_enricher.py:127,165,166` | `if item.season is not None` (not `if item.season`) |
| 4 | `backend/scanner_service.py:651,962,1045` | `if season is not None` at all three sites |
| 5 | `backend/download_service.py:132,720` | `if season is not None` at both sites |
| 6 | `ui/controllers/scanner_controller.py:824` | `quote(query)` in TMDB URL |
| 7 | `backend/detail_scraper.py:66` | `headers=headers` passed to `scraper.get()` |
| 8 | `backend/download_service.py:154` | `text={link}` not `[{link}]` in .crawljob |
| 9 | `backend/app_service.py:542-554` | `interval_hours`, `interval_seconds`, `only_when_idle` read inside loop body, not captured in closure |
| 10 | `ui/controllers/source_search_controller.py:745-752` | `_parse_size` handles `TB` unit |
| 11 | `backend/config.py:135,331` | `plex_mode` removed from TypedDict and `_DEFAULT_CONFIG`, only `plex_connection_mode` remains |
| 12 | `ui/qml/components/settings/PlexAccountTab.qml:62-68` | References `plex_connection_mode` not `plex_mode`. **Note: dead code** — component not instantiated by SettingsDialog |

### Commit 2: `774765c` — Tier 2 (thread safety)

| # | File | Verify |
|---|------|--------|
| 13 | `backend/watchlist.py:388-409` | `mark_found` does atomic SELECT+UPDATE under single `with self._lock:`, fires one `'found'` notification |
| 14 | `backend/download_service.py:277-339` | All scrape paths (HDEncode, ddlbase, adithd) run inside `with self._driver_lock:` |
| 15 | `backend/download_service.py:88-89,128-135` | `_history_lock` exists, wraps `download_history` and `_downloaded_titles_lookup` mutations |
| 16 | `backend/download_service.py:648-662` | `asyncio.set_event_loop(loop)` called, `shutdown_asyncgens()` in finally |
| 17 | `backend/watchlist.py:652-663` | `close()` acquires `self._lock`, try/except/finally structure with `self._conn = None` in finally |
| 18 | `backend/plex_service.py:209-368` | Temp lists `_movies`/`_tv` built during load, atomic swap at end |
| 19 | `backend/plex_manager.py:325-361` | Lock released before `server.library.sections()`, re-acquired for state mutation |
| 20 | `ui/controllers/main_controller.py:308` | `QMetaObject.invokeMethod` with `Qt.QueuedConnection` instead of direct `.emit()` |
| 21 | `ui/controllers/download_controller.py:165` | `cleanup_driver()` called directly (no redundant outer lock) |
| 22 | `ui/controllers/settings_controller.py:586-601` | `libraryCount/Name/Type/Assignment` reads wrapped with `self._libraries_lock` |
| 23 | `backend/analytics.py:125-133` | `_conn_lock` exists, wraps `_get_connection` |
| 24 | `ui/controllers/main_controller.py:8` | `QMetaObject, Qt, Q_ARG` imported |

### Commit 3: `2ebc0bb` — Tier 3 (quality/performance)

| # | File | Verify |
|---|------|--------|
| 25 | `ui/models/results_model.py:420-469` | `_dup_details_cache` precomputed in `_rebuild_group_counts()` |
| 26 | `ui/qml/style/Theme.qml` | `fontSizeXLarge: 24` property exists |
| 27 | `backend/matching.py:143` | `'HDR10PLUS' in title` check present |
| 28 | `backend/detail_scraper.py:103` | Single `re.findall(r'Filename\.*:\s*.+', text)` call, no `or` fallback |
| 29 | `backend/download_service.py:760-770` | `sys.platform == "win32"` guard on `CREATE_NO_WINDOW`, `xclip` fallback |
| 30 | `backend/watchlist.py:619-632` | Local-time `timedelta(days=7)` cutoff, not SQLite `datetime('now')` |
| 31 | `ui/models/results_model.py:278-280` | `_recalc_selected()` called after `endResetModel()`, not inside reset block |
| 32 | `ui/qml/ScannerTab.qml:76,98` | `if (_ready)` guard on both `onCurrentTextChanged` handlers |
| 33 | `ui/qml/SettingsDialog.qml:273,287` | `onEditingFinished` not `onTextChanged` for Plex username/password |
| 34 | `ui/qml/components/settings/NotificationsTab.qml:287` | `if (notifTab._ready)` guard on webhookMethod |
| 35 | `ui/qml/components/HistoryDialog.qml:60-83` | 200ms debounce Timer + `filterText` property |
| 36 | `ui/qml/components/settings/LogTab.qml:107-162` | 200ms debounce Timer + `debouncedText` property |

### Commit 4: `2e0b594` — Post-audit review fixes

| # | File | Verify |
|---|------|--------|
| 42 | `ui/models/results_model.py:98` | `_dup_details_cache: dict[int, str] = {}` initialized in `__init__` |
| 43 | `backend/app_config_manager.py` | **ERRATUM** — migration was placed here (dead code). Verify no `plex_mode` migration exists in this file (removed in commit 6) |
| 44 | `backend/download_service.py:281-285` | ddlbase/adithd paths inside inner `with self._driver_lock:` block |
| 45 | `tests/test_config.py:95` | `plex_connection_mode` not `plex_mode` |

### Commit 5: `ad66398` — Final cleanup

| # | File | Verify |
|---|------|--------|
| 46 | `ui/qml/components/settings/PlexAccountTab.qml:7-12,194` | `_ready` guard. **Dead code** — not instantiated |
| 47 | `ui/qml/components/settings/SettingRow.qml:7-8,70` | `if (!root._ready)` guard on `onCurrentIndexChanged` |
| 48 | `ui/controllers/download_controller.py:165` | No outer `_driver_lock`, just `cleanup_driver()` directly |
| 49 | `ui/qml/components/WatchlistDialog.qml:14-15,94` | `_ready` guard on sort ComboBox |

### Commit 6: `e49fd1f` — Migration correction

| # | File | Verify |
|---|------|--------|
| 50 | `backend/app_service.py:651-657` | `plex_mode` → `plex_connection_mode` migration in the **live** `load_config()` method. Check: uses `file_config` (user's JSON) not `config` (merged with defaults) for the `not in` check |
| 51 | `backend/app_config_manager.py` | Dead-path migration removed — file should have plain `config.update(file_config)` with no plex_mode logic |

---

## Part B: Known Issues Remaining from Codebase Audit

These items were identified in AUDIT_ISSUES.md but intentionally not fixed (Tier 4 — dead code kept for reference, plus lower-severity warnings).

### Dead code modules (Tier 4 — kept by design)

| File | Description |
|------|-------------|
| `backend/app_config_manager.py` | Only imported by its own test. Production uses `app_service.py:load_config()` |
| `backend/async_engine.py` | 845 lines, never imported anywhere |
| `backend/system_tray.py` | Duplicates `ui/system_tray.py`. Uses pystray; production uses PySide6 |
| `backend/link_scraper.py` | Possibly dead — `download_service.py` reimplements same functionality |
| `ui/qml/components/settings/PlexAccountTab.qml` | Not instantiated by SettingsDialog |

### Unfixed warnings from codebase audit

| File | Line | Issue |
|------|------|-------|
| `main.py` | 35 | No try/except around `main()` body |
| `main.py` | 40 | `setQuitOnLastWindowClosed(False)` — unkillable if tray icon fails |
| `config.py` | 199 | ~~`LOG_FILE` writes to project root~~ **Fixed** — moved to `_DATA_DIR` |
| `config.py` | 359 | `DEFAULT_CONFIG = _DEFAULT_CONFIG` exposes mutable internal dict |
| `database.py` | 178-300 | `init_db()` schema DDL runs without full lock |
| `database.py` | 358 | `save_plex_cache` mutates caller's dict (`item['key'] = fallback_key`) |
| `app_service.py` | 562 | `self.config["last_scan_time"] = now` data race on shared dict |
| `scanner_service.py` | 311 | Accesses private `self.plex._plex_loading` |
| `scanner_service.py` | 362,723 | `self.items.sort()` and iteration without `_items_lock` |
| `plex_service.py` | 134 | `_plex_loading` read without lock |
| `plex_service.py` | 108 | Accesses private `self.plex_manager._server_name` |
| `plex_service.py` | 417,482 | ~~`part.size` can be None~~ **Fixed** — null guard added |
| `plex_manager.py` | 296-299 | ~~`disconnect` doesn't clear `_account`~~ **Fixed** — `self._account = None` added |
| `plex_manager.py` | 287 | `discover_servers` creates new `MyPlexAccount` per call |
| `plex_manager.py` | 541-560 | `get_recently_added` mutates `_server` without lock |
| `watchlist.py` | 200-203 | ~~Duplicate detection only checks `imdb_id`~~ **Fixed** — title+year fallback for items without IMDb ID |
| `watchlist.py` | 430-440 | `check_against_scan_results` short-circuits on IMDb match |
| `watchlist.py` | 459-473 | `import_from_json` counts duplicates as insertions |
| `notifications.py` | 504 | ~~`asyncio.Lock()` created outside event loop~~ **Fixed** — lazy-init in `_add_to_batch` |
| `notifications.py` | 627-641 | ~~`send_notification()` event loop leak~~ **Fixed** — loop created and closed in try/finally |
| `tmdb_client.py` | 41 | `time.sleep()` while holding `self._lock` |
| `tmdb_client.py` | 36-42 | ~~Rate limiter uses `time.time()`~~ **Fixed** — switched to `time.monotonic()` |
| `download_service.py` | 711 | ~~CSV filename uses old `mediascout_results_`~~ **Fixed** — renamed to `scanhound_results_` |
| `download_service.py` | 719 | ~~`item.status.name`~~ **Fixed** — changed to `item.status.value` |
| `settings_controller.py` | 118 | `cancel()` replaces config dict — other controllers hold stale reference |
| `scanner_controller.py` | 922-924 | ~~`exportAnalytics` relative path~~ **Fixed** — writes to `~/analytics_report.json` |
| `rt_scraper.py` | 247 | New `cloudscraper` instance per call |
| `analytics.py` | 522-526 | `overall_quality_score` is unweighted average |
| `analytics.py` | 165-193 | `codec_counts` dict built but never populated |
| `analytics.py` | 580 | HTML template uses f-string injection without escaping |
| `ui/system_tray.py` | 181 | ~~`GetTickCount()` wraps~~ **Fixed** — changed to `GetTickCount64()` |
| `ResultRow.qml` | 1083 | ~~`"undefinedGB"` when size missing~~ **Fixed** — ternary guard added |
| `StyledButton.qml` | 19 | ~~Danger hover same color~~ **Fixed** — hover changed to `#ff6b5a` |
| `AnalyticsDialog.qml` | 192 | ~~Negative count possible~~ **Fixed** — wrapped in `Math.max(0, ...)` |

---

## Part C: SQLite Audit — Current Status

From SQLITE_AUDIT.md. Status verified against current code on `main` as of 2026-03-09. Several findings from the original SQLite audit have been resolved by the prior SQLite audit fix commit (`fcd3955`) or the codebase audit commits.

### Resolved

| # | Original Finding | Resolution |
|---|------------------|------------|
| 3 | **Analytics API mismatch** — `scanner_controller.py` calls `get_summary()` which doesn't exist | **Fixed** — `scanner_controller.py:905` now calls `get_dashboard_summary()`, which exists at `analytics.py:507`. |
| 4 | **WatchlistManager missing `busy_timeout` and WAL** | **Fixed** — `watchlist.py:121-122` now sets `PRAGMA journal_mode=WAL` and `PRAGMA busy_timeout=5000`. |
| 5 | **No schema versioning** | **Fixed** — `database.py:197` reads `PRAGMA user_version`, `database.py:289` applies versioned migrations (drops legacy `file_manager`, `schema_version`, `app_config` tables), `database.py:298` stamps version. |
| 7 | **StatsDashboard missing `busy_timeout` and WAL** | **Fixed** — `analytics.py:133-134` now sets `PRAGMA journal_mode=WAL` and `PRAGMA busy_timeout=5000`. Codebase audit also added `_conn_lock`. |

### Addressed (code fixed, live DB may need one-time action)

| # | Finding | File | Current Status |
|---|---------|------|----------------|
| 1 | **Watchlist schema incompatible with live DB** — live table has legacy TMDB-only columns | `watchlist.py:125-190` | **Code fixed** — `_init_db()` now adds missing columns via `ALTER TABLE ADD COLUMN` (`watchlist.py:131-151`), creates indexes after that (`watchlist.py:173-185`), and logs initialization failures at WARNING level (`watchlist.py:188-189`). Whether the live DB's legacy rows are semantically usable with the new schema (e.g. rows with `tmdb_id` but no `imdb_id` or `title`) needs a live-DB check. |

### Still Unresolved

#### Critical

| # | Finding | File | Status |
|---|---------|------|--------|
| 2 | **Plex cache has 14K stale rows** — `save_plex_cache()` only upserts, never prunes. | `database.py:342-384` | **Resolved** — pruning logic exists at `database.py:386-399`, deletes rows not in current save batch per content type. Stale rows are cleaned each full Plex load. |

#### High

| # | Finding | File | Status |
|---|---------|------|--------|
| 6 | **`INSERT OR REPLACE` on downloads resets `date_added`** | `database.py:510-515` | **Resolved** — already uses `INSERT ... ON CONFLICT(group_key) DO UPDATE` at `database.py:541-551`, preserving `date_added`. |

#### Medium

| # | Finding | File | Status |
|---|---------|------|--------|
| 8 | **`scanned_urls` read path unused** — table written to but `get_scanned_urls()` and `is_url_scanned()` only called from tests. Feature path may be dead. | `database.py:662-670` | **Unresolved** — decision needed: restore read path or remove write path |
| 9 | **Shared connection across threads** — single `sqlite3.Connection` with RLock. Nested `_mutate` inside `transaction()` would commit mid-transaction. | `database.py:69-84` | **Unresolved** — no current call site triggers this, but latent hazard |
| 10 | **Missing rollback in `add_scanned_urls_batch`** | `database.py:684-697` | **Resolved** — rollback present at `database.py:730-734`. |
| 11 | **Silent error handling in plex_cache stat methods** | `database.py:448-471,498-499` | **Resolved** — all stat methods log errors via `logger.error`/`logger.warning`. |
| 12 | **Watchlist deduplication gap** — only checks `imdb_id`. | `watchlist.py:210-222` | **Resolved** — added title+year fallback for items without IMDb ID. |

#### Low

| # | Finding | File | Status |
|---|---------|------|--------|
| 13 | `scan_history.timestamp` is TEXT — fragile ordering | `database.py:249` | **Unresolved** |
| 14 | Multiple SQLite copies create operational confusion | repo root + %LOCALAPPDATA% | **Unresolved** |
| 15 | Dead code: `is_url_scanned()`, `get_scanned_urls()`, duplicate `clear_history()` | `database.py` | **Unresolved** (note: `app_config` table now dropped by versioned migration #5) |

### Open Questions (require decisions before fixes)

1. **Plex cache refresh**: Full replace per content type, or incremental with tombstoning?
2. **`scanned_urls` feature path**: Restore the read path or remove the write path?
3. **Watchlist live data**: Do existing legacy rows (with `tmdb_id`/`media_type` but no `imdb_id`/`title`) need a data migration, or is the table effectively empty in production?

---

## Part D: What Codex Should Check

1. **Verify all 51 changes** in Part A match the actual code on current `main`
2. **Flag any regressions** — changes that broke existing behavior or introduced new bugs
3. **Assess the SQLite findings** in Part C — confirm severity, identify false positives, suggest priority order
4. **Review the open questions** — recommend decisions for each
5. **Identify anything missed** — bugs or structural issues not covered by either audit
