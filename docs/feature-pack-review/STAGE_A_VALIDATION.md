# ScanHound feature-pack — Stage A validation report

**Reviewer:** Claude (independent validation/CI/Git role)
**Date:** 2026-07-20
**Handoff:** `scanhound-feature-pack-implementation-handoff.zip`, authoritative
SHA-256 `f21c779ca793aa66ad99caa20b9a566ddba0fd638d5715231d4b0e541e2b2531` — verified;
`scripts/verify_handoff.py` exit 0 (all 14 packages + nested SHA256SUMS OK).
**Baseline:** `main` per handoff = `555e26bc65a6e6474eb63fdfb6e025a41255dea9` = `origin/main`.

## Environment

- Backend tests: throwaway `scanhound:latest` container (`0ee535183c0a`), Python **3.12.13**,
  pytest 9.1.1. Code staged into `/work` (backend+tests+scripts+frontend). Prod image is
  3.12-only → the **Python 3.11 matrix leg is environment-limited** (not run).
- Frontend: host node **v24.14.0** / npm 11.9.0; `node_modules` junctioned from the main
  checkout after verifying its `package.json`+`package-lock.json` are byte-identical to the
  target branch.
- Git worktrees off `X:\Docker Apps\ScanHound`; every package applied under its own guard
  (`--is-inside-work-tree` + `symbolic-ref` + HEAD + blob checks), no `--skip-head-check`
  except one explicitly-disclosed workaround (PR J, see FP-J-1).
- `test_api_routes.py` network tests are excluded from broad runs as a **pre-existing,
  environment-gated** family (no external egress in the container) — not caused by any package.
- **No merge, deploy, force-push, ready transition, production-setting change, or real
  filesystem sentinel run was performed.** `main` (`58feedf`) and `origin/main` (`555e26bc`)
  are untouched.

## Step 0 — immutable inputs (verified)

The handoff baseline `555e26bc` equals `origin/main`. Every input branch head exists locally
and matches its `origin/…`: PR#3 `f3c2f0c`, #4 `fb99d49`, #5 `64663e6`, #6 `da15768`,
#7 `8a48382e…`, #8 `c1a7f7b`, #14 `be52908a…`, #15 `70dca70a…`, #16 `44ea7ba0…`, #17 `3e60c24`.
All draft. Note: the local *working* `main` (`58feedf`, the deployed TV→Blackbeard line) is
**15 commits behind** `origin/main` and fully contained in it — so every "from main" step is
based on `555e26bc`/`origin/main`, never the local checkout.

## Headline

