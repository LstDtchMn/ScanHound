# HDE-4 evidence — 2026-09-04

Worktree `C:\Users\NLSur\AppData\Local\Temp\hde4`, branch `feat/hde4-reveal-accounting`, stacked on #109 @ `3abb575`. Head: 3e8fa51.

## 1. Read-only investigation (Sonnet lane; every claim with file:line)

- The boundary: `backend/download_service.py:3310 scrape_links_recorded(url, service_type, progress_callback)` → `scrape_links()` returns `ScrapedLinks` (`backend/scrape_outcome.py:284-295`, list subclass with `.diagnostic`); under `owns_source_health(url, "hdencode")` (`:422-430`) it calls `record_scrape_outcome(self.db, "hdencode", links)` (`backend/source_health.py:58-92`) and, on links, `release_verification_hold_for_source`.
- `record_scrape_outcome`: links → `record_source_success`; coordinator-owned diagnostics (`INTERACTIVE_CHALLENGE`, `REVEAL_VERIFICATION_STALLED`) → nothing (the coordinator persisted already); `REQUESTED_HOST_MISSING` / `NO_FILE_HOST_LINKS` → **`record_source_success`** (the quota-wall shape counts as a health success today; side finding, not changed); others → `record_source_failure` (DEGRADED / BLOCKED).
- `source_health` (`backend/database.py:569-579`) is one overwritten row per source: no history, no caller, no per-event time. `download_package_links` (`:977-986`) is written only on delivery to JDownloader (`record_submitted_links`, `:4150-4177`): it is what the ~20/day figure came from, and it cannot see failed, challenged or stripped reveals, nor who asked. The coordinator's `_metrics` are in-memory only. No reveal ledger existed; the closest house pattern is `download_queue_attempts` (`:1410-1451`, append-only, queue-scoped).
- Reveal attempt boundaries in `scrape_links` (`:2940`): click at `:3219`; ends at the post-click Cloudflare diagnostic (`:3241-3246`), the host-keyword wait timeout (`:3249-3258`, `requested_host` stage) or links found (`:3260-3289`). Classification in `_log_page_diagnostics` (`:2279`, branch `:2440-2665`): challenge (`:2510-2520`, calls `observe_challenge`), stalled (`:2551-2571`), control absent/layout (`:2637-2651`), stripped (`:2652-2653`).
- Callers of the boundary: `backend/api/routes/downloads.py:426` (scrape), `:484` (copy-links), `backend/download_service.py:3959` inside `download_item()` (callers: `routes/downloads.py:113`, `auto_grab_service.py:172`, `download_queue.py:982` with `attempt_id` in scope, `ui/controllers/download_controller.py:33`), `hdencode_action_service.py:209` (with `action_uuid` in scope), `ui/controllers/download_controller.py:75`. Caller identity did not reach the boundary.
- Schema pattern: `CREATE TABLE IF NOT EXISTS` in the init block, unconditional; `SCHEMA_VERSION = 9` (`:268`) used only for data migrations. UTC convention: `datetime.now(timezone.utc).isoformat()`; SQLite `date('now')` is UTC. Read surface: `GET /sources` (`backend/api/routes/sources.py:16-38`) already attaches the coordinator snapshot to the hdencode row.

## 2. What was built (Sonnet lane from a written spec)

- `hdencode_reveal_observations` (id, source, outcome, caller default 'unknown', context_id, url_hash, diagnostic_code, recorded_at UTC) + index on (source, recorded_at); `SCHEMA_VERSION` unchanged, by the house pattern.
- `DatabaseManager.record_reveal_observation(...)` (sha256 of the url; fail-soft), `get_reveal_accounting(source, day)` (UTC day: total, by_outcome, by_caller, first/last, last 20 rows), `list_reveal_days(source, limit=14)`.
- `source_health.classify_reveal_outcome(links)`: success / challenge (`health_owner == "coordinator"` or the two codes) / stripped (`NO_FILE_HOST_LINKS`, `REQUESTED_HOST_MISSING`, `LAYOUT_CHANGED`, `REVEAL_CONTROL_ABSENT`) / error (else, incl. no diagnostic).
- The one write site in `scrape_links_recorded`, inside the ownership gate: normal path one row; `scrape_links` raising → one `error/exception` row then re-raise. Keyword-only `caller="unknown"`, `context_id=None`; `download_item` threads the same. Callers: `route_scrape`, `route_copy_links`, `route_download`, `rss_action`+action id, `auto_grab`, `queue_item`+attempt id, `qt_batch`, `qt_manual`.
- `GET /sources`: `reveal_accounting` and `reveal_days` on the hdencode row, each None on DB error.
- One test fake updated to accept keyword arguments (`tests/test_hdencode_actions.py`).

