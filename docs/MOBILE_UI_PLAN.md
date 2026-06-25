# Mobile UI Polish — Plan

Status: **IMPLEMENTED** (Phases 1–4) · Branch: `claude/review-commit-status-vkxtvi` · Date: 2026-06-23

## Implemented
- **Decisions:** bottom tab bar; phones default to swipe deck; all screens done;
  Phase 4 (touch context actions) included.
- **Phase 1:** `BottomSheet`, `media`/`theme` stores, shared nav `icons`,
  `app.html` viewport-fit/standalone meta.
- **Phase 2:** `MobileTabBar` (bottom nav), mobile top bar (title + theme), Filter
  bar → compact bar + filter sheet, Scan controls → compact bar + scan sheet,
  StatusBar hidden on mobile (redundant), swipe default on phone.
- **Phase 3:** responsive padding on settings/analytics/watchlist; touch-visible
  watchlist row actions; downloads header wraps.
- **Phase 4:** `ResultActionSheet` (long-press on list/grid + ⋯ on grid tiles).
- **Verification:** `npm run check` + `npm run build` green after each phase.

## Remaining / optional
- ✅ Playwright mobile harness (`playwright.config.ts` + smoke tests) — desktop +
  mobile (Pixel 7) projects, routing/nav/sheets/no-overflow smoke tests.
