# ScanHound Codebase Audit — Complete Changelog

> **Audit period**: 2026-03-08 to 2026-03-09
> **Commits**: 7 (6 prior + 1 resolve-remaining)
> **Files modified**: 33 unique production/test files + audit docs
> **Source**: 35-pass sequential audit → Codex review → Opus 4.6 fresh-eyes review → post-fix review → final cleanup → migration correction → resolve remaining

---

## Commit 1: `5b2af8f` — Tier 1: High-priority bugs and config consolidation

12 changes: 1 CRITICAL, 4 HIGH, 4 MEDIUM, 3 LOW (1 dead-code consistency fix).

| # | File | Change | Risk | What it fixes |
|---|------|--------|------|---------------|
| 1 | `backend/watchlist.py:134-148` | `DROP TABLE watchlist` → `ALTER TABLE ADD COLUMN` for each missing column | **CRITICAL** | Legacy schema migration destroyed all watchlist data. Now adds `status`, `season`, `min_resolution`, `prefer_dovi`, `notes` columns individually. |
| 2 | `backend/metadata_enricher.py:122` | `movie_results or tv_results` → `movie_results + tv_results` | HIGH | Python `or` short-circuits on truthy lists. TV shows with movie IMDB hits got wrong metadata because `tv_results` was never checked. |
| 3 | `backend/metadata_enricher.py:127,165,166` | `if item.season` → `if item.season is not None` (3 sites) | HIGH | Season 0 (specials) is falsy in Python. These items were incorrectly classified as movies for TMDB search type and external ID lookup. |
| 4 | `backend/scanner_service.py:651,962,1045` | `if season` → `if season is not None` (3 sites) | HIGH | Season 0 items built wrong lookup keys (`normalized` instead of `normalized\|S0`), were excluded from TV grouping, and had wrong download history keys. |
| 5 | `backend/download_service.py:132,720` | `if season` → `if season is not None` (2 sites) | HIGH | History tracking and CSV export mishandled season 0 — wrong lookup keys and `TypeError` on `f"S{None:02d}"`. |
| 6 | `ui/controllers/scanner_controller.py:824` | Added `from urllib.parse import quote` + `quote(query)` in TMDB URL | MEDIUM | Titles with `&`, `#`, `?`, or Unicode broke TMDB API URLs. |
| 7 | `backend/detail_scraper.py:66` | `scraper.get(url, timeout=20)` → `scraper.get(url, headers=headers, timeout=20)` | MEDIUM | The `headers` parameter was accepted but never passed to the HTTP request. User-Agent overrides were silently ignored. |
| 8 | `backend/download_service.py:154` | `f"[{link}]\n"` → `f"text={link}\n"` | MEDIUM | `.crawljob` files used INI section-header syntax. JDownloader expects `text=<URL>` key-value format. |
| 9 | `backend/app_service.py:542-554` | Moved `interval_hours`, `interval_seconds`, `only_when_idle` from closure capture into `_scheduler_loop` body | MEDIUM | Scheduler captured config values at thread creation time. Changing scheduler interval or idle-only mode in settings had no effect until app restart. |
| 10 | `ui/controllers/source_search_controller.py:745-752` | Added `if "TB" in upper: val *= 1024` branch | LOW | `_parse_size("2.5 TB")` returned 0.0 — TB unit was unhandled. |
| 11 | `backend/config.py:135,331` | Removed duplicate `plex_mode` TypedDict field and `_DEFAULT_CONFIG` entry | LOW | `plex_mode` and `plex_connection_mode` coexisted — could silently desync. Consolidated to `plex_connection_mode`. |
| 12 | `ui/qml/components/settings/PlexAccountTab.qml:62-68` | `plex_mode` → `plex_connection_mode` (4 references) | LOW | **Dead code** — this component is not instantiated by `SettingsDialog.qml`. Changed for consistency with the config consolidation in #11. Does not affect production behavior. |

---

## Commit 2: `774765c` — Tier 2: Thread safety and stale state

12 changes: 1 HIGH, 8 MEDIUM, 2 LOW, 1 housekeeping.

