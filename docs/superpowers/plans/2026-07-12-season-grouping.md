# Collapsible Season Grouping Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Group TV episode rename jobs on the desktop Renames page into collapsible per-season rows, collapsed by default, with an "Apply all" bulk action on the header.

**Architecture:** A pure, unit-tested grouping helper (`seasonGroups.ts`) transforms the flat job list into a mixed array of `{type:'season', jobs}` groups and `{type:'single', job}` singles; the desktop page's existing `{#each}` branches on that instead of rendering every job as a bare row. A new `SeasonGroupRow.svelte` wraps the *existing, unchanged* `RenameRow.svelte` for its expanded contents and calls the *existing* `applyConfident()` store action for its header button — no backend changes.

**Tech Stack:** SvelteKit 5 (runes), TypeScript, Vitest.

## Global Constraints

- Desktop only (`frontend/src/routes/renames/+page.svelte`) — mobile's `MobileRenamesView.svelte`/`RenameReviewDeck.svelte` are untouched.
- No backend changes. `applyConfident(ids?: number[])` (`frontend/src/lib/stores/renames.ts:166`) already accepts an explicit id array — reuse it verbatim for the "Apply all" button.
- Group position in the rendered list follows the position of the group's FIRST member in the already-sorted input array — grouping must not fight the existing `detected_at`/`confidence`/`title` sort.
- `RenameRow.svelte` is unchanged — season grouping is a wrapper, not a rewrite of row rendering.
- No `.svelte` render tests exist in this repo (established convention) — all branching/grouping logic lives in tested `.ts`.
- Work directly on `main`. Commit only when `npm run check`/`build`/`vitest` are green. Watch for smart/curly quotes — grep new lines for U+201C/U+201D before considering a task done.

---

### Task 1: `seasonGroups.ts` grouping + summary helper

**Files:**
- Create: `frontend/src/lib/renames/seasonGroups.ts`
- Create: `frontend/src/lib/renames/seasonGroups.test.ts`

**Interfaces:**
- Consumes: `RenameJob` (`$lib/api/types.ts`) — uses `media_type`, `season`, `imdb_id`, `title`, `id`, `status`, `destination_conflict`, `library_duplicate` fields.
- Produces: `type GroupedEntry = { type: 'season'; key: string; show: string; season: number; jobs: RenameJob[] } | { type: 'single'; job: RenameJob }`; `groupJobsBySeason(jobs: RenameJob[]): GroupedEntry[]`; `type SeasonSummary = { matched: number; needsReview: number; conflicts: number; applied: number; other: number }`; `seasonSummary(jobs: RenameJob[]): SeasonSummary`.

- [ ] **Step 1: Write the failing tests**

