# CI Stabilization — Steps 4 & 5 Report (2026-07-19)

Claude: Git operations, independent review, validation, CI, evidence.
ChatGPT: implementation and tests.

No merges. No force-pushes. All PRs remain draft.

## Branch / PR state

| PR | Branch | Base | Head |
|----|--------|------|------|
| #12 | `fix/same-volume-trash` | `fix/case-insensitive-dedupe` | `4e2b0c4` |
| #13 | `fix/playwright-production-preview` | `fix/same-volume-trash` | `07dbfb0` |

- PR #13: https://github.com/LstDtchMn/ScanHound/pull/13
- Protected refs verified unchanged: PR #9 `fac474b`, PR #4 `f72a554`, `main` `58feedf`.

## Step 4 — Playwright production preview: APPLIED AND VERIFIED

`apply_playwright_preview.py` changed exactly one file,
`frontend/playwright.config.ts`. `git diff --check` clean.

Local validation (host, main checkout):

- `npm run check` — 0 errors (3 pre-existing warnings)
- `npm run build` — clean
- `CI=1 npx playwright test --project=desktop --grep "/ loads"` — **1 passed (9.1s)**
- `CI=1 npx playwright test` (full suite) — 17 passed, 1 failed

### Server-mode proof (the handoff's required confirmation)

Measured from Playwright traces by counting dev-only vs build-only markers:

| Mode | `@vite/client` refs | `_app/immutable` refs | Server |
|------|--------------------|------------------------|--------|
| `CI=1` | 0 | 74 | **vite preview (production build)** |
| local | 4 | 0 | **vite dev** |

Confirmed: CI mode starts the production preview; local mode still uses the
dev server.

## Step 5 — Full workflow dispatch

Manually dispatched `Tests` on `fix/playwright-production-preview`.

Run: https://github.com/LstDtchMn/ScanHound/actions/runs/29670846223

| Job | Result |
|-----|--------|
| `test (3.11)` | **failure** — 3 failed, 3762 passed, 4 skipped |
| `test (3.12)` | cancelled (matrix `fail-fast` cancelled it when 3.11 failed) |
| `frontend` | **failure** — 12 e2e failures |

### What did improve

The backend webServer now starts in CI. Every prior run died at
`SyntaxError: f-string: unmatched '['` before a single e2e test executed, so
this is the **first run in recent history where the e2e suite actually ran**.
The five original backend failures targeted by the stack are gone; the three
that remain are new (below). Frontend unit tests: 364 passed / 25 files.

---

## BLOCKER 1 — PR #12 regresses trash restore (data-loss shaped)

`test (3.11)` failures, all in `tests/test_rename_service.py`:

```
FAILED TestConflictSignal::test_overwrite_restores_trashed_original_on_place_file_failure
FAILED TestConflictSignal::test_overwrite_db_write_failure_also_restores_trashed_original
FAILED TestReplaceLibraryDupAndKeepPlex::test_replace_library_dup_restores_library_on_place_failure
```

CI error text:

```
apply: overwrite failed AND restore of the trashed original failed for job 2
-- .../lib/The Matrix (1999) [1080p].mkv is now EMPTY,
original stranded at /tmp/.scanhound-trash/20260719-025411/... : Trash entry not found
```

### Attribution — reproduced, parent vs child

Environment required to reproduce: `/tmp` on a **separate device** AND a
**non-root** uid. Both hold on GitHub runners; neither holds in a default
container, which is why this passed every local check.

```
scanhound:latest + --tmpfs /tmp + uid 1000

PARENT 81e5614 (fix/case-insensitive-dedupe)     -> 3 passed
CHILD  4e2b0c4 (fix/same-volume-trash, PR #12)   -> 3 failed
```

Same container image, same tests, same uid. **PR #12 causes it.**

### Root cause

PR #12 taught `_trash()` to fall back to progressively deeper same-volume
ancestors, and updated the path-aware `trash_roots(path)` to match. It did
**not** update the path-independent `all_trash_roots()`
(`backend/rename/fileops.py:639`), which still enumerates only mount-point-level
roots (`<mount>/.scanhound-trash`) plus the app-data root.

The overwrite/replace restore-safety path calls the path-independent variant:

