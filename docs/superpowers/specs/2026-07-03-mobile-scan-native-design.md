# Mobile-Native Scan Experience — Design Spec

**Date:** 2026-07-03
**Scope decision:** flagship phone treatment for the **Scan page only** this round. Downloads/Renames/Watchlist/Analytics/Settings are untouched (future rounds). Primary phone use case (user-confirmed): **browse + grab releases**.
**Approach (user-approved):** B — "mobile view, shared brains": one route, all existing stores/data logic shared, phone-specific presentation isolated in new components.

## Goals
- The phone app (Tauri WebView **and** mobile browser — same code serves both) stops feeling like a shrunken website: poster wall tuned for thumbs, native gestures (pull-to-refresh, swipe actions, haptics), one-handed controls, bottom-sheet detail.
- Desktop is **structurally untouched**: the desktop markup branch stays byte-identical; phone work cannot regress it.
- No fork: same SvelteKit codebase; web deploys via `docker compose up -d --build`; APK via `npm run android:build` + debug-keystore signing.

## Non-goals (this round)
- No changes to Downloads/Renames/Watchlist/Analytics/Settings pages.
- No push notifications / app shortcuts / release keystore (separate "native device features" track).
- No new backend endpoints — the phone view consumes the existing API exactly as desktop does.

## Architecture

### Form-factor detection: `lib/stores/viewport.ts`
- `export const isPhone: Readable<boolean>` — a store backed by `matchMedia('(max-width: 767px)')` combined with `matchMedia('(pointer: coarse)')` (phone = narrow AND coarse; a narrow desktop window stays desktop). Updates live on rotate/resize. SSR-safe default `false` (app is CSR/adapter-static; guard `window` access).
- Single source of truth — the Scan route branches on it; new mobile components may consume it but never re-derive their own media queries.

### The fork in `routes/+page.svelte`
- The results area becomes:
  `{#if $isPhone} <MobileScanView /> {:else} …existing desktop markup, unchanged… {/if}`
- The desktop branch is not edited beyond the wrap. Existing `md:`-based mobile fragments inside the desktop branch (mobile control rows etc.) become dead on phone (isPhone=true renders MobileScanView instead) — they are NOT deleted this round to keep the desktop diff minimal; a cleanup note is left for a future round.
- The `+layout.svelte` mobile top bar and `MobileTabBar` stay exactly as-is (they already work).

### New components — all under `lib/components/mobile/`
| File | Responsibility |
|---|---|
| `MobileScanView.svelte` | Phone orchestrator: status strip, poster wall, bottom toolbar, deck entry, detail-sheet hosting. Consumes the SAME stores as desktop (`results`, `filteredResults`, `pagedMode`, `hasMore`, `loadResults`, selection stores, filter stores, `titleCounts`, `stats`). |
| `PullToRefresh.svelte` | Generic wrapper: elastic drag-down at scrollTop=0 → spinner → `onrefresh()` callback → settle. Reusable by future pages. |
| `SwipeableTile.svelte` | Wraps a slotted tile with horizontal swipe gesture: right = primary action (grab), left = secondary (dismiss), with colored underlay icons, commit threshold, haptic at threshold, and cancel-on-release-below-threshold. |
| `DetailSheet.svelte` | Bottom sheet (half-height → drag to full → drag down/scrim-tap to dismiss) showing the DetailPanel data set for one item + sibling releases for grouped titles + pinned full-width Grab button. |
| `MobileToolbar.svelte` | Slim bar docked above the tab bar: Search (expands inline input), Filters (opens existing FilterBar mobile sheet), Sort, Deck (▶). Swaps to the bulk-action bar when selection mode is active. |
| `gestures.ts` | Pure TS touch-tracking utilities (drag state machine, axis lock, thresholds, velocity) — DOM-event-fed but logic pure, unit-testable. |
| `haptics.ts` | `tap()`, `success()`, `warning()` wrappers over the Tauri haptics plugin; dynamic import; silent no-op when the plugin/Tauri is absent (browser). |

