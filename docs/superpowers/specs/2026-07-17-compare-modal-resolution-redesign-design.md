# Compare Modal — Resolution Redesign (Keep Plex / Keep Downloaded / Keep Both)

**Date:** 2026-07-17
**Status:** Design — awaiting user review
**Area:** Renames → Compare modal (`ConflictModal` → `RenameReviewCard`), rename `apply()` backend

## Goal

Make the desktop (and mobile) duplicate-comparison view state plainly **which
file is already in Plex** and **which was just downloaded**, and let the user
resolve the conflict by an explicit, plain-language choice — *Keep the Plex
copy* / *Keep the downloaded copy* / *Keep both* — instead of decoding the
current Overwrite / Keep both / Apply anyway / Skip buttons. Achieve full
parity so "Keep the downloaded copy" works for **both** conflict kinds.

## Background — current behavior

The Compare button opens `ConflictModal`, which renders `RenameReviewCard`.
The card fetches `conflict_preview(job_id)` and shows an **Existing vs
Incoming** spec table (Resolution, HDR/DV, Video, Audio, Bitrate, Size,
Duration), starring the recommended-keep column. Below it, action buttons vary
by the conflict's `kind` (`actionsForKind`):

| `kind` | Meaning | Buttons today |
|---|---|---|
| `same_path` | A file already occupies the **exact** destination path | Overwrite · Keep both · Skip |
| `library_duplicate` | This title exists **elsewhere** in the library (movies-only) | Apply anyway · Skip |

Backend `apply(job_id, conflict_strategy)` handles a collision **only when
`os.path.lexists(dst)`** (the `same_path` case):
- `None`/`'skip'` → hold for review, touch nothing
- `'overwrite'` → trash the occupant of `dst` (recoverable), place incoming
- `'keep_both'` → place incoming under a deduped sibling name

For a `library_duplicate` the computed `dst` is **free** (the dup lives at a
different path), so none of the collision branches fire — a plain apply just
places the download as a second copy. That is why "Apply anyway" == default
apply, and why there is no one-click "keep only the download" for that kind.

## Problems being solved

1. **"Existing"/"Incoming" is jargon.** In every case `existing` = the copy
   already in the Plex library and `incoming` = the just-downloaded file, but
   the UI never says so and shows no filename/source.
2. **The choice is implicit in button names.** Overwrite = keep downloaded,
   Skip = keep Plex, Keep both = keep both — the user must decode this, and
   the mapping differs between the two kinds.
3. **Buttons read as "grayed but clickable."** Every button is
   `disabled:opacity-50` and gated on `busy || $applyActive`; when enabled the
   primary actions are still low-contrast (accent cyan / gray outline) so they
   read as disabled. A stale apply-queue (`$applyActive` stuck true) also dims
   the whole row.
4. **No parity.** "Keep only the downloaded copy" is impossible for a
   `library_duplicate` in one click.

## Design

### 1. Column labels & source anchoring

Rename the two data columns and anchor them with source + filename:

- **In Plex** — sublabel `current library copy`, badge `📀 In your library`,
  and the existing file's basename (`existing.original_filename`) shown small
  and truncated under the header.
- **Downloaded** — sublabel `new file`, badge `⬇ Just downloaded`, and the
  incoming basename (`incoming.original_filename`).

Keep the ★ recommended-keep highlight (green column + reason line) exactly as
today; it now sits under the clearer headers.

### 2. Explicit choice model

Replace the variable button row with **three radio option cards**, always the
same three regardless of kind, each mapping to the correct backend strategy
for the current `kind`:

| Choice (user sees) | `same_path` strategy | `library_duplicate` strategy | Effect |
|---|---|---|---|
| **Keep the Plex copy** | `keep_plex` *(new)* | `keep_plex` *(new)* | Do not import the download; leave the library file. **Archive** the job (resolved — stops nagging) AND move the redundant download to recoverable trash. |
| **Keep the downloaded copy** | `overwrite` | `replace_library_dup` *(new)* | Trash the library file (recoverable), place the download. |
| **Keep both** | `keep_both` | *(plain apply)* | Keep the library file; add the download as a second copy. |

### 2a. "Keep the Plex copy" cleanup (`keep_plex`)

Confirmed decision (2026-07-17): keeping the Plex copy makes the downloaded
file dead weight, so this choice cleans it up inline, safely and reversibly:

1. **Archive the job first** (status resolved) — this also removes it from the
   `detect_moved_source_files` scan set, so trashing the source next can't be
   misread as an external move.
2. **Move `job.original_path` (the downloaded file) to recoverable trash**
   (`_fileops._trash()`), never a hard delete — restorable from the existing
   Trash panel. Trash the media file itself, not its enclosing package folder
   (siblings/extras untouched).
3. **Best-effort mark handled** so the download doesn't resurface: clear/settle
   its pipeline verdict (or remove its `downloads`/download-result row if one
   maps to this job). Since the kept copy is already in Plex, the pipeline will
   in any case categorize the title as present, not re-grab.

Data safety: the download lands in recoverable trash (undoable via Trash
panel); a one-click "undo keep-Plex" is **not** in scope — Trash-panel restore
plus un-archive covers recovery. If the trash step fails, the archive still
succeeds and a warning is surfaced (the library copy is untouched either way).

Pre-selection from `preview.recommended`:
- `'existing'` → **Keep the Plex copy**
- `'incoming'` → **Keep the downloaded copy**
- `'tie'` / `null` (incl. degraded probe) → **Keep both** (non-destructive default)

