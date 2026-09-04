# TST-2 evidence — 2026-09-04

Worktree `C:\Users\NLSur\AppData\Local\Temp\tst2`, branch `fix/tst2-queue-order-dependence` off `main` @ `0a2751d`, head `458c3db`.

## 1. Baseline and reproduction (fresh tree)

```
pytest tests/test_queue_review_followups.py                                   -> 68 passed
pytest <22 predecessor files> tests/test_queue_review_followups.py            -> 2 failed, 590 passed
   FAILED ...::TestPacingThrottlesTheMachineNotTheOperator::test_the_exemption_is_spelled_the_same_way_retry_item_writes_it
   FAILED ...::TestPeerReviewFixes::test_retry_ready_does_not_bypass_pacing
   backend/download_queue.py:338  DownloadQueueSourceHeld: The source is temporarily paused.
```

The 22 predecessors: the 14 files matching `grep -lE "scrape_links|record_scrape_outcome" tests/*.py` (the earlier review said sixteen; the grep returns fourteen on this tree) plus every `tests/test_*queue*.py`.

## 2. Bisection

Delta debugging by file: 22 → 1 (`tests/test_scrape_outcomes.py`). By node id within it: 1 test, `test_challenge_iframe_signal_drops_path_query_and_fragment`.

```
pytest tests/test_scrape_outcomes.py::test_challenge_iframe_signal_drops_path_query_and_fragment tests/test_queue_review_followups.py
   -> 2 failed, 67 passed      (confirmed at first hand by the supervisor)
pytest tests/test_queue_review_followups.py
   -> 68 passed
```

## 3. The leaked state, with proof

Setter: `backend/download_service.py:~2520` `get_hdencode_coordinator().observe_challenge()` → `backend/hdencode_coordinator.py:478-491` sets `_local_cooldown_until = now + 1h` on `_COORDINATOR` (`:596`). Reader: `backend/download_queue.py:335-341`. Non-reset: `configure()` (`:172-201`) resets only on config/db identity change; the queue never calls it.

Probe tests (deleted after use) run immediately after the predecessor:

```
local_cooldown_until= 2026-09-04 17:01:43.428397+00:00  local_cooldown_reason= interactive_challenge
SNAPSHOT: {'blocked': True, 'state': 'cooldown', 'reason_code': 'interactive_challenge', ...}
fresh DownloadQueueService._assert_hdencode_available() -> raised DownloadQueueSourceHeld
fields cleared on the same singleton, same call again -> NO EXCEPTION; blocked False
```

## 4. Fix and regression

`tests/conftest.py::_fresh_hdencode_coordinator_per_test` (autouse): fresh coordinator per test. `tests/test_hdencode_coordinator_isolation.py`: forward 3 passed; reverse (`test_b` then `test_a`) 2 passed; `test_c` alone: 1 skipped (by design).

## 5. Mutant (whole-tree copy, fixture neutered)

```
isolation file -> KILLED | 2 failed, 1 passed | failed: test_b..., test_c...
minimal repro  -> KILLED (leak is back) | 2 failed, 67 passed | failed: the two original victims
control: 3 passed / 69 passed | real root exists: False
```

## 6. Suites

22 predecessors + victims + regression, in the order that used to fail: 595 passed. 13 files naming `hdencode_coordinator`: 196 passed.

Full suite on a combined copy (#110 @ 6ae62dc trash isolation + this change, so the host is never written):

```
real root before: absent           2026-09-04T16:19:43Z
1 failed, 5462 passed, 5 skipped in 570.79s (0:09:30)
guard firings: 0
real root after:  absent           2026-09-04T16:29:18Z
FAILED tests/test_dv_host_scan.py::test_post_rows_ignores_ambient_proxy
   dv_host_scan.py:383 dv-host-rows POST failed: [WinError 10053] An established connection was aborted
```

The one failure is NOT TST-2. It is the second occurrence in five full Windows runs of a local-socket abort in `tests/test_dv_host_scan.py` (a different test each time: `test_post_rows_direct_success_delivers_key` on 2026-09-03, this one today), each passing alone and with its file afterwards (see below). Recorded as an undiagnosed observation, TST-3 candidate, not labelled a flake. The two TST-2 victims passed in this run.

CI on `458c3db` (ubuntu-latest): Tests workflow green on Python 3.11 and 3.12, frontend green. CI VERIFIED.

## 7. Adversarial read (Opus), before commit

Reachability confirmed (getter reads the module global by name; two services cache on `self` at construction, safe under function-scoped fixtures only; stated in the docstring). No scoped fixtures in tests/. Fresh instance: locks and dicts only. Three changes required and made: test_c compared ids (address reuse could fail a working fixture) → holds objects and skips alone; reachability sentence corrected; `monkeypatch.undo()` trap noted. Separate latent production finding filed: `configure()` identity reset + `DetailScraper` reconfiguring with `db=None` through bridges without `db`; second path in `background_scanner.py`.