## 3. Tests (`tests/test_hde4_reveal_accounting.py`, 30)

Classification per code; one row per reveal for success / challenge / stripped / error-by-diagnostic / exception (row written, exception propagates); the caller literal at all nine sites (queue rows carry the attempt id); non-HDEncode urls write nothing; UTC day bucketing and trend order; raw url never stored (sha256 only); fail-soft; `/sources` surface (hdencode only; None keys on DB error, still 200); the load-bearing negative policy test: 25 successes recorded today, the boundary still scrapes and returns links, the coordinator is not blocked by the count, no hold armed.

Focused suites, 14 files (accounting, HDE-3 boundary, scrape outcomes, source health, round-8 discrimination, API routes, download service, queue+browser, queue attempts, HDEncode actions, verification hold, hold surface, queue followups, auto-grab): `778 passed`. Real root absent.

## 4. Mutants (supervisor's own run, whole-tree copy) — and a vacuous test caught

First run: A (write site removed) KILLED 13; B (classifier says success for all) KILLED 12; C (raw url stored) KILLED 1; **D (method's error handler made to re-raise) SURVIVED**; E (a limit inserted: raise at 20) KILLED 1.

Why D survived: the fail-soft test induced the failure as "no connection", which the lower-level `_mutate` already swallows, so the method's own handler was never reached and the test passed with the handler removed. The test now also patches `_mutate` to raise an unexpected exception; with that, D is killed.

Second run: A 13, B 12, C 1, D 1, E 1 failed respectively, each exactly the tests that claim it; control 30 passed.

## 5. Adversarial read (Opus), before commit — eight findings, all actioned

1. **Classification lie (headline).** `health_owner == "coordinator"` is set not only for the two challenge codes (`download_service.py:2545`, `:2591`) but also for `SOURCE_DISABLED` (`download_service.py:2958-2971`) and for `SOURCE_DISABLED` / `SOURCE_TEMPORARILY_BLOCKED` (`download_outcome.py:439-471`), all with `transport_attempted=False`. So local refusals (HDEncode switched off; the coordinator's own denial) were bucketed as "challenge" and counted as reveals though no request was sent; a disabled day would have reported reveals that never happened. The first test file even enshrined it. Fix: a `refused` bucket by the diagnostic's `effective_transport_attempted` (`scrape_outcome.py:214-226`), tested through the boundary.
2. **Unguarded call.** The accounting call sat before `record_scrape_outcome` and the hold release with only a `db is not None` check; an unexpected exception inside it would have skipped both. Fix: guarded with a logged `try/except`; a test raises from `record_reveal_observation` and asserts health recording and the hold release still ran.
3. **The read API could not separate the quota wall from a layout change** beyond the last 20 rows (both "stripped"). Fix: `by_diagnostic_code` in the day and trend results.
4. `download_item`'s new parameters were not keyword-only. Fixed.
5. `REQUESTED_HOST_MISSING` (the page served links, just not the requested host) was counted as stripped. Now `served_other_host`.
6. The negative policy test asserted the absence of invented key names; now compares whole coordinator snapshots and arms a real hold.
7. A NULL day could crash the trend sort. Guarded.
8. Stated in the commit: `url_hash` is a pseudonym, not anonymisation; no retention (about 20 to 100 rows a day, 1 to 5 MB a year); `GET /sources` scans the table.

Confirmed fine: exactly one row per path (normal, empty with or without diagnostic, exception then re-raise, no row when not HDEncode); nothing outside the read API and the route reads the table; the coordinator, source-health recording and hold code are untouched; SQLite `date()` parses the ISO timestamps with `+00:00`; all nine callers pass the claimed literal; the route mirrors the existing health pattern; CRLF and Qt guards fine for ubuntu-latest.

## 6. Full suite and CI

CI on `3e8fa51` (ubuntu-latest): Tests workflow green on Python 3.11 and 3.12, frontend green. CI VERIFIED.

Full suite on a combined copy (this branch, which carries #109, plus #110's four test-isolation files copied verbatim, so the host is never written):

```
real root before: absent
5508 passed, 5 skipped, 13 warnings in 843.96s (0:14:03)   exit=0
guard firings: 0
real root after:  absent           2026-09-05T03:20:22Z
```

No socket abort this run (TST-3 stays at 2 of 7 full Windows runs).
