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


---

## Round 2 (2026-08-05, after ChatGPT's CHANGES-REQUIRED verdict) -- commit `b5dd04b`

Both blockers fixed, both behaviorally tested, plus the nonblocking note closed:

1. **Empty selection sentinel:** empty `categoryFilter` now crosses the API as
   `category=__none__` (named `CATEGORY_NONE_SENTINEL` in results.py, subtracted from the
   enabled set). Contract test `tests/test_results_category_sentinel.py`; mutation run
   injecting the empty-means-all regression fails the named test (1 failed / 3 passed) --
   the raw behavior was accidental before, now it is a stated, discriminating contract.
2. **Backend-observed scan activity:** stream backstop (`handleScanResult` sets /
   `handleScanComplete` clears the flag), explicit clears in scanner.ts'
   `scan:complete`/`scan:error` (a streamed-only scan never flips `scanState`, so the
   mirror alone cannot clear), `scan:progress` adopts 'running' when idle (never
   overriding 'stopping'), and `reconcileScanActivity()` (exported, tested) reconciles
   with `api.scanStatus` at startup and every reconnect.
3. **Failure path (was a declared limitation):** the live exit now drops live rows at the
   flip; a failed cache request shows the `loadError` state, never rows contradicting the
   chips. Tested.

Evidence: six new tests red at the prior head (6 failed / 120 skipped, exit 1) -> green at
`b5dd04b`: vitest **402/0 exit 0**, svelte-check 0 errors, vite build exit 0, backend
subset 62 passed / exit 0 (throwaway container).

**Overlap change:** this round adds `backend/api/routes/results.py` + a new backend test
file to the branch. `agent/hybrid-sweep-rebased` also edits results.py -- the round-1
zero-overlap measurement no longer holds; combined-tree validation after this merge is
now mandatory (different regions of the file: `_filter_and_sort` sentinel vs
`_effective_category`/bookmark work).

The remaining declared limitation is unchanged: clicked-in-UI verification is post-deploy
(prod behind CF Access).

---

## Round 3 (2026-08-06, full-program audit finding) -- commit `bc9981a`

Not review feedback: found by the decomposed program audit, in this same file, and it is the
same *class* as the reviewer's blocker 2 (this session's view being rewritten by a scan it did
not start).

`handleScanComplete` mutated this session's view unconditionally, but `scan:complete` is
broadcast to EVERY session. A scheduled scan -- or another client's scan -- that streamed
nothing into this session therefore hit the "clear stale results" branch and wiped the rows,
selection and open detail panel of a user who was merely browsing the cache, while
`stats.set(s)` clobbered the tab counts with the scan's zero stats and `fromCache.set(false)`
dropped the cached-results banner from under still-cached rows.

The view mutations now apply only in live mode, where this session is actually showing that
scan's output; in paged mode `results` is server-owned cache content that no scan outcome
speaks for. The activity flag (`scanActive`) and the per-scan streamed counter stay
unconditional -- both are global facts about the scan, not view state.

Evidence: red-first (mutating the guard back out fails the new paged-view test, 1 failed /
127 skipped), with a **negative control** proving a LIVE scan that finds nothing still clears
its own stale rows. Green at `bc9981a`: vitest **404/0 exit 0**, svelte-check 0 errors, vite
build exit 0.
