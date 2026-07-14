# Pipeline Row Detail Enhancements — Design Spec

**Date:** 2026-07-13
**Status:** Approved by user (three-question round: timestamp scope, link target, season placement)

## Problem

User feedback on the deployed Pipeline redesign: rows need (1) a direct link to the
release's source page, (2) lifecycle timestamps, and (3) the season number moved
out of the small metadata line and placed next to the title at matching size.

## Design

### 1. Source link

Wrap the title text in `<a href={item.url} target="_blank" rel="noopener noreferrer">`.
`item.url` already exists on every `PipelineItem` — no backend change.

### 2. Timestamps — only what is genuinely persisted

Investigated `download_results` and `rename_jobs` schemas directly: only two
lifecycle moments have real, dedicated timestamp columns. `download_results` has no
`downloaded_at`/`extracted_at` — only a single `updated_at` overwritten on every poll,
which is not stage-specific and was rejected as misleading. Decision (user-confirmed):
show only real timestamps; downloading/extracting/pending/verifying rows show their
category label only, as today — no fabricated time.

- **Grabbed**: `downloads.last_grabbed_at`. Add `d.last_grabbed_at` to
  `get_pipeline_verdicts()`'s SELECT (`backend/database.py`, alongside the `d.status`
  column added in the previous fix).
- **Renamed**: `rename_jobs.processed_at` (rename applied) if present, else
  `detected_at` (job created/matched). Extend the *same* per-category `CASE` block
  that already resolves `poster_path` (lines ~1128-1148) to also select
  `COALESCE(r.processed_at, r.detected_at)` from the identical matched row each
  category branch already picks — same `WHERE` conditions, same `ORDER BY r.id DESC
  LIMIT 1`. This is a deliberate reuse, not a new join: the previous feature shipped
  a real bug (stale poster from a sibling `rename_jobs` row) from an *inconsistent*
  second join: extending the existing, already-correct CASE avoids reintroducing that
  bug class by construction.

`PipelineItem` gains `grabbed_at: string | null` and `renamed_at: string | null`.
Both render via the existing `checkedAgo()` helper (already malformed-input-safe from
the prior fix): `grabbed {checkedAgo(item.grabbed_at)}` / `renamed
{checkedAgo(item.renamed_at)}`, each only shown when non-null.

### 3. Season inline, title-sized

Move the season chip out of the small `text-xs text-[var(--text-secondary)]`
metadata row into the title line itself, matching the title's size/weight:
`Law & Order: LA S10 (2010)`. Year stays a smaller trailing span as today; the
metadata row underneath keeps resolution + grabbed/renamed and loses the
now-redundant season chip.

## Testing

- Backend: `get_pipeline_verdicts()` returns `grabbed_at`/`renamed_at` populated per
  category exactly as `poster_path` is (reuse the existing
  `TestPipelineVerdictsPosterPath` test class's fixture pattern — same rows, add
  assertions on the two new columns for each category branch, including the
  multi-sibling-row adversarial case from the prior fix to prove no regression).
- Frontend: `pipelineDisplay.test.ts` needs no new pure-function tests (reuses
  `checkedAgo`); `npm run check` + `npm run build` + `npx vitest run` cover the
  template change.

## Out of scope

- `downloaded_at`/`extracted_at`/`verified_at` (no persisted data; user chose not to
  add new columns for this pass).
- Any change to action-button wiring or category logic.