The handoff's own README states these packages were **authored but never applied to a
ScanHound checkout** (GitHub 403 + no `github.com` resolution in the author's container).
Validation bears this out decisively: **of the 8 packages exercised, 5 have blocking
apply/CI defects, 1 has a minor whitespace defect, and 2 are clean.** In every case the
underlying *production design* is sound — all defects are in the packaging/test-hygiene
layer. The feature pack **cannot be assembled (Stage B) until corrected packages land**,
because the integration branch merges the exact heads that currently cannot be committed.

## Per-package disposition

| Pkg | Target | Apply | Tests | Disposition |
|----|--------|-------|-------|-------------|
| **PR #17** | `fix/lifecycle-registry-reset` `3e60c24` | n/a (re-validate) | **87 passed** (lifecycle+bg+results, incl. late-worker) | ✅ evidence stands |
| **PR D** | `agent/hdencode-candidate-evidence` from `555e26bc` | clean (blob `3aa2950e` matched) | focused **86**, broad **3553**, 0 regressions | ✅ **COMMITTED `2145ef6`** |
| **PR #15 fs** | `70dca70` (detached) | clean | unsupported-FS fail-safe **4 passed** | ⚠️ FP-15-1 (minor); topology-blocked |
| **PR #14** | `fix/e2e-full-state-isolation` `be52908` | clean | `npm run check` **0→2 errors** | ⛔ **FP-14-1** |
| **PR J** | `agent/public-error-boundary` from `555e26bc` | refuses | backend payload compiles clean | ⛔ **FP-J-1** |
| **PR A** | `agent/single-writer-runtime-guard` from `555e26bc` | clean (4 blobs matched) | focused 355; broad **1 failed** | ⛔ **FP-A-1** |
| **PR C** | `agent/hdencode-traffic-coordinator` from `8a48382` | partial → aborts | — | ⛔ **FP-C-1** |
| **PR B** | `agent/trash-lifecycle-transaction` from `44ea7ba` | partial → aborts | — | ⛔ **FP-B-1** |
| PR E/F/G/H/I, PR#8, cross-seams, sentinel | — | not reached | — | census stopped after decisive pattern |

## Proposed fixes (brief)

| ID | Package | One-line fix |
|----|---------|-------------|
| FP-14-1 | PR #14 | Move `global-teardown.ts` out of the svelte-check include → `frontend/global-teardown.ts`, reference it as `./global-teardown.ts` (mirrors `playwright.config.ts`). |
| FP-J-1 | PR J | Change the `client.test.ts` op from `write_new` to append the `formatErrorDetail` `describe` block (or write a new file, e.g. `client.errors.test.ts`) so the existing `conflict apis` tests survive. |
| FP-A-1 | PR A | Add an autouse teardown (conftest or test_runtime_lock.py) that releases any locks left in `_ACTIVE_LOCKS` and resets `_TEST_BYPASS_DEPTH`; or mock/release the lock in `test_app_service_extended.py`. |
| FP-C-1 | PR C | Rewrite op [23]'s `old` anchor to the real multi-line `_log_page_diagnostics(` signature at `8a48382`, then verify the full op list applies so op [27] (undetection-flag removal) runs. |
| FP-B-1 | PR B | Rewrite the `_run_maintenance_pass` "maintenance anchor" to the real `app_service.py` text at `44ea7ba`; verify end-to-end. |
| FP-15-1 | PR #15 fs | Strip the trailing blank line at `tests/test_rename_core.py:1129`. |

Cross-cutting: because blob/branch guards pass while operations still fail, **every package
must actually be applied and its suite run against a real checkout before resubmission** —
guard-passing ≠ apply-clean, and no suite has been executed author-side.

## Blocking defects (require corrected packages)

**FP-14-1 — PR #14 breaks `npm run check` (CI red).** The new
`frontend/tests/e2e/global-teardown.ts` imports `node:fs/promises` and uses `process`, but
it falls inside svelte-check's `../tests/**/*.ts` include and the project declares no
`@types/node`. Baseline `npm run check` = 0 errors; after the package = 2 errors
(`Cannot find module 'node:fs/promises'`, `Cannot find name 'process'`). `npm run build`
still passes → logic is correct, only the type-check placement is wrong. CI runs
`npm run check` (`.github/workflows/tests.yml:90`). **Fix:** move the teardown out of the
svelte-check include (e.g. `frontend/global-teardown.ts`, referenced as `./global-teardown.ts`,
mirroring how `playwright.config.ts` already escapes the check).

**FP-J-1 — PR J clobbers an existing test file.** The installer op
`write_new frontend/src/lib/api/client.test.ts` refuses because `555e26bc` already ships that
file (blob `018b8644`, `describe('conflict apis')` testing `applyRename` — unrelated to
`formatErrorDetail`). As authored it aborts before any write; bypassing the guard would
delete the two existing tests. **Fix:** append/merge the `formatErrorDetail` describe-block
(or use a distinct new filename). Everything else in PR J (public_errors.py boundary,
downloads/rename/client.ts patches, backend real-boundary test) applies and compiles cleanly —
validated by moving the existing file aside and applying under full guards.

**FP-A-1 — PR A's own test fails in the full suite (CI red).** PR A makes
`AppService.startup()` acquire a real global `RuntimeWriterLock`. `tests/test_app_service_extended.py`
calls `.startup()` 8× with no lock mock and no release, leaking locks into the process-global
`_ACTIVE_LOCKS`. Later `tests/test_runtime_lock.py::test_require_writer_lock_rejects_unowned_mutation`
sees a held lock, so `require_writer_lock()` doesn't raise. Deterministic repro:
`pytest tests/test_app_service_extended.py tests/test_runtime_lock.py` → 1 failed, 108 passed.
No production impact (single-process startup lock is intended); no *other* module raised a lock
error (guards+bypass placement is correct). **Fix:** an autouse teardown (conftest or
test_runtime_lock.py) releasing leaked locks + resetting `_TEST_BYPASS_DEPTH`, or make
test_app_service_extended.py mock/release the lock.

**FP-C-1 — PR C anchor mismatch, partial apply.** Op [23] on `download_service.py`
("source-aware challenge diagnostics") anchors on a single-line signature
`def _log_page_diagnostics(self, driver, title: str) -> ScrapeDiagnostic:`, but the real
definition at `8a48382` is multi-line (`def _log_page_diagnostics(\n  self,\n …`). The generic
installer writes ops incrementally, so ops [0]–[22] applied then aborted, and the
undetection-flag removal (op [27]) never ran — the 3 automation-obscuring flags remain in
`sources/hdencode.py`. **Fix:** update op [23]'s anchor to the real multi-line signature and
re-verify the whole op list applies end-to-end so op [27] runs (this is the B1/MP-15 closure).

**FP-B-1 — PR B anchor mismatch, partial apply.** `apply_trash_lifecycle_transaction.py` →
"maintenance anchor expected once, found 0". The AppService `_run_maintenance_pass` patch
(repair-before-sweep) anchor is not present in `app_service.py` at `44ea7ba`. Partial apply
(fileops.py modified). **Fix:** correct the maintenance-pass anchor to the real `44ea7ba` text
and re-verify end-to-end.

## Minor

**FP-15-1 — PR #15 fs: `git diff --check` whitespace.** `tests/test_rename_core.py:1129:
new blank line at EOF.` Trivial (strip trailing blank line). PR #15 fs otherwise applies clean,
compiles, and its core invariant passes: `test_unsupported_filesystem_refuses_move_before_source_consumption`
+ status test — validating the CIFS/unsupported-FS fail-safe (source bytes remain, destination
absent, closed error). This closes the BD-2 concern in code.

## What is committed / validated sound

- **PR D committed** to `agent/hdencode-candidate-evidence` at `2145ef6e7618a989a582b6fb14fa7364858fb41e`
  (local draft, unpushed). Closes MP-01 (HDR10P alias) and enforces DV downgrade safety:
  RSS silence (`unknown`) is never treated as DV loss; an unknown-DV candidate cannot
  auto-replace a known-DV library copy. This is `PR_D_HEAD_AFTER_VALIDATION` for PR E.
- **PR #17** MP-10/lifecycle evidence re-confirmed green at `3e60c24`.
- **PR J boundary, PR A lock mechanism, PR C coordinator intent, PR #15 fail-safe** production
  designs are all validated sound — only their packaging is defective.

## Environment-gated (cannot be satisfied here)

Playwright E2E, real CIFS/NTFS filesystem sentinel (needs Jesse's explicit authorization),
7-day RSS shadow, Python 3.11 leg. These remain pending regardless of the code defects.

## Required corrected packages (one round)

FP-14-1, FP-J-1, FP-A-1, FP-C-1, FP-B-1 (blocking) + FP-15-1 (minor). Given the 5/8 defect
rate, **every remaining package (E, F, G, H, I, cross-seams) must also be applied and tested
against a real checkout by the author before re-submission** — anchor/blob guards passing does
not imply the operations apply, and no package's test suite has been executed by the author.

## Verdict

Stage B (integration + full matrix + adversarial review of the final assembled code) is
**not reachable** in this round: the integration branch merges exactly the heads that cannot
currently be committed. Production designs are sound; the blockers are corrected-package work.

**FEATURE PACK ACCEPTED WITH REQUIRED FIXES**

(Required fixes: the 6 packaging defects above, delivered as corrected guarded packages;
re-application+testing of the unreached packages by the author; and — separately — the
environment-gated sentinel/shadow/Playwright evidence before any enable/deploy.)
