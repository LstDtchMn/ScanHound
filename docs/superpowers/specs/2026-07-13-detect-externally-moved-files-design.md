# Detect Externally-Moved Source Files — Design Spec

**Date:** 2026-07-13
**Status:** Author-authored under explicit user delegation ("plan and execute with minimum
input, I need to sleep") — normal brainstorming Q&A cadence skipped per that instruction;
all decisions below are my own judgment calls, documented for review on wake.

## Problem

User: "items that were moved directly in windows without the app are identified and
marked and then archived, too much clutter on the page." Today, a `rename_jobs` row
sitting at `needs_review`/`matched` (identified by ScanHound but not yet applied) has no
proactive check on whether its source file is still there. If the user manually moves,
renames, or deletes the file via Windows Explorer, the job silently becomes permanently
stale — it only surfaces as broken if someone eventually clicks Apply and gets "Source
file missing" (`backend/rename/service.py:1330-1343`, an existing but purely reactive
check). Until then it just clutters every list/filter/count on the Renames page
indefinitely.

## Architecture

**Real status vocabulary** (confirmed directly from `backend/rename/service.py`'s status
writes, not assumed): `needs_review`, `matched`, `applying` (transient, mid-move),
`applied`, `failed`, `reverted`. Detection scope is `status IN ('needs_review',
'matched')` — jobs that have been sitting idle, identified but untouched. `applying` is
excluded (in-flight or handled by the existing crash-recovery mechanism that restores a
stuck `applying` job to its `prior_status`); `applied`/`failed`/`reverted` are terminal
or already-resolved.

**Two-pass confirmation (false-positive protection).** A single missing-file check is
not trustworthy on its own: this app's Downloads/library paths include NAS/UNC-mounted
shares (per project history — `\\TURTLELANDSRV2\...`), and a transient SMB hiccup can
make `os.path.isfile()` return `False` for a file that's actually still there. Rather
than act on one negative check, add a nullable `rename_jobs.source_missing_since`
TIMESTAMP column:
- First maintenance pass where a job's `original_path` fails `os.path.isfile()`: if
  `source_missing_since` is NULL, set it to `CURRENT_TIMESTAMP`. Take no further action
  this pass.
- A later pass where the SAME job is checked again: if `source_missing_since` is already
  set AND the file is *still* missing, treat it as confirmed — this is genuinely gone,
  not a blip.
- If a job's file reappears at any point before confirmation, clear
  `source_missing_since` back to NULL (self-heals).

The maintenance loop's real interval is 3600s (1 hour) — two passes are naturally ~1 hour
apart, which is ample margin against a momentary network glitch, so no additional
minutes-based threshold is needed on top of "two passes."

**On confirmation:** call `db.update_rename_job(job_id, status="failed",
error_message="Source file was moved or deleted outside ScanHound")` (distinct wording
from the existing apply-time "Source file missing" message, so the two are
distinguishable to a human reading history), then `db.archive_rename_jobs([job_id])`
— reusing the archive feature built earlier this session verbatim, including its
existing `applying`-status exclusion safety net. This mirrors the already-shipped
"auto-archive immediately on apply" pattern for the successful-outcome case; this is the
same declutter action for the doesn't-need-review-anymore-because-the-user-already-
handled-it-themselves case. The job is not gone — it's in the existing Archived tab
with a real, honest error message explaining why, restorable like any other archived job.

**Why reuse `status="failed"` rather than a new status value:** an auto-archived job is
immediately excluded from every live count/filter (same `dismissed`/`archived_at`
exclusion the archive feature already established), so the reuse is invisible in the
main UI. Inventing a new status value would require every status-aware surface
(dashboard StatCards, tests, frontend types, the pipeline tracker's own status mirroring)
to learn about it for a case that's identical in every visible respect to an ordinary
failed-and-archived job except its error text. YAGNI: reuse, don't proliferate.

**Detection function:** `RenameService.detect_moved_source_files() -> dict` (new method,
`backend/rename/service.py`) — queries eligible jobs via the existing
`DatabaseManager.list_rename_jobs(status=..., archived=False)` (called once for
`"needs_review"`, once for `"matched"`, `limit=100000` to cover the whole backlog — no
new DB read method needed, this one already exists exactly as needed), runs the
two-pass logic per job, returns `{"checked": N, "confirmed_missing": M}` for logging.
Hooked into `_run_maintenance_pass()` (`backend/app_service.py`, same location as the
pipeline reconcile and DV unmapped-path checks), gated by a new config key
`rename_detect_moved_files_enabled` (default `True`), following the exact pattern
`pipeline_reconcile_enabled` already established. New Settings UI checkbox in the
existing Renaming settings section, `SettingsUpdate` field added
(`backend/api/routes/settings.py`).

## Data flow

`_run_maintenance_pass()` → `RenameService.detect_moved_source_files()` → for each
eligible job: `os.path.isfile(original_path)` → set/clear `source_missing_since` OR
(on confirmation) `update_rename_job(status='failed', ...)` + `archive_rename_jobs([id])`
→ existing WS broadcast (`self._broadcast(job_id)`, matching every other status-changing
call in `service.py`) keeps an open Renames tab live, same mechanism the archive
feature's live-sync already uses.

## Error handling

Per-job try/except inside the detection loop — one job's unexpected error (permission
denied on the stat call, malformed path) must not abort the batch, following
`reconcile_batch`'s established per-item-failure-isolated pattern. A failed check on one
job simply skips it for that pass (retried next pass) rather than crashing the whole
maintenance loop.

## Testing

- Unit: two-pass confirmation state machine (first miss → sets timestamp, no action;
  second miss → confirmed, archived; file reappears between passes → clears timestamp,
  no action ever taken). `applying`-status jobs are never touched. A job whose file
  genuinely still exists is never touched across any number of passes.
- Integration: `_run_maintenance_pass()` invokes the detector when
  `rename_detect_moved_files_enabled` is true (default), skips when explicitly disabled.
- Settings: round-trip test for the new key (mirrors the existing
  `test_dv_settings.py` pattern).
- DB: guarded-ALTER migration test (idempotent re-init), `source_missing_since` in
  `_RENAME_FIELDS` allowlist (the exact gap the archive feature's own postmortem
  flagged — a field missing from that tuple silently no-ops).

## Out of scope

- Detecting files moved/renamed WITHIN the watched folder tree (still findable by a
  rescan) — this feature is specifically for files that vanished from where ScanHound
  last knew them, full stop, not a smarter re-match.
- Any UI surfacing beyond the existing Archived tab (no new badge/notification for this
  specific archive reason — the error_message text is suffient explanation on inspection).
- Applying the same two-pass logic to `applied` jobs' destination_path (a separate,
  already-existing concern — Plex library integrity — not this feature's job).
