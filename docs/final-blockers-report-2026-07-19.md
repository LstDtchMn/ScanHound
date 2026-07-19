# ScanHound final-blocker correction report — 2026-07-19

ChatGPT authored every implementation and test. Claude performed Git operations,
independent review, validation, CI, and fault injection.

**No merges. No force-pushes. No deployments. Nothing marked ready. `main`
untouched at `58feedf`. The PR #3 production deployment gate is unchanged.**

Package integrity: all 9 files verified against `SHA256SUMS.txt` — OK.

---

## 1. New SHAs and parent merge SHAs

| PR | Branch | Before | After | What changed |
|----|--------|--------|-------|--------------|
| #3 | `agent/hdencode-off-switch` | `397e52d` | **`a3cd13f`** | config-test fix |
| #4 | `agent/hdencode-detail-pacing` | `f72a554` | **`fb99d49`** | parent merge of #3 |
| #5 | `agent/hdencode-structured-outcomes` | `3b99985` | **`327a97d`** | parent merge of #4 (`08e523c`) + public diagnostics |
| #6 | `agent/hdencode-block-cancellation` | `b321dde` | **`25b821a`** | parent merge of #5 |
| #7 | `agent/hdencode-source-health` | `26e8ca7` | **`9d29717`** | parent merge of #6 (`ecb70cc`) + contract test |
| #8 | `agent/hdencode-lazy-hydration` | `d8f36aa` | **`cee0d23`** | parent merge of #4 |
| #12 | `fix/same-volume-trash` | `8d01846` | `8d01846` | unchanged |
| #13 | `fix/playwright-production-preview` | `de00396` | `de00396` | **body only, no code** |

All eight parent merges were **clean — zero conflicts.** This is a marked
improvement over the previous round, where #5 and #7 both conflicted; the
hostname-only resolutions from that round are now the common ancestor.

Rebuilt review-only integrations (pushed, no PRs):
`review/int2-pr3` `a230c05`, `-pr4` `ea9797b`, `-pr5` `5e743b2`,
`-pr6` `ba9e644`, `-pr7` `1bcf734`, `-pr8` `7c2b3de`.

---

## 2. Exact file lists

| Commit | Files |
|--------|-------|
| `a3cd13f` Update config tests for HDEncode switch | `tests/test_config.py` |
| `327a97d` Keep scrape exception details internal | `backend/scrape_outcome.py`, `backend/download_service.py`, `tests/test_scrape_outcomes.py` |
| `9d29717` Clarify reachable source health semantics | `tests/test_source_health.py` |

Every commit matched its handoff's expected file list exactly. No production
code was touched for PR #3, PR #7, or PR #13.

---

## 3. Focused and full test results

| Step | Command | Result |
|------|---------|--------|
| PR #3 | `test_config.py::TestDefaultConfig` + off-switch + background scanner | **42 passed** |
| PR #3 | full `tests/test_config.py` | **103 passed** |
| PR #4 | pacing + off-switch + config after parent merge | **114 passed** |
| PR #5 | outcomes + download service + off-switch + pacing | 201 passed, **2 failed*** |
| PR #6 | cancellation + outcomes + pacing after propagation | **23 passed** |
| PR #7 | source health + outcomes + cancellation | **24 passed** |

\* The two failures are `test_download_item_force_bypasses_*` with
`ModuleNotFoundError: No module named 'PySide6'`. Previously attributed by
execution: they fail identically on unmodified `origin/main`. Pre-existing and
unrelated. Note the count rose 200 → 201: the new redaction test.

Cross-version compile on real interpreters — **all six rebuilt integrations
`py3.11=OK py3.12=OK`.** The HDEncode branches alone still cannot compile on
3.11 (the `rename.py:604` f-string bug inherited from `main`), which continues
to demonstrate that the CI-stabilization stack is a hard prerequisite.

`git diff --check` clean on every commit.

---

## 4. Blocker resolution

### PR #3 — resolved and verified

`tests/test_config.py` now allowlists `hdencode_enabled` and adds a type
regression test. Executed attribution before and after:

```
before:  origin/agent/hdencode-off-switch (397e52d) -> 1 failed
         origin/main (58feedf)                      -> 1 passed
after:   agent/hdencode-off-switch (a3cd13f)        -> 42 passed / 103 passed
```

Production off-switch behaviour is unchanged — the commit touches only the test
file.

### PR #13 — body corrected, code untouched, scope verified

The four required confirmations, all checked against the real tree:

```
git diff --name-only origin/fix/same-volume-trash origin/fix/playwright-production-preview
  -> frontend/playwright.config.ts          (only file)
backend/api/__main__.py   vs parent -> IDENTICAL
docker/entrypoint.sh      vs parent -> IDENTICAL
tests/test_api_entrypoint.py        -> ABSENT (correct)
```

Production credential-free request on PR #13's HEAD, `SCANHOUND_ALLOW_OPEN`
unset (the production default):

```
has_password: False   auth_required: False   setup_required: True
GET /settings -> 401  (gated)
```

