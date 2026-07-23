# Answers for PR 1 — HDEncode Immediate Safety and Diagnostic Outcomes

*From Claude, in response to Codex's revised plan and 15 pre-implementation questions.*

Before the answers: two things surfaced during this pass that change what "small" means for Phase 0.

**A live bug: `GET /sources` always returns `[]`.** `backend/api/routes/sources.py:16` constructs `SourceRegistry()` fresh per request and never calls `discover_sources()` on it, so `list_sources()` iterates an empty `_configs` dict every time. `tests/test_api_routes.py:251-257` has `if data: assert "name" in data[0]` — a conditional that never fires, so this has been passing vacuously. Anything Phase 0 does with the source-list endpoint needs to fix this first, or it's building on top of dead output.

**A cancellation primitive already exists and needs no new code.** `scanner_service.py` has `self._stop_event` (a real `threading.Event`, line 171), exposed as `stop_scan_flag`, already checked inside every detail-fetch worker (line 749) and in the future-consumption loop, where it actively cancels not-yet-started futures (lines 768-774). It's already reachable via `POST /scan/stop`. Foreground and background scans share one `ScannerService` instance by explicit design (documented at line 174-176), so **"stop queued work after a confirmed shared block" is `scanner.stop_scan_flag = True` on the existing service — not a new primitive.** The one gap: `BackgroundScanner.stop()` (`background_scanner.py:60-66`) does *not* set this flag — it only breaks its own scheduler-wait loop and `join(timeout=2.0)`s, which will simply time out if a scan is mid-flight. A block-detection hook must call `scanner.stop_scan_flag = True` directly, not rely on `BackgroundScanner.stop()`.

Answers below follow your numbering. 6.15 was cut off mid-sentence in what you sent ("Please identify anything else that should be resolved be…") — I don't have the rest of that question, so it's not answered here.

---

## 6.1 Current request paths

| # | Entry point | Transport | Trigger | Retry | Concurrency/locking | Bypasses `SourceRegistry`? | Bypasses `SourceConfig` policy? |
|---|---|---|---|---|---|---|---|
| 1 | `scanner_service.py:640` — listing page fetch, inside `_crawl_pages` | `cloudscraper` (created once, `:427`), via `run_in_executor` | User (`/scan/start`) + background (`BackgroundScanner`) | None — non-200 just `continue`s (`:656`) | Sequential per category; `asyncio.sleep(0.3)` between pages (`:694`); block backoff `min(0.5*streak, 3.0)` on 403/429/503, abort after 3 consecutive (`:653-654`) | Yes — never imports `sources.base` | Yes — hardcoded 15s timeout, no `rate_limit` |
| 2 | `detail_scraper.py:67` via `scanner_service.py:755` (`_process_posts`) | `cloudscraper` (shared instance, or fresh per rescan) | User + background | 3 attempts (`detail_scraper.py:61`), 429 → 2/4/6s backoff, 403 **not distinguished from anything else** (`:73-75`) | **`ThreadPoolExecutor(max_workers=num_threads)`, default 10, ALL futures submitted upfront in one list comprehension** (`scanner_service.py:766-767`) — no batching | Yes | Yes — hardcoded 20s timeout, no rate_limit |
| 3 | `scanner.py:369` `/scan/rescan-item` | `cloudscraper` (**fresh instance per call** — no shared scraper passed) | User | Same as #2 | Single request, no pool | Yes | Yes |
| 4 | `download_service.py:1140` `_navigate` → `driver.get(url)` | Selenium/uc | User (`/downloads/scrape`, `/downloads/copy-links`) + auto-grab | 3 attempts, recycles driver each attempt, backoff `min(2*attempt,5)` (`:1155-1157`) — but only on Chrome's own `ERR_*` page or a raised exception; a served challenge/403 page is not detected as failure here | Serialized by `self._driver_lock` (RLock, `:167`), acquired in `scrape_links` (`:1296`) | Yes | Yes |
| 5 | `download_service.py:1379-1381` link-reveal click | Selenium `execute_script` | Same as #4 | None | Same lock as #4 | Yes | Yes |
| 6 | `sources/base.py:228` `_fetch_html`, used by `HDEncodeSource.search` (`hdencode.py:316`) | `cloudscraper` or `requests.Session` fallback | User only (`/pipeline/search-sources`) | 2 attempts; 429 retried, 403 → immediate `None`, no retry | `min_interval = 1.0/rate_limit` (2.0 → 500ms), **but state is per-instance and `pipeline.py:108` builds a new `SourceRegistry()` per request, so pacing state is discarded every call** | **No** — this is the only path that goes through `SourceRegistry` | **No** — this is the only path that honors `rate_limit`/`timeout` |