Create `frontend/src/lib/renames/seasonGroups.test.ts`:
```typescript
import { describe, it, expect } from 'vitest';
import { groupJobsBySeason, seasonSummary } from './seasonGroups';
import type { RenameJob } from '$lib/api/types';

const job = (o: Partial<RenameJob>): RenameJob => ({
  id: 1, package_name: null, original_path: '', original_filename: null,
  new_filename: null, destination_path: null, status: 'matched',
  media_type: 'movie', title: 'X', year: null, season: null, episode: null,
  tmdb_id: null, imdb_id: null, resolution: null, match_confidence: null,
  match_source: null, move_method: null, warning_message: null,
  error_message: null, plex_sort_title: null, detected_at: null,
  processed_at: null, reverted_at: null,
  ...o,
} as RenameJob);

describe('groupJobsBySeason', () => {
  it('groups TV episodes by imdb_id + season', () => {
    const jobs = [
      job({ id: 1, media_type: 'tv', title: 'Severance', season: 2, episode: 1, imdb_id: 'tt11280740' }),
      job({ id: 2, media_type: 'tv', title: 'Severance', season: 2, episode: 2, imdb_id: 'tt11280740' }),
    ];
    const groups = groupJobsBySeason(jobs);
    expect(groups).toHaveLength(1);
    expect(groups[0]).toMatchObject({ type: 'season', show: 'Severance', season: 2 });
    expect((groups[0] as any).jobs).toHaveLength(2);
  });

  it('falls back to normalized title when imdb_id is null', () => {
    const jobs = [
      job({ id: 1, media_type: 'tv', title: 'The Bear', season: 1, episode: 1, imdb_id: null }),
      job({ id: 2, media_type: 'tv', title: 'the   bear', season: 1, episode: 2, imdb_id: null }),
    ];
    const groups = groupJobsBySeason(jobs);
    expect(groups).toHaveLength(1);
    expect((groups[0] as any).jobs).toHaveLength(2);
  });

  it('never merges two different shows sharing a season number', () => {
    const jobs = [
      job({ id: 1, media_type: 'tv', title: 'Show A', season: 1, episode: 1, imdb_id: 'tt1' }),
      job({ id: 2, media_type: 'tv', title: 'Show B', season: 1, episode: 1, imdb_id: 'tt2' }),
    ];
    const groups = groupJobsBySeason(jobs);
    expect(groups).toHaveLength(2);
  });

  it('leaves movies and season-less jobs as individual entries in original position', () => {
    const jobs = [
      job({ id: 1, media_type: 'movie', title: 'A Movie' }),
      job({ id: 2, media_type: 'tv', title: 'No Season', season: null }),
    ];
    const groups = groupJobsBySeason(jobs);
    expect(groups).toEqual([
      { type: 'single', job: jobs[0] },
      { type: 'single', job: jobs[1] },
    ]);
  });

  it('positions a season group at its first member\'s original index', () => {
    const jobs = [
      job({ id: 1, media_type: 'movie', title: 'Movie First' }),
      job({ id: 2, media_type: 'tv', title: 'Show', season: 1, episode: 1, imdb_id: 'tt1' }),
      job({ id: 3, media_type: 'movie', title: 'Movie Last' }),
      job({ id: 4, media_type: 'tv', title: 'Show', season: 1, episode: 2, imdb_id: 'tt1' }),
    ];
    const groups = groupJobsBySeason(jobs);
    expect(groups.map((g) => g.type)).toEqual(['single', 'season', 'single']);
  });
});

describe('seasonSummary', () => {
  it('tallies statuses and conflicts correctly', () => {
    const jobs = [
      job({ status: 'matched' }),
      job({ status: 'matched' }),
      job({ status: 'needs_review' }),
      job({ status: 'applied' }),
      job({ status: 'matched', destination_conflict: true } as any),
    ];
    const s = seasonSummary(jobs);
    expect(s).toEqual({ matched: 3, needsReview: 1, conflicts: 1, applied: 1, other: 0 });
  });

  it('handles an empty list', () => {
    expect(seasonSummary([])).toEqual({ matched: 0, needsReview: 0, conflicts: 0, applied: 0, other: 0 });
  });
});
```

- [ ] **Step 2: Run to verify RED**

Run: `cd frontend && npx vitest run src/lib/renames/seasonGroups.test.ts`
Expected: FAIL — module not found.

- [ ] **Step 3: Implement the helper**