Fail-closed preserved. Isolated Playwright E2E re-run: **18/18 passed** with no
environment variables set by me, on a host whose real database has a password.

The contradictory `## Deterministic credential-free E2E follow-up` section and
the earlier partial-application review note were both replaced with the supplied
authoritative text (verified: authoritative text present ×1, "partial
application" occurrences 0).

### PR #7 — contract corrected and the full semantic matrix proven

The superseded test was replaced with one using the correct diagnostic
(`BROWSER_NETWORK_ERROR`, a local fault) rather than `REQUESTED_HOST_MISSING`.
Both referenced APIs were verified to exist before running:
`ScrapeCode.BROWSER_NETWORK_ERROR` and
`DatabaseManager.record_source_failure(source, state, reason_code, *, cooldown_seconds=None)`.

Every row of the required matrix, executed against the real
`record_scrape_outcome`:

| Outcome | Required | Observed |
|---|---|---|
| links found | healthy; clear streak/cooldown | healthy, streak 0, cooldown None ✅ |
| requested host missing | reachable → healthy | blocked → healthy, streak 0 ✅ |
| no supported file-host links | reachable → healthy | blocked → healthy, streak 0 ✅ |
| interactive challenge | blocked | blocked ✅ |
| layout changed | degraded | degraded ✅ |
| browser launch failed | no state change | no row / blocked snapshot preserved ✅ |
| browser network error | no state change | no row / blocked snapshot preserved ✅ |
| browser navigation failed | no state change | no row / blocked snapshot preserved ✅ |
| source disabled | no state change | no row / blocked snapshot preserved ✅ |
| internal scrape exception | no state change | no row / blocked snapshot preserved ✅ |

"Preserved" was checked strictly: state, `reason_code`, **and**
`consecutive_failures` all unchanged (`blocked/interactive_challenge/1`).

**DB-write fault injection** — an `ExplodingDB` whose every health read and
write raises, across the success, blocked, and reachable-empty paths:

```
[PASS] health write failure does not raise      (all three paths)
[PASS] scrape links unchanged                   (all three paths)
[PASS] diagnostic unchanged                     (all three paths)
[PASS] db=None is inert
```

Health persistence cannot turn a scrape result into an exception, and cannot
alter the scrape result.

---

## 5. Diagnostic-exposure audit

The four questions the handoff asked, answered against the code.

**Can raw exception text, local paths, usernames, tokens, or profile paths reach
a client?**

Through `ScrapeDiagnostic` — **no longer.** `to_dict()` now emits
`public_message` (enum-backed, fixed strings); `download_service.py:2215` uses
`public_message` for the API/WS-facing result; the `download:fallback` progress
payload now carries `reason="scrape_exception"` + `signal=<ExceptionClassName>`
instead of `str(e)`. Proven by
`test_serialized_diagnostic_never_exposes_internal_detail` (passes): a
`detail` containing `C:/Users/example/private-profile/chromedriver` appears in
`diagnostic.message` (internal) but not in `to_dict()["message"]`.

**But yes, through other routes this commit does not touch** — reported, not
silently broadened:

| Route | Location |
|---|---|
| WS notification body `str(e)` | `downloads.py:117`, `:217`; `rename.py:456`, `:612`, `:650`, `:705` |
| `HTTPException(detail=f"…{e}")` | `downloads.py:244`, `:372`; `scanner.py:412`; `results.py:893`; `settings.py:453`; `watchlist.py:191`, `:263` |

The second class reaches the UI directly: the frontend api client rethrows the
detail verbatim (`client.ts:72`, `throw new Error(detail || …)`), which surfaces
in toasts. A scrape exception raised inside `_run_grab` can therefore still
reach a client via `downloads.py:117` even after this fix. This is a broader,
pre-existing pattern and a separate decision; it is recorded in the `327a97d`
commit message.

**Do internal logs retain enough troubleshooting detail?** Yes.
`ScrapeDiagnostic.message` still returns `detail or public_message`, and
`download_service.py:1453` / `:1466` log `diagnostic.message`. Nothing was lost
from logs — only from serialization.

**Do public messages remain stable and actionable?** Yes. They come from the
`_MESSAGES` enum map, e.g. "The page loaded, but it does not contain links for
the requested file host." Stable per reason code, and `reason_code`, `retryable`
and `signals` are still emitted for programmatic handling.

**Does the frontend rely on the old detailed message text?** No. Grep of
`frontend/src` shows the download views use their own JS `Error.message` from
the api client, not the backend diagnostic `message` field, and nothing matches
on message substrings.

---

## 6. PR #6 lifecycle fault injection

Executed as a review-only harness. **PR #6 was not modified** — no defect was
found. 27 checks, all passing.

| # | Property | Evidence |
|---|---|---|
| 1 | In-flight worker completes safely after cancellation | worker `p0` held open, cancelled mid-flight, still reached `finished` and returned a usable result |
| 2 | Queued workers do not start after cancellation | 11 of 12 futures cancelled; `started-after-cancel == []` |
| 3 | DB health-write exception cannot prevent `stop_scan_flag` or `break` | with `ExplodingDB`: break engaged ✅, flag set ✅; identical with `db=None`; HTTP 200 resets the streak (no premature stop); 404/500 never trigger shared cancellation |
| 4 | Slot and `_running` release after worker/crawler/callback/DB exceptions | all four classes: slot released, `_running` cleared, slot immediately reacquirable |
| 5 | Next scan starts with a cleared stop event | flag cleared at scan start; a poisoned previous run does not block the next acquire |

**Method disclosure, so this is not over-claimed.** Property 4 exercises the
real `ScannerService.try_acquire_scan()` / `release_scan()` and the real
`threading.Event` lifecycle. Properties 1–2 use a real `ThreadPoolExecutor` and
the real `stop_scan_flag` property with a synthetic worker. Property 3 uses a
fragment that mirrors `scanner_service.py:655-690` line-for-line rather than
driving the live crawler, which would require a network scraper and event loop.
So: real primitives, mirrored control flow — not a full end-to-end crawl.

The structural reading holds independently: in the source, the health write sits
in its own `try/except: pass`, and `self.stop_scan_flag = True` plus `break` are
outside it, so no DB failure can swallow the abort.

---

## 7. Integration workflow results

All six rebuilt integrations merged cleanly and compiled on both interpreters.
Workflows dispatched with `fail-fast: false` (review-only branches):

| Branch | Run |
|---|---|
| `review/int2-pr3` | [29684377378](https://github.com/LstDtchMn/ScanHound/actions/runs/29684377378) |
| `review/int2-pr4` | [29684379822](https://github.com/LstDtchMn/ScanHound/actions/runs/29684379822) |
| `review/int2-pr5` | [29684382183](https://github.com/LstDtchMn/ScanHound/actions/runs/29684382183) |
| `review/int2-pr6` | [29684384265](https://github.com/LstDtchMn/ScanHound/actions/runs/29684384265) |
| `review/int2-pr7` | [29684386317](https://github.com/LstDtchMn/ScanHound/actions/runs/29684386317) |
| `review/int2-pr8` | [29684388447](https://github.com/LstDtchMn/ScanHound/actions/runs/29684388447) |

### Result: all six fully green

| Branch | 3.11 | 3.12 | frontend |
|---|---|---|---|
| `review/int2-pr3` | success | success* | success |
| `review/int2-pr4` | success | success | success |
| `review/int2-pr5` | success | success | success |
| `review/int2-pr6` | success | success | success |
| `review/int2-pr7` | success | success | success |
| `review/int2-pr8` | success | success | success |

**Every job on every branch passed.** All three blockers from the overnight
report are closed, and the whole combined project — CI-stabilization stack plus
each HDEncode layer — is green on both Python versions and the frontend.

\* One flaky failure, investigated and dismissed. `int2-pr3`'s first 3.12 job
failed on `tests/test_api_routes.py::TestResults::test_select_all_empty`
(`assert 15 == 0`). Evidence it is a pre-existing flake and not a regression:

- it passes alone (1 passed) and within its class (16 passed) locally on the
  same commit;
- `test_config.py` cannot pollute it — pytest collects `test_api_routes.py`
  **before** `test_config.py` alphabetically, so this round's only PR #3 change
  runs afterwards;
- it had never failed before, including the previous `int-ci-pr3` run whose only
  failure was the config test;
- **re-running the identical commit `a230c05` produced a fully green run**
  ([29684571501](https://github.com/LstDtchMn/ScanHound/actions/runs/29684571501),
  3781 passed, 4 skipped).

Root cause is visible in the failing log: a maintenance-loop thread starts
*during* the suite and calls `detect_moved_source_files` with a `None` database
(`AttributeError: 'NoneType' object has no attribute 'list_rename_jobs'`,
`service.py:1700`). A background thread mutating process-global state
concurrently with tests is what makes an "empty results" assertion racy.
Reported, not fixed — it is outside every PR in this stack and belongs to the
suite's own isolation, not to any change here.

For reference, the CI-stabilization stack itself was already fully green:
run [29681915627](https://github.com/LstDtchMn/ScanHound/actions/runs/29681915627)
— `test (3.11)` 3768 passed, `test (3.12)` 3768 passed, `frontend` 18/18.

---

## 8. Protections

- **No merges** into production branches; only the prescribed parent-into-child
  merges and review-only integration merges.
- **No force-pushes** — every push fast-forward.
- **No deployments**; the running container was never rebuilt or restarted.
- **Nothing marked ready** — all 11 PRs remain draft.
- **`main` unchanged** at `58feedf`; working checkout clean.
- **PR #3 deployment gate unchanged:** PR #3 deploys alone, HDEncode disabled,
  and one complete real background-scan cycle must show zero HDEncode listing,
  detail, Selenium and pipeline-search traffic before PRs #5–#8 advance. Nothing
  here advances that gate — but PR #3's own blocker, which previously argued
  against deploying it, is now fixed and verified.