**Auto-grab** (`auto_grab_service.py:172` → `download_service.py:1981`) and **grab/copy-links** funnel into paths #4/#5. **Manual single-item rescan** (#3) is the sharpest gap: it doesn't even reuse the shared `cloudscraper` instance, so it can't inherit pacing state you add to the shared one unless you thread it through explicitly.

**Dead paths, safe to ignore or delete rather than wire up:** `sources/hdencode.py:117,366` (`fetch_page`/`fetch_release_details`, no callers), `sources/hdencode.py:456-461` (`fetch_download_links`, builds its **own** plain `webdriver.Chrome`, bypassing the cached driver entirely — its own comment admits this), `link_scraper.py:56` (no callers).

**Answering your specific worry** ("avoid fixing one path while leaving another capable of uncontrolled requests"): #2 and #3 both hit `detail_scraper.py`, so a module-level rate limiter/semaphore in `detail_scraper.scrape_details` (rather than in `scanner_service.py`) covers both with one change, including the rescan path that currently has no shared state at all.

## 6.2 Best insertion points for Phase 0

- **Enabled-state enforcement:** `scanner_service._build_sources` (`:499-561`) — this is the single point every scan (manual + background) goes through to decide which sources to hit. One `if source_type == "HDEncode" and not self.config.get("hdencode_enabled", True): continue` at the top of that branch covers #1, #2, #3 simultaneously. Also add the same check as an early return in `download_service.scrape_links` (`:1296`, before the URL-based dispatch at `:1298-1310`) to cover #4/#5 — grabs can be triggered by things that aren't a scan (auto-grab, a stale pipeline result).
- **HDEncode-specific concurrency cap:** don't touch `scan_threads` globally — add a module-level `threading.Semaphore` in `detail_scraper.py`, sized independently of `num_threads`, acquired inside `scrape_details` before the request. This is the one-line fix for "a generic thread setting cannot create an uncontrolled source burst" — right now `scan_threads` *is* the HDEncode concurrency cap, with no separate ceiling.
- **Request-start spacing:** same location — a module-level `_last_request_at` + lock in `detail_scraper.py`, checked before each request. Keeping it there (not in `scanner_service.py`) means it also covers path #3.
- **Shared stop-on-block signal:** reuse `scanner.stop_scan_flag` (see the note above) — no new module needed for Phase 0. Set it from wherever the 403/429 streak is detected (`scanner_service.py:647-654` already counts `blocked_streak`; have it flip `self.stop_scan_flag = True` at the existing abort threshold instead of just `break`-ing the listing loop, so it also stops in-flight detail workers).
- **Structured operation result propagation:** the boundary is `download_service._log_page_diagnostics` (`:1161-1244`, currently `-> None`) and its 4 discard sites (`:1371-1372, :1390-1391, :1406, :1468-1469`). Change the return type there first — everything downstream (`scrape_links`, `download_item`) is a narrow, mechanical thread-through, not a rewrite.

These four are all small, targeted edits to existing functions — no new file is required for Phase 0 except possibly a tiny `SourcePacer` helper class if you want the semaphore+timestamp logic reusable between `detail_scraper.py` and a future Phase 3 coordinator. I'd inline it in `detail_scraper.py` for PR 1 and extract only when Phase 3 needs a second consumer.

## 6.3 Structured result design

Your proposed fields are close. Recommendations:

- **Essential:** `success`, `code` (an enum, not a free string — see 6.4 for values), `transport` (`http`|`selenium`), `retryable` (bool), `affects_source_health` (bool). `message` should be the ONE user-facing string, generated from `code` at the boundary, not carried through internally — avoids two copies of the same mapping drifting apart.
- **Keep `signals` but make it a `list[str]`, not prose** — `_log_page_diagnostics` already collects discrete markers (`turnstile`, `cf-chl`, `err_connection_reset`); preserve them as-is rather than summarizing, since Phase 1's confidence model (per the earlier plan's §6.5) wants to reason about combinations later.
- **Drop `health_state` from this type.** The scrape-result type should report what happened on *this one operation*; whether that operation *changes global source health* is a decision the caller (or the Phase 3 coordinator) makes by looking at `code` + `affects_source_health` + recent history. Baking `health_state` into every individual result couples a low-level function to a policy decision it shouldn't own.
- **`status_code`** should be `Optional[int]` — Selenium operations legitimately have none (a rendered challenge page has no distinct "status code" from Selenium's perspective; `_browser_error_code()` returns a Chrome `ERR_*` string, not an HTTP code).
- **One generic result type for both transports**, not two — the whole point is that a caller three layers up (`download_item`) shouldn't need to know whether the failure came from `cloudscraper` or Selenium. `transport` as a field, not as a type split, is what makes that possible.
- **Exceptions should NOT be represented directly in the result type.** Catch them at the boundary (inside `_navigate`, inside `scrape_details`'s retry loop) and map to `code` there. A result type that can hold either a value or an exception invites callers to skip the `try/except` and let raw exceptions leak past the classification layer — exactly the bug you're trying to fix.
- **Avoiding a large rewrite of existing callers:** `_log_page_diagnostics` currently returns `None` and every caller discards it. Change its return type and have the **4 existing discard sites** (`:1371-1372, :1390-1391, :1406, :1468-1469`) each do `diag = self._log_page_diagnostics(...); return [], diag` instead of `return []`. `scrape_links`'s signature changes from `-> list[str]` to `-> tuple[list[str], Optional[ScrapeDiagnostic]]`. That's 4 call sites plus `scrape_links`'s ~2 direct callers (`downloads.py:240,286`, `auto_grab_service.py`), not a broad rewrite.

## 6.4 Existing diagnostics refactor

Reliable enough to be classification signals directly (already discrete, already pattern-matched):
- `:1181-1189` browser network-error detection (`err_`, `dns_probe`, `connection was reset`) — **reliable**, this is Chrome's own error page, unambiguous.
- `:1227-1232` CAPTCHA/Turnstile iframe detection (`turnstile`, `challenges.cloudflare`, `recaptcha`, `hcaptcha`) — **reliable**, element-presence check.
- `:1234-1240` page-marker text matching (`"just a moment"`, `"cf-chl"`, `"checking your browser"`) — **reliable but should be a signal contributing to a code, not the code itself** — see confidence note below.

Logging aids only, should NOT determine state on their own:
- `:1190-1198` the "zero anchors + >40KB HTML" heuristic for an *unsolved* Cloudflare challenge — this is a shape heuristic, not a positive identification. A genuinely empty/broken page (e.g. a 500 rendered by the server) could match it. Use it to raise confidence when combined with a marker from `:1234-1240`, not alone.
- `:1208-1226` enumeration of available page controls — useful for diagnosing layout change, but it's descriptive, not classificatory. Feed it into `signals`, don't branch on it directly.

Missing signatures worth adding: a distinct marker set for "**post exists, requested host's link isn't present**" vs "**no reveal button at all**" — right now both collapse to the same "no links" outcome (`:1371-1372` no button, `:1406` 0 links parsed). Distinguishing them answers your own 6.4 last bullet: "host absent" means the reveal button *worked* and returned a link list that doesn't include the requested host (a data fact about the release, not a source-health fact); "layout changed" means the reveal button/expected DOM structure itself couldn't be found (a page-structure fact, and the one that *should* set `DEGRADED`/`LAYOUT_CHANGED`). The current code already has both code paths (`:1346-1349` no button vs the post-click parse) — they just aren't labeled differently at the point they're detected.

**Chrome `ERR_*` detection currently occurs at** `_browser_error_code()` (`:1097-1127`), called from `_navigate` (`:1141` region) — separate from `_log_page_diagnostics`, which runs on a page Selenium believes it successfully loaded. These are two different failure classes at two different points in the pipeline (nav-level vs content-level) and your structured result type needs both as inputs — a `_navigate` failure should short-circuit before `_log_page_diagnostics` ever runs (no page to diagnose).

**Browser launch exceptions** (from `get_driver()`, `:967-1042`) are a *third* point, upstream of both. They should map to their own `code` (`browser_launch_failed`, matching your list) and never reach the diagnostics function at all — that's the class that was misreported as "Cloudflare/captcha" in the Xvfb/lock-file incident, and it needs to be caught at the `get_driver()` call site in `scrape_links`, before any navigation is attempted.

## 6.5 Lazy hydration integration

1. **Listing URLs and visible titles become available** at `_select_posts` (`scanner_service.py:721-740`) — HDEncode's selector is `div.data h5 a` with fallbacks; the anchor text is the release title, already extracted for href resolution.
2. **Every non-cached URL is submitted for detail scraping** at `_process_posts` (`:744-791`), specifically the `ThreadPoolExecutor` submission at `:766-767`.
3. **Plex matching occurs** at `_match_against_plex` (`:1068`), called from `run_scan` at `:479` — well after step 2.
4. **History/quality-upgrade checks** happen inside `_create_media_item` (`:841`) and downstream consumers (`AutoGrabService.evaluate_item`, `auto_grab_service.py:80`) — also after step 2.

**Narrowest restructuring:** insert a relevance gate between steps 1 and 2, inside `_process_posts` itself, right before the `executor.submit` call at `:767`. Build a lightweight candidate from the title text already captured in `_select_posts` (title, inferred year/season/episode via the same regex `detail_scraper.py` uses for filenames — that parser is reusable on a title string, it doesn't require a fetched page), run it through `_match_against_plex`'s *matching* logic (not full item creation) to check "already in Plex," and only submit to the executor if it survives. Skip submission entirely for cached-and-current items.

**Fields required by current matching that are unavailable at listing time:** exact file size, video codec/HDR/DV layer, and the IMDb id (currently parsed from the detail page body, not the listing). Handle IMDb id by falling back to normalized-title+year matching (the same fallback `find_library_duplicate` already uses in `conflicts.py:352-359` for the unrelated conflict-detection feature — same pattern, different call site) — accept a slightly less certain "already have this" decision at the listing stage, and let a false negative (wrongly deciding it's new) simply fall through to a full detail fetch, which is safe, just not optimal. A false positive (wrongly skipping a genuinely new release) is the risk to test for — bias the listing-stage matcher toward "when uncertain, fetch the detail page" rather than toward "when uncertain, skip."

## 6.6 Source registry integration

**Recommendation: option 3 — a smaller shared policy object, not a migration to `SourceRegistry`.** Migrating `scanner_service.py` to call `SourceRegistry` would inherit its per-request-instance bug (6.1/6.2 above) and its `discover_sources()` plugin-loading overhead on every scan, for no benefit — `scanner_service.py`'s three sources are already hardcoded by name, and `SourceRegistry` was built for the plugin/pipeline-search use case, not the scan loop.

Concretely: read `hdencode_enabled` and a small number of policy fields (`max_concurrency`, `min_request_interval`) directly from `self.config` inside `_build_sources`/`detail_scraper.py`, the same way every other config-driven behavior in `scanner_service.py` already works. `SourceConfig`'s `rate_limit=2.0` for HDEncode (`hdencode.py:51`) can be the *documented default* those new config keys inherit, without requiring `scanner_service.py` to import `sources.base` at all.

**Bugs that must be fixed first if you ever DO want registry integration later:** the per-request `SourceRegistry()` constructions in `pipeline.py:108` and `sources.py:16` (the latter without even calling `discover_sources()`, causing the `GET /sources` bug), and the total absence of `sync_from_config()` calls anywhere in the FastAPI backend. None of these block Phase 0 if you take the option-3 path.

## 6.7 `AsyncRequestManager`

- **Extract the exception/result model?** No — per 6.3, build the new result type fresh, shaped for this use case (transport-agnostic, includes Selenium). `AsyncRequestManager`'s types (`NetworkError`, `RequestTimeoutError`, `RateLimitError`, per its docstring) are HTTP-specific and aiohttp-coupled.
- **Should its retry rules become shared policy?** Yes, this part is worth taking — its 429 exponential backoff and immediate-fail-on-other-4xx logic (`network.py:170-182`) is exactly right and matches what §5.8 asked for. Port the *values/logic*, not the class.
- **Migrate current synchronous call sites to it now?** No. It's `async`/`aiohttp`; `scanner_service.py`'s listing/detail fetches use `cloudscraper` (sync, needs `run_in_executor` specifically because `cloudscraper` has no async story) precisely because `cloudscraper`'s Cloudflare-solving logic isn't reproducible in `aiohttp`. Migrating would mean losing Cloudflare handling for #1/#2/#3 in 6.1 — a bigger and riskier change than Phase 0 should take on.
- **Does 429 handling need to change before reuse?** No — its logic is correct as-is.
- **Simpler way to avoid two retry systems?** Don't reuse the class; extract its backoff *policy* (retry count, 429 exponential curve, immediate-fail set) into a tiny standalone function shared by `detail_scraper.py`'s retry loop and (later) any Selenium retry logic. That gives you one retry policy, two transports, zero dead code. `AsyncRequestManager` itself can stay as-is or be deleted later — its 637-line test file (`test_network.py`) currently exercises code with zero production callers, which is worth knowing before anyone cites "we have 403/429 test coverage" (see 6.12).

## 6.8 Database and migrations

**Recommendation: a mixed approach — in-memory for hot counters, one new small table for what must survive restart.**

- **New table**, call it `source_health` (one row per source, upsert-only): `source TEXT PRIMARY KEY, state TEXT, reason_code TEXT, updated_at TIMESTAMP, last_success_at TIMESTAMP, last_failure_at TIMESTAMP, consecutive_failures INTEGER, cooldown_until TIMESTAMP`. Cost per `database.py`'s pattern (confirmed in the earlier review): one `CREATE TABLE IF NOT EXISTS` in `init_db` (`:225`) plus optional indexes — no `SCHEMA_VERSION` bump needed.
- **Not a new table:** rolling request counters (last-hour/last-24h). Keep those in memory (a small ring buffer or `collections.deque` with timestamps), same lifetime as the process. They're advisory for pacing decisions, not audit trail — restart naturally resets them to "we don't know recent load, so start cautious," which is the safe default anyway.
- **Notification dedup:** in-memory, keyed on `(source, state)` transition, same lifetime as the existing `NotificationManager._history` (`notifications.py:518`, already in-memory, capped at 100). Don't persist this — a restart legitimately re-arming "notify on next transition" is fine and arguably correct (you'd want a fresh notification after a restart if the source is still down).
- **Don't build a `request_events` table for Phase 1** — that's Phase 3's telemetry ask, and building it now is exactly the "overbuilding telemetry" you flagged wanting to avoid. Phase 1 needs *current state*, not *event history*.
- **Not key/value settings** — `source_health` has real per-source structure and will grow columns; forcing it into the generic settings table would mean string-encoding structured data, which `database.py`'s existing schema pattern doesn't do anywhere else.

## 6.9 Configuration model

Required edit locations for `hdencode_enabled`, confirmed against the pattern used by `ddlbase_enabled` (which already exists, so this is a proven checklist, not a guess):

1. `backend/config.py` — `AppConfig` TypedDict field (class starts ~`:18`, `ddlbase_enabled` field for reference at `:94`).
2. `backend/config.py` — default value in `_DEFAULT_CONFIG` (`ddlbase_enabled` default at `:443`).
3. `backend/api/routes/settings.py:110`-region — field on the `SettingsUpdate` Pydantic model. **This is the model using `model_config = ConfigDict(extra="forbid")`** (`settings.py:61`) — confirmed, an unlisted key gets rejected with HTTP 422, not silently ignored.
4. `frontend/src/lib/api/types.ts` — field on the `Settings` interface (`ddlbase_enabled` reference at `:291`-region).
5. `frontend/src/routes/settings/+page.svelte` — UI control, only if user-facing in Phase 0 (you said yes, so include it — pattern at line region for `discord_webhook`/similar toggles).

**Persisted config is loaded** via `backend/config.py`'s `load_config`/config-file read path (same module as items 1-2) — no separate loader to update. **The source API currently does NOT validate source IDs against a real list** — `PUT /sources/{source_id}` (`sources.py:20-31`) accepts any string and writes `{source_id}_enabled` into config unconditionally; there's no 404 for an unknown source. **Disabling a source does NOT currently affect already-running operations** — and per the design in 6.2, it shouldn't need to for Phase 0, because the enabled-check is a gate at scan/grab *start*, not a running-operation kill switch; an in-flight scan started before the toggle will finish its current batch. If you want mid-scan effect, that's what `stop_scan_flag` is for (separately) — don't conflate "disable this source" with "stop the current scan."

## 6.10 Background scanner behavior

- **How can a currently running background scan be cancelled?** `scanner.stop_scan_flag = True` on the shared `ScannerService` instance — see the note at the top of this document. `BackgroundScanner.stop()` currently does NOT do this (confirmed gap, `background_scanner.py:60-66`) and must be updated to also set it if you want `stop()` to actually interrupt an in-flight `scan_once()`.
- **Does the executor submit all detail jobs before any result is received?** Yes, confirmed: `futures = [executor.submit(process_post, post) for post in all_posts]` (`scanner_service.py:766-767`) is a single list comprehension executed before the `as_completed` consumption loop begins. All jobs are queued to the pool immediately; `max_workers` bounds how many *run* concurrently, but does not delay *submission*.
- **Does reducing worker count alone change submission behavior?** No — `max_workers` only bounds concurrent execution within the pool; every job is still submitted (and thus queued) instantly regardless of the worker count. Reducing 10→3 reduces concurrent HTTP requests in flight, which is the traffic win you want, but does not itself add pacing between request *starts* — a spacing mechanism (6.2) is still needed for that.
- **Cleanest way to prevent queued jobs from starting after block detection:** the `as_completed` loop already does exactly this pattern for the flag-based stop (`:768-774`, cancelling not-yet-started futures via `f.cancel()`). Reuse that same mechanism — set `stop_scan_flag` from the block-detection point in `_crawl_pages` (`:647-654`) rather than inventing a second cancellation path.
- **Can the background scanner safely report partial results?** Yes with no new work needed — `source_results` (`background_scanner.py:206`) already accumulates `{source, new, error}` per source as each finishes; a scan halted mid-way via `stop_scan_flag` will simply have processed fewer posts, and the existing "new item count" reporting degrades gracefully rather than needing special-casing.

## 6.11 Selenium lifecycle

- **Every path that creates/reuses/quits/force-kills Chromium:**
  - Create/reuse: `get_driver()` (`download_service.py:967-1042`) — the sole factory.
  - Quit (targeted): `_recycle_driver()` (`:1081-1095`), called only from `_navigate()` (`:1155`) on nav failure or a Chrome `ERR_*` page.
  - Quit (drain-then-quit): `cleanup_driver()` (`:1057-1079`) — **has no production callers**, referenced only by `tests/test_download_service.py:710,720,726`. Nothing quits the driver at app shutdown today.
  - Force-kill: `_kill_stale_chrome()` (`:1044-1055`), called from the launch retry loop (`:1030`).
- **Is all access protected by the same lock?** Yes — `self._driver_lock` (RLock, `:167`), acquired at the top of `scrape_links` (`:1296`) before any of the above are reachable in the live grab path. `get_driver()` itself doesn't independently lock, so it relies on being called only from within an already-locked context — worth a comment/assert if you touch this code, since a future caller outside `scrape_links` could race.
- **How could `_kill_stale_chrome()` kill unrelated processes?** Confirmed exactly as suspected: `pkill -9 -f <pattern>` for `chromedriver`/`chrome`/`chromium`, where `-f` matches the **entire command line** with **no scoping** — no `--user-data-dir=` substring check, no parent-PID restriction, no user restriction. In this container it's the only thing running Chrome, so it's low-risk *today*, but it is unscoped by construction and would need a `--user-data-dir=<this app's path>` substring match (once 6.5/Phase 5 gives you a stable, known path to match against) to be made safe before any multi-tenant or multi-process scenario.
- **Graceful shutdown opportunities before `pkill -9`:** none exist currently — there's no `driver.quit()` attempt before the kill. `_kill_stale_chrome` is reached only in the launch-retry path (a *new* driver failed to start), so the "stale" processes it's killing are, by definition, ones this app's own `cached_driver` handle doesn't own — there's no live Python-side reference to `.quit()` on them gracefully. A safer sequence would be: track the last-known PID this app itself launched, `SIGTERM` that specific PID first with a short wait, and reserve the broad `pkill -9 -f` as a last resort for orphans with no tracked PID.
- **Where would a future explicit `user_data_dir` be safest to configure?** In the options dict at `download_service.py:1009-1016`, alongside the existing flags — e.g. `os.path.join(os.environ.get("HOME", "/data"), ".config", "scanhound-chrome-profile")`, i.e. a *new*, dedicated subpath under the already-persistent `/data` mount, not the `/data/.config/chromium` path currently touched by lock-clearing in `entrypoint.sh` (keep those separate so a future profile-reset can't accidentally interact with whatever that existing directory actually is — see the caveat noted in the earlier review about its contents being ambiguous).
- **Which tests currently cover driver reuse and cleanup?** `tests/test_download_service.py::TestGetDriver` (`:543-635`) and `::TestCleanupDriver` (`:704-730`) — both against real `get_driver()`/`cleanup_driver()` logic with `_uc.Chrome` mocked. **`_kill_stale_chrome` itself is stubbed out (`MagicMock()`) in both tests that touch it** (`:564, 578`) — its actual `pkill` body has zero test coverage.

## 6.12 Testing infrastructure

| Behavior | Existing coverage | Real code or false confidence? |
|---|---|---|
| (a) Source enable/disable | `test_source_registry.py:495-552` (registry-level, real code, isolated); `test_api_routes.py:250-278` (HTTP-level) | **Partial false confidence** — `test_list_sources` has a conditional assertion that never fires (masks the `GET /sources` bug above); toggle tests only check response shape, never that a toggle changes live scan behavior |
| (b) `scan_threads`/concurrency limits | None found | **Gap** — no test bounds `_process_posts`'s executor sizing or its submit-all-upfront behavior |
| (c) Rate-limiting/pacing | None for the live scan path; only for the disconnected `SourceBase._fetch_html` (used solely by pipeline search) | **Gap on the path that matters** — the paths that actually run scans have no pacing tests because they have no pacing to test |
| (d) 403/429 | `test_network.py` (extensive, `:288,306,319,491,507,599`) | **False confidence** — exercises `AsyncRequestManager`, which is dead code with zero production callers. Scan-path 403/429 handling (`scanner_service.py:647-654`) has no dedicated test |
| (e) Driver reuse/cleanup/`_kill_stale_chrome` | `test_download_service.py:543-730` | Real code for reuse/cleanup; **`_kill_stale_chrome`'s actual body is fully stubbed** in the two tests that touch it — its unscoped `pkill` pattern is untested |
| (f) Notification dedup/batching | `test_notifications.py:1011-1029` | Only covers shutdown/cancel of the batch timer, not grouping *correctness* — flag for follow-up if you need that verified |
| (g) Background scan cancellation | `test_background_scanner.py` (uses a `_FakeScanner` with no thread pool — structurally can't test cancellation); `test_scanner_service_extended.py:152-154` (trivial initial-state check); `test_api_routes.py:342-401` (asserts `/scan/stop` returns 200, never that a scan actually halts) | **Confirmed gap** — no test anywhere starts real work and verifies stopping it actually interrupts in-flight processing |

**Biggest single risk for false confidence:** citing `test_network.py`'s 637 lines of 403/429 coverage as evidence the app handles rate limiting well. It tests a class nothing in production calls.

## 6.13 Backward compatibility

- **Existing database files:** none, per 6.8 — new table only, `CREATE TABLE IF NOT EXISTS` is safe against any existing DB.
- **Existing saved configuration:** `hdencode_enabled` needs a default of `True` in `_DEFAULT_CONFIG` (matching `ddlbase_enabled`'s pattern) so existing installs upgrade with HDEncode still enabled, not silently disabled.
- **Desktop vs Docker:** `_kill_stale_chrome` already no-ops on Windows (`sys.platform.startswith("win")` check, `:1046-1047`) — any change there needs to preserve that guard. The Docker-only Xvfb/entrypoint lock-clearing is unaffected by Phase 0 as scoped.
- **Windows vs Linux browser handling:** same file, same guard — no new divergence introduced by this phase's scope.
- **Existing API response shapes:** `scrape_links`'s return-type change (6.3) is internal to `download_service.py` unless you also change what `downloads.py:240,286` return to the frontend. Recommend: keep the HTTP response shape stable for Phase 0 (still `{"links": [...]}` etc.), and add the new reason/message fields as *additive* optional fields rather than replacing existing ones — avoids a frontend contract break in the same PR that's supposed to be small.
- **Existing frontend assumptions:** the current ambiguous error string is user-facing text, not a typed field the frontend branches on (confirmed: `download_item`'s message is a plain string written to the DB `warning`/error column) — so improving its accuracy is a pure win with no compatibility risk, as long as the field type doesn't change shape.
- **Existing tests depending on bare lists or `None`:** yes — this is real and needs attention. `tests/test_download_service.py`'s scrape tests almost certainly assert `scrape_links(...) == []` or similar bare-list returns today; changing the return type to `tuple[list, Optional[Diagnostic]]` (6.3) will break every one of them mechanically (not a logic break, just a shape break) — plan to update those call sites as part of PR 1, not as a follow-up, since "gate is unpaved" is exactly the kind of debt that doesn't get revisited.

## 6.14 Pull request boundaries

| PR | Purpose | Main files | Depends on | Migration | Tests | Rollback |
|---|---|---|---|---|---|---|
| **1a** | `hdencode_enabled` off switch + fix `GET /sources` bug | `config.py`, `settings.py`, `sources.py`, `scanner_service.py`, `download_service.py`, `types.ts`, settings page | none | none | new: off-switch integration test asserting zero requests when disabled; fix the vacuous `test_list_sources` assertion | revert commit; config key ignored harmlessly if rolled back |
| **1b** | Detail concurrency cap + spacing | `detail_scraper.py` (semaphore + timestamp gate) | 1a (for the disable check to short-circuit before the semaphore) | none | new: pacing test with a fake clock asserting min interval between calls | revert; no state to unwind |
| **1c** | Structured scrape diagnostics + propagation | `download_service.py` (`_log_page_diagnostics` return type, 4 call sites, `scrape_links` signature), `downloads.py` (response fields, additive only) | none, but land after 1a/1b to avoid conflicting edits to the same file region | none | fixture-driven: saved HTML for interstitial/Turnstile/`ERR_` page/valid-no-host, assert distinct codes; update existing bare-list assertions per 6.13 | revert; response fields were additive so no frontend break to unwind |
| **1d** | Shared stop-on-block wiring | `scanner_service.py` (`_crawl_pages` block-abort sets `stop_scan_flag`), `background_scanner.py` (`stop()` also sets it) | 1c (needs a real reason code to decide "this counts as a confirmed block") | none | new: simulate 3 consecutive 403s, assert `stop_scan_flag` becomes `True` and queued futures get cancelled | revert; flag defaults `False`, no behavior change if reverted |

I split your single "PR 1" into four small, independently revertible pieces because 1a alone (off switch) is safe to ship and verify before 1c (the diagnostics rewrite, the riskiest piece due to the test-shape breakage in 6.13) lands — if 1c needs a redo, 1a/1b/1d aren't blocked waiting on it. If you'd rather keep them as one PR for review-overhead reasons, land in that internal order (1a → 1b → 1c → 1d) as commits within it.

---

Let me know if you want the specifics behind any single answer expanded, or if you can resend the tail of 6.15.