A single **primary "Apply choice"** button (solid, high-contrast — never reads
as disabled) runs the selected strategy; a quiet **Cancel** closes. The
primary button shows a `Working…` label while an apply is in flight rather
than only dimming, so it never reads as broken.

Each option card carries a one-line consequence caption (e.g. *"The current
library file moves to recoverable trash."*) so the outcome is explicit before
the user commits.

### 3. New backend strategy: `replace_library_dup`

Add a `conflict_strategy == 'replace_library_dup'` branch to `apply()`, valid
only when the job is a movie `library_duplicate`:

1. Re-locate the existing library file: `find_library_duplicate(job,
   db.list_plex_cache_movies())` → `translate_plex_path(match["file_path"],
   cfg["plex_library_path_mappings"])`. If no duplicate is found (state
   changed since preview), fail safe: hold for review, touch nothing.
2. `trashed_lib = _fileops._trash(lib_path)` — recoverable trash, never a hard
   delete. Record the trashed source path on the job (new nullable column
   `conflict_replaced_path`, storing `lib_path`) so undo can restore it — the
   trashed file is **not** at `dst`, so the existing dst-keyed restore in
   `undo()` cannot find it otherwise.
3. Place the incoming file at the (free) `dst` via the normal placement path.
4. **Restore-on-failure:** if placement fails after the library file was
   trashed, restore `lib_path` from trash (mirror `_restore_overwritten_
   original`, but keyed on `lib_path`, not `dst`) so the library is never left
   short a file. `apply()` returns `ok: False` and never leaves the job
   `applying`.
5. **Undo symmetry:** extend `undo()` so that when `conflict_replaced_path` is
   set, after removing the placed file it restores the most-recent restorable
   trash entry whose `original_path` matches `conflict_replaced_path` (in
   addition to / instead of the current dst-keyed lookup). Surface a
   `restore_warning` if that restore fails, same pattern as the overwrite case.

`overwrite` / `keep_both` / `skip` behavior is unchanged.

### 4. Frontend wiring

- Extend `applyJob(id, strategy)` and `api.applyRename` `conflict_strategy`
  union with `'replace_library_dup'` and `'keep_plex'`.
- A small pure helper `strategyForChoice(kind, choice)` (unit-tested, in
  `conflictView.ts`) returns the strategy string per the table above. The card
  computes the three choices from `preview.kind`; the option→strategy mapping
  lives in the helper, not inline in markup.
- `ConflictModal` passes an `onChoose(strategy)` callback that runs
  `applyJob(job.id, strategy)` (or plain `applyJob(job.id)` for the Keep-both /
  library_duplicate case) through the existing `act()` wrapper.

### 5. Affordance / grayed-button fix

- Primary "Apply choice" button: solid `--accent` fill, white text, obvious
  hover, and a `Working…` busy label — disabled **only** while this card's own
  apply is in flight (`busy`), not merely because some other apply is running.
- Keep the concurrency guard (`$applyActive`) but express it as the busy label
  on the button, not a silent full-row dim, so the control never looks broken.

## Error handling

- Degraded/failed probe → `recommended: null` → default choice **Keep both**;
  the ★ highlight is simply absent (as today). The choice UI still works.
- `preview.existing.present === false` (destination genuinely free, no library
  dup) → collapse to the existing "Destination is free — Apply" fast path;
  no choice UI needed.
- `replace_library_dup` requested but the duplicate can no longer be located →
  hold for review with a clear message; nothing trashed or placed.

## Testing

Backend (`tests/test_rename_service.py`, throwaway-container pytest):
- `replace_library_dup` happy path: library file trashed, download placed at
  dst, job `applied`, `conflict_replaced_path` set.
- Restore-on-failure: force placement to fail after trashing → library file
  restored to `lib_path`, job not left `applying`, `ok: False`.
- `undo()` of a `replace_library_dup` apply restores the trashed library file;
  `restore_warning` surfaced when restore fails.
- Guard: `replace_library_dup` with no locatable duplicate → holds for review,
  touches nothing.
- `keep_plex`: job archived AND `original_path` moved to recoverable trash;
  library copy untouched. Trash-failure variant → still archived, warning set.
- Regression: `overwrite` / `keep_both` / `skip` unchanged.

Frontend:
- `strategyForChoice(kind, choice)` unit tests for all 6 combinations
  (`conflictView.test.ts`).
- Component: labels render "In Plex"/"Downloaded" with badges; recommended
  pre-selects the right radio; Apply runs the mapped strategy.

## Out of scope

- Auto-deleting the redundant *download* file when the user keeps the Plex copy
  (the download stays on disk; removing it is the Downloads manager's job).
- TV library-duplicates — `find_library_duplicate` is movies-only by design;
  the `same_path` path already covers TV episode collisions.
- Any change to how conflicts are *detected* or to `rank_conflict` scoring.

## Files touched

- `backend/rename/service.py` — `apply()` new strategy branch; `undo()`
  symmetry; restore-on-failure helper reuse.
- `backend/db.py` (or wherever `rename_jobs` schema lives) — new nullable
  `conflict_replaced_path` column + migration.
- `backend/api/routes/renames.py` — accept `replace_library_dup` in the
  apply request model.
- `frontend/src/lib/renames/conflictView.ts` (+ `.test.ts`) — `strategyForChoice`.
- `frontend/src/lib/components/renames/RenameReviewCard.svelte` — labels,
  badges, radio choice UI, primary Apply button.
- `frontend/src/lib/components/renames/ConflictModal.svelte` — `onChoose` wiring.
- `frontend/src/lib/api/client.ts` + `stores/renames.ts` — strategy union.
