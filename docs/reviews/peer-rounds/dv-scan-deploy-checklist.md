# DV scan fix — deploy checklist (Jesse runs this; nothing here has been run)

Branch `agent/dv-scan-hang-and-starvation`, base `main` `6813260`. Re-check the
head SHA with `git rev-parse HEAD` rather than trusting a number written here.

The order is load-bearing throughout. Step 1 stops CI failing on three
unrelated tests; step 5 is the only step that proves the defect is actually
fixed; and section 8 must not begin before step 3, because the safe label-sync
default does not exist until the container is rebuilt.

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

The container half is `backend/rename/dv_detect.py`, `process_control.py` AND
`backend/api/routes/rename.py` -- that last one carries the additive-only
label-sync default, which everything in section 8 depends on. Run in the BACKGROUND — this build has exceeded 10
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

## 8. THEN the bounded-FEL write — in this order, and not before

The order is a real dependency, not a preference. **The safe-by-default label
sync exists only after step 3.** Until this branch is deployed, the running
container still has `/dv-sync-labels` defaulting to a destructive full
reconciliation, so the sync step must not be reached on the old code.

Writing rows changes no labels by itself, and importing them changes no labels
by itself — only the sync does. That is what makes this ordering safe rather
than merely tidy.

### 8a. Confirm the deployed code actually has the safe default

Not "we merged it" — check the running container:

```bash
docker exec scanhound python -c "from backend.api.routes.rename import DvSyncRequest; print('additive_only default =', DvSyncRequest().additive_only)"
```

Must print `True`. If it prints `False`, the deploy did not take and **stop
here** — everything below assumes it.

### 8b. Re-run all four gates on the FINAL frozen set

The sign-off covers the gate *design* and the 694-positive population, not
whatever the completed probe produces. Re-run immediately before writing:

```bash
python scripts/stage_fel_write.py
```

Exit 0 is the gate. It exits 1 on any unmatched row lacking a recorded reason —
the invariant is **zero unexplained mismatches**, not a coverage percentage.
Check the printed accounting reconciles: rows intended for Plex effect ==
rollback snapshot population.

### 8c. Write, import, and dry-run BEFORE applying

```bash
# 1. write the staged rows into data/dv_host.db   (rows only; no labels move)
# 2. import them into the container
curl -X POST http://localhost:9721/rename/dv-import
# 3. DRY RUN the label sync and read the summary
curl -X POST http://localhost:9721/rename/dv-sync-labels -H 'Content-Type: application/json' -d '{"dry_run": true}'
```

The dry-run response is the authoritative proof of the live path. Expect
roughly `added ≈ the staged count`, and **`removed: 0`**. Any nonzero
`removed` is a stop condition — investigate before applying, because the whole
premise of the gate work is that this batch is pure additions.

### 8d. Apply

Only once the dry run reads as expected. Re-run the same call with
`{"dry_run": false}`.

### 8e. Post-write reconciliation (the completion criterion)

Keep `label_snapshot.json` until this passes. Compare expected additions
against actual Plex labels and require:

```
unexpected removals            = 0
unexpected managed-label changes = 0
unexplained missing additions  = 0
```

The explained no-Plex-target rows must remain non-targets, not be counted as
failed label writes.

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
  Prior values for the first two are in `docs/reviews/peer-rounds/dv-evidence-2026-08-10/dv_host_rows_before.json`,
  and every sweep write is recorded in `docs/reviews/peer-rounds/dv-evidence-2026-08-10/sweep_results.jsonl`.

## Not in this deploy

- **`--mode steady` is not wired into the scheduled task.** The task still runs
  one 4-hourly job doing both backfill and steady-state. Splitting it is a task
  definition change and needs your call.
- **Coverage.** The detector is configured for four roots holding 730 files.
  Measured 2026-08-10 against the LOCAL 4K drives on the scanning host
  (`C:K Drives\...`, junctions to A:/E:/G:/I:/J:/Q:/R:/U:): **2,827 distinct
  movies >= 15 GB, of which only 87 had ever been DV-scanned**. Those drives are
  almost entirely disjoint from the configured roots, and Plex addresses them by
  the junction path. Widening the config to cover them is a scope decision, not
  a bug in this change.
- **Already closed, so do not re-litigate:** the 197 files in the plain `4K`
  folders beside the scanned `4K DV` folders are genuinely non-DV (12-file
  random sample, 0 with any RPU), and `4K HDR Colombo` is 11,547 TV files.
