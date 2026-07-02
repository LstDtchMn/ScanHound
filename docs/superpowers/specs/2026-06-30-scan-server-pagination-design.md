# Scan Browse View — Server-Side Filtered Pagination + Infinite Scroll

**Date:** 2026-06-30
**Branch:** `scan-server-pagination` (off `main` @ 0bba69f)
**Status:** Design approved; ready for implementation plan.

## Problem

The Scan page's tab badges (**Total / Missing / In Library**) are computed
server-side over the *entire* result set (currently ~2,496 cached rows /
1,541 "missing"). The list, however, renders the client's `results` store,
which is hydrated **once** and hard-capped at the first **500 rows** —
`getCachedResults({ per_page: '500' })` in `results.ts`, and the server itself
caps `per_page` at `le=500`. Those 500 are the alphabetically-first titles.
There is **no "load more"**: `hydrateCache` fires once in `onMount` and never
fetches the rest.

Consequence (verified against the live cache): of 1,541 missing titles, only
~295 are even loaded into the browser, and the `categoryFilter` default of
`['4k']` hides the remux/TV subset on top of that — so the Missing view shows
under 100 items, the client pager (`{#if totalPages > 1}`) never appears, and
"the page just ends" far short of the badge count. Most missing titles are
unreachable.

## Goal

The browser stops holding the entire library. It fetches **pages on demand**
with the full filter state attached; the **server** owns filtering, sorting,
and the filtered total, so the badges and the list agree and the user can
**infinite-scroll** through every matching title.

## Non-Goals

- Changing the live-scan streaming path's fundamental behavior (it already
  streams the full set over WebSocket and is unbounded). It only gains the
  shared infinite-scroll render window.
- Server-side *grouping* of releases into title groups (grouping stays a
  client render concern; the server supplies accurate per-title counts).
- Any change to the Renames page or the DV feature (parked on its branch).

## Architecture Overview

Two population modes feed one render pipeline:

- **Browse (paged) mode** — the default on open and whenever no live scan is
  active. `results` is an **accumulator** of server pages. The server has
  already applied every filter and the sort, so the client renders `results`
  as-is and never re-filters. Changing any filter refetches page 1; scrolling
  near the bottom fetches the next page.
- **Live mode** — a "Scan now" run streams the full set over WebSocket into
  `results` as today. The existing client-side filter/sort pipeline applies
  (the full set is in memory, so no server paging is needed).

A single `pagedMode` flag selects which `filteredResults` derivation runs.
Both modes render through the same grouped list / grid / deck and the same
infinite-scroll render window.

## Component A — Backend (`backend/api/routes/results.py`)

`_shape_results` and both GET endpoints (`/results`, `/results/cached`) gain
**filter parity** with the client plus **typed sorting**, so a server page is
byte-for-byte what the client would have computed in memory.

### New query params (added to existing `filter/search/sort/order/page/per_page/include_dismissed`)

