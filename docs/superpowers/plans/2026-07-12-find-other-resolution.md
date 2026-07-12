# Find Other Resolution (TV) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** While browsing a TV show at one resolution, one click finds the same show + season at a different (typically smaller) resolution — cache-first (instant), falling back to a live Site Search.

**Architecture:** A pure helper `findCachedAlternative()` (`frontend/src/lib/resultActions/findOtherResolution.ts`) scans the already-loaded `results` store for a same-identity, same-season, different-resolution item using the same imdb_id-first/normalized-title-fallback identity-key pattern already established elsewhere in this codebase. If nothing is found, the button falls back to the existing `searchThisSite(query, source)` (built earlier this session for the empty-state search fallback) with a constructed `"{title} S{season}"` query. No new backend endpoint — this is a pure frontend composition of already-existing pieces. TV only (`item.season != null`); the button appears in `DetailPanel.svelte`/`DetailSheet.svelte` and as a compact icon button on `ResultRow.svelte`'s season badge.

**Tech Stack:** SvelteKit 5 (runes), vitest.

## Global Constraints

- **TV only.** The button and helper are gated on `item.season != null`; movies never show this control (per scope decision — a movie's "other resolution" is covered by the existing Compare/duplicate-comparison feature, not this one).
- **Never a false match.** `findCachedAlternative` must never match a different show or a different season — precision over recall. If identity can't be confidently established (target has no imdb_id and no title match found), return `null` rather than guess.
- **No new backend endpoint.** This is entirely a frontend composition of `results` (existing store), `findCachedAlternative` (new pure function), and `searchThisSite` (existing, `frontend/src/lib/stores/scanner.ts:95`).
- **Which resolution to target:** current item is `4K`/`2160p` -> search implies `1080p`; anything else -> search implies `4K`/`2160p`. A simple toggle, not a full picker.
- Frontend tests: host node (`cd frontend && npm run check && npm run build && npx vitest run`).
- Work directly on `main`. Commit only when genuinely green.
- Smart/curly-quote hazard: plain ASCII quotes only; grep new/changed files before committing.
- **File overlap note:** three other workflows may be running concurrently against this same working directory this session (audio/HDR metadata, Plex library metadata-scan, genre-exclude-filter). None of them touch `ResultRow.svelte`, `DetailPanel.svelte`, `DetailSheet.svelte`, or any new file this plan creates — safe to run in parallel. Always `git add` only the exact files each task lists, never `-A`.

---

### Task 1: `findCachedAlternative()` — pure local-search helper

**Files:**
- Create: `frontend/src/lib/resultActions/findOtherResolution.ts`
- Test: `frontend/src/lib/resultActions/findOtherResolution.test.ts`

**Interfaces:**
- Produces: `export interface FindAlternativeTarget { imdbId: string | null; title: string; season: number; excludeResolution: string; }`, `export function findCachedAlternative(items: ScanResult[], target: FindAlternativeTarget): ScanResult | null`, `export function targetResolution(current: string): string` (returns `'1080p'` when `current` is `'4K'` or `'2160p'`, otherwise `'4K'`), `export function seasonSearchQuery(title: string, season: number): string` (returns `` `${title} S${String(season).padStart(2, '0')}` ``, e.g. `"Show Name S02"`).

- [ ] **Step 1: Write the failing tests**

Create `frontend/src/lib/resultActions/findOtherResolution.test.ts`:

```typescript
import { describe, expect, it } from 'vitest';
import { findCachedAlternative, seasonSearchQuery, targetResolution } from './findOtherResolution';
import type { ScanResult } from '$lib/api/types';

function makeItem(overrides: Partial<ScanResult> = {}): ScanResult {
	return {
		title: 'Show Name',
		year: 2020,
		season: 2,
		episodes: 10,
		resolution: '4K',
		size: '20 GB',
		status: 'missing',
		status_text: 'Missing',
		color: '',
		url: 'https://example.com/show',
		group_key: 'show-name-s2',
		rating: null,
		votes: null,
		votes_source: '',
		rt_score: null,
		genres: [],
		language: 'en',
		poster_url: '',
		imdb_id: 'tt1234567',
		description: '',
		hdr: '',
		dovi: false,
		selected: false,
		plex_info: '',
		plex_versions: '',
		plex_rating_key: null,
		posted_date: null,
		host_pref: '',
		is_duplicate_group: false,
		...overrides
	} as ScanResult;
}

describe('targetResolution', () => {
	it('4K implies 1080p', () => {
		expect(targetResolution('4K')).toBe('1080p');
	});
	it('2160p implies 1080p', () => {
		expect(targetResolution('2160p')).toBe('1080p');
	});
	it('1080p implies 4K', () => {
		expect(targetResolution('1080p')).toBe('4K');
	});
	it('720p implies 4K', () => {
		expect(targetResolution('720p')).toBe('4K');
	});
});

describe('seasonSearchQuery', () => {
	it('formats a single-digit season with a leading zero', () => {
		expect(seasonSearchQuery('Show Name', 2)).toBe('Show Name S02');
	});
	it('formats a double-digit season without truncation', () => {
		expect(seasonSearchQuery('Show Name', 12)).toBe('Show Name S12');
	});
});

describe('findCachedAlternative', () => {
	it('matches by imdb_id, same season, different resolution', () => {
		const target = { imdbId: 'tt1234567', title: 'Show Name', season: 2, excludeResolution: '4K' };
		const alt = makeItem({ resolution: '1080p' });
		const items = [makeItem({ resolution: '4K' }), alt, makeItem({ season: 3, resolution: '1080p' })];
		expect(findCachedAlternative(items, target)).toBe(alt);
	});

	it('falls back to normalized title match when target has no imdb_id', () => {
		const target = { imdbId: null, title: 'Show Name', season: 2, excludeResolution: '4K' };
		const alt = makeItem({ resolution: '1080p', imdb_id: null });
		const items = [alt];
		expect(findCachedAlternative(items, target)).toBe(alt);
	});

	it('never matches a different season', () => {
		const target = { imdbId: 'tt1234567', title: 'Show Name', season: 2, excludeResolution: '4K' };
		const items = [makeItem({ season: 3, resolution: '1080p' })];
		expect(findCachedAlternative(items, target)).toBeNull();
	});

	it('never matches a different show even with the same season', () => {
		const target = { imdbId: 'tt1234567', title: 'Show Name', season: 2, excludeResolution: '4K' };
		const items = [makeItem({ imdb_id: 'tt9999999', title: 'Different Show', resolution: '1080p' })];
		expect(findCachedAlternative(items, target)).toBeNull();
	});

	it('excludes items matching the excluded resolution', () => {
		const target = { imdbId: 'tt1234567', title: 'Show Name', season: 2, excludeResolution: '4K' };
		const items = [makeItem({ resolution: '4K' })];
		expect(findCachedAlternative(items, target)).toBeNull();
	});

	it('returns null when nothing qualifies', () => {
		const target = { imdbId: 'tt1234567', title: 'Show Name', season: 2, excludeResolution: '4K' };
		expect(findCachedAlternative([], target)).toBeNull();
	});

	it('does not cross-match when target has an imdb_id but the candidate does not', () => {
		const target = { imdbId: 'tt1234567', title: 'Show Name', season: 2, excludeResolution: '4K' };
		const items = [makeItem({ imdb_id: null, resolution: '1080p' })];
		expect(findCachedAlternative(items, target)).toBeNull();
	});
});
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `npx vitest run src/lib/resultActions/findOtherResolution.test.ts`
Expected: FAIL — module does not exist yet.

- [ ] **Step 3: Write the implementation**

Create `frontend/src/lib/resultActions/findOtherResolution.ts`:

```typescript
import type { ScanResult } from '$lib/api/types';

export interface FindAlternativeTarget {
	imdbId: string | null;
	title: string;
	season: number;
	excludeResolution: string;
}

function normalizeTitle(title: string): string {
	return title
		.toLowerCase()
		.replace(/[^a-z0-9]+/g, ' ')
		.trim();
}

/** imdb_id first, normalized-title fallback -- mirrors this codebase's
 *  established identity-key pattern (see backend `_identity_key` /
 *  `find_library_duplicate`). Returns null when neither is available, so a
 *  titleless/idless item can never accidentally match anything. */
function identityKey(imdbId: string | null | undefined, title: string): string | null {
	if (imdbId) return `imdb:${imdbId}`;
	const norm = normalizeTitle(title);
	return norm ? `title:${norm}` : null;
}

/** Finds an already-cached same-show, same-season item at a different
 *  resolution than `target.excludeResolution`. Never a false match across
 *  shows or seasons -- returns null rather than guess. */
export function findCachedAlternative(
	items: ScanResult[],
	target: FindAlternativeTarget
): ScanResult | null {
	const targetKey = identityKey(target.imdbId, target.title);
	if (!targetKey) return null;
	for (const item of items) {
		if (item.season !== target.season) continue;
		if (item.resolution === target.excludeResolution) continue;
		const itemKey = identityKey(item.imdb_id, item.title);
		if (itemKey === targetKey) return item;
	}
	return null;
}

/** 4K/2160p implies 1080p is wanted (and vice versa) -- matches the stated
 *  use case exactly (found 4K, want 1080p), not a full resolution picker. */
export function targetResolution(current: string): string {
	return current === '4K' || current === '2160p' ? '1080p' : '4K';
}

export function seasonSearchQuery(title: string, season: number): string {
	return `${title} S${String(season).padStart(2, '0')}`;
}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `npx vitest run src/lib/resultActions/findOtherResolution.test.ts`
Expected: PASS, all 11 tests green.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/lib/resultActions/findOtherResolution.ts frontend/src/lib/resultActions/findOtherResolution.test.ts
git commit -m "feat(results): findCachedAlternative + query helpers for TV other-resolution search"
```

---

### Task 2: Wire the button into `DetailPanel.svelte` + `DetailSheet.svelte`

**Files:**
- Modify: `frontend/src/lib/components/DetailPanel.svelte`
- Modify: `frontend/src/lib/components/mobile/DetailSheet.svelte`

**Interfaces:**
- Consumes: `findCachedAlternative`, `targetResolution`, `seasonSearchQuery` (Task 1, `$lib/resultActions/findOtherResolution`), `results` (existing store, `$lib/stores/results`), `searchThisSite`, `selectedScanSource` (existing, `$lib/stores/scanner.ts:95` and `:19`), `selectedDetail` (existing store `DetailPanel`/`DetailSheet` bind their `item` prop to — read `frontend/src/lib/stores/results.ts` for its exact set/update API before wiring, per this session's established gotcha: a store-mutation feature must patch `selectedDetail` too, or the open panel won't visibly update, though this feature doesn't mutate the CURRENT item, only navigates/searches, so confirm whether a patch is even needed here or if this concern doesn't apply — it likely doesn't, since this action either selects a different existing item or starts a new scan, neither of which requires patching the currently-open item in place).

- [ ] **Step 1: Add the handler and button to `DetailPanel.svelte`**

Read the existing `rescanItem` function (around line 134) and its button (around line 419-427) in `frontend/src/lib/components/DetailPanel.svelte` first, to match the exact style. Add near the top-level imports:

```typescript
import { findCachedAlternative, targetResolution, seasonSearchQuery } from '$lib/resultActions/findOtherResolution';
import { searchThisSite, selectedScanSource } from '$lib/stores/scanner';
import { get } from 'svelte/store';
```

(`results` is likely already imported per line 6's existing `import { results, markDownloaded, updateResultFromRescan } from '$lib/stores/results';` — reuse that import, do not add a duplicate.)

Add a handler function near `rescanItem`:

```typescript
function findOtherResolution() {
	if (item.season == null) return;
	const wanted = targetResolution(item.resolution);
	const cached = findCachedAlternative(get(results), {
		imdbId: item.imdb_id,
		title: item.title,
		season: item.season,
		excludeResolution: item.resolution
	});
	if (cached) {
		selectedDetail.set(cached);
		addToast('Found locally', `${wanted} version already in your cached results.`);
		return;
	}
	searchThisSite(seasonSearchQuery(item.title, item.season), get(selectedScanSource));
	addToast('Searching', `Searching ${get(selectedScanSource)} for ${wanted} version...`);
}
```

Check whether `selectedDetail` is already imported/available in this file (it is the store `DetailPanel`'s `item` prop is conventionally paired with, per this session's established pattern) — if this component receives `item` purely as a prop with no local `selectedDetail` import, read how the parent (`+page.svelte`) opens/changes the detail panel selection and call whatever function it already uses there instead of `selectedDetail.set(...)` directly (e.g. it may be an `onselect` callback prop). Match the existing navigation mechanism exactly rather than introducing a second one.

Add the button in the action-button row, immediately after the existing Rescan button (around line 427):

```svelte
{#if item.season != null}
	<button
		onclick={findOtherResolution}
		aria-label="Find other resolution"
		title="Find the same show and season at a different resolution"
		class="flex items-center gap-1.5 px-3 py-1.5 rounded text-xs font-medium bg-[var(--bg-tertiary)] text-[var(--text-secondary)] hover:text-[var(--text-primary)] transition-colors"
	>
		Find {targetResolution(item.resolution)}
	</button>
{/if}
```

- [ ] **Step 2: Add the equivalent to `DetailSheet.svelte` (mobile)**

Read the existing `rescanItem` function (around line 70) and its button (around line 254-262) in `frontend/src/lib/components/mobile/DetailSheet.svelte`. Add the same imports and `findOtherResolution` handler (adjust the `selectedDetail`/navigation call to whatever mechanism this file already uses to switch the shown item, matching Step 1's investigation). Add the button immediately after the existing Rescan button:

```svelte
{#if item.season != null}
	<button
		onclick={findOtherResolution}
		aria-label="Find other resolution"
		title="Find the same show and season at a different resolution"
		class="px-4 py-2.5 rounded-xl bg-[var(--bg-tertiary)] text-sm font-semibold text-[var(--text-primary)]"
	>
		Find {targetResolution(item.resolution)}
	</button>
{/if}
```

- [ ] **Step 3: Verify manually in the browser**

Open a TV item's detail panel (desktop), confirm the "Find 1080p"/"Find 4K" button only appears for TV items (not movies), click it with a matching cached alternative present and confirm it switches to that item with a "Found locally" toast, then click it for a season with no cached alternative and confirm it starts a site search with the constructed query. Repeat on a mobile viewport (`resize_window` to the mobile preset) for `DetailSheet.svelte`.

- [ ] **Step 4: Run tests**

```bash
cd frontend
npm run check
npm run build
npx vitest run
```
Expected: 0 errors, build succeeds, no regressions in the full suite.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/lib/components/DetailPanel.svelte frontend/src/lib/components/mobile/DetailSheet.svelte
git commit -m "feat(results): Find other resolution button in detail panel (desktop + mobile)"
```

---

### Task 3: Compact icon button on `ResultRow.svelte`'s season badge + full verification

**Files:**
- Modify: `frontend/src/lib/components/ResultRow.svelte`

**Interfaces:**
- Consumes: same as Task 2 (`findCachedAlternative`, `targetResolution`, `seasonSearchQuery`, `searchThisSite`, `selectedScanSource`, `results`).

**Note:** `ResultRow.svelte` is a dense, compact row (see its existing season badge at line 260-261: `{#if item.season != null}<span ...>S{String(item.season).padStart(2, '0')}</span>{/if}`) with no existing per-row action-button row — do not add a full labeled button here; add a small icon-only button immediately next to the season badge so it doesn't disrupt the row's density.

- [ ] **Step 1: Add the icon button next to the season badge**

Read `ResultRow.svelte` lines 255-270 in full first to see the exact surrounding markup (flex/gap structure) before inserting. Add the same `findOtherResolution` handler used in Task 2 (import `findCachedAlternative`, `targetResolution`, `seasonSearchQuery` from `$lib/resultActions/findOtherResolution`, `searchThisSite`/`selectedScanSource` from `$lib/stores/scanner`, `results`/`selectedDetail` per Task 2's established import), then add immediately after the existing season `<span>`:

```svelte
{#if item.season != null}
	<button
		type="button"
		onclick={(e) => { e.stopPropagation(); findOtherResolution(); }}
		aria-label="Find other resolution"
		title="Find {targetResolution(item.resolution)} version of this season"
		class="shrink-0 w-4 h-4 flex items-center justify-center rounded text-[10px] text-[var(--text-secondary)] hover:text-[var(--accent)] hover:bg-[var(--bg-tertiary)]"
	>
		&#8635;
	</button>
{/if}
```

`e.stopPropagation()` is required because `ResultRow`'s root element likely has its own `onclick`/selection handler (read the row's root element markup to confirm) — without it, clicking this icon would also trigger the row's own click behavior.

- [ ] **Step 2: Verify manually in the browser**

Confirm the icon button renders only on TV rows (next to the season badge, not on movie rows), and clicking it does not also select/open the row (confirms `stopPropagation` worked) while still triggering the same cached-lookup/search-fallback behavior as Task 2's detail-panel button.

- [ ] **Step 3: Run the full verification suite**

```bash
cd frontend
npm run check
npm run build
npx vitest run
```
Expected: all green, no regressions. Grep every file touched across Tasks 1-3 for curly/smart quotes and confirm zero matches.

- [ ] **Step 4: Commit**

```bash
git add frontend/src/lib/components/ResultRow.svelte
git commit -m "feat(results): compact Find-other-resolution icon on TV result rows + full verification"
```
