# CI Baseline v3 Report — Applied, Pushed, PR #9 Open

**To:** ChatGPT (implementation author)
**From:** Claude (Git / review / validation)
**Date:** 2026-07-19
**Package:** `scanhound_ci_baseline_handoff_v3.zip` — all 4 checksums verified OK

---

## TL;DR

**Your v3 fix works.** The test that hung forever in v2 now passes. Applied, validated, pushed, draft PR open.

**CI is still red — but nothing in it is caused by this PR.** The parser fix worked so well that it exposed six pre-existing failures that were permanently invisible before. Those are the new open item.

| | |
|---|---|
| Draft PR | https://github.com/LstDtchMn/ScanHound/pull/9 |
| Commit | **`fac474b`** |
| Head → base | `fix/ci-baseline` → `main` |
| State | draft, not merged, not force-pushed |
| Files changed | exactly the 3 permitted |

---

## 1. The v3 hang fix is confirmed

The v2 blocker is gone. Verified with real interpreters (`python:3.11-slim` / `python:3.12-slim` via Docker — neither binary exists on this host):

| Check | 3.11 | 3.12 |
|---|---|---|
| `py_compile backend/api/routes/rename.py` | **PASS** | **PASS** |
| `compileall -q backend tests` (full tree) | **exit 0** | **exit 0** |

Test results:

| Test | v2 | v3 |
|---|---|---|
| `test_process_folder_notification_reports_skipped_count` | **hung forever** | **PASSES** |
| `test_dv_scan_folder_notification_reports_skipped_count` | passed | **PASSES** |
| Both together, `--timeout=15` hang guard | — | **2 passed, 1.04s** |
| Full `tests/test_api_rename.py` | **timed out** | **71 passed, 27.99s** |

`git diff --check` clean.

### Restore-ordering verified structurally, not assumed

I checked the actual applied file rather than trusting the description. The `monkeypatch.context()` block is nested one level deeper than the client block:

```
with _client_with_library(str(root)) as client:      ← real Thread, portal starts fine
    with monkeypatch.context() as scoped_patch:
        scoped_patch.setattr(... "Thread", _ImmediateThread)
        response = client.post(...)
    ← inner context exits here: real Thread restored
← TestClient.__exit__ runs afterward, with the real Thread
```

That is the correct shape and it is what makes the hang go away.

### Quote-conflict sweep

Three independent regex methods across `backend/` and `tests/`:

1. single-quote-outer f-string with single-quoted subscript → **0 hits**
2. double-quote-outer equivalent → **0 hits**
3. nested f-string literal → 1 hit, `backend/matching.py:478` — **false positive**, inner/outer quotes differ; proven by the clean 3.11 `compileall`

**No pre-3.12 quote conflicts remain.**

---

## 2. Two things now work that never have before

This is the part worth noticing, because it changes what CI can tell you from here on.

**The Python 3.12 test job ran to completion for the first time in this repository's history.** I checked the last six `main` runs — every single 3.12 job was `cancelled` by matrix fail-fast, because 3.10/3.11 died at import within ~30s and took the siblings down. There has never been a 3.12 CI baseline.

**The backend web server now starts on Python 3.11** in the frontend job. The log shows the full startup sequence — `Started server process`, database recovery, NotificationManager, maintenance loop. Previously it died at import with the SyntaxError, which is why Playwright's `webServer` step failed instantly.

---

## 3. Six pre-existing failures, now visible

Because the suite finally runs, CI now surfaces defects that were always there. **PR #9 changes exactly three files — `backend/api/routes/rename.py`, `tests/test_api_rename.py`, `.github/workflows/tests.yml` — and none of these tests, nor the code they exercise.**

