# Desktop Rename-Conflict Resolution — Design Spec

**Date:** 2026-07-10
**Status:** Draft for review

## Problem

When an auto-rename would land on a destination that already has a file, the job
is held as `needs_review` with a `warning_message`. On desktop, `RenameRow.svelte`
renders that raw string into a single truncated line
([RenameRow.svelte:61-64](../../../frontend/src/lib/components/renames/RenameRow.svelte)):

> A file already exists at the destination: /library/movies-4k/The Threesome (2025)/The Threesome (2025) [2160p].mkv (existing 14359773138 bytes vs. candidate 14359773138…

This is poor because it (1) prints raw byte counts instead of human sizes, (2) shows
two identical 11-digit numbers where "same size" is the single most useful fact and
it's buried, (3) truncates so the real content hides behind a hover tooltip, and (4)
crams machine data (path, bytes) together with human advice.

Meanwhile a full, polished conflict-comparison UI **already exists** and is used by
the mobile review deck — it is simply never offered on the desktop row:

- Backend `POST /jobs/{job_id}/conflict-preview`
  ([rename.py:270](../../../backend/api/routes/rename.py), `service.conflict_preview`
  [service.py:1554](../../../backend/rename/service.py)) probes the on-disk existing
  file and the incoming source and returns `{existing, incoming, recommended, reason}`
  with per-file `FileSpec` (resolution/HDR/DV/codec/audio/bitrate/size/duration). It
  already handles the pure on-disk-library-file case, returning `existing.present: false`
  when the destination is actually free.
- Backend `POST /jobs/{job_id}/scan-dv-conflict` does the on-demand FEL/MEL layer probe.
- Frontend `conflictView.ts` (`specRows`, `formatBytes`, `needsDvScan`) +
  `RenameReviewCard.svelte` render that comparison with per-row "better" highlighting,
  the recommendation, and Overwrite / Keep both / Skip actions.

## Goals

1. Replace the raw truncated string on the desktop row with a compact, human-readable
   conflict summary that leads with the useful fact and flags a likely duplicate.
2. Give the desktop row a **Compare** action that opens the existing `RenameReviewCard`
   comparison in a modal, with the same resolve actions (Overwrite / Keep both / Skip)
   mobile already has.
3. Improve the backend `warning_message` itself (human sizes, same-size detection,
   drop the redundant path) — it still surfaces in notifications, mobile, and tooltips.
4. Keep behavior safe: byte-identical / same-size files are still flagged for review,
   never silently dropped — just labelled clearly so they are a one-glance skip.

## Non-Goals

- No change to the conflict *resolution* behavior (overwrite still trashes-not-deletes,
  keep-both still dedupes the name, skip still holds for review).
- No auto-resolution of identical files (explicitly deferred — "flag but make it obvious").
- No change to the mobile review deck's layout (it already has this card).
- No inline row expansion — the comparison opens in a modal (decided).

## Design

### 1. Backend — human-readable, structured conflict message

**File:** `backend/rename/service.py`, the collision branch at
[service.py:1322-1342](../../../backend/rename/service.py) (the `else:` that holds the
job for review).

Today it builds:
```
A file already exists at the destination: {dst} (existing {existing_size} bytes vs. candidate {candidate_size} bytes) — review to replace or keep the existing file.
```

Replace the size portion with a human-readable, lead-with-the-fact summary. A new
private module helper formats bytes with the **same** KB/MB/GB/TB, 1-decimal logic the
frontend `formatBytes` uses (so the two never disagree):

```python
def _fmt_size(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    units = ["KB", "MB", "GB", "TB"]
    v = n / 1024.0
    i = 0
    while v >= 1024 and i < len(units) - 1:
        v /= 1024.0
        i += 1
    return f"{v:.1f} {units[i]}"
```

New message shapes (path dropped — the row already shows the title and destination):

- Both sizes known, **equal**:
  `"A copy is already in the library at the same size ({size}) — likely a duplicate. Review to replace or keep."`
- Both sizes known, **different**:
  `"A copy is already in the library (existing {existing} vs. new {candidate}). Review to replace or keep."`
- Size unreadable (the existing `except OSError`): fall back to
  `"A copy is already in the library. Review to replace or keep."`

**Cross-cutting constraint (must not break mobile):** `hasDestinationConflict()`
([review.ts:46-49](../../../frontend/src/lib/renames/review.ts)) currently detects the
on-disk conflict by `/already exists/i.test(warning_message)`. The reworded strings no
longer contain "already exists", so string-sniffing must be replaced by a structured
signal (see §2) rather than left to regex-match new prose.

### 2. Backend — durable structured conflict signal

Both the desktop and mobile conflict gates should key off a structured field, not the
message prose. Add one nullable, additive column to `rename_jobs`:

- `conflict_kind TEXT` — set to `'destination_exists'` in the collision branch
  (§1) at the same time the warning is written; left `NULL` otherwise. Additive-only,
  no rebuild, mirrors the pipeline feature's additive-column migrations. Cleared back
  to `NULL` whenever the job leaves the conflict state (a successful apply via
  overwrite/keep-both, or a rematch to a new destination) so a stale marker never
  lingers.

Expose it on the jobs list: `list_jobs` ([rename.py:131](../../../backend/api/routes/rename.py))
already annotates each job; include `conflict_kind` in the serialized job (it is a
plain column, so `get_rename_job`/`list_rename_jobs` carry it once the column exists —
verify the row→dict serialization includes new columns, add if not).

Add `conflict_kind?: 'destination_exists' | null` to the `RenameJob` type
(`frontend/src/lib/api/types.ts`).

Update `hasDestinationConflict()` to prefer the structured signal, keeping the old
checks as backward-compat for rows written before the migration:
```ts
export function hasDestinationConflict(job: RenameJob): boolean {
  if (job.conflict_kind === 'destination_exists') return true;
  if (job.destination_conflict) return true;
  return /already exists/i.test(job.warning_message ?? '');
}
```

### 3. Frontend — desktop conflict summary + Compare action

**File:** `frontend/src/lib/components/renames/RenameRow.svelte` (the
`{:else if job.warning_message}` branch, [RenameRow.svelte:61](../../../frontend/src/lib/components/renames/RenameRow.svelte)).

When `hasDestinationConflict(job)` is true, instead of the raw truncated warning:

- Render a compact, color-coded conflict line: a small ⚠ "Conflict" chip, the
  human-readable summary (reuse the improved `warning_message` directly — it is now
  clean), and a subtle "likely duplicate" chip when the message indicates same-size.
  (The chip can key off the same structured info; if a same-size boolean is worth
  exposing, add `conflict_same_size?: boolean` alongside `conflict_kind` — otherwise the
  message text carries it. Decide in the plan; a boolean is cleaner than parsing prose.)
- Render a **Compare** button next to the existing Apply / Rematch actions.

Non-conflict `warning_message` (e.g. a year-mismatch note) keeps today's plain
truncated-line rendering — this change is scoped to conflict rows.

### 4. Frontend — desktop conflict modal

A new thin wrapper mounts the **existing** `RenameReviewCard` inside the shared
`ModalOverlay` (the same modal pattern used by `SourceSearchModal`/`RematchModal`):

- On open, call `conflict-preview` for the job (mirror how the mobile deck loads it),
  render `RenameReviewCard` with `existing`/`incoming`/`recommended`, the `specRows`
  table, and the "Scan DV layers" affordance (`needsDvScan` → `scan-dv-conflict`).
- Resolve actions map to the existing apply-with-strategy endpoint
  (`POST /jobs/{job_id}/apply` with `conflict_strategy` `overwrite` | `keep_both` | `skip`):
  - **Overwrite** → existing file trashed (recoverable), incoming placed.
  - **Keep both** → incoming placed under a deduped sibling name.
  - **Skip** → close, leave held for review.
- On success, close the modal and let the jobs list refresh (reuse the existing
  post-apply refresh/broadcast path the row's Apply already uses).

Prefer reusing whatever load-and-resolve logic the mobile `RenameReviewDeck` already
has around `RenameReviewCard`; if that logic is deck-coupled, extract the shared piece
so desktop and mobile drive the same card the same way (DRY — do not fork a second copy
of the resolve wiring).

## Data flow

```
apply-time collision (service.py)
  → sets status=needs_review, conflict_kind='destination_exists',
    warning_message="A copy is already in the library at the same size (13.4 GB) …"
  → list_jobs serializes conflict_kind
  → RenameRow: hasDestinationConflict(job) === true
       → renders ⚠ Conflict summary + "likely duplicate" chip + [Compare]
  → [Compare] → modal → conflict-preview → RenameReviewCard (existing vs incoming)
       → Overwrite / Keep both / Skip → apply(conflict_strategy) → refresh
```

## Testing

- **Backend:** unit-test `_fmt_size` (boundaries: <1 KB, exact 1024, GB, TB) and the
  three message shapes (equal / different / unreadable). Test that the collision branch
  sets `conflict_kind='destination_exists'`, and that a subsequent successful
  overwrite/keep-both/rematch clears it back to `NULL`. Test `conflict_preview` still
  returns `existing.present:false` when the destination is free (regression).
- **Frontend:** unit-test the updated `hasDestinationConflict()` (structured signal wins;
  legacy prose + `destination_conflict` still detected) in `review.test.ts`. Extend
  `conflictView.test.ts` only if new formatting logic is added there. Component wiring
  (modal open, resolve calls) is verified via `npm run check`/`build` + a live browser
  pass, per this repo's convention (no Svelte component test harness).
- **Migration:** additive `conflict_kind` column — verify it applies cleanly on the live
  DB with no data loss (dry-run pattern), same as prior additive migrations.

## Rollout

Additive schema change + UI wiring; deploy via `docker compose up -d --build`. No
backfill needed — pre-existing conflict rows fall back to the legacy `/already exists/`
detection until they're re-applied. Because the reworded message no longer says
"already exists", any pre-migration conflict row keeps working via that same
backward-compat branch (its old message text is unchanged on disk), and new rows use
the structured signal.

## Resolved decisions (reviewer-approved 2026-07-10)

1. **"Likely duplicate" chip uses an explicit `conflict_same_size` boolean** from the
   backend — no prose parsing on the frontend. Add `conflict_same_size` as a second
   additive nullable column on `rename_jobs` (set alongside `conflict_kind` at collision
   time: `True` when `existing_size == candidate_size`, `False` when both are known and
   differ, `NULL` when a size was unreadable), serialized on the jobs list and typed on
   `RenameJob`. The desktop "likely duplicate" chip renders when `conflict_same_size === true`.
2. **Same-size chip is desktop-only for now.** Mobile already shows the Size row in its
   compare table; no mobile chip this round.