- ⏳ On-device pass once the APK is built (needs the user's machine).

---
*(Original plan retained below for reference.)*

---

Goal: make the existing SvelteKit UI genuinely usable on a phone for the Android
app, **without regressing desktop**. The swipe deck is already mobile-first; this
plan covers the surrounding chrome and the secondary screens.

## Guiding principles
- **Breakpoint = Tailwind `md` (768px)**, matching the layout's existing
  `md:hidden` / `hidden md:flex` split. Phones are `< md`.
- **Never fork behavior, only layout.** Keep all current desktop markup behind
  `hidden md:flex`; add mobile markup behind `flex md:hidden`. Shared *logic*
  (filter state, scan params, batch actions) is extracted so both presentations
  call the same functions — no duplicated logic.
- **Thumb-first.** Primary actions ≥ 44px tap targets; move dense toolbars into
  **bottom sheets**; put navigation in reach at the bottom.
- **Respect safe areas** (notch / gesture bar) via `viewport-fit=cover` +
  `env(safe-area-inset-*)`.
- Ship in **phases, each independently building green** (`npm run check` +
  `npm run build`). Highest-traffic, worst-offending screen first (Scan).

## Current-state findings (what's actually wrong)
- `ScanControls.svelte` — one non-wrapping `flex` row: type+source selects, page
  stepper, category checkboxes, Start, progress. Overflows hard `< md`.
- `FilterBar.svelte` — one dense non-wrapping row: status tabs, quick chips,
  select/batch actions, export, host select, genre/language/sort selects, search,
  density, columns, view switcher (incl. the new swipe toggle). Worst offender.
- `StatusBar.svelte` — 4 stat spans + selected + logs at fixed `gap-4`; tight `< sm`.
- `+layout.svelte` — mobile nav is a top-left hamburger drawer (works, but a
  bottom tab bar is far better on a phone).
- Secondary: `watchlist` & `analytics` already use some `grid-cols-2 md:grid-cols-*`
  but use `p-6` and hover-only row actions (`opacity-50 group-hover`) that never
  reveal on touch. `downloads` is flex-based but dense. `settings` is already a
  single `max-w-2xl` column with a horizontally-scrolling tab bar — mostly fine.
- `app.html` has the viewport meta but no `viewport-fit=cover`.

---

## Phase 1 — Foundations (primitives + globals)
Small, unblocks everything else.

1. **`src/app.html`**: change viewport to
   `width=device-width, initial-scale=1, viewport-fit=cover`; add `theme-color`
   and `apple-mobile-web-app-*` meta so standalone/full-screen looks right.
2. **`src/lib/stores/media.ts`** (new): a `mobile` readable store backed by
   `window.matchMedia('(max-width: 767px)')` for the few places that must *mount*
   different components (sheets) rather than just toggle CSS. SSR-safe (defaults
   false on server).
3. **`src/lib/components/BottomSheet.svelte`** (new): reusable slide-up panel —
   backdrop (tap to dismiss), drag-down-to-close, `max-h-[85vh]` scroll body,
   safe-area bottom padding, `Esc` to close, focus moved to the panel. Props:
   `open`, `title?`, `onclose`, snippet `children`. Used by the filter, scan, and
   row-action sheets below.

**Acceptance:** `check` + `build` green; BottomSheet demoable from any page.

---

## Phase 2 — Scan page chrome (biggest win)
Each sub-step is a self-contained edit.

### 2a. Bottom tab bar (navigation)
- **`src/lib/components/MobileTabBar.svelte`** (new): fixed bottom bar, the 5
  routes (Scan / Downloads / Watchlist / Analytics / Settings) as icon+label
  tabs, active state from `$page.url.pathname`, safe-area padding. Reuse the icon
  set already in `Sidebar.svelte` (extract icons to `lib/icons.ts` so both share).
- **`+layout.svelte`**: render `<MobileTabBar />` with `md:hidden`; keep the
  desktop sidebar (`hidden md:flex`) untouched. Simplify the mobile top bar to
  just the title + connection dot (drop the hamburger, or keep it for the
  theme/settings overflow — recommend dropping; nav now lives at the bottom).
  Add bottom padding to the main scroll area on mobile so content clears the bar.

### 2b. FilterBar → compact bar + filter sheet
- Extract the option lists already in `FilterBar` (`sortOptions`, `filters`,
  `quickChips`, `columnDefs`) and the batch handlers (`bulkDownloadAll`,
  `bulkCopyLinks`, `bulkAddToWatchlist`, `handleExport`) into the component but
  render two layouts:
  - **Desktop** (`hidden md:flex`): the current row, unchanged.
  - **Mobile** (`flex md:hidden`): a slim bar = horizontally-scrollable status
    chips + quick chips, a result/selected count, and a **“Filters”** button
    (badge shows active-filter count). The button opens a **`BottomSheet`**
    containing sort, genre, language, quick filters, columns, density, download
    host, and the batch-action buttons (Download all / Copy links / Watchlist /
    Export) as full-width rows. The view switcher (grid/list/swipe) becomes a
    segmented control at the top of the sheet (and swipe is the sensible mobile
    default — see Phase 1 note / open decisions).
- Search: a full-width input pinned at the top of the mobile bar (or in the sheet).

### 2c. ScanControls → compact bar + scan sheet
- **Desktop** (`hidden md:flex`): current row, unchanged.
- **Mobile** (`flex md:hidden`): a single row = current scan-type label + a
  primary **“Scan”** button when idle / **“Stop”** + inline progress when running.
  Tapping “Scan” opens a **`BottomSheet`** with type, source, page stepper,
  category toggles, optional search, and a big Start button. All bound to the same
  `$state` already in the component.

### 2d. StatusBar mobile
- `< sm`: collapse to the two counts that matter (Missing, Upgrades) + selected
  badge + Logs button; allow horizontal scroll for the rest. `sm+` unchanged.

**Acceptance:** On a 390px viewport, the Scan page shows no horizontal overflow;
filters/scan reachable via sheets; bottom nav switches routes; desktop identical.

---

## Phase 3 — Secondary screens (responsive passes)
Mostly Tailwind class work; no logic changes.

- **`downloads/+page.svelte`**: `p-4 md:p-6`; ensure the active/results/history
  panels stack (`flex-col md:flex-row` where they sit side-by-side); wrap row
  metadata, truncate long names, bump control sizes; make per-row controls
  full-size on touch.
- **`watchlist/+page.svelte`**: `p-4 md:p-6`; header + action buttons `flex-wrap`;
  reveal per-row actions on mobile (replace `opacity-50 group-hover:opacity-100`
  with `opacity-100 md:opacity-50 md:group-hover:opacity-100`); larger tap
  targets; stat grid already responsive.
- **`analytics/+page.svelte`**: `p-4 md:p-6`; verify the `grid-cols-2 md:grid-cols-4`
  cards and the resolution/size bars fit ~360px; shrink fixed widths (`w-12`,
  legends) `< sm` if they crowd.
- **`settings/+page.svelte`**: `p-4 md:p-6` on the inner container; make the
  Save/Reset action bar sticky at the bottom on mobile (safe-area padded) so it’s
  reachable in long forms; inputs already full-width.

**Acceptance:** each route scrolls cleanly at 390px, no overflow, actions tappable.

---

## Phase 4 — Touch actions in list/grid (optional, lower priority)
The desktop right-click `ContextMenu` never fires on touch.
- Add a per-row/tile **“⋯”** button (visible `< md`) and **long-press** on the
  card that opens a `BottomSheet` with the same actions the `ContextMenu` offers
  (download, copy links, open source/IMDb/Plex, add to watchlist). Reuse the
  ContextMenu’s action handlers. Desktop keeps right-click.

Lower priority because the **swipe deck** is the primary mobile triage surface;
list/grid are secondary on a phone.

---

## Phase 5 — Verification
- After each phase: `npm run check` (svelte-check) + `npm run build`.
- Optional but recommended: add the missing **`playwright.config.ts`** with
  `desktop` + `mobile` (Pixel-class) projects to match the existing
  `test:e2e:mobile` script, plus smoke tests: bottom-nav switches routes, filter
  sheet opens, scan sheet opens, no horizontal overflow at 390px. (Playwright can
  run headless here.)
- Self-review the diff for desktop regressions (every change is additive behind a
  breakpoint).

---

## Sequencing & rough effort
1. Phase 1 (foundations) — small.
2. Phase 2 (Scan chrome) — the bulk; biggest visible win. 2a→2b→2c→2d.
3. Phase 3 (secondary screens) — medium, mechanical.
4. Phase 5 verification can fold in after Phase 2 and again at the end.
5. Phase 4 — only if wanted.

## Open decisions
1. **Bottom tab bar vs. keep the hamburger drawer** for mobile nav?
   (Recommend bottom tab bar.)
2. **Default mobile view = swipe deck?** (Recommend yes on phones, while keeping
   list/grid available; desktop keeps its saved preference.)
3. **Scope of first cut:** Phase 2 only (Scan page), or Phase 2 + 3 together?
4. Include **Phase 4** (touch context actions) now or defer?
