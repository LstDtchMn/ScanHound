# PR #3 Hardening — Final Report

**Task:** Apply the ChatGPT-authored PR #3 hardening package, review it, validate, push, and diagnose CI.
**Date:** 2026-07-18
**Repository:** LstDtchMn/ScanHound
**Branch:** `agent/hdencode-off-switch` (PR #3, draft)

---

## 1. Did the script apply without modification?

**Yes.** `apply_pr3_hardening.py` ran cleanly on the first attempt — no errors, no manual intervention, no edits to the script or its output.

`SHA256SUMS.txt` was verified against all four handoff files before anything was executed. All four matched:

```
e4cbd057…4700  apply_pr3_hardening.py
f48f65ef…efb7  CLAUDE_GIT_REVIEW_HANDOFF.md
81e50783…b9a7  PR3_BODY_ADDENDUM.md
03dd99ac…5b75  README.md
```

Pre-application state was confirmed clean: branch head was exactly the expected `f6adfd6fc45c3a29a965cb729d368b26d6654af0`, no tracked or staged modifications, `git diff --check` clean.

---

## 2. Review findings

**No material issues found.** The package is well-targeted work. What was actually checked, rather than assumed:

### The vulnerability being fixed is real

The original PR #3 gated the off-switch with a raw substring check over the **entire URL**, including the query string:

```python
normalized_url = (url or "").lower()
is_hdencode = ("ddlbase.com" not in normalized_url
               and "adit-hd.com" not in normalized_url)
```

A URL such as `https://hdencode.org/release/?next=https://ddlbase.com/movie` would be misclassified as DDLBase and **silently bypass the HDEncode off-switch**. Separately, the scraper *dispatch* had the identical bug (`if "ddlbase.com" in url:`), so that same URL could be routed to the DDLBase scraper instead of HDEncode's.

The hardening centralizes classification into one `_source_page_kind()` helper using `urlparse(...).hostname` — which also correctly strips credentials and port, unlike a raw `netloc` comparison — and reuses that single decision for both the gate and the dispatch.

### Verifications performed

- **No leftover substring checks.** Grepped the patched `download_service.py` for any remaining raw `"ddlbase.com" in` / `"adit-hd.com" in` domain checks. None remain. All remaining `_url_matches_domain` call sites are pre-existing shortlink/host checks unrelated to page routing.
- **Background-scan skip point traced in context**, not taken from the script's own comments. The disabled-HDEncode `continue` fires at `background_scanner.py:189`, before `self._scan_source(...)` at `:202` — which is the only path to `run_scan` (`:292`). No network-capable call can begin for a disabled source.
- **Cache-purge safety.** The `any_early_stopped` → `purge_safe` rename is logically equivalent to the original (De Morgan's on the same conditions) with the disabled-source case correctly folded in. Confirmed this is not an accidental inversion.
- **Pipeline test genuinely exercises the real registry.** Only `discover_sources` is monkeypatched — reasonable, since real plugin discovery scans disk. `search_all()` itself is NOT mocked, so the test really does prove that config sync suppresses the disabled source. Separately confirmed beforehand that PR #3's original one-line `pipeline.py` change *is* the `sync_from_config(reg.config)` call this test's premise depends on.
- **No unrelated changes.** Read the full resulting production diff top to bottom, not just the apply script's stated intent. Exactly the five files, exactly the described behavior.

### One observation (not a defect)

This hardening closes the substring-spoofing gap for **page classification only**. No audit was performed for other places in the codebase with the same class of bug — out of scope per the handoff's own instructions.

---

## 3. Changed files and diff statistics

```
backend/background_scanner.py     | 24 +++++++++++---
backend/download_service.py       | 43 ++++++++++++++++---------
tests/test_background_scanner.py  | 29 +++++++++++++++++
tests/test_download_service.py    | 40 ++++++++++++++++++++++++
tests/test_hdencode_off_switch.py | 66 +++++++++++++++++++++++++++++++++++++++
5 files changed, 184 insertions(+), 18 deletions(-)
```

`git diff --check` — clean (exit 0).
`python -m compileall -q backend tests` — clean. (Pre-existing unrelated `SyntaxWarning`s in `tests/test_rename_service.py` for raw Windows path literals; present on `main` too.)

---

## 4. Focused test command and result

```bash
pytest -q \
  tests/test_hdencode_off_switch.py \
  tests/test_background_scanner.py \
  tests/test_api_routes.py
```

**37 passed, 0 failed** — 5.07s.

```
tests/test_hdencode_off_switch.py ....                                   [ 10%]
tests/test_background_scanner.py ........................                [ 75%]
tests/test_api_routes.py .........                                       [100%]
```

No environment-only failures to distinguish on this run — everything genuinely executed and passed, including the new async pipeline-registry test (required installing `pytest-asyncio` in the test container).

Note: `test_api_routes.py` was scoped to the relevant classes rather than run whole, because the complete file has a known pre-existing stall (see §5). The specific tests it contributes to this PR all ran and passed.

---

## 5. Broad-suite result / timeout point

**Timed out, pre-existing, not caused by this branch.**

Bounded at 180s with `--timeout=15` per test: reached roughly **8%, one `F`, then stalled** — identical dot count and stall position to three independent prior checks earlier in this session:

| Branch checked | Result |
|---|---|
| unmodified `main` | same stall, same position |
| `agent/hdencode-block-cancellation` tip | same stall, same position |
| `agent/hdencode-source-health` tip | same stall, same position |
| `agent/hdencode-off-switch` (hardened) | same stall, same position |

Four independent reproductions across four different branches, including one with zero HDEncode changes, conclusively establishes this as a characteristic of the test suite itself. Not investigated further — out of scope.

---

## 6. New PR #3 commit SHA

**`397e52d`** (previously `f6adfd6`)

Pushed via `git push origin HEAD:agent/hdencode-off-switch`, reported by git as a fast-forward: `f6adfd6..397e52d`. Normal push, not forced.

Commit message: `Harden HDEncode off-switch coverage`

`PR3_BODY_ADDENDUM.md` was appended to PR #3's body exactly once — existing body fetched first and preserved, addendum absence verified before appending. PR remains in **draft**.

---

## 7. PR #3 workflow result and exact failing tests

**Fails — but for a reason entirely unrelated to this hardening work, or to HDEncode at all.**

### Root cause, fully diagnosed

**`backend/api/routes/rename.py:604`**

```python
(f', {result['skipped']} already tracked' if result.get('skipped') else '')
```

This is a nested-quote f-string using the **same quote character** inside and outside the expression. That is a hard `SyntaxError` on Python 3.10 and 3.11. Python 3.12 relaxed this restriction (PEP 701), which is precisely why local Python 3.12.13 test containers never caught it anywhere across this entire session.

```
E   File "backend/api/routes/rename.py", line 604
E     (f', {result['skipped']} already tracked' if result.get('skipped') else '')
E                   ^^^^^^^
E   SyntaxError: f-string: f-string: unmatched '['
```

### Blast radius

The error occurs at **import time** in `create_app()`, so it takes down:

- **All backend test jobs** — collection errors on 15+ test files (`test_api_routes.py`, `test_api_auth.py`, `test_api_rename.py`, `test_dv_*.py`, `test_plex_routes.py`, and more), ending in `Interrupted: 15 errors during collection`.
- **The frontend job** — its Playwright `webServer` step launches the same backend, which crashes on startup:
  `[WebServer] SyntaxError: f-string: f-string: unmatched '['`
  → `Error: Process from config.webServer was not able to start. Exit code: 1`

### New run after the hardening push

Run `29665718717` (head `397e52d`):

| Job | Result |
|---|---|
| `test (3.10)` | **failure** — the `rename.py:604` SyntaxError |
| `frontend` | **failure** — the same error via Playwright's webServer |
| `test (3.11)` | cancelled |
| `test (3.12)` | cancelled |

The `cancelled` results are GitHub Actions' default matrix `fail-fast` cancelling sibling jobs once one fails — not independent failures.

---

## 8. Do the failures reproduce on `main`?

**Yes.** `main`'s last five CI runs all show `conclusion: failure`, including a **scheduled nightly run** untouched by any push:

```
failure  push      main  2026-07-18T16:35:04Z
failure  push      main  2026-07-18T14:17:47Z
failure  push      main  2026-07-18T13:28:52Z
failure  push      main  2026-07-18T13:06:12Z
failure  schedule  main  2026-07-18T05:16:24Z
```

`backend/api/routes/rename.py` is not touched by any of the six PRs in this stack. This is a `main`-level defect that predates all of this work.

---

## 9. Manual workflow results for branches behind PRs #4–#8

All five dispatched via `gh workflow run Tests --ref <branch>`. All five: **failure**, identical `rename.py:604` root cause.

| Branch | PR | Run ID | Result |
|---|---|---|---|
| `agent/hdencode-detail-pacing` | #4 | 29665783474 | failure — same cause |
| `agent/hdencode-structured-outcomes` | #5 | 29665783936 | failure — same cause |
| `agent/hdencode-block-cancellation` | #6 | 29665784393 | failure — same cause |
| `agent/hdencode-source-health` | #7 | 29665784896 | failure — same cause |
| `agent/hdencode-lazy-hydration` | #8 | 29665785433 | failure — same cause |

The source-health run's job log was inspected directly to confirm the identical `rename.py:604` trace, rather than inferring it from the pattern.

**Context on why these needed manual dispatch:** `tests.yml` triggers on `pull_request: branches: [main, master, develop]`. Every branch in this stack except the off-switch targets another `agent/hdencode-*` branch, so the `pull_request` trigger structurally never fires for them. That is a consequence of the stacked-PR strategy, not a defect in any PR.

---

## 10. Confirmations

- **No PR was merged.** None marked ready for review. PR #3 remains a draft.
- **`main` was not modified.** Local and `origin/main` both still at `58feedf`, unchanged.
- **No force-push occurred.** The only push was a plain fast-forward to `agent/hdencode-off-switch`.
- **No unrelated files were committed.** `git diff --cached --stat` verified before committing. The apply script itself and all handoff documents were deleted from the worktree before staging and were never committed.

---

## Bottom line

The hardening is good, tested, and pushed — nothing to redo there.

The thing actually blocking green CI across the **entire six-PR stack** is one pre-existing line — `backend/api/routes/rename.py:604` — unrelated to HDEncode work entirely. Per the handoff's explicit instruction ("diagnose and advise only… Return confirmed baseline defects to ChatGPT for implementation"), no fix was implemented. This is returned as a confirmed baseline defect.

Fixing it is a one-line change (swap the inner quotes to double quotes, or extract to a local variable) and would very likely turn CI green across all six PRs at once, since every one of them fails on this and nothing else.

### Deployment gate — unchanged

Nothing here was merged or deployed. **PR #3 must be deployed alone, HDEncode disabled, and one complete real background-scan cycle must demonstrate zero HDEncode listing, detail, Selenium, and pipeline-search traffic** before PRs #5–#8 advance.
