# ScanHound feature-pack — corrected Stage A validation (round 2)

**Reviewer:** Claude (independent validation). **Date:** 2026-07-20.
**Corrected bundle** `scanhound-feature-pack-stage-a-corrected-handoff.zip`
master SHA-256 `109f2c7f335903a8fcd4c18cc58bcceb3237834471ddb49205b157c147f323b6` — **verified**;
`verify_handoff.py` exit 0 (14 packages + docs + scripts checksums OK).
**Baseline** `555e26bc`=origin/main; **PR D pinned** `2145ef6`. Env: container Py3.12,
host node v24. No merge/deploy/force-push/ready/production-change/real-sentinel. main untouched.

## The six original findings — all corrected at the apply/mechanics level

| ID | Verified fixed | Evidence |
|----|---|---|
| FP-14-1 | ✅ **committed** | teardown at `frontend/global-teardown.ts` (outside `tests/**`); `npm run check` **0 errors** (was 2) + build ok. Commit `932cefb`. Playwright e2e = env-gated. |
| FP-J-1 | ✅ | new `client.errors.test.ts`; `client.test.ts` blob `018b8644` **byte-unchanged**; check 0 / unit **5 passed** (both files) / build ok; backend boundary **4 passed**. |
| FP-C-1 | ✅ (apply) | `--preflight-only` writes **nothing**; all 30 ops apply; undetection strings **3→0** in source. |
| FP-B-1 | ✅ (apply) | maintenance anchor matches; clean apply + compile. |
| FP-15-1 | ✅ | `git diff --check` CLEAN; focused `test_rename_core+test_rename_service` **236 passed**. |
| FP-A-1 | ✅ (original repro) | `test_app_service_extended + test_runtime_lock` = **109 passed**. |

Preflight-before-write works: **zero partial writes** on the generic installers.

## Real-checkout full/focused suites expose THREE still-broken packages

ChatGPT's local validation ran only isolated payload suites; the real repository test
suites reveal behavioral/isolation failures in the three most complex packages.

**FP-A-1-residual — PR A (blocks).** The FP-A-1 fix added the fileops bypass to
`test_rename_core`/`test_rename_service` and a cleanup to `test_app_service_extended` ONLY.
Broad suite = **5 failed / 3551 passed**: 4× `test_apply_conflict_strategy.py` raise
`RuntimeWriterLockError: writer lock is not held` (guarded fileops mutations, no bypass), plus
`test_runtime_lock::test_require_writer_lock_rejects_unowned_mutation` ("DID NOT RAISE"). In
round 1 these passed only because a *leaked* lock masked them; the new cleanup unmasked them.
Deterministic isolation repro: `pytest tests/test_apply_conflict_strategy.py` → **4 failed, 4 passed**.
**Fix:** a ROOT `tests/conftest.py` autouse fixture that wraps every test in
`_unlocked_fileops_for_tests()` AND clears `_ACTIVE_LOCKS` + resets `_TEST_BYPASS_DEPTH` in
teardown (global, not per-module). (My round-1 report recommended a conftest-scoped teardown;
the correction scoped it to one module.)

**FP-C-2 — PR C (blocks).** Applies clean (FP-C-1 fixed) but the coordinator refactor regresses
existing tests. PR C staged, 3× identical:
`pytest test_detail_scraper_pacing.py test_scan_block_cancellation.py` → **13 failed, 2 passed**;
baseline `8a48382` (PR C stashed) → **15 passed**. First failure (fresh process):
`test_three_consecutive_blocks_set_existing_stop_event` → `assert scraper.calls == 3` got **0** —
the refactored `scanner_service._crawl_pages` routes through the process-wide coordinator and no
longer issues the request the existing PR#5/#6 pacing + block-cancellation tests expect. PR C's own
README requires these 7 files to pass. **Fix:** make the coordinator path actually issue requests
under the test harness and update the affected pacing/cancellation tests for the new coordinator.

**FP-B-2 — PR B (blocks).** Applies clean (FP-B-1 fixed) but its SH-R04 transaction fails.
`pytest tests/test_trash_lifecycle_transaction.py` (freshly staged, ×2) → **2 failed, 6 passed**
(test_ambiguous_restore_state_remains_visible, test_sweep_uses_transactional_delete). It also
regresses existing trash tests: baseline `44ea7ba` `TestTrashListAndRestore` = **17 passed**, with
PR B = **2 failed, 15 passed** (test_restore_moves_file_back_and_removes_manifest_record,
test_empty_trash_removes_everything_regardless_of_age). **Fix:** the transactional restore/sweep
+ ambiguous-state handling must satisfy both its own new tests and the existing trash lifecycle;
run PR B's full README test list against a real checkout.

## Green this round

- **Committed:** PR #14 (`932cefb`), PR D (`2145ef6`, prior round). PR #17 re-validated green.
- **Validated green, commit deferred to the corrected round:** PR #15-fs (236 passed),
  PR J (frontend + backend green), PR E (focused 11 passed incl. step-7 crash rollback; full
  backend + prod-schema migration pending).

## Not reached / gated

PR F/G/H/I (sequential RSS stack gated behind the PR C runtime-base). Stage B integration
(merges PR A/B/C, which cannot be committed). Environment-gated: Playwright e2e, UID-1000 as a
distinct run, Python 3.11, real CIFS/NTFS sentinel (Jesse's authorization), 7-day RSS shadow.

## Verdict

Apply/packaging mechanics are fully corrected, and 6 of 9 exercised packages are green. But the
three most complex packages (PR A lock guards, PR C coordinator, PR B SH-R04 transaction) still
fail real-checkout suites, so Stage A is not fully green and Stage B (which merges them) is not
reachable.

**FEATURE PACK ACCEPTED WITH REQUIRED FIXES**

(Required: corrected PR A/PR B/PR C packages fixing FP-A-1-residual / FP-B-2 / FP-C-2, each with
its full README test list run against a real checkout; then re-validation + Stage B + the
environment-gated sentinel/shadow/Playwright evidence.)