| # | File | Change | Risk | What it fixes |
|---|------|--------|------|---------------|
| 13 | `backend/watchlist.py:388-409` | Replaced `get()` + `update()` with atomic SELECT+UPDATE under single `with self._lock:`, single `'found'` notification | HIGH | TOCTOU race: another thread could modify/delete the item between `get()` and `update()`. Also fired two notifications (`found` + `updated`) for one operation. |
| 14 | `backend/download_service.py:277-339` | Wrapped HDEncode scraping path with `with self._driver_lock:` | MEDIUM | WebDriver navigation was unprotected. Concurrent scrape requests could corrupt driver state. |
| 15 | `backend/download_service.py:88-89,128-135` | Added `self._history_lock = threading.Lock()`, wrapped `download_history`/`_downloaded_titles_lookup` mutations | MEDIUM | Download history sets/dicts accessed from multiple threads without synchronization. |
| 16 | `backend/download_service.py:648-662` | Added `asyncio.set_event_loop(loop)` + `loop.run_until_complete(loop.shutdown_asyncgens())` in finally | MEDIUM | Adit-HD scraper created event loops without setting them as current or cleaning up async generators. Could cause `RuntimeError` on resources tied to different loops. |
| 17 | `backend/watchlist.py:652-663` | Wrapped `close()` body with `with self._lock:`, restructured try/except/finally | MEDIUM | `close()` nulled `self._conn` without holding `_lock`. A concurrent thread calling `_get_connection()` could get `None` or a closed connection. |
| 18 | `backend/plex_service.py:209-368` | Built `_movies`/`_tv` temp lists during load, atomic swap `self.plex_movies = _movies` at end | MEDIUM | Lists were appended one-by-one during loading. UI thread reading `plex_movies` mid-load saw partial/inconsistent state. |
| 19 | `backend/plex_manager.py:325-361` | Release `_lock` before `server.library.sections()` network call, re-acquire for state mutation | MEDIUM | Lock was held during network I/O (potentially seconds). Blocked all other lock-acquiring methods including UI-thread callers. |
| 20 | `ui/controllers/main_controller.py:308` | `plexConnectedChanged.emit()` → `QMetaObject.invokeMethod(self, "plexConnectedChanged", Qt.QueuedConnection)` | MEDIUM | Signal emitted from non-Qt background thread. Qt signals must be emitted from the object's thread or use queued connections. |
| 21 | `ui/controllers/download_controller.py:166-168` | Added `if self._download_service._active_scrapes <= 0` guard before `cleanup_driver()` | MEDIUM | Concurrent downloads: first thread completing called `cleanup_driver()` which killed the WebDriver while second thread was still using it. |
| 22 | `ui/controllers/settings_controller.py:586-601` | Wrapped `libraryCount`, `libraryName`, `libraryType`, `libraryAssignment` reads with `self._libraries_lock` | LOW | `_libraries` list read by QML property accessors while `testPlex()` writes from background thread. |
| 23 | `backend/analytics.py:125-133` | Added `self._conn_lock = threading.Lock()`, wrapped `_get_connection` | LOW | Connection initialization had a theoretical race (double-create). WAL mode mitigated in practice but added lock for correctness. |
| 24 | `ui/controllers/main_controller.py:8` | Added `QMetaObject, Qt, Q_ARG` to imports | — | Required for the `invokeMethod` call in #20. |

---

## Commit 3: `2ebc0bb` — Tier 3: Quality and performance improvements

17 changes: 1 MEDIUM, 12 LOW, 3 housekeeping, 1 setup-only.

