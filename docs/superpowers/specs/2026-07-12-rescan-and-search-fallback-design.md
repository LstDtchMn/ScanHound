# Item Rescan + Search-Fallback — Design Spec

**Date:** 2026-07-12
**Status:** Approved (design), pending implementation plan

Two small, related additions to the scan-results browsing experience, both aimed at recovering from "the cache doesn't have what I need" moments without a full re-scan.

## Feature A — Rescan button (per-item metadata refresh)

**Problem:** A small fraction (~1.3%, confirmed by live investigation) of scanned items end up with no poster/rating/genres/IMDb id — most often a transient scrape hiccup on one request during a large concurrent batch scan, not a systemic bug (verified: manually re-fetching the same page and re-running the same extraction code succeeds cleanly). Because scans skip already-cached URLs, an item that hit this once stays broken until something explicitly re-visits it.

**Backend:** `POST /scan/rescan-item`, body `{url: string}`.
- Looks up the item's existing cached row (for its known `source`/`category`, so the refreshed record doesn't lose that classification).
- Calls `reg.scanner.scrapers.scrape_details(url, headers)` directly for this ONE url — bypasses the skip-already-cached-URLs optimization entirely (that only applies to the listing crawl, not a direct fetch).
- Builds a fresh `MediaItem` via `reg.scanner._create_media_item({...})`, preserving the recovered `source`/`category`.
- Re-runs the full enrichment pipeline: `await reg.scanner._enricher.enrich([item])` — same TMDB/OMDb/RT logic that already works correctly; no new matching logic.
- Upserts the refreshed item into `background_scan_cache` via `db.upsert_background_cache([...])`.
- Returns the refreshed item (via the existing `_media_item_to_dict`) so the UI can update in place.
- If `scrape_details` returns `None` (genuinely blocked/failed), respond with a clear error rather than silently no-op'ing.

**Frontend:** A "Rescan" button in `DetailPanel.svelte`'s action row (shared by both desktop `+page.svelte` and mobile `DetailSheet.svelte` — one change covers both platforms), next to "Copy Links"/"Open Page". Shows an in-progress state while the request is outstanding; on success, merges the returned fields into the item in the `results` store (so poster/rating/genres/imdb_id update live without a page reload) and shows a success toast; on failure, an error toast.

## Feature B — Search-fallback to Site Search

**Problem:** The search box (`searchFilter`) only filters what's already in the local cache/live results. "Site Search" (a live scrape of one source's own search results) already exists as a feature but requires manually switching scan mode — it's not connected to the everyday search box at all.

**Trigger (per user decision):** Manual, not automatic. When the search box has text and the current view has zero matches for it, the empty state shows a button — never auto-starts a scan just from typing (starting any scan, including Site Search, replaces the current browse view via `clearResults()`, so an automatic trigger would be surprising).

**Source (per user decision):** Whichever source is currently selected in the scan toolbar (`ScanControls`'s source picker, e.g. "HDEncode"), not hardcoded.

**State change required:** `ScanControls.svelte`'s `selectedSource` is currently local `$state`, invisible outside that component. Lift it into a small shared store in `stores/scanner.ts` (e.g. `selectedScanSource`, default `'HDEncode'`) so both `ScanControls` and the empty-state buttons agree on "the current source." `ScanControls` switches from local state to reading/writing the store; no behavior change for it otherwise.

**Wiring:** A new helper (e.g. `searchThisSite(query, source)`) in `stores/scanner.ts` that calls the existing `clearResults()` + `startScan('search', query, 1, source, {})` — flags are irrelevant for Site Search mode (confirmed: `_build_sources` never reads `flags` when `scan_type == "Site Search"`, only `search_query`/`source_type`), so an empty flags object is correct.

**UI:** Both `+page.svelte` (desktop) and `MobileScanView.svelte` (mobile) already share the same empty-state gate (`isResultsViewEmpty` + `hiddenByFiltersCount > 0`, which already covers "search text matches nothing"). Add a new button there, shown only when `$searchFilter` is non-empty: **"Search {selectedScanSource} for '{searchFilter}' →"**, calling `searchThisSite($searchFilter, $selectedScanSource)`. Sits alongside the existing "Clear filters" button, not replacing it (a search with zero local matches might still benefit from clearing an unrelated resolution/genre filter first — both options stay visible).

**Not touching:** the existing Site Search mode/endpoint itself, `_build_sources`, or any matching/enrichment logic — this is purely a UI bridge between two already-working features.

## Testing

- Backend: `test_scan_rescan_item.py` — success path (mocked `scrape_details` + `enrich`, asserts cache upsert + returned dict shape), not-found source URL, `scrape_details` returning `None` → clear error.
- Frontend: `scanner.test.ts` additions for `selectedScanSource` store default/set, and `searchThisSite()` (asserts it calls `clearResults` then `startScan` with the right args — mocked). `DetailPanel`'s Rescan button logic (the merge-into-`results`-store behavior) tested as a pure store-update function if it can be isolated, matching this project's "no `.svelte` render tests" convention.