Create `frontend/src/lib/renames/seasonGroups.ts`:
```typescript
import type { RenameJob } from '$lib/api/types';

export type GroupedEntry =
  | { type: 'season'; key: string; show: string; season: number; jobs: RenameJob[] }
  | { type: 'single'; job: RenameJob };

export interface SeasonSummary {
  matched: number;
  needsReview: number;
  conflicts: number;
  applied: number;
  other: number;
}

/** Lowercase + collapse whitespace — enough to fold cosmetic differences
 *  ("the   bear" vs "The Bear") without the year-stripping the backend's
 *  own normalize_title does (RenameJob.title is already a matched, clean
 *  title, not a raw scraped one). */
function normalizeTitle(title: string | null): string {
  return (title ?? '').toLowerCase().trim().replace(/\s+/g, ' ');
}

function seasonKey(job: RenameJob): string | null {
  if (job.media_type !== 'tv' || job.season == null) return null;
  const identity = job.imdb_id ? `imdb:${job.imdb_id}` : `title:${normalizeTitle(job.title)}`;
  return `${identity}|S${job.season}`;
}

/** Groups TV episodes by (imdb_id ?? normalized title, season); movies and
 *  season-less jobs pass through as individual entries. A season group's
 *  position in the output is its first member's position in the input —
 *  grouping never fights the caller's existing sort order. */
export function groupJobsBySeason(jobs: RenameJob[]): GroupedEntry[] {
  const groups = new Map<string, { show: string; season: number; jobs: RenameJob[] }>();
  const order: string[] = []; // first-seen order of group keys
  const singles: Array<{ index: number; job: RenameJob }> = [];

  jobs.forEach((job, index) => {
    const key = seasonKey(job);
    if (key === null) {
      singles.push({ index, job });
      return;
    }
    let g = groups.get(key);
    if (!g) {
      g = { show: job.title ?? 'Unknown', season: job.season as number, jobs: [] };
      groups.set(key, g);
      order.push(key);
    }
    g.jobs.push(job);
  });

  // First-seen-index per group key, for position interleaving with singles.
  const firstIndexByKey = new Map<string, number>();
  jobs.forEach((job, index) => {
    const key = seasonKey(job);
    if (key !== null && !firstIndexByKey.has(key)) firstIndexByKey.set(key, index);
  });

  type Positioned = { pos: number; entry: GroupedEntry };
  const positioned: Positioned[] = [
    ...singles.map(({ index, job }): Positioned => ({ pos: index, entry: { type: 'single', job } })),
    ...order.map((key): Positioned => {
      const g = groups.get(key)!;
      return { pos: firstIndexByKey.get(key)!, entry: { type: 'season', key, show: g.show, season: g.season, jobs: g.jobs } };
    }),
  ];
  positioned.sort((a, b) => a.pos - b.pos);
  return positioned.map((p) => p.entry);
}

/** Pure status tally for a season group's collapsed header. */
export function seasonSummary(jobs: RenameJob[]): SeasonSummary {
  const s: SeasonSummary = { matched: 0, needsReview: 0, conflicts: 0, applied: 0, other: 0 };
  for (const j of jobs) {
    if ((j as any).destination_conflict || (j as any).library_duplicate) s.conflicts++;
    switch (j.status) {
      case 'matched': s.matched++; break;
      case 'needs_review': s.needsReview++; break;
      case 'applied': s.applied++; break;
      default: s.other++;
    }
  }
  return s;
}
```

- [ ] **Step 4: Run to verify GREEN**

