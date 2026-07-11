# Skipped-Items Manager — Design Spec

**Date:** 2026-07-11
**Status:** Approved (design), pending implementation plan

## Goal

Give users an in-app way to **see and restore** items they've swiped away ("skipped"/dismissed). Today ~426 items are hidden with no UI to review or un-skip them — the backend supports it, but nothing is wired up. Add a "Skipped (N)" entry that opens a searchable manager on both desktop and mobile.

## Existing backend + client (reused as-is, no backend changes)

- `GET /results/dismissed` → `{ items: [{ url, title, dismissed_at }], count }` (newest first, capped at 1000).
- `POST /results/dismiss` with `{ urls, dismissed: false }` → restore (un-dismiss) those URLs.
- `DELETE /results/dismissed` → clear all dismissals.
- Client: `api.dismissedList()`, `api.clearDismissed()`, `api.dismissItems(urls, titles, dismissed, meta)`.
- `dismissedUrls` (`stores/results.ts`) is a `writable<Set<string>>` hydrated from `dismissedList()` on scan-view load — so `$dismissedUrls.size` is the live skipped count (drives the badge with no extra request).

## Components

1. **`stores/results.ts` additions** (the restore counterparts to the existing `dismissItem`):
   - `restoreDismissed(url: string): Promise<boolean>` — optimistically remove `url` from `dismissedUrls`, call `api.dismissItems([url], undefined, false)`, revert on failure. Returns success.
   - `restoreAllDismissed(): Promise<boolean>` — snapshot then clear `dismissedUrls`, call `api.clearDismissed()`, revert on failure.
   - No change to `hydrateDismissed()` (already seeds the set) or `dismissItem()`.

2. **`lib/renames/`-style helper `lib/skipped.ts`** (pure, unit-tested — no logic in `.svelte`):
   - `filterSkipped(items: SkippedItem[], query: string): SkippedItem[]` — case-insensitive title substring match; empty query returns all; items with a null title are matched on their URL as a fallback.
   - `relativeTime(iso: string | null, now: number): string` — "just now" / "3h ago" / "3d ago" / a date for older; null → "".
   - `type SkippedItem = { url: string; title: string | null; dismissed_at: string | null }`.

3. **`components/SkippedManager.svelte`** — the shared content (list + search + actions), platform-agnostic:
   - On mount / when opened: `api.dismissedList()` → local `items`. Loading + error states.
   - A search `<input>` bound to a `query` state; the rendered list is `filterSkipped(items, query)`.
   - Each row: title (or URL), `relativeTime(dismissed_at)`, and a **Restore** button → `restoreDismissed(url)`, then drop the row locally on success.
   - Header: count + a **Restore all** button → confirm (reuse `ConfirmDialog` — it's 400+ items) → `restoreAllDismissed()` → clear local list.
   - Empty state: "No skipped items."

4. **Desktop entry** — a "Skipped (N)" button in the scan toolbar (`FilterBar.svelte`, or the `+page.svelte` toolbar row alongside it), where `N = $dismissedUrls.size`, hidden when N is 0. Opens `SkippedManager` inside the existing `ModalOverlay.svelte`.

5. **Mobile entry** — a matching entry in the mobile toolbar (`MobileToolbar.svelte` / `MobileScanView.svelte`), opening `SkippedManager` inside `BottomSheet.svelte`.

## Data flow / reappearance

- The badge reads `$dismissedUrls.size` — updates instantly on any restore/clear (optimistic).
- The manager fetches the rich rows (`title`, `dismissed_at`) via `dismissedList()` on open; `dismissedUrls` alone only has URLs.
- **Reappearance in the scan list:** in live mode, removing a URL from `dismissedUrls` immediately re-includes it in `filteredResults`. In paged/cached mode the server excludes dismissed rows, so a restored item reappears on the next results refresh (natural refresh or reopening the tab) — the manager does **not** force a full reload per restore (avoids jank with the sheet open). This matches how the existing swipe-dismiss already behaves in reverse.

## Error handling

- `dismissedList()` failure → the manager shows an inline "Couldn't load skipped items" with a retry, never a blank list.
- `restoreDismissed`/`restoreAllDismissed` revert their optimistic `dismissedUrls` change on API failure (mirrors `dismissItem`) and surface a toast.

## Testing

- `lib/skipped.ts` unit tests: `filterSkipped` (match, case-insensitivity, empty query, null-title URL fallback, no match) and `relativeTime` (just-now / hours / days / null / old-date buckets, using an injected `now`).
- No `.svelte` render tests (the repo has none); all decision logic lives in `skipped.ts` and the `results.ts` store functions (the latter covered by the existing store test patterns if a `restoreDismissed` unit test is cheap to add against a mocked `api`).

## Non-goals (YAGNI)

- No multi-select bulk restore (search + per-item + restore-all covers it).
- No pagination (1000-item server cap is far above the current 426; if it ever matters, add later — note it, don't build it).
- No re-skip-from-manager, no per-item metadata display beyond title + time.
- No backend changes.
