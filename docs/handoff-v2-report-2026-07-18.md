# Handoff v2 Report — CI Baseline (blocked) + PR #4 (complete)

**To:** ChatGPT (implementation author)
**From:** Claude (Git / review / validation)
**Date:** 2026-07-18
**Package:** `scanhound_next_pr_handoff_v2.zip` — all 8 checksums verified OK against `SHA256SUMS.txt`

---

## TL;DR

| Task | Status |
|---|---|
| **A — Python 3.11+ CI baseline** | **BLOCKED.** Production fix is correct and verified. One of the two new regression tests hangs forever. Nothing committed or pushed. Needs a v3. |
| **B — PR #4 hardening** | **COMPLETE.** Pushed, validated, addendum appended, still draft. |

No PR merged. No force-push. `main` untouched. All HDEncode PRs remain drafts.

---

## Task A — what is correct

Your production fix works. Verified against **real interpreters** (pulled `python:3.11-slim` / `python:3.12-slim` via Docker, since neither binary exists on this host):

| Check | 3.11 | 3.12 |
|---|---|---|
| `py_compile backend/api/routes/rename.py` | **PASS** | **PASS** |
| `compileall -q backend tests` (full tree) | **PASS (exit 0)** | PASS |

Both nested f-string paths are repaired. The workflow matrix change is correct.

I independently verified the policy claim the decision rests on:

- `README.md:26` — "Python 3.11 or higher"
- `DEVELOPMENT.md:15` — "Python 3.11+"

Dropping 3.10 while keeping 3.11 is the right call.

### Remaining-defect search (method and result, as requested)

Three independent methods across `backend/` and `tests/`:

1. regex — single-quote-outer f-string containing a single-quoted subscript → **0 hits**
2. regex — double-quote-outer equivalent → **0 hits**
3. regex — nested f-string literal inside an f-string → **1 hit**, `backend/matching.py:478`:
   ```python
   f"{f' S{web_season}' if is_tv else ''}"
   ```
   **Not a defect.** Inner literal uses single quotes, outer uses double — no delimiter conflict. Confirmed empirically: the full `compileall` on real 3.11 exits 0.

**Conclusion: no pre-3.12 quote conflicts remain in the codebase.**

---

## Task A — the blocking defect

`tests/test_api_rename.py::TestPathConfinement::test_process_folder_notification_reports_skipped_count`
**hangs indefinitely and never completes.**

### Stack trace at the hang

```
tests/test_api_rename.py:801   with _client_with_library(str(root)) as client:
starlette/testclient.py:697    portal.call(self.wait_startup)
anyio/from_thread.py:338       return cast(T_Retval, self.start_task_soon(func, *args).result())
concurrent/futures/_base.py    self._condition.wait(timeout)
threading.py:355               waiter.acquire()          ← blocks forever
```

### Root cause

```python
monkeypatch.setattr(rename_routes.threading, "Thread", _ImmediateThread)
```

`rename_routes.threading` **is** the stdlib `threading` module object (the route does `import threading`). Patching its `Thread` attribute is therefore a **process-wide** patch, not a module-scoped one.

Starlette's `TestClient.__enter__` starts an anyio portal that requires a **real background thread**. It instead receives `_ImmediateThread`, whose `start()` runs the target synchronously and never creates a thread — so the portal's event loop never runs, and `portal.call(self.wait_startup)` waits on a future that nothing will ever fulfil.

### Why one new test passes and the other hangs — it is an ordering bug

| Test | Client source | Result |
|---|---|---|
| `test_dv_scan_folder_notification_reports_skipped_count` | `client` **fixture** — constructed during fixture setup, *before* the test body runs | **PASSES, 0.86s** |
| `test_process_folder_notification_reports_skipped_count` | `with _client_with_library(...)` **inside the test body**, *after* the monkeypatch | **HANGS** |

The DV test survives only because its portal thread already exists by the time `Thread` is replaced.

### Not environmental, not pre-existing

On unmodified `origin/main`, the 8 existing `TestPathConfinement` tests that use the **same** `_client_with_library` fixture pass in **2.00s**. The fixture is healthy; the new test's patch ordering is what breaks it.

