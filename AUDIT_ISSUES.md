# ScanHound Codebase Audit — Issues List

> ⚠️ **LEGACY / STALE (note added 2026-06-19).** This audit (Mar 2026) largely
> describes the earlier PySide6/QML `ui/` stack and cites commit hashes absent
> from current git history. Several items are already fixed (e.g. the `.crawljob`
> format in #6). Treat all file:line references as **stale** — re-verify against
> the live FastAPI + Svelte code before acting. Use this as a catalog of *areas
> worth checking*, not an accurate map of the current tree.

> 35-pass sequential audit covering every production file.
> Reviewed and corrected with fresh-eyes pass (Opus 4.6, 2026-03-08).
> Codex review applied 2026-03-08: removed false positives, re-tiered dead-code items, consolidated duplicates.
> Severity: CRITICAL > BUG > WARNING > INFO

---

## Prioritized Action Plan

### Tier 1 — Fix Immediately (data loss, broken functionality)

| # | File | Line | Severity | Summary |
|---|------|------|----------|---------|
| 1 | `watchlist.py` | 139 | CRITICAL | `DROP TABLE` migration destroys all watchlist data instead of `ALTER TABLE ADD COLUMN` |
| 2 | `metadata_enricher.py` | 122 | BUG | `movie_results or tv_results` short-circuits — TV shows with movie IMDB hits get wrong metadata |
| 3 | `scanner_service.py` | 651 | BUG | `if season` is falsy for season 0 (specials) — S0 items match against wrong download history entries |
| 4 | `scanner_controller.py` | 824 | BUG | TMDB query not URL-encoded — titles with `&`, `#`, `?` produce malformed API URLs |
| 5 | `detail_scraper.py` | 33,66 | BUG | `headers` parameter accepted but never passed to `scraper.get()` — User-Agent overrides silently ignored |
| 6 | `download_service.py` | 151-158 | BUG | `.crawljob` uses `[url]` section syntax — JDownloader expects `text=url` key-value format |
| 7 | `source_search_controller.py` | 745-748 | BUG | `_parse_size` doesn't handle TB — "2.5 TB" returns 0.0 |
| 8 | `config.py` | 28,136,264,331 | BUG | `plex_connection_mode` and `plex_mode` duplicate schema — consolidate into single key and prune dead UI callers (`PlexAccountTab.qml` uses `plex_mode` but is not instantiated) |
| 9 | `app_service.py` | 542-562 | BUG | Scheduler captures `interval_seconds`/`only_when_idle` in closure — config changes ignored until restart |

### Tier 2 — Fix Soon (thread safety, stale state, silent failures)

| # | File | Line | Severity | Summary |
|---|------|------|----------|---------|
| 10 | `download_service.py` | 631-653 | BUG | `_scrape_adithd_links` creates new event loop — async resources from different loop cause RuntimeError |
| 11 | `download_service.py` | 263-336 | WARNING | Shared Selenium WebDriver not thread-safe — concurrent `driver.get()` corrupts navigation |
| 12 | `download_service.py` | 89-90 | WARNING | `download_history` and `_downloaded_titles_lookup` accessed from multiple threads without sync |
| 13 | `watchlist.py` | 377 | BUG | `mark_found` TOCTOU: `get()` then `update()` without holding `_lock` across both |
| 14 | `watchlist.py` | 382-383 | BUG | `mark_found` fires two notifications (`found` + `updated`) for one operation |
| 15 | `watchlist.py` | 626-638 | WARNING | `close()` nulls `_conn` without `_lock` — concurrent thread gets `ProgrammingError` |
| 16 | `plex_service.py` | 36-38 | WARNING | `plex_movies`/`plex_tv` lists appended from background thread with no lock — UI reads partial state |
| 17 | `plex_manager.py` | 325-356 | WARNING | `refresh_libraries` holds lock during network I/O — blocks UI thread |
| 18 | `main_controller.py` | 308 | WARNING | `plexConnectedChanged.emit()` from non-Qt thread — needs queued connection |
| 19 | `settings_controller.py` | 586 | WARNING | `_libraries` read without lock while `testPlex()` writes from background thread |
| 20 | `download_controller.py` | 152-168 | WARNING | Concurrent downloads — first thread's `cleanup_driver()` kills second thread's driver |
| 21 | `analytics.py` | 126-133 | WARNING | `_get_connection()` has no lock — theoretically racy, but WAL+busy_timeout and limited callsites (init + reads) mitigate. Low priority. |

### Tier 3 — Improve (correctness, UX, performance)

| # | File | Line | Severity | Summary |
|---|------|------|----------|---------|
| 22 | `Theme.qml` | — | BUG | `fontSizeXLarge` property missing — `AnalyticsDialog.qml:47` references it, gets undefined/0 |
| 23 | `matching.py` | 138 | BUG | `HDR10+` check misses `HDR10Plus` variant — inconsistent with `filename_utils.py` TAGS_RE |
| 24 | `detail_scraper.py` | 103 | BUG | `all_filenames` fallback uses `or` with overlapping regex patterns — unreliable for mixed formats |
| 25 | `download_service.py` | 720 | WARNING | `f"S{item.season:02d}"` raises TypeError when season is None; season 0 outputs "-" |
| 26 | `download_service.py` | 755-758 | WARNING | `subprocess.CREATE_NO_WINDOW` with no platform guard — crashes on non-Windows |
| 27 | `watchlist.py` | 597-601 | WARNING | `get_stats` compares UTC SQL against local-time `added_date` — wrong count outside UTC |
| 28 | `results_model.py` | 244-282 | WARNING | `DuplicateDetails` role iterates entire list per `data()` call — O(n×m) for large sets |
| 29 | `results_model.py` | 312-321 | WARNING | `_recalc_selected()` emits signal inside `beginResetModel/endResetModel` — stale QML reads |
| 30 | `ScannerTab.qml` | 74,96 | WARNING | `onCurrentTextChanged` fires on init — silently overrides backend defaults |
| 31 | `SettingsDialog.qml` | 273-293 | WARNING | Plex credentials saved on every keystroke via `onTextChanged` |
| 32 | `SettingsDialog.qml` | 285 | WARNING | `webhookMethod` ComboBox has no `_ready` guard — init fire overwrites saved value |
| 33 | `HistoryDialog.qml` | 77-83 | WARNING | Filter recalculates full JS array on every keystroke — full delegate recreation |
| 34 | `WatchlistDialog.qml` | 113 | WARNING | Same filter-as-model perf issue as HistoryDialog |
| 35 | `LogTab.qml` | 151-162 | WARNING | Filtering via `visible: false` keeps all delegates in memory — degrades at 1000+ entries |

### Tier 4 — Clean Up (dead code, naming, minor issues)

| # | File | Line | Severity | Summary |
|---|------|------|----------|---------|
| 36 | `app_config_manager.py` | — | WARNING | Entire module is dead code (only imported by its own test) |
| 37 | `async_engine.py` | — | WARNING | Entire module (845 lines) is dead code — never imported |
| 38 | `backend/system_tray.py` | — | WARNING | Entire module duplicates `ui/system_tray.py` — dead code (uses pystray, production uses PySide6) |
| 39 | `link_scraper.py` | — | INFO | Possibly dead code — `download_service.py` reimplements same functionality |
| 40 | `PlexAccountTab.qml` | — | INFO | Component not instantiated by SettingsDialog. Dead UI code — remove or wire up |
| 41 | `plex_manager.py` | 451-486 | INFO | `save_to_dict`/`load_from_dict` omit fields and lack locking, but are test-only (no production callers). Fix if/when production use is added |
| 42 | `download_service.py` | 711 | WARNING | CSV filename uses old app name `mediascout_results_` |
| 43 | `backend/system_tray.py` | 196 | WARNING | Icon draws "M" (old MediaScout branding) |
| 44 | `ui/system_tray.py` | 140 | INFO | Docstring says "M" but code draws "S" — stale comment |
| 45 | `download_service.py` | 5-6 | INFO | `base64` imported but never used |
| 46 | `scanner_controller.py` | 686-700 | INFO | `setFlag4k`, `setFlag1080p`, etc. are empty `pass` stubs |
| 47 | `watchlist.py` | 10-13 | INFO | `re`, `time` imported but never used |
| 48 | `tmdb_client.py` | 125-131 | INFO | `season()` and `episode_external_ids()` appear unused |
| 49 | `network.py` | 16-31 | INFO | `RateLimitError` and `RequestTimeoutError` defined but never raised |

---

## Detailed Findings by Pass

### Pass 1: main.py

- **[WARNING]** `main.py:35` — No try/except around `main()` body. If `AppService()` or any controller constructor throws, the app crashes with a raw traceback.
- **[WARNING]** `main.py:40` — `setQuitOnLastWindowClosed(False)` means if tray icon fails, the app becomes unkillable without Task Manager.
- **[INFO]** `main.py:111` — Signal forwarding `plexConnectedChanged` is fragile — if either signal's signature changes, it silently breaks at runtime.
- **[INFO]** `main.py:46-53` — No log message when all icon files are missing.

---

### Pass 2: backend/config.py

- **[BUG]** `config.py:28,136` — `plex_connection_mode` and `plex_mode` both exist in `_DEFAULT_CONFIG`. Code uses both in different places — they can silently desync.
- **[BUG]** `config.py:306` — `ddlbase_manual_resolution_timeout` in `_DEFAULT_CONFIG` but not declared in `AppConfig` TypedDict.
- **[WARNING]** `config.py:359` — `DEFAULT_CONFIG = _DEFAULT_CONFIG` exposes the mutable internal dict. Any caller mutating it affects all future `get_default_config()` calls.
- **[WARNING]** `config.py:170` — `LOG_FILE` writes to project root. If in a cloud-synced folder (OneDrive), causes sync conflicts. DB was migrated to `%LOCALAPPDATA%` but log was not.
- **[INFO]** `config.py:176-196` — `_try_migrate_dir()` has TOCTOU race between `os.path.exists()` and `shutil.move()`. Low risk (startup-only).
- **[INFO]** `config.py:169` — `HISTORY_FILE` labeled "Legacy file to migrate" but still defined as module-level constant.

---

### Pass 3: backend/database.py

- **[WARNING]** `database.py:178-300` — `init_db()` acquires `_lock` only for `_init_depth` check, then releases. Schema DDL runs without lock — concurrent `init_db()` calls could race on CREATE/ALTER TABLE.
- **[WARNING]** `database.py:358` — `save_plex_cache` mutates caller's dict: `item['key'] = fallback_key`. Side effect — callers don't expect their data modified.
- **[INFO]** `database.py:360-384` — `INSERT OR REPLACE` vs `INSERT...ON CONFLICT DO UPDATE` inconsistency between plex_cache and add_to_history.
- **[INFO]** `database.py:702` — `get_scanned_urls()` full table scan. Fine at small scale, could slow with tens of thousands of URLs.

---

### Pass 4: backend/models.py

- **[WARNING]** `models.py:15` — `FilenameResult(TypedDict, total=False)` makes all keys optional, contradicting the docstring which says "Core keys (always present)". Type checkers won't enforce required keys.
- **[INFO]** `models.py:39,47` — `ScrapeResult.rating` is `str` and `ScrapeResult.hdr` is `str`. Likely hold formatted strings but type annotations suggest otherwise.

---

### Pass 5: backend/app_service.py

- **[BUG]** `app_service.py:542-562` — Scheduler thread captures `interval_seconds` and `only_when_idle` as closure variables. Config changes don't take effect until thread is restarted, but no code does this.
- **[WARNING]** `app_service.py:562` — `self.config["last_scan_time"] = now` written from scheduler thread while UI thread reads — data race on shared dict.
- **[WARNING]** `app_service.py:145-161` — `retry_request` decorator doesn't use `@functools.wraps(func)` — decorated functions lose metadata.
- **[INFO]** `app_service.py:110-112` — `LRUCache.__contains__` doesn't call `move_to_end`. Design choice — `in` check doesn't promote. Could cause unexpected evictions.
- **[INFO]** `app_service.py:470` — `seen: set[Tuple[str, str]]` uses Python 3.9+ syntax. Minor inconsistency with `typing.Set` elsewhere.
- **[INFO]** `app_service.py:691` — `os.open(..., 0o600)` POSIX permissions may not work on Windows.

---

### Pass 6: backend/scanner_service.py

- **[BUG]** `scanner_service.py:651` — `if season` is falsy for season 0 (specials). `lookup_key` drops the `|S0` suffix, causing false matches against movie entries.
- **[WARNING]** `scanner_service.py:234-235` — `asyncio.set_event_loop(loop)` from background thread — DeprecationWarning in Python 3.10+.
- **[WARNING]** `scanner_service.py:311` — Directly accesses `self.plex._plex_loading` (private attribute).
- **[WARNING]** `scanner_service.py:697` — Stores reference to scraped dict on MediaItem. Mutation of the dict later changes the item's data.
- **[INFO]** `scanner_service.py:570` — Hardcoded User-Agent. Should be centralized.
- **[INFO]** `scanner_service.py:362,723` — `self.items.sort()` and iteration operate without `_items_lock`. Safe in practice but inconsistent with lock discipline elsewhere.

---

### Pass 7: backend/plex_service.py

- **[WARNING]** `plex_service.py:134` — `_plex_loading` read without lock. Concurrent thread can slip between check and lock acquisition.
- **[WARNING]** `plex_service.py:36-38` — `plex_movies`/`plex_tv` lists appended from background thread with no lock. UI reads partial state.
- **[WARNING]** `plex_service.py:108` — Accesses `self.plex_manager._server_name` (private). Fragile.
- **[WARNING]** `plex_service.py:168` — Cached load sets `_last_full_load_time = time.time()`, making stale data appear current.
- **[INFO]** `plex_service.py:417,482` — `part.size` can be None — `TypeError` in size summation.
- **[INFO]** `plex_service.py:345` — `tv_seasons` counter is cumulative across all libraries. Log message misleading in multi-library setups.
- **[INFO]** `plex_service.py:20-24` — `PLEX_AVAILABLE` flag set but never checked in this file.

---

### Pass 8: backend/plex_manager.py

- **[INFO]** `plex_manager.py:451-462` — `save_to_dict` omits `connection_mode`, `username`, `password`, `server_name`. Real omission, but these methods have **no production callers** (test-only in `tests/test_plex_manager.py`). Fix if/when production serialization is added.
- **[INFO]** `plex_manager.py:464-486` — `load_from_dict` doesn't acquire `self._lock`. Same caveat: test-only, no production callers.
- **[WARNING]** `plex_manager.py:451-462` — `save_to_dict` serializes `self._token` in plaintext. Should use secure credential storage.
- **[WARNING]** `plex_manager.py:325-356` — `refresh_libraries` holds `self._lock` during network calls — blocks all lock-acquiring methods including UI thread.
- **[WARNING]** `plex_manager.py:158-159` — `is_connected` only checks `self._server is not None`. Stale server object not detected.
- **[WARNING]** `plex_manager.py:296-299` — `disconnect` doesn't clear `self._account` — authenticated session persists in memory.
- **[WARNING]** `plex_manager.py:541-560` — `get_recently_added` calls chain acquires `_lock` then mutates `_server` without the lock.
- **[WARNING]** `plex_manager.py:287` — `discover_servers` creates new `MyPlexAccount` per call with no rate limiting.
- **[INFO]** `plex_manager.py:568-601` — `migrate_library_config` references legacy config keys. Likely dead code.
- **[INFO]** `plex_manager.py:301-303` — `add_callback` appends without sync. Concurrent `_notify` iteration can raise `RuntimeError`.
- **[INFO]** `plex_manager.py:610-616` — Singleton reads `_plex_manager` outside lock before double-check.

---

### Pass 9: backend/watchlist.py

- **[CRITICAL]** `watchlist.py:139` — Legacy migration uses `DROP TABLE watchlist` when `status` column is missing. **Destroys all existing watchlist data.** Should use `ALTER TABLE ADD COLUMN status TEXT DEFAULT 'wanted'`.
- **[BUG]** `watchlist.py:377` — `mark_found` TOCTOU: `get()` then `update()` without holding `_lock` across both. Another thread can modify/delete between the two calls.
- **[BUG]** `watchlist.py:382-383` — `mark_found` fires two notifications (`found` after `update` which already fires `updated`).
- **[WARNING]** `watchlist.py:626-638` — `close()` nulls `self._conn` without `_lock`. Concurrent thread gets `ProgrammingError`.
- **[WARNING]** `watchlist.py:459-473` — `import_from_json` counts duplicates as insertions. Reported count overstated.
- **[WARNING]** `watchlist.py:597-601` — `get_stats` compares UTC SQL against local-time `added_date`. Wrong outside UTC.
- **[WARNING]** `watchlist.py:200-203` — Duplicate detection only checks `imdb_id`. Items without IMDb ID with identical title+year can be added repeatedly.
- **[WARNING]** `watchlist.py:430-440` — `check_against_scan_results` short-circuits on IMDb match, missing title-only duplicate.
- **[INFO]** `watchlist.py:10-13` — `re`, `time` imported but never used.

---

### Pass 10: backend/analytics.py

- **[WARNING]** `analytics.py:126-133` — `_get_connection()` has no lock. Theoretically racy on `self._conn is None`, but WAL journal mode + `busy_timeout=5000` mitigate, and production usage is limited to init (`app_service.py:520`) and reads (`scanner_controller.py:904`). Low priority.
- **[WARNING]** `analytics.py:522-526` — `overall_quality_score` is unweighted average of movie and TV scores. Misleading when library sizes differ significantly (1000 movies at 90 + 2 TV at 30 = 60 instead of ~89.9).
- **[WARNING]** `analytics.py:165-193` — `codec_counts` dict built but never populated. Always empty.
- **[WARNING]** `analytics.py:184` — Unknown resolutions default to quality score 50, scoring higher than 720p (30). Inflates scores.
- **[WARNING]** `analytics.py:200` — `upgrade_potential` double-counts SDR 1080p items.
- **[WARNING]** `analytics.py:536` — `format` parameter name shadows Python builtin.
- **[WARNING]** `analytics.py:580` — HTML template uses f-string injection without escaping.
- **[INFO]** `analytics.py:639-651` — No `close()` method. Singleton connection only closes at process exit.

---

### Pass 11: backend/app_config_manager.py

- **[WARNING]** `app_config_manager.py` — Entire module is dead code. Only imported by its own test. `AppService` has its own config management.

---

### Pass 12: backend/async_engine.py

- **[WARNING]** `async_engine.py` — Entire module (845 lines) is dead code. Not imported anywhere.
- **[WARNING]** `async_engine.py:494` — `MetadataEnricher` class name shadows the production one in `backend.metadata_enricher`.
- **[INFO]** `async_engine.py:99` — `asyncio.Lock` created outside event loop — `DeprecationWarning`/`RuntimeError` in Python 3.10+/3.12+.

---

### Pass 13: backend/auto_grab_service.py

- **[WARNING]** `auto_grab_service.py:88-93` — `evaluate_item()` filters out `DOWNLOADED` via `allowed_statuses`, then checks `if item.status == DOWNLOADED` — unreachable. Counter always 0. Harmless but indicates logic confusion.
- **[INFO]** `auto_grab_service.py:166-169` — Hardcoded service_type mapping only handles "rapidgator" and "nitroflare". Other hosts fall through to "Rapidgator" default.

---

### Pass 14: backend/download_service.py

- **[BUG]** `download_service.py:631-653` — `_scrape_adithd_links` creates `new_event_loop()` then calls async methods. If source initialized resources on a different loop, raises `RuntimeError`.
- **[BUG]** `download_service.py:151-158` — `.crawljob` format is incorrect. Uses `[{link}]` section-header syntax. JDownloader expects `text=<URL>` key-value pairs.
- **[WARNING]** `download_service.py:263-336` — Shared Selenium WebDriver not thread-safe. Concurrent `driver.get()` corrupts navigation.
- **[WARNING]** `download_service.py:89-90` — `download_history` and `_downloaded_titles_lookup` accessed from multiple threads without synchronization.
- **[WARNING]** `download_service.py:110-119` — `load_download_history` returns new set but never assigns back. Caller must do manually.
- **[WARNING]** `download_service.py:132` — Key format produces bare `normalized` when season=None, while `group_key` convention uses `|S0`. Lookups won't match.
- **[WARNING]** `download_service.py:711` — CSV filename uses old `mediascout_results_` name.
- **[WARNING]** `download_service.py:719` — `item.status.name` outputs enum member name instead of `.value`. Inconsistent.
- **[WARNING]** `download_service.py:720` — `f"S{item.season:02d}"` raises `TypeError` if season is None. Season 0 (falsy) outputs "-" instead of "S00".
- **[WARNING]** `download_service.py:755-758` — `subprocess.CREATE_NO_WINDOW` (Windows-only) with no platform guard. Crashes on other platforms.
- **[INFO]** `download_service.py:763-771` — `QApplication.clipboard()` from background thread — can deadlock on some Qt builds.
- **[INFO]** `download_service.py:5-6` — `base64` imported but never used.

---

### Pass 15: backend/link_scraper.py + backend/detail_scraper.py

- **[BUG]** `detail_scraper.py:33,66` — `headers` parameter accepted but never passed to `scraper.get()`. User-Agent/cookie overrides silently ignored.
- **[BUG]** `detail_scraper.py:103` — `all_filenames` fallback uses `or` with partially overlapping regex patterns. Unreliable for mixed formats.
- **[WARNING]** `detail_scraper.py:92-96` — `all_filenames` runs against `text` (content div) but filename may exist in full-page fallback. Episode count can be wrong.
- **[WARNING]** `detail_scraper.py:167-170` — cp437 mojibake repair applied unconditionally. Valid ASCII could be silently corrupted.
- **[WARNING]** `link_scraper.py:68-74` — Each of 4 XPath selectors waits 8s. Worst case: 32s blocking before CSS fallback. Timeout should be shared.
- **[WARNING]** `link_scraper.py:149-158` — 8 button selectors × 15s `WebDriverWait`. Worst case: 120s blocking.
- **[WARNING]** `link_scraper.py:123` — Domain check uses substring `in` instead of `urlparse` netloc check. `evil.com/cuty.io/payload` would pass.
- **[INFO]** `link_scraper.py` — Possibly dead code. `download_service.py` reimplements same functionality.
- **[INFO]** `link_scraper.py:93` — Only "Rapidgator" and "Nitroflare" handled; others silently fall through.
- **[INFO]** `detail_scraper.py:199` — `s_str.upper()` produces "GIB" instead of "GiB".

---

### Pass 16: backend/metadata_enricher.py

- **[BUG]** `metadata_enricher.py:122` — `data.get("movie_results", []) or data.get("tv_results", [])` short-circuits. If `movie_results` is non-empty, `tv_results` is never checked. TV shows with movie IMDB hits get wrong metadata.
- **[WARNING]** `metadata_enricher.py:254-258` — `ThreadPoolExecutor` futures not cancelled on stop. Already-submitted futures block `executor.__exit__`.
- **[INFO]** `metadata_enricher.py:137` — `search_type` only assigned inside `if not result_data` blocks. Currently unreachable `NameError` but fragile.

---

### Pass 17: backend/network.py

- **[WARNING]** `network.py:152` — Session fetched once before retry loop. If session becomes invalid mid-retry, stale session is reused for all attempts.
- **[INFO]** `network.py:16-31` — `RateLimitError` and `RequestTimeoutError` defined but never raised.
- **[INFO]** `network.py:155` — `range(max_retries)` gives 3 attempts vs `TmdbClient._get` uses `range(max_retries + 1)` for same semantic.

---

### Pass 18: backend/scrapers.py

- **[WARNING]** `scrapers.py:50-56` — Facade exposes private `RTScraper` methods as public. Callers can bypass validation, caching, and fallback logic.
- **[INFO]** `scrapers.py:20-39` — `self.app` stored on facade but never used directly (sub-scrapers get `parent_app` independently).

---

### Pass 19: backend/tmdb_client.py

- **[WARNING]** `tmdb_client.py:41` — `time.sleep()` called while holding `self._lock` — blocks all other threads for the full sleep duration.
- **[WARNING]** `tmdb_client.py:53-81` — Doesn't catch `JSONDecodeError` from `resp.json()`. Malformed 200 response falls to generic `except Exception`.
- **[WARNING]** `tmdb_client.py:36-42` — Rate limiter uses `time.time()` (wall clock) which can jump on NTP sync. Should use `time.monotonic()`.
- **[INFO]** `tmdb_client.py:125-131` — `season()` and `episode_external_ids()` appear unused.

---

### Pass 20: backend/notifications.py + backend/notification_bridge.py

- **[WARNING]** `notifications.py:504` — `asyncio.Lock()` created at `__init__` time. If instantiated outside event loop, raises `DeprecationWarning` (3.10+) or `RuntimeError` (3.12+).
- **[WARNING]** `notifications.py:627-641` — `send_notification()` creates `new_event_loop()` + `set_event_loop()` per call. Leaks loops (never closed).
- **[WARNING]** `notifications.py:80` — Filters default to ALL notification types. Every channel handles every type unless explicitly restricted.
- **[WARNING]** `notifications.py:686` — `_combine_notifications` merges data dicts with `.update()` — later notifications silently overwrite earlier keys.
- **[INFO]** `notification_bridge.py:89` — `asyncio.set_event_loop()` from background thread — DeprecationWarning in Python 3.10+.
- **[INFO]** `notification_bridge.py:122-125` — `run_coroutine_threadsafe` result never awaited. Exceptions silently lost.

---

### Pass 21: backend/system_tray.py

- **[WARNING]** `system_tray.py` — Entire module duplicates `ui/system_tray.py`. Uses pystray while production uses PySide6. Dead code.
- **[WARNING]** `system_tray.py:196` — Icon draws "M" (old MediaScout branding).
- **[WARNING]** `system_tray.py:462-470` — macOS idle detection spawns `ioreg` subprocess every 60s. Fragile.
- **[INFO]** `system_tray.py:546-551` — `get_tray_manager()` singleton has no thread safety.
- **[INFO]** `system_tray.py:430` — When system is busy, scan is silently skipped and rescheduled.

---

### Pass 22: backend/rt_scraper.py + backend/imdb_scraper.py

- **[WARNING]** `rt_scraper.py:247` — New `cloudscraper` instance per call. Should reuse session.
- **[WARNING]** `rt_scraper.py:233-234` — Cache key uses `clean_string(title)` but scraping uses original `title`. If `clean_string` changes title, cache serves wrong data.
- **[INFO]** `rt_scraper.py:177` — Second `cloudscraper` instance created inside `_scrape_rt_direct` for one call path.
- **[INFO]** `rt_scraper.py:341` — Search URL doesn't filter by content_type (tv vs movie).
- **[INFO]** `imdb_scraper.py:75` — Return type annotation says `Optional[IMDbData]` but returns plain dict.
- **[INFO]** `imdb_scraper.py:52` — Uses `requests.get()` directly instead of `retry_request()`. No retry logic.

---

### Pass 23: backend/filename_utils.py + backend/matching.py

- **[BUG]** `matching.py:138` — `HDR10+` check misses `HDR10Plus`/`HDR10PLUS` variants. `filename_utils.py` TAGS_RE recognizes `HDR10Plus` — inconsistency means preference upgrade doesn't trigger.
- **[WARNING]** `matching.py:20-27` — `lru_cache(maxsize=10000)` accumulates stale entries. `clear_fuzzy_cache()` exists but is never called.
- **[WARNING]** `matching.py:573` — Per-episode size divides by `web.get('episodes', 1)` but guard uses default 0. Inconsistent defaults.
- **[WARNING]** `filename_utils.py:121` — Year regex requires leading delimiter. Filenames starting with year (e.g. `2024.Movie.mkv`) won't match.
- **[INFO]** `matching.py:617` — `res_priority` dict defined in multiple places. Should be module-level constant.

---

### Pass 24: ui/system_tray.py

- **[WARNING]** `system_tray.py:181` — `GetTickCount()` returns 32-bit DWORD, wraps after ~49.7 days. Should use `GetTickCount64()`.
- **[INFO]** `system_tray.py:140` — Docstring says "M" but code draws "S". Stale comment.

---

### Pass 25: ui/controllers/main_controller.py

- **[WARNING]** `main_controller.py:308` — `plexConnectedChanged.emit()` from non-Qt thread. Needs `QMetaObject.invokeMethod` for thread safety.

---

### Pass 26: ui/controllers/scanner_controller.py

- **[BUG]** `scanner_controller.py:824` — TMDB query not URL-encoded. `f"...&query={query}&page=1"` — titles with `&`, `#`, `?`, Unicode break the URL.
- **[WARNING]** `scanner_controller.py:922-924` — `exportAnalytics` writes to relative path `"analytics_report.json"` — unpredictable CWD.
- **[WARNING]** `scanner_controller.py:1037` — Accesses `ScannerService._posted_date_sort_key()` (private method). Silently breaks if renamed.
- **[INFO]** `scanner_controller.py:686-700` — `setFlag4k`, `setFlag1080p`, etc. are empty `pass` stubs. Dead code.
- **[INFO]** `scanner_controller.py:814` — `import threading` redundant (already at module level).

---

### Pass 27: ui/controllers/settings_controller.py

- **[WARNING]** `settings_controller.py:118` — `cancel()` replaces config dict. Other controllers holding old reference use stale config.
- **[WARNING]** `settings_controller.py:586` — `_libraries` read without lock while `testPlex()` writes from background thread.

---

### Pass 28: ui/controllers/download_controller.py

- **[WARNING]** `download_controller.py:57-60` — `_download_service` could be None if `ensure_service()` not called. Path from `_mark_items_downloaded` doesn't guarantee this.
- **[WARNING]** `download_controller.py:152-168` — Concurrent downloads: first thread's `cleanup_driver()` kills second thread's Selenium driver.
- **[INFO]** `download_controller.py:100` — `exportResultsCsv` only exports current page, not all pages. Inconsistent with other bulk operations.

---

### Pass 29: ui/controllers/source_search_controller.py

- **[BUG]** `source_search_controller.py:745-748` — `_parse_size` doesn't handle TB. "2.5 TB" returns 0.0.
- **[WARNING]** `source_search_controller.py:53` — `asyncio.run()` inside QThread creates/destroys event loop per search.
- **[WARNING]** `source_search_controller.py:793` — `finished.disconnect()` without specifying slot. Disconnects all; raises `RuntimeError` if none exist.
- **[INFO]** `source_search_controller.py:674` — `season or 0` conflates `season=None` (movie) with `season=0` (specials).

---

### Pass 30: ui/models/results_model.py + ui/models/log_model.py

- **[WARNING]** `results_model.py:244-282` — `DuplicateDetails` role iterates entire list per `data()` call. O(n×m) for large result sets. Should precompute.
- **[WARNING]** `results_model.py:312-321` — `setItems` emits signal inside `beginResetModel/endResetModel`. QML queries stale data.
- **[WARNING]** `log_model.py:227` — `getGroupCount` iterates entire list per call. O(sections × items).

---

### Pass 31: ui/qml/main.qml

- **[INFO]** `main.qml:242-249` — ~~Previously flagged as shadowing a Python context property, but `logListModel` is not exposed as a context property (only `app`, `scanner`, `settings`, `sourceSearch` are). False positive — removed from action plan.~~
- **[WARNING]** `main.qml:35-36` — Saved window position ignored when negative (multi-monitor to left/above primary).
- **[WARNING]** `main.qml:329-331` — Competing `NumberAnimation on opacity` for snackbar. Rapid triggers cause fight.

---

### Pass 32: ui/qml/ScannerTab.qml

- **[WARNING]** `ScannerTab.qml:74` — `onCurrentTextChanged` fires on ComboBox init — silently overrides backend default.
- **[WARNING]** `ScannerTab.qml:96` — Same init-fire issue for source ComboBox.
- **[INFO]** `ScannerTab.qml:563` — Quick filter parses JSON on every property evaluation per delegate.

---

### Pass 33: ui/qml/SettingsDialog.qml + ui/qml/style/Theme.qml

- **[BUG]** `Theme.qml` — `fontSizeXLarge` property missing. `AnalyticsDialog.qml:47` references `Style.Theme.fontSizeXLarge` — gets undefined/0 font size.
- **[WARNING]** `SettingsDialog.qml:273-293` — Plex credentials saved on every keystroke via `onTextChanged`.
- **[WARNING]** `SettingsDialog.qml:285` — `webhookMethod` ComboBox no `_ready` guard — init fire overwrites saved value.
- **[WARNING]** `SettingsDialog.qml:584` — Library assignment ComboBox `onCurrentTextChanged` fires when `libraryCount` changes.

---

### Pass 34: ui/qml/components/ (ResultTile, ResultRow, GroupHeader, SourceSearchWindow)

- **[WARNING]** `ResultRow.qml:1083` — `modelData.size + "GB"` renders as `"undefinedGB"` when size is missing.
- **[WARNING]** `ResultRow.qml:52-104` — `_plexParsed` computed property runs O(n²) deduplication.
- **[WARNING]** `GroupHeader.qml` — Declares `required property` not provided by `ResultsModel` roles. Fails at runtime if used as delegate.

---

### Pass 35: ui/qml/components/ (Dialogs, settings tabs, StyledButton)

- **[INFO]** `PlexAccountTab.qml:65-66` — Uses `plex_mode` while SettingsDialog uses `plex_connection_mode`. Two keys for same concept — but **this component is not instantiated** by the current SettingsDialog. Dead UI code. Root cause is the duplicate config key in `config.py` (see Tier 1 #8).
- **[INFO]** `PlexAccountTab.qml:192` — `plex_selected_server` cleared on init (no `_ready` guard). Same caveat: component is not instantiated in production.
- **[WARNING]** `StyledButton.qml:19` — Danger hover color identical to non-hover. No visual feedback.
- **[WARNING]** `HistoryDialog.qml:77-83` — Filter recalculates full JS array on every keystroke.
- **[WARNING]** `WatchlistDialog.qml:113` — Same filter-as-model perf issue.
- **[WARNING]** `AnalyticsDialog.qml:192` — Negative count possible when `items_4k_hdr > items_4k`.
- **[WARNING]** `LogTab.qml:151-162` — Filter via `visible: false` keeps all delegates in memory.
- **[WARNING]** `SettingRow.qml:68` — `onCurrentIndexChanged` fires on init, writing existing value back.

---

## Audit Metadata

- **Passes**: 35 (covering all production files)
- **Total findings**: 127
- **CRITICAL**: 1 (watchlist data destruction)
- **BUG**: 15 (3 downgraded to INFO after Codex review — test-only/false positive)
- **WARNING**: 77
- **INFO**: 34 (includes 1 false positive struck through, 4 reclassified from BUG/WARNING)
- **Dead code modules**: 3 (app_config_manager, async_engine, backend/system_tray) + 1 possible (link_scraper) + 1 dead UI (PlexAccountTab)
- **Recurring patterns**: season 0 falsy (4 occurrences), `onCurrentTextChanged` init fire (6 occurrences), `asyncio.set_event_loop` deprecation (3 occurrences), plaintext credential storage (2 occurrences)
- **Codex review corrections**: `plex_manager.py` save/load moved to Tier 4 (test-only), `main.qml` logListModel shadow removed (false positive), `analytics.py` connection race downgraded (WAL mitigates), `config.py` plex_mode duplication consolidated with dead `PlexAccountTab` callers into single action item