| # | File | Change | Risk | What it fixes |
|---|------|--------|------|---------------|
| 25 | `ui/models/results_model.py:420-469` | Precomputed `_dup_details_cache` dict in `_rebuild_group_counts()` | MEDIUM | `DuplicateDetails` role iterated the entire item list per `data()` call — O(n×m) complexity. Now O(n) precompute, O(1) lookup. |
| 26 | `ui/qml/style/Theme.qml` | Added `readonly property int fontSizeXLarge: 24` | LOW | `AnalyticsDialog.qml:47` referenced `Style.Theme.fontSizeXLarge` which didn't exist — got undefined/0 font size. |
| 27 | `backend/matching.py:143` | Added `or 'HDR10PLUS' in title` to HDR10+ preference check | LOW | `filename_utils.py` TAGS_RE recognizes `HDR10Plus` but the preference upgrade check only looked for `HDR10+`. Inconsistency meant HDR10Plus-tagged files didn't trigger the preference. |
| 28 | `backend/detail_scraper.py:103` | Consolidated two overlapping `re.findall` calls into single `re.findall(r'Filename\.*:\s*.+', text)` | LOW | The second pattern (`\.*`) is a superset of the first (`\.+`). The `or` meant the second only ran when the first returned `[]`, but both matched the same inputs. |
| 29 | `backend/download_service.py:760-770` | Added `sys.platform == "win32"` guard for `subprocess.CREATE_NO_WINDOW`, fallback to `xclip` on Linux | LOW | `CREATE_NO_WINDOW` is a Windows-only constant. Code would crash on Linux/macOS. |
| 30 | `backend/watchlist.py:619-632` | `datetime('now', '-7 days')` SQLite UTC → `(datetime.now() - timedelta(days=7)).isoformat()` Python local time | LOW | `added_date` and `found_date` are stored as local-time ISO strings. Comparing against SQLite's `datetime('now')` (UTC) gave wrong counts for users outside UTC. |
| 31 | `ui/models/results_model.py:278-280` | Moved `_recalc_selected()` after `endResetModel()` | LOW | `_recalc_selected()` emits `selectedCountChanged` signal. Inside the `beginResetModel/endResetModel` block, QML could query stale model state in response to the signal. |
| 32 | `ui/qml/ScannerTab.qml:76,98` | Added `_ready` guard on both ComboBox `onCurrentTextChanged` handlers | LOW | ComboBox `onCurrentTextChanged` fires during QML component initialization, silently calling `setScanType`/`setSource` before the app is ready. |
| 33 | `ui/qml/SettingsDialog.qml:273,287` | `onTextChanged` → `onEditingFinished` for Plex username and password TextFields | LOW | Credentials were saved to config on every keystroke. Now saves on Enter or focus loss. |
| 34 | `ui/qml/components/settings/NotificationsTab.qml:287` | Added `_ready` guard on webhookMethod `onCurrentTextChanged` | LOW | ComboBox fired during init, writing the default value to config and potentially overwriting the user's saved method. |
| 35 | `ui/qml/components/HistoryDialog.qml:60-62,75-83` | Added 200ms debounce Timer + `filterText` property on ListView | LOW | Filter model binding re-evaluated the full JS array on every keystroke. Now debounced to 200ms. |
| 36 | `ui/qml/components/settings/LogTab.qml:107-109,160-162` | Added 200ms debounce Timer + `debouncedText` property on TextField | LOW | Log filter visibility recalculated for all 1000+ delegates on every keystroke. Now debounced to 200ms. |
| 37 | `ui/qml/ScannerTab.qml:11-12` | Added `property bool _ready: false` + `Component.onCompleted: _ready = true` | — | Setup for ComboBox init-fire guards in #32. |
| 38 | `ui/qml/components/settings/NotificationsTab.qml:11-12` | Added `property bool _ready: false` + `Component.onCompleted: _ready = true` | — | Setup for webhookMethod guard in #34. |
| 39 | `backend/download_service.py:5-6` | Removed dead `import base64`, added `import sys` | — | `base64` was imported but never used. `sys` needed for platform guard in #29. |
| 40 | `backend/watchlist.py:1` | Added `timedelta` to `from datetime import datetime` | — | Required for the UTC fix in #30. |
| 41 | `ui/models/results_model.py:242` | `DuplicateDetails` role handler reduced to `return self._dup_details_cache.get(row, "")` | — | Replaced ~40 lines of per-call computation with cache lookup (paired with #25). |

---

## Commit 4: `2e0b594` — Post-audit review: Fix 3 bugs introduced by audit

4 changes: 2 HIGH, 1 LOW, 1 erratum (see commit 6).

| # | File | Change | Risk | What it fixes |
|---|------|--------|------|---------------|
| 42 | `ui/models/results_model.py:98` | Added `self._dup_details_cache: dict[int, str] = {}` to `__init__` | HIGH | Tier 3 #25 added `_dup_details_cache` in `_rebuild_group_counts()` but never initialized it in `__init__`. If QML called `data()` with the `DuplicateDetails` role before `setItems()`, it would crash with `AttributeError`. |
| 43 | `backend/app_config_manager.py:37-39` | ~~Added `plex_mode` → `plex_connection_mode` migration during config load~~ | ~~HIGH~~ | **ERRATUM**: This migration was placed in `app_config_manager.py`, which is dead code (test-only, not used by the live app). The production config loader is `app_service.py:load_config()`. This change had no production effect. Corrected in commit 6 (`e49fd1f`). |
| 44 | `backend/download_service.py:281-285` | Moved ddlbase/adithd early-return paths inside the inner `with self._driver_lock:` block | HIGH | Tier 2 #14 added driver lock for HDEncode scraping, but the ddlbase and adithd code paths branched *before* the lock. Both call `self.get_driver()` and `driver.get(url)` — concurrent scrapes from these sources had unprotected WebDriver access. |
| 45 | `tests/test_config.py:95` | `"plex_mode"` → `"plex_connection_mode"` | LOW | Test expected the removed `plex_mode` key in config. Would fail after Tier 1 changes. |

---

## Commit 5: `ad66398` — Final cleanup: fix remaining warnings

4 changes: 4 LOW (1 dead-code consistency fix).

| # | File | Change | Risk | What it fixes |
|---|------|--------|------|---------------|
| 46 | `ui/qml/components/settings/PlexAccountTab.qml:7-12,194` | Added `_ready` guard on `onCurrentTextChanged` for server ComboBox | LOW | **Dead code** — this component is not instantiated by `SettingsDialog.qml` (the live settings tabs are loaded at lines 1325, 1328, 1331). Changed for consistency only. Does not affect production behavior. |
| 47 | `ui/qml/components/settings/SettingRow.qml:7-8,70` | Added `_ready` guard on `onCurrentIndexChanged` | LOW | Every settings ComboBox wrote its current value back to config on component init — redundant config writes on every settings dialog open. |
| 48 | `ui/controllers/download_controller.py:165` | Removed redundant outer `_driver_lock` — just call `cleanup_driver()` directly | LOW | `cleanup_driver()` already acquires `_driver_lock` internally and waits for active scrapes to finish. The outer lock was redundant (RLock prevented deadlock but added unnecessary complexity). |
| 49 | `ui/qml/components/WatchlistDialog.qml:14-15,94` | Added `_ready` guard on sort ComboBox `onCurrentIndexChanged` | LOW | Sort ComboBox fired `onCurrentIndexChanged` during init, setting `sortMode` before the dialog was ready. |

---

## Commit 6: `e49fd1f` — Correct misplaced plex_mode migration

2 changes: 1 HIGH, 1 housekeeping. Fixes erratum from commit 4 #43.

| # | File | Change | Risk | What it fixes |
|---|------|--------|------|---------------|
| 50 | `backend/app_service.py:651-657` | Added `plex_mode` → `plex_connection_mode` migration in the live `load_config()` method | HIGH | Commit 4 #43 placed this migration in `app_config_manager.py` (dead code). The production loader in `app_service.py` had no migration, so users with `"plex_mode": "account"` in their `config.json` silently reverted to "direct" mode. |
| 51 | `backend/app_config_manager.py:37-39` | Removed the dead-path migration added in commit 4 #43 | — | Cleanup — migration logic should only exist in the live loader. |

---

## Summary Statistics

Counts derived from the tables above. Housekeeping rows (risk `—`) are excluded from risk totals.

| Risk | C1 | C2 | C3 | C4 | C5 | C6 | C7 | Total |
|------|----|----|----|----|----|----|-----|-------|
| CRITICAL | 1 | — | — | — | — | — | — | **1** |
| HIGH | 4 | 1 | — | 2 | — | 1 | — | **8** |
| MEDIUM | 4 | 8 | 1 | — | — | — | 5 | **18** |
| LOW | 3 | 2 | 12 | 1 | 4 | — | 9 | **31** |
| — (housekeeping) | — | 1 | 4 | 1 | — | 1 | 2 | **9** |
| **Subtotal** | **12** | **12** | **17** | **4** | **4** | **2** | **16** | **67** |

| Metric | Value |
|--------|-------|
| Production files modified | 33 |
| Dead-code-only edits | 2 (#12 PlexAccountTab config key, #46 PlexAccountTab _ready guard) |
| Erratum corrections | 1 (#43 → #50: migration moved to live path) |
| Tier 4 (dead code cleanup) | Skipped — kept for reference per user decision |

## Dead-code edits (not production-impacting)

These changes touch files or components that are not part of the live application path. They are included for consistency but do not affect runtime behavior.

| # | File | Why it is dead code |
|---|------|---------------------|
| 12 | `PlexAccountTab.qml` | Component not instantiated by `SettingsDialog.qml`. Live settings tabs: `GeneralTab`, `PlexLibrariesTab`, `NotificationsTab` (loaded at lines 1325, 1328, 1331). |
| 46 | `PlexAccountTab.qml` | Same component, same reason. |
| 43 | `app_config_manager.py` | Module only imported by `tests/test_app_config_manager.py`. Production config loaded by `app_service.py:load_config()`. |

---

## Commit 7 — Resolve remaining audit warnings

16 changes across 11 files. Addresses remaining items from Part B (codebase audit) and Part C (SQLite audit).

| # | File | Change | Risk | What it fixes |
|---|------|--------|------|---------------|
| 52 | `backend/config.py:199` | `LOG_FILE` moved from `_BASE_DIR` to `_DATA_DIR` | MEDIUM | Log file was written to project root (cloud-sync folder), causing OneDrive conflicts. Now writes to `%LOCALAPPDATA%\ScanHound\`. |
| 53 | `backend/download_service.py:719` | `mediascout_results_` → `scanhound_results_` | LOW | CSV export filename still used old app name. |
| 54 | `backend/download_service.py:727` | `item.status.name` → `item.status.value` | LOW | Enum `.name` outputs member name (e.g. `STATUS_MISSING`), `.value` outputs display string. |
| 55 | `ui/system_tray.py:181` | `GetTickCount()` → `GetTickCount64()` | MEDIUM | 32-bit `GetTickCount()` wraps after ~49.7 days, causing idle detection to report 0. `GetTickCount64()` is 64-bit. |
| 56 | `ui/qml/components/ResultRow.qml:1083` | `modelData.size + "GB"` → ternary guard | LOW | Rendered `"undefinedGB"` when `size` property was missing from model data. |
| 57 | `ui/qml/components/StyledButton.qml:19` | Danger hover `"#e74c3c"` → `"#ff6b5a"` | LOW | Hover color was identical to non-hover (`Theme.error` = `#e74c3c`). No visual feedback on hover. |
| 58 | `ui/qml/components/AnalyticsDialog.qml:192` | Wrapped 4K SDR count in `Math.max(0, ...)` | LOW | Negative count when `items_4k_hdr > items_4k` (data inconsistency edge case). |
| 59 | `backend/tmdb_client.py:38,42` | `time.time()` → `time.monotonic()` | LOW | Wall clock jumps on NTP sync could cause rate limiter to sleep for negative or excessive durations. |
| 60 | `backend/plex_service.py:421` | `if part` → `if part and part.size` | MEDIUM | `part.size` can be `None` for items without file size metadata. `TypeError` on `None / (1024**3)`. |
| 61 | `backend/plex_service.py:486` | `media.parts[0].size` → `media.parts[0].size or 0` | MEDIUM | Same null guard for TV episode size summation. |
| 62 | `backend/plex_manager.py:298` | Added `self._account = None` in `disconnect()` | LOW | Authenticated `MyPlexAccount` session persisted in memory after disconnect. |
| 63 | `backend/notifications.py:504` | `asyncio.Lock()` → `None` (lazy-init) | MEDIUM | `asyncio.Lock()` created outside event loop raises `DeprecationWarning` (3.10+) or `RuntimeError` (3.12+). Now initialized on first use inside async context. |
| 64 | `backend/notifications.py:627-644` | Event loop created in try/finally with `loop.close()` | MEDIUM | `send_notification()` created new event loop per call, never closed. Leaked OS resources. |
| 65 | `ui/controllers/scanner_controller.py:923` | `"analytics_report.json"` → `os.path.join(os.path.expanduser("~"), ...)` | LOW | Relative path wrote to unpredictable CWD. Now writes to user's home directory. |
| 66 | `ui/controllers/scanner_controller.py:4` | Added `import os` | — | Required for #65. |
| 67 | `backend/watchlist.py:210-222` | Title+year dedup fallback for items without IMDb ID | MEDIUM | Items without `imdb_id` could be added repeatedly with identical title and year. |

## Known Remaining Items (not fixed, by design)

| Scope | Reason |
|-------|--------|
| All Tier 4 items (dead code, stale naming) | Kept for reference per user decision |
