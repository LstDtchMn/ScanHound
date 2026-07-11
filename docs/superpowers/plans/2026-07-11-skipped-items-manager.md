# Skipped-Items Manager Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a searchable "Skipped (N)" manager (desktop modal + mobile sheet) to view and restore swiped-away scan items, wiring the already-existing backend endpoints to UI.

**Architecture:** A pure `lib/skipped.ts` helper (search + relative-time) and one new `restoreAllDismissed()` store function drive a shared `SkippedManager.svelte` content component, mounted inside the existing `ModalOverlay` (desktop) and `BottomSheet` (mobile). Per-item restore reuses the existing `restoreItem(url)` store function. No backend changes.

**Tech Stack:** SvelteKit 5 (runes), TypeScript, Vitest. Frontend `npm run check`/`build`/`vitest` on host node.

## Global Constraints

- **No backend changes** — reuse `api.dismissedList()`, `api.clearDismissed()`, `api.dismissItems(urls, undefined, false)`.
- **Per-item restore reuses the existing `restoreItem(url)`** in `stores/results.ts` (do NOT add a new per-item function). Only `restoreAllDismissed()` is new.
- The live count is `$dismissedUrls.size` (already hydrated on scan-view load) — the badge uses it directly; no new count fetch.
- All decision logic lives in unit-tested `.ts` (`lib/skipped.ts`); there are NO `.svelte` render tests in this repo — do not add a test framework.
- `dismissItems` signature is `dismissItems(urls, titles?, dismissed=true, meta?)` — restore is `dismissItems([url], undefined, false)`.
- Component props (verified): `ModalOverlay` = `{ onclose, align?, children }`; `BottomSheet` = `{ open, title?, onclose, children }`; `ConfirmDialog` = `{ title?, message, confirmLabel?, cancelLabel?, variant?, onconfirm, oncancel }`.
- Work directly on `main`. Commit only when `npm run check` is 0 ERRORS and `npm run build` succeeds; new commit per task.
- **Watch for smart quotes:** write straight ASCII quotes (`"`) in all markup — curly quotes break `svelte-check`.

---

### Task 1: `lib/skipped.ts` helper + `restoreAllDismissed()` store fn

**Files:**
- Create: `frontend/src/lib/skipped.ts`
- Create: `frontend/src/lib/skipped.test.ts`
- Modify: `frontend/src/lib/stores/results.ts` (add `restoreAllDismissed`, near `restoreItem` ~line 942)

**Interfaces:**
- Produces: `type SkippedItem = { url: string; title: string | null; dismissed_at: string | null }`; `filterSkipped(items, query): SkippedItem[]`; `relativeTime(iso, now): string`; `restoreAllDismissed(): Promise<boolean>`.
- Consumes: existing `dismissedUrls` store, `api.clearDismissed()`.

- [ ] **Step 1: Write the failing helper tests**

Create `frontend/src/lib/skipped.test.ts`:
```typescript
import { describe, it, expect } from 'vitest';
import { filterSkipped, relativeTime, type SkippedItem } from './skipped';

const item = (o: Partial<SkippedItem>): SkippedItem => ({
  url: 'u', title: 'A Movie', dismissed_at: null, ...o,
});

describe('filterSkipped', () => {
  const items = [
    item({ url: 'a', title: 'Sinners' }),
    item({ url: 'b', title: 'The Batman' }),
    item({ url: 'c', title: null }),
  ];
  it('empty query returns all', () => {
    expect(filterSkipped(items, '')).toHaveLength(3);
    expect(filterSkipped(items, '   ')).toHaveLength(3);
  });
  it('case-insensitive title substring match', () => {
    expect(filterSkipped(items, 'batman').map((i) => i.url)).toEqual(['b']);
    expect(filterSkipped(items, 'SIN').map((i) => i.url)).toEqual(['a']);
  });
  it('falls back to url match when title is null', () => {
    expect(filterSkipped(items, 'c').map((i) => i.url)).toEqual(['c']);
  });
  it('no match returns empty', () => {
    expect(filterSkipped(items, 'zzz')).toEqual([]);
  });
});

describe('relativeTime', () => {
  const now = Date.parse('2026-07-11T12:00:00Z');
  it('null / empty returns empty string', () => {
    expect(relativeTime(null, now)).toBe('');
    expect(relativeTime('', now)).toBe('');
  });
  it('under a minute is "just now"', () => {
    expect(relativeTime('2026-07-11T11:59:30Z', now)).toBe('just now');
  });
  it('hours ago', () => {
    expect(relativeTime('2026-07-11T09:00:00Z', now)).toBe('3h ago');
  });
  it('days ago', () => {
    expect(relativeTime('2026-07-08T12:00:00Z', now)).toBe('3d ago');
  });
  it('older than 30d returns a date (not "Nd ago")', () => {
    expect(relativeTime('2026-01-01T12:00:00Z', now)).not.toMatch(/ago/);
  });
});
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd frontend && npx vitest run src/lib/skipped.test.ts`
Expected: FAIL — module `./skipped` not found.

