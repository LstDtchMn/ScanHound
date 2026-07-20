# PR #17 MP-10 — RESOLVED and landed (2026-07-19)

Follow-up to `MP10_RESULT.md`. Stage 2 fix v2 (`scanhound-pr17-stage2-fix-v2`,
checksum verified) addressed both findings I returned:

- **§4 regression:** `_FakeRegistry` in `tests/test_background_scanner.py` now
  models `lifespan_generation`/`owns_lifespan`; a direct stale-generation
  `BackgroundScanner` test was added. Production stays strict (no `getattr`
  fallback — they chose option (a), as recommended).
- **§5 guards:** the fix script now uses `git rev-parse --is-inside-work-tree` +
  `symbolic-ref`, so it runs in a linked worktree and detached HEAD. It also
  self-verified `tests/test_source_registry.py` is unchanged.

## Full validation (probe v2 + fix v2, on branch `fix/lifecycle-registry-reset`)

Applied exactly 5 files; `test_source_registry.py` confirmed unchanged.

| Suite | Result |
|---|---|
| reproduction test | **PASS** (fail→pass) |
| full `test_api_lifecycle.py` | 5 passed |
| `test_api_results.py` | 58 passed |
| `test_background_scanner.py` (the 15 that regressed + new stale-gen test) | **24 passed** |
| `test_source_registry.py` (unchanged) | 60 passed |
| `TestResults` flake family | 16 passed |
| `test_config.py` + `test_dv_acceptance.py` | 103 passed |
| lifecycle + background_scanner as **uid 1000** | 29 passed |

No regression anywhere. Generation increments exactly once per
`_prepare_registry_for_startup`; old generations lose ownership, current retains.

## Landed

Committed to `fix/lifecycle-registry-reset` (draft branch, not a
merge/deploy/force-push/ready): **PR #17 head `bf07697` → `3e60c24`**.

One transparent hygiene fix: the probe left a trailing blank line at EOF of
`tests/test_api_lifecycle.py`; I stripped it so the pre-merge `git diff --check`
gate stays clean. No other content change.

## MP-10 status: CLOSED

The last plan-level uncertainty besides the CIFS sentinel is resolved — the
defect was real, the generation token was warranted, and the validated fix is now
on PR #17. Remaining open gate: the Jesse-authorized CIFS/NTFS sentinel (MP-09).

State: PR #3 `f3c2f0c`, #15 `70dca70`, #16 `44ea7ba` unchanged; #17 `3e60c24`;
`main` `555e26b`. All draft. Nothing merged/deployed/force-pushed/ready.
Auto-rename still enabled in production.
