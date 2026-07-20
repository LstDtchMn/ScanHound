# ScanHound feature-pack — Stage A round-3 corrective validation

**Reviewer:** Claude. **Date:** 2026-07-20.
**Bundle** `scanhound-stage-a-round3-corrective-handoff.zip` master SHA-256
`044d08a366935f71b913b4e2a62fd7db43a7a9fcf68d9134028ab412ef95db58` — verified;
`verify_round3.py` OK; all nested checksums OK. Replaces PR A/B/C only (round-2 PR #14,
#15-fs, #17, D, E, J stand). Env: container Py3.12, host node v24. No merge/deploy/
force-push/ready/production/sentinel. main untouched.

## Result: 2 of 3 corrected and committed; PR C still fails

### PR A — FP-A-1-residual FIXED ✅ committed `4bfe5ca`
The bypass + cleanup now live in root `tests/conftest.py` (autouse: releases active locks,
clears `_ACTIVE_LOCKS`, resets `_TEST_BYPASS_DEPTH`, wraps each test in
`_unlocked_fileops_for_tests()`; the enforcement test restores production semantics to prove an
unowned mutation raises).
- `pytest tests/test_apply_conflict_strategy.py` = **8 passed** (was 4 failed)
- conflict + extended + runtime_lock = **117 passed** (rejection self-test still raises — not masked)
- broad backend (all minus test_api_routes) = **3556 passed, 4 skipped, 0 RuntimeWriterLockError**

### PR B — FP-B-2 FIXED ✅ committed `4d678bd`
- `pytest tests/test_trash_lifecycle_transaction.py` = **8 passed** (was 2 failed;
  ambiguous-restore repair marker + sweep-count + empty-manifest all correct)
- `test_rename_core.py::TestTrashListAndRestore` = **17 passed** (was 2 failed; == baseline)
- focused (lifecycle + rename_core + rename_service + app_service) = **369 passed**

### PR C — FP-C-2 PARTIALLY fixed, STILL BLOCKED ⛔
Applies clean (32 ops preflighted, undetection strings 3→0). `test_scan_block_cancellation.py`
now **6 passed** (fixed by the explicit fresh-coordinator install). But the focused 7-file set is
**10 failed, 40 passed** (×2 deterministic):

- **9× `test_detail_scraper_pacing.py`** (fail even in isolation — 9 failed alone).
  Representative: `test_non_hdencode_sources_bypass_hdencode_coordinator` →
  `assert detail.scrape_details("https://ddlbase.com/post/example", {}, session)` is **None**.
  The refactored `DetailScraper.scrape_details(url, headers, scraper=None, *, stop_requested=None)`
  returns None for DDLBase/Adit-HD under the test harness, so none of the pacing/spacing/
  cancellation assertions in this file are reached. (Baseline 8a48382: this file passed.)
- **1× `test_hdencode_constructor_gate.py::test_raw_http_and_webdriver_constructors_are_confined`**:
  PR C's own single-policy-boundary invariant reports violations —
  `backend/rt_scraper.py: cloudscraper.create_scraper(` **+ 3 more** modules construct raw
  scrapers OUTSIDE the coordinator. PR C did not route these through the coordinator (its changed
  files were scanner_service/detail_scraper/download_service/sources.hdencode/api.routes.sources),
  so the coordinator is not the sole traffic boundary the test asserts.

**Fixes required (PR C only):**
1. Make `scrape_details` return a truthy `ScrapeResult` for DDLBase/Adit-HD under the coordinator-
   bypass path (or repair the modified pacing test harness so it exercises the request path); PR C's
   own README requires all 7 focused files green.
2. Either route `rt_scraper.py` (+ the 3 other flagged modules — the test prints the full list) through
   the coordinator, or scope the constructor-gate invariant to the modules genuinely in HDEncode
   traffic scope. As written the test fails, so the single-policy-boundary claim is not yet true.

## Stage status

Green & committed this feature pack: PR #14 (`932cefb`), PR #17, PR D (`2145ef6`), PR A
(`4bfe5ca`), PR B (`4d678bd`); plus PR #15-fs, PR J, PR E validated green (commit deferred with
the file-safety/RSS assembly). **PR C is the sole remaining Stage-A blocker.** It gates the RSS
runtime-base (PR C + E → F/G/H/I) and Stage B integration, so neither is reachable this round.
Environment-gated legs unchanged (Playwright, UID-1000 distinct run, Py3.11, real sentinel,
7-day shadow).

## Verdict

**FEATURE PACK ACCEPTED WITH REQUIRED FIXES** — one corrected PR C (detail-scraper coordinator
path + constructor-confinement), validated against its full README test list on a real checkout;
then RSS F/G/H/I, Stage B integration + full matrix, the final adversarial review, and the
environment-gated sentinel/shadow/Playwright evidence.