- [ ] **Step 3: Implement the helper**

Create `frontend/src/lib/skipped.ts`:
```typescript
export type SkippedItem = {
  url: string;
  title: string | null;
  dismissed_at: string | null;
};

/** Case-insensitive title substring filter; empty/whitespace query returns all.
 *  Items with a null title fall back to matching on their URL. */
export function filterSkipped(items: SkippedItem[], query: string): SkippedItem[] {
  const q = query.trim().toLowerCase();
  if (!q) return items;
  return items.filter((i) => (i.title ?? i.url).toLowerCase().includes(q));
}

/** Relative "skipped …" label. null/empty → "". < 60s → "just now";
 *  < 60m → "Nm ago"; < 24h → "Nh ago"; < 30d → "Nd ago"; else a locale date. */
export function relativeTime(iso: string | null, now: number): string {
  if (!iso) return '';
  const then = Date.parse(iso);
  if (Number.isNaN(then)) return '';
  const secs = Math.max(0, Math.floor((now - then) / 1000));
  if (secs < 60) return 'just now';
  const mins = Math.floor(secs / 60);
  if (mins < 60) return `${mins}m ago`;
  const hours = Math.floor(mins / 60);
  if (hours < 24) return `${hours}h ago`;
  const days = Math.floor(hours / 24);
  if (days < 30) return `${days}d ago`;
  return new Date(then).toLocaleDateString();
}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd frontend && npx vitest run src/lib/skipped.test.ts`
Expected: all pass.

- [ ] **Step 5: Add `restoreAllDismissed()` to the store**

In `frontend/src/lib/stores/results.ts`, immediately after the `restoreItem` function (ends ~line 973), add:
```typescript
/** Restore ALL dismissed items (clear the skip list). Optimistically empties
 *  `dismissedUrls`; on API failure, restores the previous set. Restored items
 *  reappear in results on the next refresh (paged) or immediately (live). */
export function restoreAllDismissed(): Promise<boolean> {
  const prev = get(dismissedUrls);
  dismissedUrls.set(new Set());
  return api.clearDismissed().then(
    () => true,
    () => {
      dismissedUrls.set(prev);
      return false;
    }
  );
}
```
(Confirm `get` from `svelte/store` and `api` are already imported at the top of `results.ts` — they are, used by `restoreItem`/`dismissItem`.)

- [ ] **Step 6: Verify check/build + full vitest**

Run: `cd frontend && npm run check && npm run build && npx vitest run`
Expected: 0 ERRORS, build clean, all tests pass (existing + new skipped tests).

- [ ] **Step 7: Commit**

```bash
git add frontend/src/lib/skipped.ts frontend/src/lib/skipped.test.ts frontend/src/lib/stores/results.ts
git commit -m "feat(skipped): skipped.ts filter/format helper + restoreAllDismissed store fn"
```

---

### Task 2: `SkippedManager.svelte` shared content component

**Files:**
- Create: `frontend/src/lib/components/SkippedManager.svelte`

**Interfaces:**
- Consumes Task 1's `filterSkipped`, `relativeTime`, `SkippedItem`, `restoreAllDismissed`; existing `restoreItem` (`stores/results.ts`), `api.dismissedList`, `ConfirmDialog`.
- Produces: `<SkippedManager onclose={...} />` — a self-contained panel (no modal chrome of its own; the caller wraps it in ModalOverlay/BottomSheet).
- Restore failures are handled by `restoreItem`/`restoreAllDismissed` themselves (they revert `dismissedUrls` optimistically on API error) — the component simply keeps the row if `ok` is false; no toast needed.

- [ ] **Step 1: Create the component**

