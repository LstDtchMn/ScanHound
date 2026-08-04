# Category-switch live-mode fix — deep-dive findings + change record

**Date:** 2026-08-05 · **Branch:** `agent/category-switch-cache-fix` (off `main@7adb17b`) ·
**Builds:** Claude · **Verifies:** vitest + svelte-check + ChatGPT review · **Approves:** Jesse (🔒 merge)

## The reported symptom

Open the app (it shows the last scan's TV results), switch the category to 4K or Remux → the list
stays empty. The user wondered whether a rescan should be required and whether RSS/page crawls had
even captured recent items.

## Root cause (verified on the live instance, read-only)

The data was never missing. A read-only snapshot of the production DB (2026-08-04 00:56 UTC) showed
`background_scan_cache` holding 2,963 rows — 1,623 `4k`, 1,156 `tv`, 184 `remux` (the UI category is
`json data.category`; the `source_category` COLUMN holds the source name, e.g. 'HDEncode', for all
rows) — and **all three categories had `MAX(last_seen_at)` 38 minutes old**: the hourly `rss_shadow`
cycle keeps every category fresh even with `background_scan_enabled=false` (an active RSS mode gates
the background cycle on; sources collapse to `['HDEncode']`).

The lock is in the frontend. `+page.svelte` `onMount` calls `GET /results` (the last scan's
in-memory snapshot); if it returns ANY items the app sets `pagedMode=false` ("live mode"). In live
mode a category chip only client-filters the in-memory array (`filteredResults`); the debounced
refetch bails (`if (!get(pagedMode)) return;`) and `loadResults` no-ops. Nothing ever flips live →
paged, so the fresh cached rows for other categories are **never requested**. Paged mode's
`GET /results/cached?category=…` path always worked — live mode just never used it.

## The fix (Jesse-ratified design: "category switch always reads the saved list")

`toggleCategoryFilter` — the single writer of `categoryFilter` (both FilterBar's Category row and
ScanControls' chips call it) — now exits live mode and refetches from the server cache when toggled
outside a running scan:

- `results.ts`: after the toggle, `if (!pagedMode && !scanActive) { pagedMode.set(true);
  loadResults(true); }`. A new module-level `scanActive` flag + exported `setScanActive()` receives
  scan state from scanner.ts — results.ts cannot import the scanner store without an import cycle
  (scanner.ts already imports `clearResults` from results.ts).
- `scanner.ts`: `scanState.subscribe((s) => setScanActive(s !== 'idle'))` — every scanState writer
  (startScan, scan:complete/error, the idle-reconcile poll) flows through one subscription.
- **Scan streams are untouched**: while a scan is running/stopping the toggle keeps the old
  client-side filtering (the stream owns the deck; `handleScanResult` clears paged rows and flips
  back to live on the next streamed item — flipping mid-stream would churn against it). A scan you
  run still shows its results immediately; the first category switch afterward returns to the full
  saved list.
- **Latent timer leak found & fixed while proving single-fetch:** the debounced `_filterKey`
  subscription cleared a pending refetch timer only on its paged path, so a timer scheduled just
  before a paged→live flip survived and could fire a duplicate page-1 fetch into a later paged
  world. The clear now happens unconditionally before the mode check.

## Evidence (executed, exact)

- **Red first:** with the fix reverted to main's behavior (plus an inert `setScanActive` stub so the
  import resolves), the three discriminating tests FAIL — 3 failed / 117 passed, exit 1
  (`red_run.log`): live toggle doesn't exit live mode, fetch count is 0 not 1, post-scan toggle
  stays locked. The fourth test (paged-mode debounce path, pre-existing behavior) passes, proving
  the suite discriminates on exactly the changed axis.
- **Green:** full frontend unit suite **396 passed / 0 failed / exit 0**; `svelte-check` 0 errors
  (3 pre-existing a11y warnings in unrelated files); production `vite build` exit 0.
- New suite: `results.test.ts` "category toggle exits live mode to the server cache" — (1) live
  toggle flips to paged and fetches exactly the narrowed categories (`category=4k,remux` asserted
  on the request); (2) exactly ONE fetch (direct call; debounce bailed) over a 350 ms window;
  (3) mid-scan toggle stays live/client-filtered and post-scan toggle exits — wired through the
  REAL scanner store (`scanState.set`), not the mirror flag; (4) paged-mode toggle still debounces
  and never double-fires.

## Declared limitation (deferral rule 4)

- **(a) Browser-level (clicked-in-UI) verification is pending deploy.** The production backend is
  reachable only via Cloudflare Access (no host-published port), so the executed evidence is at the
  store layer, where the behavior lives; the chip → `toggleCategoryFilter` wiring is unchanged code.
  *Residual risk:* low — a UI regression here would be a wiring break the unchanged components
  already cover. *Trigger:* first post-deploy check exercises open → switch-category → populated
  list and records a screenshot.

## Side findings routed elsewhere (not in this change)

- `POST /scanner/rescan-item` writes `details['category'] = source NAME` (scanner.py:419), so a
  rescanned row passes EVERY category filter afterward — spun off as its own task.
- Manual `POST /scanner/start` results never reach `background_scan_cache` (in-memory
  `_last_scan_items` only) — Jesse explicitly chose NOT to change this now (option declined in the
  design popup).
- Under future `rss_primary`, the main deck would starve (RSS candidates are quarantined by design;
  the deck is fed only by the listing crawl, which `rss_primary` skips unless `fallback_qualified`)
  — recorded as promotion-program input for the R-track.

## Proposed contract row (fold into rev 3.2 as Track 5 / D-7)

| ID | Exit criterion | Evidence (executed, exact) | B/V/A | Status |
|---|---|---|---|---|
| D-7 | Category switch always consults the server cache; live mode is a scan-time overlay, not a lock; scan streams unaffected | red-first suite (3-fail discrimination run) + 396/0 green + check/build clean at the branch head; post-deploy UI check per the declared limitation | C / CG + vitest / 🔒 | 🔨 built, verdict pending |
