# Mobile-Native Scan Experience Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give the phone (Tauri WebView + mobile browser) a first-class Scan experience — tuned poster wall, pull-to-refresh, swipe-to-grab/dismiss, haptics, bottom-sheet detail, one-handed toolbar, deck mode — without touching desktop markup or any shared store logic.

**Architecture:** Approach B "mobile view, shared brains". A single `isPhone` store forks the Scan route's results area into `<MobileScanView/>` (phone) vs the existing desktop markup (byte-identical). All new presentation lives in `frontend/src/lib/components/mobile/`; all data logic stays in the existing shared stores.

**Tech Stack:** SvelteKit 5 (runes), TypeScript, Tailwind (var(--*) theme tokens), vitest, `navigator.vibrate` for haptics.

**Spec:** `docs/superpowers/specs/2026-07-03-mobile-scan-native-design.md`

## Global Constraints

- Frontend-only. NO changes under `backend/`, `scripts/`, `data/`.
- Desktop markup in `frontend/src/routes/+page.svelte` stays **byte-identical** except: (a) the `{#if $isPhone}` wrap around the results area, (b) the DetailPanel fork, (c) importing extracted grouping helpers (script-only). No visual desktop change.
- Shared stores (`results.ts`, selection, filters), `resultActions.ts`, `api/client.ts` are NOT modified — with exactly two additive exceptions named in Tasks 4 and 9 (toast `action` param; FilterBar optional props). Default behavior unchanged.
- No new npm dependencies. Haptics = `navigator.vibrate` (decision: the Tauri haptics plugin would add a cargo dep + capability config for the same effect; `navigator.vibrate` works in Android WebView with the VIBRATE manifest permission and no-ops elsewhere).
- Undo semantics (decision, deviates from spec wording): swipe-**left** dismiss gets a real Undo (via existing `restoreItem`); swipe-**right** grab shows a "Grabbed" toast with NO undo — the grab sends to JDownloader and cannot be recalled, so an Undo button would lie.
- Verification per task: `cd frontend && npx vitest run` (all pass) + `npm run check` (0 errors; exactly the 3 pre-existing warnings — Tooltip.svelte ×1, downloads/+page.svelte ×2 — no new) + `npm run build` (succeeds). Component `.svelte` files have no test harness in this repo — pure TS gets TDD; components get check+build+code-review verification (established repo convention).
- Branch: `mobile-scan` off `main`. One commit per task. Do NOT `git add .superpowers/`. Do NOT deploy.
- Theme: always `var(--bg-primary/secondary/tertiary)`, `var(--text-primary/secondary)`, `var(--border)`, `var(--accent)` — never hardcoded colors (except the swipe underlays' green/amber, which use Tailwind `green-600`/`amber-600` like existing status colors).

## Verified interfaces (ground truth — do not re-derive)

- `results.ts` exports used: `results, statusFilter, searchFilter, filteredResults, filteredTotal, titleCounts, stats, pagedMode, hasMore, loadingMore, loadResults(reset), selectedKeys, toggleSelect, deselectAll, selectAll, dismissItem(url, title?) → Promise<boolean>, restoreItem(url) → Promise<boolean>, markDownloaded(urls), markGrabbedSiblings(url), selectedDetail (writable<ScanResult|null>), deckResults, viewMode, StatusFilter`
- Grab = `api.download(item.url, item.title, get(downloadHost), item.year, item.resolution || '', item.size || '', item.hdr || '', item.dovi ?? false)` (mirrors `ResultTile.handleDownload`); `downloadHost` from `$lib/stores/downloads`.
- `addToast(title, body, priority)` in `$lib/stores/notifications` (Task 4 adds optional `action`).
- `ResultTile` props: `{ item, focused?, onmore? }`. `GroupTile` props: `{ title, items, count, formats, statusSummary, sizeRange, dateRange, onToggle }`.
- `ResultActionSheet` props: `{ item: ScanResult|null, onclose }` — its `buildResultActions(item, $downloadHost, selected)` list ALREADY includes a Select/Deselect entry.
- `BottomSheet` props: `{ open, title?, onclose, children }` (has drag-down dismiss).
- `SwipeDeck` — store-driven, takes no props; renders the deck over `deckResults`.
- `FilterBar` — no props today; internal `let filterSheet = $state(false)`; mobile row at its template top is `md:hidden` (status chips + Filters button → BottomSheet "View & filters" which already contains sort/display/date controls).
- `DetailPanel` props: `{ item, onclose }`, rendered at `+page.svelte` ~line 842 inside `{#if $selectedDetail}`.
- `ScanResult` fields: `url, title, year, status, resolution, size, group_key, rating (number|null), poster_url, hdr, dovi, posted_date, description?, genres?, plex_*`.

## File structure

```
frontend/src/lib/stores/viewport.ts                 (new) isPhone store
frontend/src/lib/stores/viewport.test.ts            (new)
frontend/src/lib/grouping.ts                        (new) pure grouping helpers extracted from +page.svelte
frontend/src/lib/grouping.test.ts                   (new)
frontend/src/lib/components/mobile/gestures.ts      (new) pure drag state machine
frontend/src/lib/components/mobile/gestures.test.ts (new)
frontend/src/lib/components/mobile/haptics.ts       (new) vibrate wrapper
frontend/src/lib/components/mobile/haptics.test.ts  (new)
frontend/src/lib/components/mobile/PullToRefresh.svelte (new)
frontend/src/lib/components/mobile/SwipeableTile.svelte (new)
frontend/src/lib/components/mobile/DetailSheet.svelte   (new)
frontend/src/lib/components/mobile/MobileToolbar.svelte (new)
frontend/src/lib/components/mobile/MobileScanView.svelte(new)
frontend/src/lib/stores/notifications.ts            (modify, additive: action param)
frontend/src/lib/components/Snackbar.svelte         (modify: render action button)
frontend/src/lib/components/FilterBar.svelte        (modify, additive: sheetOpen bindable + showMobileTrigger props)
frontend/src/routes/+page.svelte                    (modify: import grouping helpers; isPhone fork; DetailSheet fork)
frontend/src-tauri/gen/android/app/src/main/AndroidManifest.xml (modify: VIBRATE permission)
```

---

### Task 1: `isPhone` viewport store

**Files:**
- Create: `frontend/src/lib/stores/viewport.ts`
- Test: `frontend/src/lib/stores/viewport.test.ts`

**Interfaces:**
- Produces: `export const isPhone: Readable<boolean>` — true iff `(max-width: 767px)` AND `(pointer: coarse)` both match; updates live; `false` when `window` is absent (SSR-safe).

- [ ] **Step 1: Write the failing test**

```ts
// frontend/src/lib/stores/viewport.test.ts
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { get } from 'svelte/store';

type MqlListener = (e: { matches: boolean }) => void;

function mockMatchMedia(initial: Record<string, boolean>) {
  const listeners = new Map<string, MqlListener[]>();
  const state = { ...initial };
  vi.stubGlobal('matchMedia', (query: string) => ({
    get matches() { return state[query] ?? false; },
    media: query,
    addEventListener: (_: string, cb: MqlListener) => {
      listeners.set(query, [...(listeners.get(query) ?? []), cb]);
    },
    removeEventListener: () => {}
  }));
  return {
    set(query: string, matches: boolean) {
      state[query] = matches;
      for (const cb of listeners.get(query) ?? []) cb({ matches });
    }
  };
}

const NARROW = '(max-width: 767px)';
const COARSE = '(pointer: coarse)';

describe('isPhone', () => {
  beforeEach(() => vi.resetModules());

  it('is true only when narrow AND coarse', async () => {
    mockMatchMedia({ [NARROW]: true, [COARSE]: true });
    const { isPhone } = await import('./viewport');
    expect(get(isPhone)).toBe(true);
  });

  it('is false for a narrow desktop window (fine pointer)', async () => {
    mockMatchMedia({ [NARROW]: true, [COARSE]: false });
    const { isPhone } = await import('./viewport');
    expect(get(isPhone)).toBe(false);
  });

  it('updates live when the viewport changes (rotate/resize)', async () => {
    const mql = mockMatchMedia({ [NARROW]: false, [COARSE]: true });
    const { isPhone } = await import('./viewport');
    expect(get(isPhone)).toBe(false);
    mql.set(NARROW, true);
    expect(get(isPhone)).toBe(true);
  });
});
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd frontend && npx vitest run src/lib/stores/viewport.test.ts`
Expected: FAIL — `Cannot find module './viewport'`.

- [ ] **Step 3: Implement**

```ts
// frontend/src/lib/stores/viewport.ts
import { readable } from 'svelte/store';

const NARROW = '(max-width: 767px)';
const COARSE = '(pointer: coarse)';

/** True on phone-class devices: narrow viewport AND coarse (touch) pointer.
 *  A narrow desktop window stays desktop. SSR-safe (false without window).
 *  Single source of truth for the phone/desktop fork — components must not
 *  re-derive their own media queries. */
export const isPhone = readable(false, (set) => {
  if (typeof window === 'undefined' || typeof window.matchMedia !== 'function') {
    set(false);
    return;
  }
  const narrow = window.matchMedia(NARROW);
  const coarse = window.matchMedia(COARSE);
  const update = () => set(narrow.matches && coarse.matches);
  update();
  narrow.addEventListener('change', update);
  coarse.addEventListener('change', update);
  return () => {
    narrow.removeEventListener('change', update);
    coarse.removeEventListener('change', update);
  };
});
```

Note: `readable` only runs the setup when subscribed; the test's `get()` subscribes momentarily, which triggers setup synchronously — the live-update test must keep a subscription open if `get()` proves flaky; in that case use `const un = isPhone.subscribe(v => last = v)` in the test instead.

- [ ] **Step 4: Run to verify pass**

Run: `cd frontend && npx vitest run src/lib/stores/viewport.test.ts`
Expected: 3 passed.

- [ ] **Step 5: Full verify + commit**

Run: `cd frontend && npx vitest run && npm run check` — all green, 0 errors, no new warnings.

```bash
git add frontend/src/lib/stores/viewport.ts frontend/src/lib/stores/viewport.test.ts
git commit -m "mobile: isPhone viewport store (narrow AND coarse, live, SSR-safe)"
```

---

### Task 2: `gestures.ts` — pure drag state machine

**Files:**
- Create: `frontend/src/lib/components/mobile/gestures.ts`
- Test: `frontend/src/lib/components/mobile/gestures.test.ts`

**Interfaces:**
- Produces:
  ```ts
  export type Axis = 'x' | 'y';
  export interface DragState { dx: number; dy: number; active: boolean; locked: Axis | null; }
  export interface DragEnd { committed: boolean; direction: 'left'|'right'|'up'|'down'|null; }
  export function createDragTracker(opts: { axis: Axis; threshold: number; lockSlop?: number }): {
    start(x: number, y: number): void;
    move(x: number, y: number): DragState;   // dx/dy are 0 until the axis lock resolves to opts.axis
    end(): DragEnd;                           // committed when |delta on axis| >= threshold
    cancel(): void;
    readonly state: DragState;
  }
  ```
- Axis lock: the first movement whose distance exceeds `lockSlop` (default 8px) decides — if its dominant component is the tracker's axis, the gesture locks to it (subsequent `move` reports deltas); otherwise it locks to the other axis and the tracker reports zeros forever (scroll wins).

- [ ] **Step 1: Write the failing test**

```ts
// frontend/src/lib/components/mobile/gestures.test.ts
import { describe, it, expect } from 'vitest';
import { createDragTracker } from './gestures';

describe('createDragTracker (axis=x)', () => {
  it('locks to x when first movement is horizontal, reports dx', () => {
    const t = createDragTracker({ axis: 'x', threshold: 80 });
    t.start(100, 100);
    const s = t.move(120, 103); // 20px right, 3px down → locks x
    expect(s.locked).toBe('x');
    expect(s.dx).toBe(20);
  });

  it('yields to vertical scroll: locks y, reports zero deltas', () => {
    const t = createDragTracker({ axis: 'x', threshold: 80 });
    t.start(100, 100);
    const s = t.move(103, 130); // mostly vertical → locks y
    expect(s.locked).toBe('y');
    expect(s.dx).toBe(0);
    // even a later horizontal move stays dead — scroll owns the gesture
    expect(t.move(200, 130).dx).toBe(0);
  });

  it('stays unlocked inside the slop', () => {
    const t = createDragTracker({ axis: 'x', threshold: 80, lockSlop: 8 });
    t.start(100, 100);
    expect(t.move(104, 102).locked).toBe(null);
  });

  it('commits right past threshold, with direction', () => {
    const t = createDragTracker({ axis: 'x', threshold: 80 });
    t.start(0, 0);
    t.move(90, 0);
    expect(t.end()).toEqual({ committed: true, direction: 'right' });
  });

  it('does not commit below threshold', () => {
    const t = createDragTracker({ axis: 'x', threshold: 80 });
    t.start(0, 0);
    t.move(-50, 0);
    expect(t.end()).toEqual({ committed: false, direction: 'left' });
  });

  it('cancel resets everything', () => {
    const t = createDragTracker({ axis: 'x', threshold: 80 });
    t.start(0, 0);
    t.move(90, 0);
    t.cancel();
    expect(t.end()).toEqual({ committed: false, direction: null });
    expect(t.state.dx).toBe(0);
  });
});

describe('createDragTracker (axis=y) — pull-to-refresh shape', () => {
  it('locks to y on a downward pull and commits down', () => {
    const t = createDragTracker({ axis: 'y', threshold: 70 });
    t.start(50, 0);
    const s = t.move(52, 40);
    expect(s.locked).toBe('y');
    expect(s.dy).toBe(40);
    t.move(52, 80);
    expect(t.end()).toEqual({ committed: true, direction: 'down' });
  });
});
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd frontend && npx vitest run src/lib/components/mobile/gestures.test.ts`
Expected: FAIL — module not found.

- [ ] **Step 3: Implement**

```ts
// frontend/src/lib/components/mobile/gestures.ts
/** Pure touch-drag state machine with axis locking.
 *  DOM-free: feed it coordinates from pointer/touch events; it answers
 *  whether the gesture belongs to us (locked to our axis) or to the
 *  scroller (locked to the other axis), and whether release commits. */

export type Axis = 'x' | 'y';

export interface DragState {
  dx: number;
  dy: number;
  active: boolean;
  locked: Axis | null;
}

export interface DragEnd {
  committed: boolean;
  direction: 'left' | 'right' | 'up' | 'down' | null;
}

const IDLE: DragState = { dx: 0, dy: 0, active: false, locked: null };

export function createDragTracker(opts: { axis: Axis; threshold: number; lockSlop?: number }) {
  const lockSlop = opts.lockSlop ?? 8;
  let startX = 0;
  let startY = 0;
  let cur: DragState = { ...IDLE };

  function start(x: number, y: number): void {
    startX = x;
    startY = y;
    cur = { dx: 0, dy: 0, active: true, locked: null };
  }

  function move(x: number, y: number): DragState {
    if (!cur.active) return cur;
    const rawDx = x - startX;
    const rawDy = y - startY;
    if (cur.locked === null) {
      if (Math.hypot(rawDx, rawDy) < lockSlop) return cur;
      const dominant: Axis = Math.abs(rawDx) >= Math.abs(rawDy) ? 'x' : 'y';
      cur = { ...cur, locked: dominant };
    }
    if (cur.locked !== opts.axis) return cur; // the scroller owns this gesture
    cur = {
      ...cur,
      dx: opts.axis === 'x' ? rawDx : 0,
      dy: opts.axis === 'y' ? rawDy : 0
    };
    return cur;
  }

  function end(): DragEnd {
    const delta = opts.axis === 'x' ? cur.dx : cur.dy;
    const committed = cur.locked === opts.axis && Math.abs(delta) >= opts.threshold;
    let direction: DragEnd['direction'] = null;
    if (cur.locked === opts.axis && delta !== 0) {
      direction = opts.axis === 'x' ? (delta > 0 ? 'right' : 'left') : (delta > 0 ? 'down' : 'up');
    }
    cur = { ...IDLE };
    return { committed, direction };
  }

  function cancel(): void {
    cur = { ...IDLE };
  }

  return {
    start, move, end, cancel,
    get state() { return cur; }
  };
}
```

- [ ] **Step 4: Run to verify pass**

Run: `cd frontend && npx vitest run src/lib/components/mobile/gestures.test.ts`
Expected: 8 passed.

- [ ] **Step 5: Full verify + commit**

Run: `cd frontend && npx vitest run && npm run check` — green.

```bash
git add frontend/src/lib/components/mobile/gestures.ts frontend/src/lib/components/mobile/gestures.test.ts
git commit -m "mobile: pure drag state machine with axis lock (gestures.ts)"
```

---

### Task 3: `haptics.ts` + VIBRATE permission

**Files:**
- Create: `frontend/src/lib/components/mobile/haptics.ts`
- Test: `frontend/src/lib/components/mobile/haptics.test.ts`
- Modify: `frontend/src-tauri/gen/android/app/src/main/AndroidManifest.xml` (add one line)

**Interfaces:**
- Produces: `export function tap(): void` (10ms), `export function success(): void` ([15, 60, 15] pattern), `export function warning(): void` (35ms). Each silently no-ops when `navigator.vibrate` is absent (desktop browsers, SSR).

- [ ] **Step 1: Write the failing test**

```ts
// frontend/src/lib/components/mobile/haptics.test.ts
import { describe, it, expect, vi, afterEach } from 'vitest';
import { tap, success, warning } from './haptics';

afterEach(() => vi.unstubAllGlobals());

describe('haptics', () => {
  it('tap vibrates 10ms when supported', () => {
    const vibrate = vi.fn();
    vi.stubGlobal('navigator', { vibrate });
    tap();
    expect(vibrate).toHaveBeenCalledWith(10);
  });

  it('success uses a pattern', () => {
    const vibrate = vi.fn();
    vi.stubGlobal('navigator', { vibrate });
    success();
    expect(vibrate).toHaveBeenCalledWith([15, 60, 15]);
  });

  it('silently no-ops without navigator.vibrate', () => {
    vi.stubGlobal('navigator', {});
    expect(() => { tap(); success(); warning(); }).not.toThrow();
  });
});
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd frontend && npx vitest run src/lib/components/mobile/haptics.test.ts`
Expected: FAIL — module not found.

- [ ] **Step 3: Implement**

```ts
// frontend/src/lib/components/mobile/haptics.ts
/** Haptic feedback via navigator.vibrate — works in the Android WebView
 *  (with VIBRATE manifest permission) and Android Chrome; silently no-ops
 *  everywhere else (desktop, iOS Safari, SSR). Deliberately NOT the Tauri
 *  haptics plugin: same effect, zero cargo/capability surface. */

function vibrate(pattern: number | number[]): void {
  try {
    if (typeof navigator !== 'undefined' && typeof navigator.vibrate === 'function') {
      navigator.vibrate(pattern);
    }
  } catch {
    /* never let a haptic break a gesture */
  }
}

/** Tiny tick — swipe crossed its commit threshold, pull-to-refresh armed. */
export function tap(): void { vibrate(10); }

/** Double-pulse — action succeeded (grab sent, refresh done). */
export function success(): void { vibrate([15, 60, 15]); }

/** Single firmer buzz — destructive-ish commit (dismiss) or error. */
export function warning(): void { vibrate(35); }
```

- [ ] **Step 4: Add VIBRATE permission**

In `frontend/src-tauri/gen/android/app/src/main/AndroidManifest.xml`, alongside the existing `<uses-permission android:name="android.permission.INTERNET" />` line, add:

```xml
<uses-permission android:name="android.permission.VIBRATE" />
```

- [ ] **Step 5: Run to verify pass + commit**

Run: `cd frontend && npx vitest run src/lib/components/mobile/haptics.test.ts` → 3 passed. Then `npx vitest run && npm run check` — green.

```bash
git add frontend/src/lib/components/mobile/haptics.ts frontend/src/lib/components/mobile/haptics.test.ts frontend/src-tauri/gen/android/app/src/main/AndroidManifest.xml
git commit -m "mobile: haptics wrapper (navigator.vibrate) + VIBRATE permission"
```

---

### Task 4: Toast action buttons (additive)

**Files:**
- Modify: `frontend/src/lib/stores/notifications.ts`
- Modify: `frontend/src/lib/components/Snackbar.svelte`
- Test: extend `frontend/src/lib/stores/notifications.test.ts` (create if absent)

**Interfaces:**
- Produces: `addToast(title, body, priority = 'normal', action?: { label: string; run: () => void })`. `Toast` gains optional `action`. Existing 3-arg callers unaffected. `Snackbar` renders the action as a button that runs `action.run()` then dismisses the toast.

- [ ] **Step 1: Write the failing test**

```ts
// frontend/src/lib/stores/notifications.test.ts  (create; if the file exists, append the describe block)
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { get } from 'svelte/store';

vi.mock('./connection', () => ({ connection: { on: vi.fn() } }));

describe('addToast action support', () => {
  beforeEach(() => vi.resetModules());

  it('carries an optional action and stays back-compatible', async () => {
    const { addToast, toasts } = await import('./notifications');
    addToast('Plain', 'no action');
    const run = vi.fn();
    addToast('Dismissed', 'Boxcar Bertha', 'normal', { label: 'Undo', run });
    const list = get(toasts);
    expect(list[1].action).toBeUndefined();
    expect(list[0].action?.label).toBe('Undo');
    list[0].action?.run();
    expect(run).toHaveBeenCalled();
  });
});
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd frontend && npx vitest run src/lib/stores/notifications.test.ts`
Expected: FAIL — `action` undefined on Toast / TS error.

- [ ] **Step 3: Implement store change**

In `frontend/src/lib/stores/notifications.ts`:

```ts
export interface ToastAction {
  label: string;
  run: () => void;
}

export interface Toast {
  id: string;
  title: string;
  body: string;
  priority: string;
  timestamp: number;
  action?: ToastAction;
}
```

and change the function signature + object literal:

```ts
export function addToast(
  title: string,
  body: string,
  priority = 'normal',
  action?: ToastAction
) {
  const id = crypto.randomUUID();
  const toast: Toast = { id, title, body, priority, timestamp: Date.now(), action };
  // ...rest of the function body unchanged...
```

- [ ] **Step 4: Render the action in Snackbar.svelte**

Inside the toast card in `frontend/src/lib/components/Snackbar.svelte`, after the `<p class="text-xs ...">{toast.body}</p>` line, add:

```svelte
          {#if toast.action}
            <button
              class="mt-1.5 text-xs font-semibold text-[var(--accent)] hover:underline"
              onclick={() => { toast.action?.run(); dismissToast(toast.id); }}
            >{toast.action.label}</button>
          {/if}
```

- [ ] **Step 5: Verify + commit**

Run: `cd frontend && npx vitest run && npm run check && npm run build` — green, no new warnings.

```bash
git add frontend/src/lib/stores/notifications.ts frontend/src/lib/stores/notifications.test.ts frontend/src/lib/components/Snackbar.svelte
git commit -m "toasts: optional action button (additive) for undo snackbars"
```

---

### Task 5: Extract grouping helpers to `lib/grouping.ts`

The phone grid needs the same title-grouping logic the desktop grid uses, currently trapped inside `+page.svelte`'s script. Extract the PURE functions; import them back so desktop behavior is unchanged (markup untouched).

**Files:**
- Create: `frontend/src/lib/grouping.ts`
- Test: `frontend/src/lib/grouping.test.ts`
- Modify: `frontend/src/routes/+page.svelte` (script only: delete the moved function bodies, import them instead)

**Interfaces:**
- Produces (moved verbatim from `+page.svelte`, signatures made explicit):
  ```ts
  export interface ResultGroup { title: string; items: ScanResult[]; }
  export interface GroupFormats { res: string[]; dv: boolean; hdr: boolean; }
  export function groupResults(items: ScanResult[]): ResultGroup[];          // the grouping body of groupedResults() WITHOUT store reads
  export function isDuplicateGroup(group: ResultGroup, titleCounts: Record<string, number>, paged: boolean): boolean;
  export function groupSizeRange(items: ScanResult[]): string;
  export function groupDateRange(items: ScanResult[]): string;
  export function groupStatusSummary(items: ScanResult[]): { status: string; count: number }[];
  export function groupFormats(items: ScanResult[]): GroupFormats;
  ```
- IMPORTANT — store-read separation: today's `groupedResults()`/`isDuplicateGroup()` in `+page.svelte` read stores (`$filteredResults`, `siblingCounts()`, `$pagedMode`) directly. The extracted versions take those values as PARAMETERS. `+page.svelte` keeps thin local wrappers with the ORIGINAL names that pass the store values in — so its markup does not change at all:
  ```ts
  // in +page.svelte script, replacing the moved bodies:
  import { groupResults, isDuplicateGroup as isDupGroup, groupSizeRange, groupDateRange, groupStatusSummary, groupFormats, type ResultGroup, type GroupFormats } from '$lib/grouping';
  function groupedResults(): ResultGroup[] { return groupResults(renderedResults()); }   // keep whatever source list the current body uses — read the existing body first and preserve it EXACTLY
  function isDuplicateGroup(group: ResultGroup) { return isDupGroup(group, siblingCounts(), $pagedMode); }
  ```
  The implementer MUST read the current bodies first (`+page.svelte` ~lines 130-260) and move logic verbatim — parameterizing only the store reads. If a helper reads additional stores not listed here, parameterize those identically.

- [ ] **Step 1: Write the failing test** (behavioral snapshot of the moved logic)

```ts
// frontend/src/lib/grouping.test.ts
import { describe, it, expect } from 'vitest';
import { groupResults, isDuplicateGroup, groupSizeRange, groupStatusSummary, groupFormats } from './grouping';
import type { ScanResult } from './api/types';

const r = (over: Partial<ScanResult>): ScanResult => ({
  url: over.url ?? Math.random().toString(36), title: 'Dune', year: 2021, status: 'missing',
  resolution: '2160p', size: '20 GB', group_key: over.url ?? 'k', rating: null,
  poster_url: '', hdr: 'HDR10', posted_date: null, ...over
} as ScanResult);

describe('grouping', () => {
  it('groups same-title items preserving order', () => {
    const groups = groupResults([r({ title: 'Dune', url: 'a' }), r({ title: 'Blade', url: 'b' }), r({ title: 'Dune', url: 'c' })]);
    expect(groups.map(g => g.title)).toEqual(['Dune', 'Blade']);
    expect(groups[0].items.map(i => i.url)).toEqual(['a', 'c']);
  });

  it('isDuplicateGroup uses titleCounts in paged mode', () => {
    const g = { title: 'Dune', items: [r({ url: 'a' })] };
    expect(isDuplicateGroup(g, { Dune: 3 }, true)).toBe(true);   // server says 3 siblings
    expect(isDuplicateGroup(g, {}, true)).toBe(false);
    expect(isDuplicateGroup({ title: 'Dune', items: [r({ url: 'a' }), r({ url: 'b' })] }, {}, false)).toBe(true); // live mode: local count
  });

  it('groupFormats aggregates resolutions + dv/hdr flags', () => {
    const f = groupFormats([r({ resolution: '2160p', dovi: true } as Partial<ScanResult>), r({ resolution: '1080p', hdr: '' })]);
    expect(f.res).toContain('2160p');
    expect(f.res).toContain('1080p');
    expect(f.dv).toBe(true);
  });

  it('groupSizeRange and groupStatusSummary produce non-empty strings/entries', () => {
    const items = [r({ size: '10 GB', status: 'missing' }), r({ size: '20 GB', status: 'library' })];
    expect(groupSizeRange(items)).toBeTruthy();
    expect(groupStatusSummary(items).length).toBe(2);
  });
});
```

NOTE: these assertions encode the EXPECTED shape; if the verbatim-moved logic differs (e.g. isDuplicateGroup's exact live-mode rule), adjust the TEST to match the moved code's actual behavior — the contract of this task is "behavior identical to before the move", not the test author's guess.

- [ ] **Step 2: Run to verify it fails** — module not found.

- [ ] **Step 3: Move the functions** — create `lib/grouping.ts` with the verbatim bodies (parameterized store reads), delete them from `+page.svelte`, add the import + thin wrappers shown above. Markup untouched.

- [ ] **Step 4: Verify**

Run: `cd frontend && npx vitest run && npm run check && npm run build`
Expected: grouping tests pass; ALL existing tests pass; 0 errors; build OK. Desktop grid still groups identically (check/build + unchanged store tests are the guard).

- [ ] **Step 5: Commit**

```bash
git add frontend/src/lib/grouping.ts frontend/src/lib/grouping.test.ts frontend/src/routes/+page.svelte
git commit -m "refactor: extract pure title-grouping helpers to lib/grouping (script-only, markup untouched)"
```

---

### Task 6: `PullToRefresh.svelte`

**Files:**
- Create: `frontend/src/lib/components/mobile/PullToRefresh.svelte`

**Interfaces:**
- Consumes: `createDragTracker` (Task 2), `tap`/`success` (Task 3).
- Produces: `<PullToRefresh onrefresh={async () => …} disabled={false}> {children} </PullToRefresh>` — wraps a scrollable region; engages only when the wrapped scroller is at `scrollTop === 0`; elastic pull (×0.45 damping, max 110px); indicator arm at 70px (haptic `tap()`); on release past threshold shows a spinner, awaits `onrefresh()`, haptic `success()`, settles back.

- [ ] **Step 1: Implement**

```svelte
<!-- frontend/src/lib/components/mobile/PullToRefresh.svelte -->
<script lang="ts">
  import type { Snippet } from 'svelte';
  import { createDragTracker } from './gestures';
  import { tap, success } from './haptics';

  interface Props {
    onrefresh: () => Promise<void> | void;
    disabled?: boolean;
    children: Snippet;
  }
  let { onrefresh, disabled = false, children }: Props = $props();

  const TRIGGER = 70;   // px of (damped) pull that arms the refresh
  const MAX_PULL = 110;
  const DAMP = 0.45;

  let scroller: HTMLDivElement | undefined = $state();
  let pull = $state(0);          // damped visual offset
  let refreshing = $state(false);
  let armed = $state(false);

  const tracker = createDragTracker({ axis: 'y', threshold: TRIGGER / DAMP });

  function onPointerDown(e: PointerEvent) {
    if (disabled || refreshing) return;
    if ((scroller?.scrollTop ?? 1) > 0) return; // only from the very top
    tracker.start(e.clientX, e.clientY);
  }

  function onPointerMove(e: PointerEvent) {
    if (disabled || refreshing || !tracker.state.active) return;
    const s = tracker.move(e.clientX, e.clientY);
    if (s.locked !== 'y' || s.dy <= 0) { pull = 0; armed = false; return; }
    // While pulling, keep the browser from scrolling/overscroll-glow.
    e.preventDefault();
    pull = Math.min(s.dy * DAMP, MAX_PULL);
    const nowArmed = pull >= TRIGGER;
    if (nowArmed && !armed) tap();
    armed = nowArmed;
  }

  async function onPointerUp() {
    if (!tracker.state.active) return;
    tracker.end();
    if (armed && !refreshing) {
      refreshing = true;
      pull = TRIGGER; // hold at spinner height
      try { await onrefresh(); success(); } finally {
        refreshing = false;
        pull = 0;
        armed = false;
      }
    } else {
      pull = 0;
      armed = false;
    }
  }
</script>

<div class="relative h-full min-h-0 flex flex-col overflow-hidden">
  <!-- Indicator -->
  <div
    class="absolute inset-x-0 top-0 z-10 flex justify-center pointer-events-none transition-opacity"
    style="height: {pull}px; opacity: {pull > 8 ? 1 : 0};"
    aria-hidden="true"
  >
    <div class="flex items-end pb-1">
      {#if refreshing}
        <div class="w-5 h-5 border-2 border-[var(--accent)] border-t-transparent rounded-full animate-spin"></div>
      {:else}
        <svg class="w-5 h-5 text-[var(--text-secondary)] transition-transform {armed ? 'rotate-180 text-[var(--accent)]' : ''}"
          fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2">
          <path stroke-linecap="round" stroke-linejoin="round" d="M19 14l-7 7m0 0l-7-7m7 7V3" />
        </svg>
      {/if}
    </div>
  </div>

  <div
    bind:this={scroller}
    class="flex-1 min-h-0 overflow-y-auto overscroll-y-contain transition-transform duration-150"
    style="transform: translateY({pull}px); touch-action: pan-y;"
    onpointerdown={onPointerDown}
    onpointermove={onPointerMove}
    onpointerup={onPointerUp}
    onpointercancel={onPointerUp}
  >
    {@render children()}
  </div>
</div>
```

Implementation notes for the engineer:
- `e.preventDefault()` inside `pointermove` requires the handler to be non-passive; Svelte attaches pointer events non-passively by default — verify by testing that the page does not scroll while pulling (if it does, add `style="touch-action: none"` WHILE `pull > 0` instead of preventDefault).
- The scroll container OWNS scrolling for the wall — MobileScanView (Task 10) renders the grid inside this component, not in an outer scroller.

- [ ] **Step 2: Verify + commit**

Run: `cd frontend && npx vitest run && npm run check && npm run build` — green, no new warnings (add ARIA/role only if check flags — the wrapper divs carry handlers, so if `a11y_no_static_element_interactions` fires, add `role="presentation"` to the scroller div).

```bash
git add frontend/src/lib/components/mobile/PullToRefresh.svelte
git commit -m "mobile: PullToRefresh wrapper (elastic, haptic, top-anchored)"
```

---

### Task 7: `SwipeableTile.svelte`

**Files:**
- Create: `frontend/src/lib/components/mobile/SwipeableTile.svelte`

**Interfaces:**
- Consumes: `createDragTracker` (Task 2), `tap`/`warning` (Task 3).
- Produces:
  ```svelte
  <SwipeableTile
    onswiperight={() => …}  <!-- grab -->
    onswipeleft={() => …}   <!-- dismiss -->
    onlongpress={() => …}   <!-- action sheet -->
    disabled={false}         <!-- group cards: true -->
  > {children} </SwipeableTile>
  ```
  Right underlay: green download arrow. Left underlay: amber eye-off. Haptic `tap()` when crossing the commit threshold (72px), `warning()` on a committed left-swipe. Tile springs back unless committed; a committed swipe animates off slightly then calls the handler. Long-press = 450ms hold with <6px movement (suppresses the subsequent click). Tap/click passes through to the child.

- [ ] **Step 1: Implement**

```svelte
<!-- frontend/src/lib/components/mobile/SwipeableTile.svelte -->
<script lang="ts">
  import type { Snippet } from 'svelte';
  import { createDragTracker } from './gestures';
  import { tap, warning } from './haptics';

  interface Props {
    onswiperight?: () => void;
    onswipeleft?: () => void;
    onlongpress?: () => void;
    disabled?: boolean;
    children: Snippet;
  }
  let { onswiperight, onswipeleft, onlongpress, disabled = false, children }: Props = $props();

  const THRESHOLD = 72;
  const LONGPRESS_MS = 450;

  let dx = $state(0);
  let animating = $state(false);
  let crossed = false;
  let longpressTimer: ReturnType<typeof setTimeout> | null = null;
  let longpressed = false;

  const tracker = createDragTracker({ axis: 'x', threshold: THRESHOLD });

  function clearLongpress() {
    if (longpressTimer) { clearTimeout(longpressTimer); longpressTimer = null; }
  }

  function onPointerDown(e: PointerEvent) {
    if (disabled) return;
    longpressed = false;
    tracker.start(e.clientX, e.clientY);
    if (onlongpress) {
      longpressTimer = setTimeout(() => {
        // still essentially stationary → it's a hold
        if (Math.abs(tracker.state.dx) < 6 && tracker.state.locked === null) {
          longpressed = true;
          onlongpress();
        }
      }, LONGPRESS_MS);
    }
  }

  function onPointerMove(e: PointerEvent) {
    if (disabled || !tracker.state.active) return;
    const s = tracker.move(e.clientX, e.clientY);
    if (s.locked === 'x') {
      clearLongpress();
      dx = s.dx;
      const over = Math.abs(dx) >= THRESHOLD;
      if (over && !crossed) tap();
      crossed = over;
    } else if (s.locked === 'y') {
      clearLongpress();
      dx = 0;
    }
  }

  function onPointerUp() {
    clearLongpress();
    if (!tracker.state.active) return;
    const { committed, direction } = tracker.end();
    crossed = false;
    if (committed && direction === 'right' && onswiperight) {
      animating = true;
      dx = THRESHOLD * 1.4;
      setTimeout(() => { onswiperight(); dx = 0; animating = false; }, 120);
    } else if (committed && direction === 'left' && onswipeleft) {
      warning();
      animating = true;
      dx = -THRESHOLD * 1.4;
      setTimeout(() => { onswipeleft(); dx = 0; animating = false; }, 120);
    } else {
      dx = 0; // spring back
    }
  }

  function onClickCapture(e: MouseEvent) {
    // A long-press or a horizontal swipe must not ALSO count as a tap.
    if (longpressed || Math.abs(dx) > 6) {
      e.stopPropagation();
      e.preventDefault();
    }
  }
</script>

<div class="relative overflow-hidden rounded-lg" role="presentation">
  <!-- Underlays -->
  <div class="absolute inset-0 flex items-center justify-between px-4 rounded-lg
      {dx > 0 ? 'bg-green-600/80' : dx < 0 ? 'bg-amber-600/80' : ''}"
    style="opacity: {Math.min(Math.abs(dx) / THRESHOLD, 1)};" aria-hidden="true">
    <svg class="w-6 h-6 text-white {dx > 0 ? '' : 'invisible'}" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2">
      <path stroke-linecap="round" stroke-linejoin="round" d="M4 16v1a3 3 0 003 3h10a3 3 0 003-3v-1m-4-4l-4 4m0 0l-4-4m4 4V4" />
    </svg>
    <svg class="w-6 h-6 text-white {dx < 0 ? '' : 'invisible'}" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2">
      <path stroke-linecap="round" stroke-linejoin="round" d="M13.875 18.825A10.05 10.05 0 0112 19c-4.478 0-8.268-2.943-9.543-7a9.97 9.97 0 011.563-3.029m5.858.908a3 3 0 114.243 4.243M9.878 9.878l4.242 4.242M9.88 9.88l-3.29-3.29m7.532 7.532l3.29 3.29M3 3l3.59 3.59m0 0A9.953 9.953 0 0112 5c4.478 0 8.268 2.943 9.543 7a10.025 10.025 0 01-4.132 5.411m0 0L21 21" />
    </svg>
  </div>
  <!-- The tile -->
  <div
    class="relative {animating ? 'transition-transform duration-100' : dx === 0 ? 'transition-transform duration-150' : ''}"
    style="transform: translateX({dx}px); touch-action: pan-y;"
    onpointerdown={onPointerDown}
    onpointermove={onPointerMove}
    onpointerup={onPointerUp}
    onpointercancel={onPointerUp}
    onclickcapture={onClickCapture}
    role="presentation"
  >
    {@render children()}
  </div>
</div>
```

- [ ] **Step 2: Verify + commit**

Run: `cd frontend && npx vitest run && npm run check && npm run build` — green, no new warnings.

```bash
git add frontend/src/lib/components/mobile/SwipeableTile.svelte
git commit -m "mobile: SwipeableTile (swipe right=grab, left=dismiss, long-press, underlays, haptics)"
```

---

### Task 8: `DetailSheet.svelte`

**Files:**
- Create: `frontend/src/lib/components/mobile/DetailSheet.svelte`

**Interfaces:**
- Consumes: `ScanResult` type; `api.download` + `downloadHost` (grab, mirroring ResultTile.handleDownload); `markDownloaded`, `markGrabbedSiblings` from results store; `addToast`; `statusVariant`, `formatStatus` from `$lib/constants`; `Badge`.
- Produces: `<DetailSheet item={ScanResult} siblings={ScanResult[]} onclose={() => …} onselect={(s: ScanResult) => …} />` — bottom sheet at ~55vh, drag handle expands to 92vh, drag down (or scrim tap / Escape) closes. Pinned bottom action: full-width **Grab** (or **Copy links** when status is `library`). Sibling releases listed when `siblings.length > 1`; tapping one calls `onselect(s)`.
- Focus: traps Tab within the sheet; restores focus to the previously-focused element on close (mirror DetailPanel's pattern — read `DetailPanel.svelte`'s trap first and copy its mechanism).

- [ ] **Step 1: Implement**

```svelte
<!-- frontend/src/lib/components/mobile/DetailSheet.svelte -->
<script lang="ts">
  import type { ScanResult } from '$lib/api/types';
  import { api } from '$lib/api/client';
  import { downloadHost } from '$lib/stores/downloads';
  import { markDownloaded, markGrabbedSiblings } from '$lib/stores/results';
  import { addToast } from '$lib/stores/notifications';
  import { copyResultLinks } from '$lib/resultActions';
  import { statusVariant, formatStatus } from '$lib/constants';
  import Badge from '../Badge.svelte';
  import { createDragTracker } from './gestures';
  import { success } from './haptics';

  interface Props {
    item: ScanResult;
    siblings?: ScanResult[];
    onclose: () => void;
    onselect?: (s: ScanResult) => void;
  }
  let { item, siblings = [], onclose, onselect }: Props = $props();

  let expanded = $state(false);
  let dragY = $state(0);
  let sheetEl: HTMLDivElement | undefined = $state();
  const tracker = createDragTracker({ axis: 'y', threshold: 60 });

  function onHandleDown(e: PointerEvent) { tracker.start(e.clientX, e.clientY); }
  function onHandleMove(e: PointerEvent) {
    const s = tracker.move(e.clientX, e.clientY);
    if (s.locked === 'y') dragY = s.dy;
  }
  function onHandleUp() {
    const { committed, direction } = tracker.end();
    if (committed && direction === 'down') { expanded ? (expanded = false) : onclose(); }
    else if (committed && direction === 'up') expanded = true;
    dragY = 0;
  }

  function grab() {
    if (!item.url) return;
    api.download(item.url, item.title, $downloadHost, item.year,
                 item.resolution || '', item.size || '', item.hdr || '', item.dovi ?? false)
      .then(() => {
        markDownloaded([item.url]);
        markGrabbedSiblings(item.url);
        success();
        addToast('Grabbed', item.title);
        onclose();
      })
      .catch(() => addToast('Error', 'Download failed', 'error'));
  }

  // Focus trap + restore (same pattern as DetailPanel)
  let prevFocus: HTMLElement | null = null;
  $effect(() => {
    prevFocus = document.activeElement as HTMLElement | null;
    sheetEl?.focus();
    return () => prevFocus?.focus?.();
  });

  function onKeydown(e: KeyboardEvent) {
    if (e.key === 'Escape') { onclose(); return; }
    if (e.key !== 'Tab' || !sheetEl) return;
    const focusables = sheetEl.querySelectorAll<HTMLElement>('button, [href], input, [tabindex]:not([tabindex="-1"])');
    if (!focusables.length) return;
    const first = focusables[0];
    const last = focusables[focusables.length - 1];
    if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
    else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
  }
</script>

<!-- Scrim -->
<div class="fixed inset-0 z-40 bg-[var(--bg-overlay)] md:hidden" onclick={onclose} role="presentation"></div>

<!-- Sheet -->
<div
  bind:this={sheetEl}
  tabindex="-1"
  role="dialog"
  aria-modal="true"
  aria-label="{item.title} details"
  onkeydown={onKeydown}
  class="fixed inset-x-0 bottom-0 z-50 md:hidden flex flex-col rounded-t-2xl border-t border-x border-[var(--border)]
    bg-[var(--bg-secondary)] shadow-2xl transition-[height] duration-200 outline-none"
  style="height: {expanded ? '92vh' : '55vh'}; transform: translateY({Math.max(dragY, 0)}px);
    padding-bottom: env(safe-area-inset-bottom);"
>
  <!-- Drag handle -->
  <div
    class="shrink-0 py-2 flex justify-center cursor-grab touch-none"
    onpointerdown={onHandleDown} onpointermove={onHandleMove} onpointerup={onHandleUp} onpointercancel={onHandleUp}
    role="presentation"
  >
    <div class="w-10 h-1 rounded-full bg-[var(--border)]"></div>
  </div>

  <!-- Content -->
  <div class="flex-1 min-h-0 overflow-y-auto px-4 pb-3">
    <div class="flex gap-3">
      {#if item.poster_url}
        <img src={item.poster_url} alt="" class="w-20 rounded-md shrink-0 self-start" />
      {/if}
      <div class="min-w-0">
        <h2 class="text-base font-bold text-[var(--text-primary)] leading-snug">{item.title}</h2>
        <p class="text-xs text-[var(--text-secondary)] mt-0.5">
          {item.year || ''}{#if item.rating} · ★ {item.rating.toFixed(1)}{/if}{#if item.size} · {item.size}{/if}
        </p>
        <div class="flex flex-wrap gap-1 mt-2">
          <Badge label={formatStatus(item.status)} variant={statusVariant(item.status)} />
          {#if item.resolution}<Badge label={item.resolution} />{/if}
          {#if item.dovi}<Badge label="DV" variant="info" />{/if}
          {#if item.hdr}<Badge label={item.hdr} />{/if}
        </div>
      </div>
    </div>

    {#if item.description}
      <p class="text-xs text-[var(--text-secondary)] mt-3 leading-relaxed">{item.description}</p>
    {/if}

    {#if siblings.length > 1}
      <h3 class="text-xs font-semibold text-[var(--text-secondary)] mt-4 mb-1">Releases ({siblings.length})</h3>
      <div class="flex flex-col gap-1">
        {#each siblings as s (s.url)}
          <button
            class="flex items-center gap-2 px-2 py-1.5 rounded-md text-left text-xs
              {s.url === item.url ? 'bg-[var(--accent)]/15 border border-[var(--accent)]' : 'bg-[var(--bg-tertiary)] border border-transparent'}"
            onclick={() => onselect?.(s)}
          >
            <span class="font-medium text-[var(--text-primary)]">{s.resolution || '?'}</span>
            <span class="text-[var(--text-secondary)]">{s.size}</span>
            {#if s.dovi}<Badge label="DV" variant="info" size="xs" />{/if}
            <span class="flex-1"></span>
            <Badge label={formatStatus(s.status)} variant={statusVariant(s.status)} size="xs" />
          </button>
        {/each}
      </div>
    {/if}
  </div>

  <!-- Pinned action -->
  <div class="shrink-0 px-4 py-3 border-t border-[var(--border)]">
    {#if item.status === 'library'}
      <button class="w-full py-2.5 rounded-xl bg-[var(--bg-tertiary)] text-sm font-semibold text-[var(--text-primary)]"
        onclick={() => { copyResultLinks(item, $downloadHost); onclose(); }}>Copy links</button>
    {:else}
      <button class="w-full py-2.5 rounded-xl bg-[var(--accent)] text-sm font-semibold text-white" onclick={grab}>
        Grab{#if item.size}&nbsp;· {item.size}{/if}
      </button>
    {/if}
  </div>
</div>
```

Engineer note: read `DetailPanel.svelte` first — if its focus-trap/restore helpers are importable, import rather than duplicate; the inline trap above is the fallback. Also confirm `copyResultLinks(item, host)` signature in `resultActions.ts` (it is `(item, host)`).

- [ ] **Step 2: Verify + commit**

Run: `cd frontend && npx vitest run && npm run check && npm run build` — green, no new warnings (the scrim div may need `onkeydown` or role adjustments if check flags; use `role="presentation"` + no tabindex).

```bash
git add frontend/src/lib/components/mobile/DetailSheet.svelte
git commit -m "mobile: DetailSheet bottom sheet (half/full drag, pinned grab, siblings, focus trap)"
```

---

### Task 9: FilterBar additive props + `MobileToolbar.svelte`

**Files:**
- Modify: `frontend/src/lib/components/FilterBar.svelte` (additive props only)
- Create: `frontend/src/lib/components/mobile/MobileToolbar.svelte`

**Interfaces:**
- FilterBar gains: `interface Props { sheetOpen?: boolean; showMobileTrigger?: boolean }` via `let { sheetOpen = $bindable(false), showMobileTrigger = true }: Props = $props();` — replace the internal `let filterSheet = $state(false)` with the bindable `sheetOpen` (rename all internal references `filterSheet` → `sheetOpen`), and wrap the existing mobile trigger row's Filters BUTTON in `{#if showMobileTrigger}`. Defaults preserve today's behavior exactly (desktop + current mobile row unaffected).
- MobileToolbar produces:
  ```svelte
  <MobileToolbar
    onfilters={() => …}      <!-- opens FilterBar sheet -->
    ondeck={() => …}         <!-- opens deck overlay -->
  />
  ```
  Internally: Search button expands an inline input bound to the `searchFilter` store (collapse on blur when empty; ✕ clears); when `$selectedKeys.size > 0` the whole bar swaps to the bulk bar: `{n} selected · Grab all · Clear` (Grab all = `api.download` loop over `selectedItems`… NO — keep it simple and honest: bulk bar shows `{n} selected` + **Clear** (calls `deselectAll()`) + **Grab all** which calls the SAME per-item grab used elsewhere for each selected item currently loaded). Sort is inside the FilterBar sheet already — no separate button (recorded deviation: approved toolbar listed Sort; it lives one tap away inside Filters, which already contains the sort controls).

- [ ] **Step 1: FilterBar props change**

In `frontend/src/lib/components/FilterBar.svelte`:
1. Add at the top of the script:
```ts
  interface Props { sheetOpen?: boolean; showMobileTrigger?: boolean }
  let { sheetOpen = $bindable(false), showMobileTrigger = true }: Props = $props();
```
2. Delete `let filterSheet = $state(false);` and rename every `filterSheet` reference to `sheetOpen` (the trigger button's `onclick={() => (sheetOpen = true)}`, the `<BottomSheet open={sheetOpen} … onclose={() => (sheetOpen = false)}>`).
3. Wrap ONLY the mobile Filters trigger button (`~line 457`) in `{#if showMobileTrigger} … {/if}` — the status-chip row around it stays.

- [ ] **Step 2: Implement MobileToolbar**

```svelte
<!-- frontend/src/lib/components/mobile/MobileToolbar.svelte -->
<script lang="ts">
  import { get } from 'svelte/store';
  import { searchFilter, selectedKeys, deselectAll, filteredResults, markDownloaded, markGrabbedSiblings } from '$lib/stores/results';
  import { downloadHost } from '$lib/stores/downloads';
  import { api } from '$lib/api/client';
  import { addToast } from '$lib/stores/notifications';
  import { success } from './haptics';

  interface Props {
    onfilters: () => void;
    ondeck: () => void;
  }
  let { onfilters, ondeck }: Props = $props();

  let searchOpen = $state(false);
  let searchEl: HTMLInputElement | undefined = $state();

  function openSearch() {
    searchOpen = true;
    setTimeout(() => searchEl?.focus(), 30);
  }
  function onSearchBlur() {
    if (!$searchFilter) searchOpen = false;
  }

  let selCount = $derived($selectedKeys.size);
  let selItems = $derived($filteredResults.filter((i) => $selectedKeys.has(i.url)));

  async function grabAll() {
    const items = selItems;
    let ok = 0;
    for (const item of items) {
      if (!item.url) continue;
      try {
        await api.download(item.url, item.title, get(downloadHost), item.year,
                           item.resolution || '', item.size || '', item.hdr || '', item.dovi ?? false);
        markDownloaded([item.url]);
        markGrabbedSiblings(item.url);
        ok++;
      } catch { /* per-item failure tolerated; summarized below */ }
    }
    success();
    addToast('Grabbed', `${ok} of ${items.length} sent to JDownloader`);
    deselectAll();
  }
</script>

<div
  class="shrink-0 flex md:hidden items-center gap-1 h-11 px-2 border-t border-[var(--border)] bg-[var(--bg-secondary)]"
  role="toolbar" aria-label="Scan actions"
>
  {#if selCount > 0}
    <span class="text-xs font-semibold text-[var(--text-primary)] px-1">{selCount} selected</span>
    <div class="flex-1"></div>
    <button class="px-3 py-1.5 rounded-lg bg-[var(--accent)] text-xs font-semibold text-white" onclick={grabAll}>Grab all</button>
    <button class="px-3 py-1.5 rounded-lg bg-[var(--bg-tertiary)] text-xs text-[var(--text-primary)]" onclick={() => deselectAll()}>Clear</button>
  {:else if searchOpen}
    <input
      bind:this={searchEl}
      bind:value={$searchFilter}
      onblur={onSearchBlur}
      type="search"
      placeholder="Search titles…"
      class="flex-1 h-8 px-3 rounded-lg bg-[var(--bg-tertiary)] border border-[var(--border)] text-sm text-[var(--text-primary)] outline-none focus:border-[var(--accent)]"
      aria-label="Search titles"
    />
    <button class="p-2 text-[var(--text-secondary)]" aria-label="Close search"
      onclick={() => { searchFilter.set(''); searchOpen = false; }}>&times;</button>
  {:else}
    <button class="flex-1 flex items-center justify-center gap-1.5 py-2 text-xs text-[var(--text-secondary)]" onclick={openSearch} aria-label="Search">
      <svg class="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2"><path stroke-linecap="round" stroke-linejoin="round" d="M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0z"/></svg>
      Search
    </button>
    <button class="flex-1 flex items-center justify-center gap-1.5 py-2 text-xs text-[var(--text-secondary)]" onclick={onfilters} aria-label="Filters and sort">
      <svg class="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2"><path stroke-linecap="round" stroke-linejoin="round" d="M3 4a1 1 0 011-1h16a1 1 0 011 1v2.586a1 1 0 01-.293.707l-6.414 6.414a1 1 0 00-.293.707V17l-4 4v-6.586a1 1 0 00-.293-.707L3.293 7.293A1 1 0 013 6.586V4z"/></svg>
      Filters
    </button>
    <button class="flex-1 flex items-center justify-center gap-1.5 py-2 text-xs text-[var(--text-secondary)]" onclick={ondeck} aria-label="Triage deck">
      <svg class="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2"><path stroke-linecap="round" stroke-linejoin="round" d="M19 11H5m14-4H5m14 8H5m14 4H5"/></svg>
      Deck
    </button>
  {/if}
</div>
```

- [ ] **Step 3: Verify + commit**

Run: `cd frontend && npx vitest run && npm run check && npm run build` — ALL existing tests green (FilterBar rename is the risk: grep the file for any missed `filterSheet` reference), 0 errors, no new warnings.

```bash
git add frontend/src/lib/components/FilterBar.svelte frontend/src/lib/components/mobile/MobileToolbar.svelte
git commit -m "mobile: bottom toolbar (search/filters/deck + bulk bar); FilterBar sheet externally bindable"
```

---

### Task 10: `MobileScanView.svelte` + the fork in `+page.svelte`

**Files:**
- Create: `frontend/src/lib/components/mobile/MobileScanView.svelte`
- Modify: `frontend/src/routes/+page.svelte` (the fork + DetailPanel fork ONLY)

**Interfaces:**
- Consumes everything above. MobileScanView takes NO props — fully store-driven like the desktop branch.

- [ ] **Step 1: Implement MobileScanView**

```svelte
<!-- frontend/src/lib/components/mobile/MobileScanView.svelte -->
<script lang="ts">
  import { get } from 'svelte/store';
  import {
    filteredResults, filteredTotal, titleCounts, pagedMode, hasMore, loadingMore,
    loadResults, handleReconnectSnapshot, selectedKeys, toggleSelect, selectedDetail,
    dismissItem, restoreItem, markDownloaded, markGrabbedSiblings
  } from '$lib/stores/results';
  import { downloadHost } from '$lib/stores/downloads';
  import { api } from '$lib/api/client';
  import { addToast } from '$lib/stores/notifications';
  import type { ScanResult } from '$lib/api/types';
  import { groupResults, isDuplicateGroup, groupFormats, groupStatusSummary, groupSizeRange, groupDateRange, type ResultGroup } from '$lib/grouping';
  import ResultTile from '../ResultTile.svelte';
  import GroupTile from '../GroupTile.svelte';
  import FilterBar from '../FilterBar.svelte';
  import ResultActionSheet from '../ResultActionSheet.svelte';
  import SwipeDeck from '../SwipeDeck.svelte';
  import PullToRefresh from './PullToRefresh.svelte';
  import SwipeableTile from './SwipeableTile.svelte';
  import MobileToolbar from './MobileToolbar.svelte';
  import { success } from './haptics';

  let filterSheetOpen = $state(false);
  let deckOpen = $state(false);       // LOCAL deck overlay — deliberately NOT viewMode
                                       // ('sh-view-mode' is persisted and shared with desktop;
                                       // entering the deck on the phone must not flip it).
  let actionItem = $state<ScanResult | null>(null);
  let expandedGroups = $state(new Set<string>());
  let renderLimit = $state(60);
  let sentinel: HTMLDivElement | undefined = $state();

  let groups = $derived(groupResults($filteredResults.slice(0, renderLimit)));
  let counts = $derived($pagedMode ? $titleCounts : {});

  function toggleGroup(title: string) {
    const next = new Set(expandedGroups);
    next.has(title) ? next.delete(title) : next.add(title);
    expandedGroups = next;
  }

  // Infinite scroll: grow the render window; top up server pages.
  $effect(() => {
    if (!sentinel) return;
    const obs = new IntersectionObserver((entries) => {
      if (!entries[0].isIntersecting) return;
      if (renderLimit < $filteredResults.length) renderLimit += 60;
      else if ($pagedMode && $hasMore && !$loadingMore) loadResults(false);
    });
    obs.observe(sentinel);
    return () => obs.disconnect();
  });

  async function refresh() {
    if (get(pagedMode)) await loadResults(true);
    else await handleReconnectSnapshot();
    renderLimit = 60;
  }

  function grab(item: ScanResult) {
    if (!item.url) return;
    api.download(item.url, item.title, get(downloadHost), item.year,
                 item.resolution || '', item.size || '', item.hdr || '', item.dovi ?? false)
      .then(() => {
        markDownloaded([item.url]);
        markGrabbedSiblings(item.url);
        success();
        addToast('Grabbed', item.title);
      })
      .catch(() => addToast('Error', `Grab failed: ${item.title}`, 'error'));
  }

  function dismissWithUndo(item: ScanResult) {
    dismissItem(item.url, item.title);
    addToast('Dismissed', item.title, 'normal', { label: 'Undo', run: () => restoreItem(item.url) });
  }

  // Siblings for the detail sheet = the full group of the open item.
  let detailSiblings = $derived.by(() => {
    const item = $selectedDetail;
    if (!item) return [];
    return $filteredResults.filter((r) => r.title === item.title);
  });
</script>

<FilterBar bind:sheetOpen={filterSheetOpen} showMobileTrigger={false} />

<PullToRefresh onrefresh={refresh}>
  <div class="grid grid-cols-2 landscape:grid-cols-3 gap-2 p-2">
    {#each groups as group (group.title)}
      {#if isDuplicateGroup(group, counts, $pagedMode) && !expandedGroups.has(group.title)}
        <GroupTile
          title={group.title}
          items={group.items}
          count={counts[group.title] ?? group.items.length}
          formats={groupFormats(group.items)}
          statusSummary={groupStatusSummary(group.items)}
          sizeRange={groupSizeRange(group.items)}
          dateRange={groupDateRange(group.items)}
          onToggle={() => toggleGroup(group.title)}
        />
      {:else}
        {#each group.items as item, idx (item.url || item.group_key + '-' + idx)}
          <SwipeableTile
            onswiperight={() => grab(item)}
            onswipeleft={() => dismissWithUndo(item)}
            onlongpress={() => (actionItem = item)}
          >
            <ResultTile {item} onmore={() => (actionItem = item)} />
          </SwipeableTile>
        {/each}
      {/if}
    {/each}
  </div>
  <div bind:this={sentinel} class="h-8"></div>
  {#if $loadingMore}
    <div class="flex justify-center py-3">
      <div class="w-5 h-5 border-2 border-[var(--accent)] border-t-transparent rounded-full animate-spin"></div>
    </div>
  {/if}
  {#if $filteredTotal === 0}
    <p class="text-center text-sm text-[var(--text-secondary)] py-10">No results — pull to refresh or adjust filters.</p>
  {/if}
</PullToRefresh>

<MobileToolbar onfilters={() => (filterSheetOpen = true)} ondeck={() => (deckOpen = true)} />

{#if deckOpen}
  <div class="fixed inset-0 z-40 bg-[var(--bg-primary)] flex flex-col md:hidden">
    <div class="flex items-center justify-between px-3 h-11 border-b border-[var(--border)]" style="padding-top: env(safe-area-inset-top);">
      <span class="text-sm font-semibold text-[var(--text-primary)]">Triage deck</span>
      <button class="p-2 text-[var(--text-secondary)]" aria-label="Close deck" onclick={() => (deckOpen = false)}>&times;</button>
    </div>
    <div class="flex-1 min-h-0"><SwipeDeck /></div>
  </div>
{/if}

<ResultActionSheet item={actionItem} onclose={() => (actionItem = null)} />
```

Engineer notes:
- `GroupTile` prop names/types MUST match the component (verified earlier: `title, items, count, formats, statusSummary, sizeRange, dateRange, onToggle`) — check its `interface Props` before wiring.
- `handleReconnectSnapshot` is the live-mode snapshot re-fetch that already exists (results.ts line ~258). If it is scoped to reconnect semantics that don't fit manual refresh, use the same internal call it makes (read it first).
- If svelte-check flags `$derived.by`, use a plain `$derived(...)` expression.

- [ ] **Step 2: The fork in `+page.svelte`**

1. Add imports: `import { isPhone } from '$lib/stores/viewport';` and `import MobileScanView from '$lib/components/mobile/MobileScanView.svelte';` and `import DetailSheet from '$lib/components/mobile/DetailSheet.svelte';`
2. Find the results-area region — it begins at the `<FilterBar />` render (~line 452) and ends after the grid/list/deck region (past `{#if $viewMode === 'swipe'}`, the grid branch, and the list branch — identify the exact closing tag of that container by reading the template). Wrap:

```svelte
{#if $isPhone}
  <MobileScanView />
{:else}
  <!-- EVERYTHING that was here stays byte-identical -->
{/if}
```

The wrapped block must include the desktop `<FilterBar />` (MobileScanView renders its own). ScanControls / StatusBar placement: read the template — if ScanControls sits above FilterBar, leave it OUTSIDE the fork (phone keeps scan start/stop controls). The mobile action sheet at the bottom (`<ResultActionSheet item={mobileActionItem} …>` ~line 839) stays where it is (desktop path only uses it below md, which can no longer occur — harmless; do NOT remove it this round, per spec's dead-fragment note).

3. Fork the detail panel (~line 842):

```svelte
{#if $selectedDetail}
  {#if $isPhone}
    <DetailSheet
      item={$selectedDetail}
      siblings={$filteredResults.filter((r) => r.title === $selectedDetail!.title)}
      onclose={() => selectedDetail.set(null)}
      onselect={(s) => selectedDetail.set(s)}
    />
  {:else}
    <DetailPanel item={$selectedDetail} onclose={() => selectedDetail.set(null)} />
  {/if}
{/if}
```

(Import `filteredResults` in the page script if not already imported — check first; it almost certainly is.)

- [ ] **Step 3: Full verification**

Run: `cd frontend && npx vitest run` → ALL pass. `npm run check` → 0 errors, exactly 3 pre-existing warnings. `npm run build` → succeeds.
Desktop smoke: `git diff main -- frontend/src/routes/+page.svelte` — confirm the desktop branch shows ONLY indentation/wrap changes and the script-only grouping import from Task 5, no markup edits inside the `{:else}` block.

- [ ] **Step 4: Commit**

```bash
git add frontend/src/lib/components/mobile/MobileScanView.svelte frontend/src/routes/+page.svelte
git commit -m "mobile: MobileScanView + isPhone fork — phone-native Scan wall, deck overlay, detail sheet"
```

---

### Task 11: Final whole-branch verification + on-device checklist doc

**Files:**
- Create: `docs/superpowers/mobile-scan-device-checklist.md`

- [ ] **Step 1: Full suite ×2**

Run twice: `cd frontend && npx vitest run && npm run check && npm run build` — identical green results.

- [ ] **Step 2: Write the on-device checklist** (verbatim):

```markdown
# Mobile Scan — on-device test checklist (post web-deploy, pre-APK)
Test at scanhound.turtleland.us in the phone browser first, then the APK.
- [ ] Wall renders 2 cols portrait / 3 landscape; stacked group cards work (tap expands in place)
- [ ] Pull-to-refresh: arrow → armed haptic tick → spinner → list refreshes
- [ ] Swipe a tile right: green underlay, haptic at threshold, "Grabbed" toast, tile marked downloading
- [ ] Swipe a tile left: amber underlay, "Dismissed" toast, Undo restores it
- [ ] Vertical scroll never triggers a horizontal swipe (axis lock) and vice versa
- [ ] Long-press: action sheet opens, no accidental tap-through; Select entry enters selection mode
- [ ] Selection: toolbar swaps to bulk bar; Grab all sends N; Clear exits
- [ ] Tap a tile: detail sheet at half height; drag up = full; drag down/scrim/back = close
- [ ] Detail sheet Grab works; sibling releases switch the sheet
- [ ] Bottom toolbar: search expands + filters open the sheet (genre/language/dates/sort) + Deck opens
- [ ] Deck: swipe cards, close ✕ returns to the wall at the same scroll position
- [ ] Rotate: layout adapts, nothing clipped by notch/home bar (safe-area)
- [ ] Android back button: closes sheet/deck before navigating (KNOWN GAP if not — file follow-up)
- [ ] Desktop browser: literally unchanged (spot-check grid/list/detail/filters)
```

- [ ] **Step 3: Commit**

```bash
git add docs/superpowers/mobile-scan-device-checklist.md
git commit -m "mobile: on-device test checklist"
```

---

## Self-review (author-run)

- **Spec coverage:** isPhone store (T1) ✓; gestures.ts (T2) ✓; haptics (T3, navigator.vibrate decision recorded) ✓; PullToRefresh (T6) ✓; SwipeableTile w/ underlays+undo semantics (T7 + Global) ✓; DetailSheet half/full + pinned grab + siblings + trap (T8) ✓; MobileToolbar one-handed + bulk bar (T9; Sort folded into Filters sheet — recorded deviation) ✓; deck overlay local (T10, not persisted viewMode — recorded) ✓; fork + DetailPanel fork + desktop byte-identical guard (T10 step 3 diff check) ✓; grouping reuse via extraction (T5) ✓; selection via existing action-sheet Select entry (verified present) ✓; error handling via toasts + existing banners ✓; rollout order is post-plan (web deploy → eyeball → APK), captured in T11 checklist.
- **Known intentional deviations from spec wording (all recorded in Global Constraints / task notes):** navigator.vibrate instead of Tauri haptics plugin; no Undo on grab (JD send is irreversible); Sort button folded into the Filters sheet; Android back-button handling verified on-device rather than pre-wired (checklist has a follow-up trigger).
- **Placeholder scan:** clean — every code step has complete code; the two "read the existing body first" directives in T5/T10 are verbatim-move instructions with explicit line ranges, not placeholders.
- **Type consistency:** `createDragTracker` shape used identically in T6/T7/T8; `addToast` 4-arg form matches T4; `GroupTile`/`ResultTile`/`ResultActionSheet`/`BottomSheet` props match verified ground truth; `grab()` mirrors `ResultTile.handleDownload` exactly.
```
