# HDE-6 evidence — 2026-09-04

Worktree `C:\Users\NLSur\AppData\Local\Temp\hde6`, branch `fix/hde6-coordinator-context-reset`, stacked on #111 @ `1db4ac4`. Head: 061a6a0.

## 1. Read-only investigation (Sonnet lane; every claim with file:line)

- Callers of `configure_hdencode_coordinator`: `backend/detail_scraper.py:95-98` (bridge config, `getattr(parent_app,"db",None)` → None in every production construction), `backend/scanner_service.py:214`, `backend/download_service.py:435`, `backend/hdencode_rss_service.py:42` (real config and db).
- Config identity is stable for the process: `backend/api/main.py:113-114` assigns `reg.config`/`reg.db` once at lifespan start; `AppService.save_config` (`backend/app_service.py:1145-1190`) sets keys in place; `PUT /settings` (`backend/api/routes/settings.py:311`) does `reg.config.update`. The settings-save open question is therefore resolved: not a trigger.
- The identity reset was in the coordinator's first commit `dc6397e` (2026-07-20), by its own comment against cross-test leakage. One test asserted it: `tests/test_hdencode_coordinator.py::test_new_application_context_clears_stale_local_cooldown`.
- Legitimate protection resets: only the 2xx branch of `observe_http_status()` (`hdencode_coordinator.py:439-449`). No operator reset endpoint exists.
- `_persist_failure` (`:422-437`) is a no-op with `_db` None; `_load_health` (`:209-227`) returns unknown. The local cooldown alone still gates, unless wiped by the same configure() call that detached the db.
- Bridges (`backend/api/dependencies.py:29-90`, `ui/controllers/scanner_controller.py:35-`) expose `config` but no `db`. `DetailScraper(` is constructed only via `WebScrapers(bridge)` at `backend/api/main.py:157` and `ui/controllers/scanner_controller.py:412`, each once per process, immediately before `ScannerService(...)` reconfigures with the real db.

## 2. The change

`configure()`: attaches config, clears the health cache, attaches a db only when given one, never touches protection state. Both bridges expose `db`. DetailScraper's call site unchanged.

## 3. Tests

`test_new_application_context_clears_stale_local_cooldown` replaced by `test_reconfiguring_with_a_new_context_keeps_an_active_cooldown`; added `test_configure_with_no_db_keeps_the_attached_db`, `test_a_real_success_still_clears_protection_state`; new file `tests/test_hde6_detail_scraper_keeps_cooldown.py` with the reviewer's regression and two bridge tests (desktop bridge skips without Qt).

Focused suites (14 files: coordinator, HDE-6, isolation, detail-scraper pacing, scan-block cancellation, scrape outcomes, queue followups, source health, RSS shadow/primary, API routes, download service, scanner service, HDEncode actions): `663 passed in 117s`. Real root absent.

## 4. Mutants (whole-tree copy) — and a masking finding

First run:

```
A identity reset restored          SURVIVED the DetailScraper regression (only the coordinator test caught it)
B unconditional db assignment      KILLED
C API bridge db removed            KILLED
```

Why A survived: with a real db attached, `observe_challenge()` also persists the cooldown to the database (`_persist_failure`), so after the identity reset wiped the in-memory fields, `snapshot()["blocked"]` was still True from the DB read. The regression asserted the symptom, not the mechanism, and the two halves of the bug mask each other: the DB detach (B) is what made the identity wipe (A) visible in production. The regression now also pins `_local_cooldown_until`, `_block_streak` and `_local_cooldown_reason` across the scraper construction.

Second run:

```
A identity reset restored          KILLED: DetailScraper regression + keeps-cooldown test fail
B unconditional db assignment      KILLED: keeps-db test + DetailScraper regression fail
C API bridge db removed            KILLED: API-bridge test fails
control                            17 passed
```

## 5. Adversarial read (Opus), before commit

Verdicts: never-downgrade db acceptable (no production or teardown path drops a db: `_clear_registry_lifespan_state` `backend/api/main.py:65-70` and `AppService.shutdown` `backend/app_service.py:1004-1044` never reconfigure the coordinator; cost named: `DatabaseManager.close()` `backend/database.py:121-131` only drops the connection and `get_connection()` reconnects silently). Removing the reset acceptable: the operator toggle `PUT /sources/{id}` (`backend/api/routes/sources.py:41-59`) mutates config in place and never called `configure()`; cooldowns are time-bounded at `hdencode_coordinator.py:239` (429: 15 min; 403/503: 30 min; challenge: 60 min; reveal-stall: config-clamped × step, default hours); the 2xx branch is now the sole clearing mechanism and `request()` raises while blocked, so recovery is expiry-then-probe; no operator override exists (`clear_source_health` has no route). Health-cache clear acceptable (all callers are `__init__` paths). Bridge `db` properties lazy; `AppService.db` always exists; `getattr(parent_app, "db", ...)` appears only at `backend/detail_scraper.py:97`. Regression file faithful: `DetailScraper.__init__` (`:86-98`) touches only `config` and `db` and reaches `configure_hdencode_coordinator` unconditionally.

Five edits required and made: the `decision.blocked is False` assertion checked a hardcoded literal (removed, docstring corrected); the keeps-db test now also proves the db is reached (`observe_http_status(200)` → `real_db.successes == 1`); the desktop-bridge test uses `pytest.importorskip("PySide6.QtCore")`; the production docstring no longer names a PR; it states the never-downgrade cost and that `config` is deliberately not protected the same way.

Focused suites after the edits: 663 passed. Mutants after the edits: A, B, C all KILLED by exactly the tests that claim them; control 17 passed.

## 6. Full suite and CI

Full suite on a combined copy (#110 @ 6ae62dc trash isolation + this branch, which carries #111; the host is never written):

```
real root before: absent
5469 passed, 5 skipped in 572.87s (0:09:32)   exit=0
guard firings: 0
real root after:  absent
```

No socket abort this run (TST-3 stays at 2 of 6 full Windows runs).

CI on `0382c30` (ubuntu-latest): Tests workflow green on Python 3.11 and 3.12, frontend green. CI VERIFIED.

## 7. HDE6-R1: the recovery predicate (peer review of 0382c30)

Confirmed at `hdencode_coordinator.py:454`: recovery cleared protection and recorded success for `200 <= status < 400`. Call sites read: `detail_scraper.py:155` (cloudscraper session, follows redirects, treats only 200 as a page); `scanner_service.py:910` (same client); `hdencode_rss_service.py:259-261` (its feed client follows up to 3 redirects itself and returns only 304 or a terminal status; 304 recorded as not-modified, other non-200 as http_error); `download_service.py:2244` (hardcoded 200). A bare non-304 3xx reaching the coordinator is therefore an unresolved redirect, not health.

Change: `_is_recovery(status)` = any 2xx, or 304; other 3xx are neutral (no protection change, no success, no failure; the decision returned is the current active one). Docstrings updated; no test relied on a 3xx clearing protection (grep: the only 302 in tests is the DV host-scan redirect-refusal detector; 304s are feed not-modified cases, still recovery).

Tests: 200 and 304 clear protection and record success; 301/302/307/308 leave the cooldown and streak unchanged, record nothing; predicate boundaries at 199/200/299/300/304/305/399/400.

Mutant (whole-tree copy, below-400 predicate restored): KILLED by exactly the four redirect cases and the 300/305/399 boundary cases; control 28 passed. Fourteen focused files: 677 passed. Real root absent.

CI on the second commit `061a6a0` (ubuntu-latest): Tests workflow green on Python 3.11 and 3.12, frontend green. CI VERIFIED.