- `category` — comma-list of enabled categories (`4k,remux,tv`). Predicate
  mirrors the client's `effCategory`: `cat = item.category or ('tv' if
  item.season is not None else '4k')`; the item shows when `cat` is **not** in
  the known set `{4k,remux,tv}` **or** is in the enabled set. Empty/absent
  param = no category filtering (show all).
- `genre` — comma-list; item shows if any of `item.genres` is in the list.
- `language` — comma-list; item shows if `item.language` is in the list.
- `quick` — comma-list of `4k` / `hdrdv` / `inplex`, AND-combined:
  - `4k` → `item.resolution == '4K'`
  - `hdrdv` → `item.dovi` truthy **or** (`item.hdr` truthy and `item.hdr != 'SDR'`)
  - `inplex` → `len(json.loads(item.plex_versions or '[]')) > 0` (fail-safe to
    False on parse error)

All new filters are applied **after** the dismissal hide and **after** the
`visible_items` snapshot (so tab `stats` remain whole-set), alongside the
existing status `filter` and `search`, before sort + pagination.

### Typed sort (replaces `items.sort(key=lambda x: str(x.get(sort,"")))`)

A `sort` field with an `order` produce a typed key:

| `sort` field   | key                                             |
|----------------|-------------------------------------------------|
| `title`        | `str(title).casefold()`                         |
| `year`         | `float(year or 0)`                              |
| `rating`       | `float(rating or 0)`                            |
| `size`         | `parse_size_to_bytes(size)` (TB/GB/MB/KB/B)     |
| `posted_date`  | `parse_posted_date(posted_date)` (`" at "`→ts)  |

`parse_size_to_bytes` and `parse_posted_date` are ported from the client
(`results.ts`) as small backend helpers. Unknown `sort` → stable/no reorder.
`order == 'desc'` reverses.

### Response additions

- `title_counts`: `{ title: count }` over the **filtered** items (after all
  filters, before pagination) — lets the client show an accurate "N releases"
  collapse badge even when a title's releases span pages.
- Keep `items`, `total` (filtered total), `page`, `per_page`, `stats`
  (whole visible set → tab badges), `filtered_stats`.
- Page size: default `per_page=100`; cap raised is unnecessary — real paging
  now — keep a sane guard (`le=200`).

### Filter-aware select-all

`POST /results/select-all` accepts a body `{ source: 'live'|'cache', filter,
search, category, genre, language, quick }`. It resolves the same item source
(`get_last_scan_items()` or `get_background_cache()`), applies the identical
filter pipeline, collects every `group_key`, replaces the server `_selected`
set with them, and returns `{ selected_count, group_keys }`. (The prior no-arg
form — select all last-scan items — is preserved when the body is absent.)

### Shared filter module

The filter/sort predicates are extracted into one internal helper
(`_filter_and_sort(items, params)`) reused by `_shape_results` and select-all,
so the two can never diverge.

## Component B — Client store (`frontend/src/lib/stores/results.ts`)

### New paged state

```
export const pagedMode      = writable<boolean>(true);
export const hasMore        = writable<boolean>(false);
export const loadingMore    = writable<boolean>(false);
export const filteredTotal  = writable<number>(0);
export const titleCounts    = writable<Record<string, number>>({});
let currentPage = 0;      // last loaded page (0 = nothing loaded)
let currentQueryKey = ''; // serialized filter state of the loaded pages
```

### `loadResults(reset: boolean)`

1. Build params from the filter stores: `filter` (statusFilter, omitted when
   `'all'`), `search`, `category` (join), `genre`, `language`, `quick`,
   `sort`+`order` (translated from `sortBy`, table below), `page`, `per_page:100`.
2. `sortBy` → `(sort, order)`: `title-asc`→`(title,asc)`, `title-desc`→
   `(title,desc)`, `year-desc`→`(year,desc)`, `year-asc`→`(year,asc)`,
   `size-desc`→`(size,desc)`, `size-asc`→`(size,asc)`, `rating-desc`→
   `(rating,desc)`, `rating-asc`→`(rating,asc)`, `posted-desc`→
   `(posted_date,desc)`, `posted-asc`→`(posted_date,asc)`.
3. Endpoint: browse/paged mode always pages `/results/cached`
   (`getCachedResults`). A live scan uses the WebSocket stream + live-mode
   pipeline instead — it never calls `loadResults`.
4. `reset` → `page=1`, replace `results`, reset `currentPage=1`; else
   `page=currentPage+1`, **append** to `results`, bump `currentPage`.
5. Update `hasMore = results.length < total`, `filteredTotal = total`,
   `titleCounts = resp.title_counts`, `stats = resp.stats`, and (on reset)
   `cacheUpdatedAt`/`fromCache` when the response is `source: 'cache'`.
6. Guard against overlap and stale responses via `currentQueryKey`: a response
   whose query key ≠ the current one is discarded.

### `filteredResults` derivation (mode-aware)

- **pagedMode true** → return `results` unchanged (server already filtered +
  sorted). `siblingCounts` uses `$titleCounts`.
- **pagedMode false** (live) → the existing client filter+sort pipeline over
  `results` is retained verbatim.

### Filter-change subscription

A derived "query key" of `(statusFilter, searchFilter, genreFilter,
languageFilter, quickFilters, categoryFilter, sortBy)` is subscribed with a
**250 ms debounce**; on change **in paged mode**, call `loadResults(reset:true)`
and reset the render window. In live mode the change re-derives client-side as
today.

### Dismissal + optimistic updates in paged mode

- `dismissItem` still persists server-side; in paged mode it also removes the
  row from `results` optimistically (the server would exclude it on next
  fetch). `restoreItem` refetches page 1 (reset) so the item can reappear.
- `markDownloaded` / `markGrabbedSiblings` continue to patch loaded rows.

### `hydrateCache` / `onMount`

`hydrateCache` is replaced by `loadResults(reset:true)` in paged mode. The
`onMount` order is unchanged: try live `getResults`; if it returns items, live
mode; otherwise paged browse via `loadResults`.

### Deck

`deckResults` derives from the accumulated `results` filtered to actionable
(`missing`/`upgrade`, has `url`, not selected). A helper `deckNeedsMore()` is
true when un-swiped actionable cards fall below a threshold (e.g. 8) and
`hasMore`; the deck component calls `loadResults()` when it fires.

### Category default

`categoryFilter` default changes from `['4k']` to `['4k','remux','tv']` (all),
so remux/TV missing titles are visible by default.

## Component C — Page (`frontend/src/routes/+page.svelte`)

- Remove `currentPage`/`perPage`/`totalPages`/`paginatedResults` slice and the
  Prev/Next pager block.
- Introduce a client **render window** `renderLimit = $state(100)`; the list
  and grid render `filteredResults.slice(0, renderLimit)` grouped as today.
- An `IntersectionObserver` on a bottom **sentinel** element fires when it
  enters view:
  - `renderLimit += 100`.
  - In paged mode, if `renderLimit` is within a threshold of
    `results.length` and `$hasMore && !$loadingMore`, call `loadResults()`
    (append) to fetch the next server page.
- A "loading more…" row shows while `$loadingMore`; a failed fetch shows an
  inline **Retry** on that row and leaves loaded rows intact. A footer shows
  "showing {rendered} of {filteredTotal}".
- Changing a filter resets `renderLimit = 100` and scrolls the container to
  top.
- `groupedResults` groups `filteredResults.slice(0, renderLimit)`;
  `siblingCounts` reads `$titleCounts` in paged mode (falls back to a local
  count in live mode).
- Select-all wiring passes the current filter state + source to the updated
  `selectAll`, which posts to `/results/select-all` and sets `selectedKeys`
  from the returned `group_keys`.

## Component D — API client (`frontend/src/lib/api/client.ts`)

- `getResults` / `getCachedResults` already accept an arbitrary params record —
  no signature change; callers pass the new filter params.
- `selectAll` gains an optional payload `{ source, filter, search, category,
  genre, language, quick }` posted as the request body.

## Error Handling

- A failed page fetch: keep loaded rows, surface Retry on the loading row, log
  once. Never clear `results` on error.
- Debounced filter changes (250 ms) prevent fetch storms while typing search.
- Stale-response guard (query-key check) prevents an out-of-order page from a
  superseded filter overwriting the current view.
- Empty filtered set → the existing "No results match your filter" empty state
  (driven by `filteredTotal === 0`, not `results.length`).

## Testing

**Backend (pytest, `tests/test_api_results.py`):**
- Each new filter param narrows correctly (category effCategory incl. the
  season→tv and unknown-shows rules; genre intersect; language; each `quick`).
- Typed sort orders correctly for size ("10 GB" > "9 GB"), posted_date
  (chronological, not lexical), year/rating numeric, title casefold; `order`
  reverses.
- `title_counts` sums to the filtered total and is per-title accurate.
- Pagination: pages are disjoint and their union equals the full filtered set
  (no gaps/overlaps) for a fixture > 1 page.
- `stats` stay whole-set while `filter` narrows `items`/`filtered_stats`.
- Filter-aware select-all returns exactly the matching `group_keys`; absent
  body preserves the legacy select-all-last-scan behavior.

**Frontend (vitest):**
- `loadResults(reset)` replaces + sets page 1; `loadResults()` appends + bumps
  page; `hasMore` flips false when `results.length >= total`.
- Query-key change triggers a debounced reset; a stale response is discarded.
- `pagedMode` toggles the `filteredResults` derivation (paged = passthrough).
- `deckNeedsMore` triggers a fetch when actionable cards run low and `hasMore`.

**Manual acceptance:** open the Scan page on the browse/cached view with the
Missing tab; scroll — the list keeps loading past 100/500 until "showing N of
N" equals the badge; switching category/search/sort refetches from page 1;
badges continue to reflect the whole set.

## Files Touched

- `backend/api/routes/results.py` — filter parity, typed sort, `title_counts`,
  `_filter_and_sort` helper, filter-aware select-all.
- `tests/test_api_results.py` — new coverage (create if absent; repo tests live
  at the root `tests/` dir).
- `frontend/src/lib/stores/results.ts` — paged state, `loadResults`, mode-aware
  `filteredResults`, subscription/debounce, deck helper, category default.
- `frontend/src/routes/+page.svelte` — infinite-scroll render window +
  sentinel, remove Prev/Next pager, select-all wiring, footer/loading row.
- `frontend/src/lib/api/client.ts` — `selectAll` payload.
- `frontend/src/lib/components/*` deck component — `deckNeedsMore` trigger.
- Frontend unit tests for the store.

## Deploy

In-app changes deploy via `docker compose up -d --build` (frontend baked into
the image) — **only when the user asks**; not during the build.
