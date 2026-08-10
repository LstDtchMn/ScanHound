# DV scan fix — deploy checklist (Jesse runs this; nothing here has been run)

Branch `agent/dv-scan-hang-and-starvation`, head `1c2a64c`, base `main` `6813260`.

Steps are ordered because step 1 is what stops CI failing on three unrelated
tests, and step 5 is the only one that proves the whole point of the change.

---

## 1. Merge the date bomb FIRST

`main` currently fails three `tests/test_queue_recovery_policy.py` tests on a
hard-coded date, on `main` itself — measured, not assumed:

```
main   6813260 : 3 failed, 4626 passed
branch 1c2a64c : 3 failed, 4656 passed   <- same three
```

Merge `fix/queue-policy-test-time-bomb` (`a88d541`) before this branch, or CI
will red on the merge and the cause will look like this change.

## 2. Merge this branch

```bash
gh pr create --base main --head agent/dv-scan-hang-and-starvation --fill
```

`gh pr merge` BLOCKS in this environment — merge from the GitHub web UI.

## 3. Deploy the container

Only the container half needs deploying (`backend/rename/dv_detect.py`,
`process_control.py`). Run in the BACKGROUND — this build has exceeded 10
minutes before:

```bash
cd "X:/Docker Apps/ScanHound" && docker compose up -d --build
```

## 4. The host detector needs NO deploy, but DOES need the repo updated

`scripts/host-detector/dv_host_scan.py` runs from the working tree on
TurtleLandSRVR, not from the image. It takes effect on the next scheduled run
as soon as `main` is checked out there.

**Check first:** the main worktree was on `fix/dv-scan-live-progress` with
uncommitted changes to `run-dv-scan.ps1` (another session's live-progress work,
which is complementary — it makes the wrapper stream output, while this branch
makes the detector actually emit any). Do not clobber it:

```bash
cd "X:/Docker Apps/ScanHound" && git status
```

## 5. VERIFY THE POINT OF THE CHANGE — the import must actually land

This is the step that matters. The whole defect was that results never reached
the container, so a deploy that does not move this number has not fixed
anything.

```bash
docker exec scanhound python -c "import sqlite3; c=sqlite3.connect('file:/dbvol/crawler.db?mode=ro',uri=True); print(c.execute(\"select count(*), max(scanned_at) from dv_scan where source='scan'\").fetchone())"
```

- **Before:** `(466, '2026-07-25 16:45:35')` — unchanged for two weeks.
- **After a completed run:** the count should jump toward the host DB's total
  (498+ and climbing) and `max(scanned_at)` should be today.

If the count has NOT moved, the run still is not reaching `_post_import()` and
the fix did not take.

## 6. Confirm the run now ends cleanly instead of being killed

```powershell
(Get-ScheduledTaskInfo -TaskName 'ScanHound-DVScan').LastTaskResult
```

- `0x41301` = still running.
- `0` = **completed normally** — the new behaviour, and what step 5 depends on.
- Anything else = read `data/dv-scan-logs/dv-scan-*.log`; the detector now logs
  one line per file, so a stalled run is visible rather than silent.

## 7. Watch for the log line that proves the watchdog works

A wedged file should now produce `error=stalled` after ~180 s, not a 1800 s
`timeout`:

```
[n/m] -> unknown (stalled) in 300s ...
```

---

## Rollback

Everything is additive and reversible.

- **Code:** revert the merge commit; the host detector reverts by checking out
  the previous `main`.
- **Schema:** three added columns (`attempts`, `last_error`, `next_retry_at`).
  The old code never selects them, so an old build reads the table fine —
  no migration to undo.
- **Tonight's data writes:** the two hand-written FEL rows and any rows the
  bounded sweep added are all proven-FEL with real signatures. To undo a
  specific one, set `dv_layer='unknown'` and both signature columns to NULL.
  Prior values for the first two are in `scratchpad/dv_host_rows_before.json`,
  and every sweep write is recorded in `scratchpad/sweep_results.jsonl`.

## Not in this deploy

- **`--mode steady` is not wired into the scheduled task.** The task still runs
  one 4-hourly job doing both backfill and steady-state. Splitting it is a task
  definition change and needs your call.
- **Coverage.** The detector is configured for four roots holding 730 files,
  while ~3,370 distinct 4K movie filenames are known to the seed and only 460
  have ever had a real detection. That is a scope decision, not a bug in this
  change.
