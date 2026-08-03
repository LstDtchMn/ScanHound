# B1 — historical rename evidence: limitations note (contract N-1)

**Date:** 2026-08-03 · **Author:** Claude · **Status:** the short note plan
rev 2 (R2) reduced B1 to. It records what the historical data CAN and CANNOT
say, so nobody re-derives a reliability rate from it. It gates nothing —
per R2, B7 no longer waits on historical analysis.

## What the historical record is

`rename_jobs` is a **single mutable row per job, not an event ledger**. A row
shows the job's CURRENT state and paths; every intermediate transition,
retry, and the filesystem's state at failure time were overwritten or never
recorded. The append-only file-operation ledger (safety-gate step 2,
`0b0a398` on `agent/rename-safety-gate`) exists precisely because of this —
it applies FORWARD only.

## What may be read from pre-ledger rows

Current status; present path values; `move_method` where populated; current
error and collision categories; timestamps; package grouping; and a WEAK
same/cross-volume inference from current path mappings.

## What must NOT be inferred, and why

* **No success or failure RATE.** The historically quoted `69/158` spans
  unknown commit ranges — including commits whose defects are now fixed on
  main (`70dca70` no-replace placement, `44ea7ba` durable trash) — with
  unknown retry counts folded into single rows. A rate computed from that
  mixes at least three different programs under one denominator.
* **No per-cause attribution.** Attempt counts, transition history, volume
  topology at failure time, and post-failure filesystem state are not
  recoverable from any surviving source (checked: retained logs, DB backups,
  prior review artifacts — plan rev 2.1 B1 already recorded the gaps).
* **No data-loss claims.** The rev 2.1 correction stands: the earlier
  "reproduced TOCTOU data-loss defect" claim was withdrawn as
  commit-unpinned; B2's re-run (`62c3dae`) showed pre-fix code loses data
  and current code does not, under controls.

## The one sentence to carry forward

The freeze is *"no known live data-loss defect; rename stays disabled until
the fixes are demonstrated under the real storage topology and interruption"*
(B5/B6) — historical rows are colour, never evidence, and every future
reliability figure starts from the append-only ledger's first entry.