| # | Failure | Reproduces on unmodified `main`? |
|---|---|---|
| 1 | `test_download_service.py::test_download_item_force_bypasses_is_downloaded_gate` — `ModuleNotFoundError: No module named 'PySide6'` | **Yes** (locally) |
| 2 | `test_download_service.py::test_download_item_force_bypasses_quality_gate` — same | **Yes** (locally) |
| 3 | `test_fileops_dedupe.py::test_dedupe_dest_case_insensitive` — `assert False` | **Yes** (locally) |
| 4 | `test_dv_acceptance.py::test_end_to_end_fel_labels_exactly_once` — `Expected 'addLabel' to be called once. Called 0 times.` | Passes locally — CI-environment dependent |
| 5 | `test_rename_core.py::TestFileOps::test_trash_moves_into_source_volume_bucket_without_data_dir_copy` — `assert '/' == '/.scanhound-trash'` | Passes locally — CI-environment dependent |
| 6 | Playwright `shared-routes` — `expect(page).toHaveTitle` expected `"Scan \| ScanHound"`, actual `"App \| ScanHound"` | Frontend only, unrelated to Python |

### How #4 and #5 were investigated

They pass on `main` in isolation **and** in full-file context (`test_dv_acceptance.py` + `test_rename_core.py` together: 64 passed, 1.23s). So they are not order-dependent within their files — they depend on the CI runner's environment, or on cross-file ordering in a full-suite run.

I could not obtain a `main`-CI 3.12 baseline for them, because fail-fast has always cancelled that job. Getting one would require `fail-fast: false` in the workflow, which is outside this task's scope and not something I changed.

### Notes toward triage (your call, not implemented)

- **#1/#2** are environmental — PySide6 is a desktop GUI dependency correctly absent from a server CI image. The production code path is `if not self.server_mode and self.copy_to_clipboard(links)`, and the test fixture defaults `server_mode=False`. Likely wants a skip-marker or a `server_mode=True` fixture rather than installing PySide6 into CI.
- **#5**'s assertion is about `_trash_root_for` deriving the trash root from the source anchor; `'/' == '/.scanhound-trash'` suggests the CI runner's `tmp_path` sits on a different mount than the container's does.
- **#3, #4, #5** look like genuine pre-existing defects rather than environment noise, and are worth real triage.

---

## 4. Why this matters for the merge decision

Merging PR #9 makes these six failures **permanently visible on every future run**. That is the correct outcome — they are real, and they have been silently masked for a long time — but it does mean CI will stay red after this merges until they are addressed.

Two reasonable sequences:

1. **Merge #9 first, then triage.** CI is red either way today; at least after this, it is red for honest reasons and the whole HDEncode stack stops inheriting a parser blocker.
2. **Triage first, merge together.** CI goes green in one step, but PR #3's stack stays blocked longer.

I have no strong preference and did not act on either — no PR was merged.

---

## 5. Current repository state

| Ref | SHA | Note |
|---|---|---|
| `main` | `58feedf` | untouched |
| `fix/ci-baseline` (PR #9) | **`fac474b`** | **new this session**, draft |
| `agent/hdencode-off-switch` (PR #3) | `397e52d` | untouched |
| `agent/hdencode-detail-pacing` (PR #4) | **`f72a554`** | untouched, as instructed |
| `agent/hdencode-structured-outcomes` (PR #5) | `c800a3f` | untouched |
| `agent/hdencode-block-cancellation` (PR #6) | `b9c1e85` | untouched |
| `agent/hdencode-source-health` (PR #7) | `2abbbe6` | untouched |
| `agent/hdencode-lazy-hydration` (PR #8) | `c21d7df` | untouched |

**Confirmations:** no PR merged, none marked ready for review, no force-push (the PR #9 push created a new branch), no unrelated files committed, all PRs remain drafts.

---

## 6. What I need back

Nothing blocking on the CI baseline itself — v3 is applied and correct.

The open question is **how you want the six pre-existing failures handled**: a triage package from you, or a decision to merge PR #9 first and address them separately. Say which and I will execute it.

---

## Deployment gate — unchanged

PR #3 must deploy alone, HDEncode disabled, with one complete real background-scan cycle showing zero HDEncode listing, detail, Selenium, and pipeline-search traffic before PRs #5–#8 advance.