Create `frontend/src/lib/components/SkippedManager.svelte`:
```svelte
<script lang="ts">
  import { onMount } from 'svelte';
  import { api } from '$lib/api/client';
  import { restoreItem } from '$lib/stores/results';
  import { restoreAllDismissed } from '$lib/stores/results';
  import { filterSkipped, relativeTime, type SkippedItem } from '$lib/skipped';
  import ConfirmDialog from './ConfirmDialog.svelte';

  let { onclose }: { onclose: () => void } = $props();

  let items = $state<SkippedItem[]>([]);
  let loading = $state(true);
  let error = $state(false);
  let query = $state('');
  let confirmingClear = $state(false);
  let now = $state(Date.now());

  const visible = $derived(filterSkipped(items, query));

  async function load() {
    loading = true;
    error = false;
    try {
      const res = await api.dismissedList();
      items = res.items;
      now = Date.now();
    } catch {
      error = true;
    } finally {
      loading = false;
    }
  }

  onMount(load);

  async function restoreOne(url: string) {
    const ok = await restoreItem(url);
    if (ok) items = items.filter((i) => i.url !== url);
  }

  async function doClearAll() {
    confirmingClear = false;
    const ok = await restoreAllDismissed();
    if (ok) items = [];
  }
</script>

<div class="flex flex-col gap-3 min-h-0">
  <div class="flex items-center gap-2">
    <h2 class="text-sm font-semibold text-[var(--text-primary)]">Skipped items ({items.length})</h2>
    {#if items.length > 0}
      <button
        class="ml-auto text-xs px-2 py-1 rounded border border-[var(--border)] text-[var(--text-secondary)] hover:bg-[var(--bg-tertiary)]"
        onclick={() => (confirmingClear = true)}
      >Restore all</button>
    {/if}
    <button class="p-1 text-[var(--text-secondary)] hover:text-[var(--text-primary)]" aria-label="Close" onclick={onclose}>&times;</button>
  </div>

  <input
    type="text"
    placeholder="Search skipped titles…"
    bind:value={query}
    class="w-full bg-[var(--bg-tertiary)] text-[var(--text-primary)] px-3 py-2 rounded-lg border border-[var(--border)] text-sm"
  />

  <div class="overflow-y-auto min-h-0 max-h-[60vh] flex flex-col gap-1">
    {#if loading}
      <p class="text-sm text-[var(--text-secondary)] py-4 text-center">Loading…</p>
    {:else if error}
      <div class="py-4 text-center">
        <p class="text-sm text-[var(--text-secondary)]">Couldn't load skipped items.</p>
        <button class="mt-2 text-xs px-3 py-1 rounded bg-[var(--accent)] text-white" onclick={load}>Retry</button>
      </div>
    {:else if items.length === 0}
      <p class="text-sm text-[var(--text-secondary)] py-4 text-center">No skipped items.</p>
    {:else if visible.length === 0}
      <p class="text-sm text-[var(--text-secondary)] py-4 text-center">No matches.</p>
    {:else}
      {#each visible as it (it.url)}
        <div class="flex items-center gap-2 px-2 py-1.5 rounded hover:bg-[var(--bg-tertiary)]">
          <span class="text-sm text-[var(--text-primary)] truncate min-w-0 flex-1">{it.title ?? it.url}</span>
          <span class="text-[11px] text-[var(--text-secondary)] shrink-0">{relativeTime(it.dismissed_at, now)}</span>
          <button
            class="shrink-0 text-xs px-2 py-0.5 rounded bg-[var(--accent)] text-white hover:brightness-110"
            onclick={() => restoreOne(it.url)}
          >Restore</button>
        </div>
      {/each}
    {/if}
  </div>
</div>

{#if confirmingClear}
  <ConfirmDialog
    title="Restore all skipped items?"
    message={`This will un-skip all ${items.length} items so they can appear in scans again.`}
    confirmLabel="Restore all"
    oncancel={() => (confirmingClear = false)}
    onconfirm={doClearAll}
  />
{/if}
```

- [ ] **Step 3: Verify check/build**

Run: `cd frontend && npm run check && npm run build`
Expected: 0 ERRORS (grep the output for `ERROR` and for smart quotes U+201C/U+201D), build clean.

- [ ] **Step 4: Commit**

```bash
git add frontend/src/lib/components/SkippedManager.svelte
git commit -m "feat(skipped): SkippedManager content component (search, list, restore)"
```