### Shared, untouched
Stores (`results.ts` incl. this week's pagination/reconnect/facet fixes, selection, filters, settings, connection), `resultActions.ts`, `api/client.ts` + `endpoint.ts`, `ResultTile`, `GroupTile`, `Badge`, `FilterBar` (its mobile bottom sheet is reused as the phone Filters surface), `ResultActionSheet`, `SwipeDeck`, `DetailPanel` (desktop only; DetailSheet renders from the same `ScanResult` item object and imports the same shared formatting helpers — size/date/status from `lib/constants` — rather than sharing DetailPanel markup), `MobileTabBar`, `ConnectionBanner`, `Snackbar`.

## The phone Scan experience

### Poster wall
- 2 columns portrait, 3 columns landscape (CSS auto-fit with a phone-tuned min tile width; the desktop `tileSize` store is not surfaced on phone).
- Tiles are the existing `ResultTile` (scrim title/year, status badge, DV/HDR chips) and stacked `GroupTile` for multi-release titles — reused as-is inside `SwipeableTile` wrappers.
- Status tabs (Missing/Upgrade/In Library/All) = a horizontally swipeable segmented strip under the top bar with live counts (same `stats`/`titleCounts` data).
- Infinite scroll unchanged (renderLimit + IntersectionObserver + paged `hasMore`).

### Gestures
- **Pull-to-refresh** (wall at top): elastic pull → haptic tick at trigger point → `loadResults(true)` in paged mode / re-snapshot in live mode → settle. Failure → snackbar with Retry.
- **Swipe right on a tile → Grab:** tile translates with the finger, green ⬇ underlay grows, haptic tap at commit threshold; on commit, the existing grab action (same one the desktop Download pill calls via `resultActions`) fires and an **Undo snackbar** appears (undo = existing un-mark-downloaded). Below threshold on release → spring back, no action.
- **Swipe left on a tile → Dismiss/hide:** same mechanics, amber underlay, existing dismiss action, Undo snackbar.
- **Long-press → ResultActionSheet** (existing), which gains a **"Select"** entry (see Selection).
- **Tap → DetailSheet.**
- Group tiles: tap expands the group in place exactly like today; swipe gestures are disabled on the collapsed group card (act on individual releases after expanding).
- Axis lock: vertical intent scrolls, horizontal intent swipes — resolved by the gesture util's first-movement angle; no diagonal jank.

### Bottom toolbar (one-handed)
Docked directly above `MobileTabBar`, ~44px + safe-area:
- **Search** — expands an inline input across the toolbar (existing `searchQuery` store, same debounce); collapse on blur/clear.
- **Filters** — opens the existing FilterBar bottom sheet (status extras, genre/language from server facets, date range, quick chips).
- **Sort** — compact popover of the existing sort options.
- **Deck ▶** — enters deck mode.
- In selection mode the toolbar swaps to: `N selected · Grab all · Export · Clear`.

### DetailSheet
- Opens at ~55% height: poster, title/year, rating, size, posted date, format chips, status badge, pinned full-width **Grab** button (or status-appropriate action: e.g. "In Library" shows Copy links as primary).
- Drag up → full height reveals the complete DetailPanel data set (description, genres, Plex versions, prior grab, links).
- Multi-release groups: a sibling-release list inline (the group's other releases, same data as desktop's "Other Releases"); tapping a sibling re-points the sheet.
- Dismiss: drag down past threshold, scrim tap, or Back gesture (Android back button closes the sheet before navigating — Tauri back-button event hook).
- Focus/a11y: focus trapped in the sheet while open (mirror DetailPanel's new trap), `aria-modal`, restores focus on close.

### Deck mode
- Toolbar ▶ opens the existing `SwipeDeck` full-screen over the **current filtered results** starting at the top of the list; progress "N of M" in the header; ✕ exits back to the wall preserving scroll position.
- Deck swipe right = grab, left = skip (SwipeDeck's existing semantics), with haptics added at commit.

### Selection & bulk
- Entry: long-press → action sheet → "Select". Selection mode ticks tiles on tap (checkmark overlay — reuse ResultTile's existing selection visuals), toolbar shows the bulk bar. Exit via Clear or Back.
- Same `selectedKeys`/`selectAll` stores; the paged-mode "Select loaded (N)" honesty rule carries over.

## Error handling
- ConnectionBanner + WS reconnect snapshot (already shipped) are inherited unchanged.
- Pull-to-refresh failure → Retry snackbar (existing Snackbar store).
- Grab failure → tile springs back with a brief shake + error snackbar (message from the API error detail).
- Haptics/plugin absent → all haptic calls silently no-op.

## Testing
- `gestures.ts` drag state machine (axis lock, thresholds, velocity, cancel) — pure vitest.
- Store integration untouched → the existing 97 vitest tests keep guarding shared logic; new tests only where mobile view adds store interactions (e.g. deck-entry preserves filter slice).
- `npm run check`: 0 errors, no new warnings (3 known pre-existing allowed).
- `npm run build` must pass; desktop visual smoke on the unchanged branch.
- On-device checklist (manual, post-deploy): pull-to-refresh, swipe grab + undo, long-press sheet, detail sheet drag, deck round-trip, rotation, Android back button, safe-area on notched screens.

## Rollout
1. Merge → **web deploy first** (`docker compose up -d --build`): the user's phone browser at scanhound.turtleland.us is the live testbed.
2. After eyeball approval → `npm run android:build` + sign + stage `Desktop\ScanHound.apk` (in-place update).
3. Desktop unaffected throughout.

## Future rounds (explicitly out of scope, recorded)
- Downloads/Renames mobile card layouts + swipe actions; Settings/Analytics/Watchlist comfort pass.
- Push notifications, app shortcuts, release keystore.
- Deleting the now-dead `md:` mobile fragments from the desktop branch of the Scan page.