- `backend/rename/service.py:1379` → `restore_trash_entry(bucket, name, _fileops.all_trash_roots())`
- `backend/rename/service.py:1754` → `_fileops.trash_roots(restore_key)`  ← path-aware, unaffected

So when `_trash()` selects a deeper ancestor root, the restore lookup cannot see
that root, falls through the loop, and returns `"Trash entry not found"`. The
destination is left empty and the original is stranded — exactly the failure
those three tests exist to prevent.

Second consequence, not covered by any test: `all_trash_roots()` is also the
single source of truth for the trash list/delete endpoints and the maintenance
retention sweep. Trash placed in a deeper root is invisible to the UI and is
never swept, so it accumulates indefinitely.

**Not fixed here** — returning it per the standing instruction. The fix is
ChatGPT's call; the obvious shape is to make `all_trash_roots()` cover the same
candidate set `_trash()` can choose, and to add coverage that runs non-root with
a separately-mounted tmp.

### Validation gap worth noting

The Step 3 handoff specified only `tests/test_rename_core.py` for root and
uid-1000 validation. All three regressions live in `test_rename_service.py`, so
the prescribed validation could not have caught them regardless of diligence.

---

## BLOCKER 2 — the e2e suite is blocked in CI by the auth gate, not by compile time

12 of 18 e2e tests failed. Every failure is the same shape:

```
expect(page).toHaveTitle(expected) failed
Received: "App | ScanHound"          <- fallback shell title
2 x unexpected value ""
7 x unexpected value "App | ScanHound"
```

plus `element(s) not found` for the mobile sheets and a `/login` URL in the
tab-bar test. The app never reaches the route: it renders the shell and stops.

### Mechanism

`backend/api/routes/auth.py:94`:

```python
setup_required = not has_password and not nonce_active and not allow_open()
```

In CI: fresh checkout → no credential row → `has_password` false;
`--no-auth` → `nonce_active` false; `SCANHOUND_ALLOW_OPEN` unset → `allow_open()`
false. Therefore **`setup_required` is true**, and
`frontend/src/routes/+layout.svelte:123-125` redirects to `/login` and returns
early before the route renders.

This is the SH-H01 fail-closed behaviour working as designed. It is not a bug in
the app and it is **not caused by the Step 4 change** — it is simply the first
time CI has run e2e at all, so it has never been visible before.

### Consequence for the Step 4 premise

The root-route title failure was diagnosed locally as Vite cold-compile cost
(title unset until ~7.7s vs a 5000ms assertion). The production-preview change
does fix that specific local symptom, and the trace evidence above confirms it
runs against the built artifact. But it does **not** unblock CI, because CI's
actual cause is the auth redirect. The change is sound and worth keeping; it is
just not sufficient.

Deciding how CI should authenticate (set `SCANHOUND_ALLOW_OPEN=1` for the e2e
step, seed a credential, or have the fixture log in) is a design call. I have
not changed the workflow, the frontend, or the assertions.

### Local vs CI discrepancy, disclosed

Locally the same suite gave 17/18. My host backend runs against the production
`data/crawler.db`, which **has** a password → `auth_required` true → the same
redirect, but it lands racily rather than deterministically. Evidence:

- `bottom tab bar switches routes` passed on the parent (dev) on one run and
  failed on the next run of the identical command — it is flaky locally.
- Traces show `/auth/status` returning **200 in both dev and preview**, so the
  redirect is environment-driven, not mode-driven.

I initially classified this as a Step 4 regression on the strength of one
passing parent run; re-running the parent disproved that. Recording the
correction rather than the first conclusion.

---

## Recommendation

Both blockers belong to ChatGPT.

1. **PR #12** — real regression, reproduced parent-vs-child. Should not advance
   until `all_trash_roots()` covers the deeper roots and the sweep/list path is
   re-checked.
2. **CI auth** — decide the e2e authentication strategy. Until then the frontend
   job cannot pass, independent of PR #13.

PR #13 itself is verified as applied and behaving as specified; it is stacked on
#12, so it inherits #12's status.

## Housekeeping

- Temporary containers and worktrees removed.
- `main` checkout clean; `frontend/playwright.config.ts` reverted there.
- Deployment gate untouched: PR #3 still must deploy alone with HDEncode
  disabled and demonstrate one clean background-scan cycle before #5–#8 advance.