Run: `npx vitest run src/lib/renames/seasonGroups.test.ts` → all pass.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/lib/renames/seasonGroups.ts frontend/src/lib/renames/seasonGroups.test.ts
git commit -m "feat(renames): seasonGroups.ts grouping + status-summary helper"
```

---

### Task 2: `SeasonGroupRow.svelte` + wire into the desktop Renames page

**Files:**
- Create: `frontend/src/lib/components/renames/SeasonGroupRow.svelte`
- Modify: `frontend/src/routes/renames/+page.svelte` (both `{#each shown as job (job.id)}` loops, lines ~307 and ~351 — check both, the file may render the list twice for different view states; grep `#each shown as job` to confirm exact current line numbers before editing)

**Interfaces:**
- Consumes: Task 1's `groupJobsBySeason`/`seasonSummary`/`GroupedEntry`; existing `applyConfident`, `bulkBusy`, `applyActive` (`$lib/stores/renames`); existing `RenameRow.svelte`.

- [ ] **Step 1: Create `SeasonGroupRow.svelte`**

```svelte
<script lang="ts">
  import RenameRow from './RenameRow.svelte';
  import { seasonSummary } from '$lib/renames/seasonGroups';
  import { applyConfident, bulkBusy, applyActive } from '$lib/stores/renames';
  import type { RenameJob } from '$lib/api/types';

  let { jobs, show, season, onRematch, onCompare }: {
    jobs: RenameJob[];
    show: string;
    season: number;
    onRematch: (job: RenameJob) => void;
    onCompare: (job: RenameJob) => void;
  } = $props();

  let expanded = $state(false);
  let summary = $derived(seasonSummary(jobs));
  let applyDisabled = $derived($bulkBusy || $applyActive || summary.matched === 0);

  function applyAll() {
    if (applyDisabled) return;
    const ids = jobs.filter((j) => j.status === 'matched').map((j) => j.id);
    if (ids.length > 0) applyConfident(ids);
  }
</script>

<div class="border border-[var(--border)] rounded-lg overflow-hidden">
  <button
    type="button"
    class="w-full flex items-center gap-2 px-3 py-2 bg-[var(--bg-secondary)] hover:bg-[var(--bg-tertiary)] transition-colors text-left"
    onclick={() => (expanded = !expanded)}
    aria-expanded={expanded}
  >
    <span class="text-xs">{expanded ? '▾' : '▸'}</span>
    <span class="text-sm font-medium text-[var(--text-primary)]">{show} &mdash; Season {season}</span>
    <span class="text-xs text-[var(--text-secondary)] ml-2">
      {summary.matched} matched
      {#if summary.needsReview > 0} &middot; {summary.needsReview} needs review{/if}
      {#if summary.conflicts > 0} &middot; {summary.conflicts} conflict{summary.conflicts === 1 ? '' : 's'}{/if}
      {#if summary.applied > 0} &middot; {summary.applied} applied{/if}
    </span>
    <button
      type="button"
      class="ml-auto px-2 py-1 rounded text-[11px] font-medium bg-[var(--accent)] text-white disabled:opacity-50"
      disabled={applyDisabled}
      onclick={(e) => { e.stopPropagation(); applyAll(); }}
    >Apply all</button>
  </button>
  {#if expanded}
    <div class="divide-y divide-[var(--border)]">
      {#each jobs as job (job.id)}
        <RenameRow {job} {onRematch} {onCompare} />
      {/each}
    </div>
  {/if}
</div>
```

Use STRAIGHT ASCII quotes throughout in your actual edit — no curly/smart quotes. (The `&mdash;`/`&middot;` HTML entities and the `▾`/`▸` unicode escapes above are safe as written — they are not quote characters.)

- [ ] **Step 2: Wire into `+page.svelte`**

First run `grep -n "#each shown as job" frontend/src/routes/renames/+page.svelte` to find the CURRENT exact line numbers (the spec noted lines ~307 and ~351 as of plan-writing time; confirm before editing, this file gets touched by other concurrent work). For each `{#each shown as job (job.id)}` ... `<RenameRow {job} {onRematch} {onCompare} />` ... `{/each}` block found:
- Add the import: `import SeasonGroupRow from '$lib/components/renames/SeasonGroupRow.svelte';` and `import { groupJobsBySeason } from '$lib/renames/seasonGroups';` near the existing `RenameRow` import.
- Replace the loop:
```svelte
      {#each groupJobsBySeason(shown) as entry (entry.type === 'season' ? entry.key : entry.job.id)}
        {#if entry.type === 'season'}
          <SeasonGroupRow jobs={entry.jobs} show={entry.show} season={entry.season} {onRematch} {onCompare} />
        {:else}
          <RenameRow job={entry.job} {onRematch} {onCompare} />
        {/if}
      {/each}
```
If the two `{#each shown as job}` occurrences render into genuinely different DOM structures (e.g. one is a `<table>` row context, the other a card/grid context — check the surrounding markup before assuming they're identical), adapt `SeasonGroupRow`'s wrapper markup to fit each context rather than force-fitting one shape into both; note any such adaptation in your report.

- [ ] **Step 3: Verify + full checks**

```bash
cd frontend && npm run check && npm run build && npx vitest run
```
Expected: 0 ERRORS, build succeeds, all pass (existing + Task 1's new tests). Grep your new/changed files for curly quotes (U+201C/U+201D) before considering this done — do not trust a self-reported "it built" without actually re-running check yourself.

- [ ] **Step 4: Manual verification (browser)**

Since this is user-facing UI, start the dev server / preview and verify by hand if the environment allows it: queue several episodes of one show (or seed test data), confirm they collapse into one group, confirm "Apply all" queues only the group's own matched jobs, confirm a movie row still renders individually. If a live backend isn't reachable in this environment (matches a precedent from earlier this session — the scanhound container publishes no host port), rely on the automated checks + a careful manual read of the rendered markup instead, and say so explicitly in the report rather than silently skipping verification.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/lib/components/renames/SeasonGroupRow.svelte frontend/src/routes/renames/+page.svelte
git commit -m "feat(renames): collapsible per-season grouping on the desktop Renames page"
```

---

## Deployment

This plan does NOT deploy — joins the queue of work awaiting a combined deploy after user review, per this project's established practice this session.