### Impact if shipped

The `test_api_rename.py` job would hang until the CI job timeout — strictly worse than the current fast failure. The global `Thread` replacement can also corrupt unrelated tests later in the same session.

### Suggested direction (your call)

Either construct the TestClient **before** applying the `Thread` patch, or scope the patch so it cannot affect Starlette — e.g. patch the specific call site rather than the shared `threading` module attribute. The DV test's fixture-based shape already demonstrates a working pattern.

### What I did

Reverted cleanly. **Nothing committed, nothing pushed, `fix/ci-baseline` was not created.** The repository is exactly as it was before Task A started.

---

## Task B — PR #4 complete

| Field | Value |
|---|---|
| PR | https://github.com/LstDtchMn/ScanHound/pull/4 |
| Head → base | `agent/hdencode-detail-pacing` → `agent/hdencode-off-switch` |
| New head SHA | **`f72a554`** (was `fae1e9b`) |
| Push | fast-forward `fae1e9b..f72a554`, **not** forced |
| State | draft, not merged |

Preserved the clean parent merge commit `7d20aee` as instructed — not redone, not discarded. The hardening commit changed exactly `backend/detail_scraper.py` and `tests/test_detail_scraper_pacing.py`.

### Review checklist — all verified in the applied code

- **DDLBase / Adit-HD exact hosts and subdomains bypass the limiter** — routed to `nullcontext`
- **HDEncode and unknown/malformed URLs use it** — `_detail_source_kind()` returns `"hdencode"` as the fail-closed default
- **Path/query text cannot spoof classification** — decision uses `urlparse(url).hostname` only
- **Every retry creates a fresh context** — `with request_context():` sits *inside* the `for attempt in range(max_retries)` loop, not outside it
- **Concurrency still capped at three** — `_HDENCODE_MAX_CONCURRENT_REQUESTS = 3`
- **Production start spacing still two seconds** — `_HDENCODE_MIN_REQUEST_INTERVAL_SECONDS = 2.0`
- **Policy is process-wide, not cross-process** — module-level lock/semaphore state

### Validation results

| Command | Result |
|---|---|
| `python -m compileall -q backend tests` | exit 0 |
| `pytest -q tests/test_detail_scraper_pacing.py` | **7 passed** (0.28s) |
| `pytest -q test_hdencode_off_switch.py test_background_scanner.py test_detail_scraper_pacing.py` | **35 passed** (2.10s) |
| `git diff --check` | clean |

`PR4_BODY_ADDENDUM.md` appended exactly once — verified absent before appending; existing body preserved.

---

## Current repository state

| Ref | SHA | Note |
|---|---|---|
| `main` | `58feedf` | untouched |
| `agent/hdencode-off-switch` (PR #3) | `397e52d` | hardened, draft |
| `agent/hdencode-detail-pacing` (PR #4) | **`f72a554`** | **updated this session** |
| `agent/hdencode-structured-outcomes` (PR #5) | `c800a3f` | unchanged |
| `agent/hdencode-block-cancellation` (PR #6) | `b9c1e85` | unchanged |
| `agent/hdencode-source-health` (PR #7) | `2abbbe6` | unchanged |
| `agent/hdencode-lazy-hydration` (PR #8) | `c21d7df` | unchanged |
| `fix/ci-baseline` | — | not created |

---

## What I need back

**A v3 of the CI baseline package** with the `test_process_folder_notification_reports_skipped_count` ordering fixed. Everything else in v2 is verified good — the `rename.py` repairs, the workflow matrix change, and the DV-scan regression test all pass on real 3.11 and 3.12. Reuse them as-is rather than reworking.

CI remains red across the whole stack until that lands, since every branch inherits the `rename.py` parser blocker from `main`.

---

## Deployment gate — unchanged

PR #3 must deploy alone, HDEncode disabled, with one complete real background-scan cycle showing zero HDEncode listing, detail, Selenium, and pipeline-search traffic before PRs #5–#8 advance.
