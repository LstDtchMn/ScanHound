# Collapsible Season Grouping — Design Spec

**Date:** 2026-07-12
**Status:** Approved (design), pending implementation plan

## Goal

Group TV episode rename jobs on the desktop Renames page into collapsible/expandable per-season rows instead of one flat row per episode, so a queued season pack (e.g. 10 episodes) shows as one compact summary until expanded.

## Scope

**Desktop only.** Mobile's Renames view (`MobileRenamesView.svelte`) is a stats dashboard + one-at-a-time swipe deck (`RenameReviewDeck.svelte`), not a flat row list — the "collapsible row" concept has no equivalent there and is left untouched.

## Grouping key

For a job with `media_type === 'tv'` and a non-null `season`: group key is `(imdb_id ?? normalize_title(title), season)` — imdb_id first (exact identity), normalized-title fallback, mirroring the identity-based grouping pattern already used elsewhere in this codebase (e.g. `find_library_duplicate`, the group_key recipe in `_assign_group_keys`). Movies, and any TV job with a null season, render as individual rows exactly as today — no grouping applies to them.

## Components

1. **`frontend/src/lib/renames/seasonGroups.ts`** (new, pure, unit-tested — no decision logic inline in `.svelte`, matching this project's established convention):
   - `type GroupedEntry = { type: 'season'; key: string; show: string; season: number; jobs: RenameJob[] } | { type: 'single'; job: RenameJob }`
   - `groupJobsBySeason(jobs: RenameJob[]): GroupedEntry[]` — preserves the incoming sort order: a season group's position in the output is the position of its FIRST member job in the input array (so grouping doesn't fight the existing `detected_at`/`confidence`/`title` sort already applied upstream).
   - `seasonSummary(jobs: RenameJob[]): { matched: number; needsReview: number; conflicts: number; applied: number }` — pure status tally for the collapsed header, reusing this project's existing status-string vocabulary (`status` field values: pending/matched/needs_review/applied/failed/reverted) plus the existing `destination_conflict`/`library_duplicate` flags for the conflict count.

2. **Desktop Renames page** (`frontend/src/routes/renames/+page.svelte`): the existing `{#each shown as job (job.id)}` becomes `{#each groupJobsBySeason(shown) as entry (entry.type === 'season' ? entry.key : entry.job.id)}`, branching on `entry.type`:
   - `'single'` → renders the existing `<RenameRow>` unchanged.
   - `'season'` → renders a new `SeasonGroupRow.svelte` component.

3. **`frontend/src/lib/components/renames/SeasonGroupRow.svelte`** (new):
   - Collapsed by default. A local `Set<string>` of expanded group keys (page-level `$state`, not persisted — resets to all-collapsed on reload) tracks open/closed.
   - Header row: `{show} — Season {season}` + the `seasonSummary()` tally rendered as compact badges (reusing existing badge styling from `RenameRow`/`BadgeCluster`) + an expand/collapse chevron (click anywhere on the header toggles) + an **Apply all** button.
   - **Apply all** calls the *already-existing* `applyConfident(jobIds)` store action (`stores/renames.ts`) with this group's own job ids — no new backend endpoint. Disabled while `$bulkBusy || $applyActive` (matching `BulkBar`'s existing guard pattern) or while the group has zero matched/confident jobs.
   - Expanded state renders each job in the group via the existing `<RenameRow>`, unchanged — per-episode actions (rematch, conflict compare, delete, individual apply, the existing multi-select checkboxes wired to `selectedJobIds`) all keep working exactly as today; the group is purely a visual/interaction wrapper, not a new selection concept.

## Testing

- `seasonGroups.test.ts`: `groupJobsBySeason` — groups by imdb_id when present, falls back to normalized title when imdb_id is null, movies/season-less jobs stay ungrouped and in original position, group position follows first-member order, two different shows' "Season 1" are never merged. `seasonSummary` — correct tallies across a mixed-status job list, zero-job edge case.
- No `.svelte` render tests (none exist in this repo, per established convention) — all branching/grouping logic lives in the tested `.ts` helper; `SeasonGroupRow.svelte` is thin wiring over it and `applyConfident`.

## Non-goals (YAGNI)

- No persistence of expand/collapse state across reloads.
- No mobile equivalent.
- No new backend endpoint — `applyConfident` already accepts an explicit id list.
- No nested show-level grouping (a show with two different seasons queued at once shows as two adjacent season-group rows, each independently collapsible) — matches the user's literal request (season-level grouping) without adding an unrequested hierarchy level.