---

### Task 3: Desktop + mobile entry points

**Files:**
- Modify: `frontend/src/routes/+page.svelte` (import `dismissedUrls`; add a "Skipped (N)" button near `<FilterBar />` ~line 363; render `ModalOverlay` + `SkippedManager` when open)
- Modify: `frontend/src/lib/components/mobile/MobileToolbar.svelte` (add an `onskipped` prop + a Skipped button)
- Modify: `frontend/src/lib/components/mobile/MobileScanView.svelte` (pass `onskipped`; render `BottomSheet` + `SkippedManager` when open)

**Interfaces:**
- Consumes Task 2's `SkippedManager`, existing `ModalOverlay`/`BottomSheet`, `dismissedUrls` (`$dismissedUrls.size`).

- [ ] **Step 1: Desktop button + modal**

In `frontend/src/routes/+page.svelte`:
- Add `dismissedUrls` to the existing `$lib/stores/results` import (line 12).
- Add imports: `import ModalOverlay from '$lib/components/ModalOverlay.svelte';` and `import SkippedManager from '$lib/components/SkippedManager.svelte';` (near the other component imports at the top).
- Add state near the other `$state` declarations: `let skippedOpen = $state(false);`
- Immediately AFTER `<FilterBar />` (line 363), add the trigger (only shown when there are skipped items):
```svelte
{#if $dismissedUrls.size > 0}
  <div class="flex justify-end px-1">
    <button
      class="text-xs px-2 py-1 rounded border border-[var(--border)] text-[var(--text-secondary)] hover:bg-[var(--bg-tertiary)]"
      onclick={() => (skippedOpen = true)}
    >Skipped ({$dismissedUrls.size})</button>
  </div>
{/if}

{#if skippedOpen}
  <ModalOverlay onclose={() => (skippedOpen = false)}>
    <div class="w-[min(32rem,92vw)]">
      <SkippedManager onclose={() => (skippedOpen = false)} />
    </div>
  </ModalOverlay>
{/if}
```
(Placement detail: if line 363 has drifted, anchor on the `<FilterBar />` usage instead. Keep the button in the scan toolbar region, not inside a results row.)

- [ ] **Step 2: Mobile toolbar button**

In `frontend/src/lib/components/mobile/MobileToolbar.svelte`:
- Add `onskipped` to the `Props` interface and the `$props()` destructure: `let { onfilters, ondeck, onskipped }: Props = $props();` (make `onskipped?: () => void` optional in the interface).
- Add a Skipped button alongside the existing search/filters/deck buttons (match their `flex-1 … text-xs` styling), e.g.:
```svelte
    <button class="flex-1 flex items-center justify-center gap-1.5 py-2 text-xs text-[var(--text-secondary)]" onclick={onskipped} aria-label="Skipped items">
      Skipped
    </button>
```

- [ ] **Step 3: Mobile sheet**

In `frontend/src/lib/components/mobile/MobileScanView.svelte`:
- Add imports: `import BottomSheet from '$lib/components/BottomSheet.svelte';` and `import SkippedManager from '$lib/components/SkippedManager.svelte';` (BottomSheet may already be imported — check first).
- Add state: `let skippedOpen = $state(false);`
- Pass the handler to the toolbar (line 166): `<MobileToolbar onfilters={() => (filterSheetOpen = true)} ondeck={() => (deckOpen = true)} onskipped={() => (skippedOpen = true)} />`
- Render the sheet (near the other sheets/overlays in this file):
```svelte
<BottomSheet open={skippedOpen} title="Skipped items" onclose={() => (skippedOpen = false)}>
  <SkippedManager onclose={() => (skippedOpen = false)} />
</BottomSheet>
```

- [ ] **Step 4: Verify check/build**

Run: `cd frontend && npm run check && npm run build`
Expected: 0 ERRORS (grep for `ERROR` and smart quotes), build clean.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/routes/+page.svelte frontend/src/lib/components/mobile/MobileToolbar.svelte frontend/src/lib/components/mobile/MobileScanView.svelte
git commit -m "feat(skipped): desktop + mobile entry points for the skipped-items manager"
```

---

## Deployment

This plan does NOT deploy. It joins the batch deploy (`docker compose up -d --build`) with the flat-movie-folders + split-part-suffix work, after the user reviews.
