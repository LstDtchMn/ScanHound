# Item Rescan + Search-Fallback Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a per-item "Rescan" action that force-refreshes a scan result's metadata (bypassing the scan's normal skip-already-cached-URLs optimization), and bridge the existing "Site Search" scan mode to the everyday search box so a zero-local-matches search can offer a one-click live search fallback.

**Architecture:** Feature A adds one backend endpoint that reuses the scan pipeline's existing scraping/enrichment/cache-upsert building blocks for a single URL, plus a frontend button + store-merge function. Feature B lifts one piece of component-local state into a shared store so the empty-state UI (which already exists) can read "current source" and trigger the existing Site Search flow. Neither feature touches matching/enrichment logic itself — both are wiring around already-working pieces.

**Tech Stack:** FastAPI (Python) backend, SvelteKit 5 (Svelte runes) frontend, Vitest, pytest.

## Global Constraints

- Feature A must NOT change `scrape_details()`, `_create_media_item()`, or `MetadataEnricher.enrich()` — it only calls them for a single item, exactly as the bulk scan already does. No new matching/enrichment logic.
- Feature A's rescan endpoint follows this codebase's established pattern of a plain (non-async) `def` route handler doing blocking I/O directly (see `backend/api/routes/downloads.py:224 scrape_links`) — FastAPI runs sync handlers in a worker thread automatically. Since `MetadataEnricher.enrich()` is `async def`, call it via `asyncio.run(...)` from inside the sync handler (safe: sync handlers run in a plain thread with no event loop of their own).
- Feature B's fallback trigger is manual only (a button), never automatic — starting any scan (including Site Search) calls `clearResults()` and replaces the current browse view.
- Feature B's fallback source is whichever source is currently selected in the scan toolbar, not hardcoded.
- Backend tests: throwaway-container pytest pattern (prod image has no pytest) — `docker run -d --name <c> --entrypoint sleep scanhound:latest infinity`, `docker cp backend/. tests/. <c>:/app/...`, `docker exec <c> pip install -q pytest httpx`, run, `docker rm -f <c>`. Re-copy after every edit.
- Frontend: `cd frontend && npm run check && npm run build && npx vitest run` on HOST node. Watch for smart/curly quotes in any new markup — verify by grepping new lines for U+201C/U+201D before considering a task done; do not trust a "build passed" self-report without independently re-running check.
- Work directly on `main`. Commit only when green; new commit per task.

---

### Task 1: Backend — `get_background_cache_by_url` + `POST /scan/rescan-item`

**Files:**
- Modify: `backend/database.py` (add near `get_background_cache`, line ~1762)
- Modify: `backend/api/routes/scanner.py` (add new route + `RescanItemRequest` model near `ScanRequest`, line ~50)
- Test: `tests/test_scan_rescan_item.py` (new)

**Interfaces:**
- Produces: `DatabaseManager.get_background_cache_by_url(url: str) -> dict | None` — single cached row (same columns as `get_background_cache`'s rows: url, title, year, status, source_category, data, scraped_at, last_seen_at), or `None` if not cached.
- Produces: `POST /scan/rescan-item` — body `{"url": str}`, response `{"status": "ok", "item": <_media_item_to_dict shape>}` on success; `404` if the URL isn't in the cache at all; `502` if the live re-scrape fails/returns nothing.

- [ ] **Step 1: Write the failing DB test**

Add to `tests/test_database_media_probe.py` (reuse its existing fixtures — this file already has cache-adjacent tests from a prior feature; grep it for its `db` fixture pattern first) OR create `tests/test_database_bg_cache.py` if no suitable home exists — check with `grep -n "background_scan_cache\|upsert_background_cache" tests/*.py` first and place beside an existing sibling test rather than guessing:

```python
def test_get_background_cache_by_url_found_and_missing(db):
    db.upsert_background_cache([{
        "url": "https://hdencode.org/example/",
        "title": "Example", "year": 2020, "status": "missing",
        "source_category": "hdencode", "data": '{"title": "Example"}',
    }])
    row = db.get_background_cache_by_url("https://hdencode.org/example/")
    assert row is not None
    assert row["title"] == "Example"
    assert row["source_category"] == "hdencode"
    assert db.get_background_cache_by_url("https://hdencode.org/nope/") is None
```

- [ ] **Step 2: Run to verify it fails**

Run (throwaway container): `python3 -m pytest tests/<chosen_file>.py -k background_cache_by_url -q`
Expected: FAIL — `AttributeError: 'DatabaseManager' object has no attribute 'get_background_cache_by_url'`

- [ ] **Step 3: Implement the DB method**

In `backend/database.py`, immediately after `get_background_cache` (ends ~line 1767):

```python
    def get_background_cache_by_url(self, url):
        """Return one cached background-scan row by URL, or None."""
        rows = self._query_dicts(
            'SELECT url, title, year, status, source_category, data, '
            'scraped_at, last_seen_at FROM background_scan_cache '
            'WHERE url = ? LIMIT 1', (url,), default=[])
        return rows[0] if rows else None
```

- [ ] **Step 4: Run to verify it passes**

Re-copy `backend/.`, run the same test → PASS.

- [ ] **Step 5: Write the failing route tests**

Create `tests/test_scan_rescan_item.py`, matching `tests/test_api_rename.py`'s `create_app(config_override=...)` + `TestClient` fixture pattern:

```python
"""Tests for POST /scan/rescan-item."""
import pytest
from unittest.mock import patch, MagicMock
from fastapi.testclient import TestClient

from backend.api.main import create_app
from backend.database import DatabaseManager


@pytest.fixture
def client():
    app = create_app(config_override={"plex_url": "", "plex_token": ""})
    with TestClient(app) as c:
        yield c


def _seed_cache_row(url):
    dm = DatabaseManager()
    dm.upsert_background_cache([{
        "url": url, "title": "Old Title", "year": 1969, "status": "missing",
        "source_category": "hdencode", "data": '{"title": "Old Title"}',
    }])
    dm.close()


def test_rescan_item_not_in_cache_returns_404(client):
    resp = client.post("/scan/rescan-item", json={"url": "https://hdencode.org/unknown/"})
    assert resp.status_code == 404


def test_rescan_item_success_updates_cache(client):
    url = "https://hdencode.org/journey-example/"
    _seed_cache_row(url)
    fake_details = {
        "display_title": "Doppelganger", "year": 1969, "rating": "-",
        "url": url, "imdb_id": "tt0064519", "size": "23.9 GB", "res": "1080p",
        "hdr": "SDR", "dovi": False, "is_tv": False, "season": None,
        "episode_number": None, "episodes": None, "posted_date": None,
    }
    with patch("backend.scanner_service.ScannerService.scrapers", create=True), \
         patch("backend.database.DatabaseManager.get_background_cache_by_url",
               return_value={"url": url, "source_category": "hdencode"}):
        with patch.object(
            __import__("backend.api.dependencies", fromlist=["registry"]).registry.scanner.scrapers,
            "scrape_details", return_value=fake_details,
        ):
            resp = client.post("/scan/rescan-item", json={"url": url})
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["item"]["imdb_id"] == "tt0064519"
    # Confirm it actually persisted.
    dm = DatabaseManager()
    row = dm.get_background_cache_by_url(url)
    dm.close()
    assert row is not None
    assert '"imdb_id": "tt0064519"' in row["data"]


def test_rescan_item_scrape_failure_returns_502(client):
    url = "https://hdencode.org/journey-example-2/"
    _seed_cache_row(url)
    import backend.api.dependencies as deps
    with patch.object(deps.registry.scanner.scrapers, "scrape_details", return_value=None):
        resp = client.post("/scan/rescan-item", json={"url": url})
    assert resp.status_code == 502
```

Note: the exact mocking approach above (patching `registry.scanner.scrapers.scrape_details`) is a starting point — verify against the REAL `ServiceRegistry`/`registry` singleton shape used elsewhere in `tests/test_api_rename.py`-style tests (grep `registry\.` usage in existing route tests) and adjust the patch target to whatever actually works; the assertions (404/200-with-imdb_id/502) are the real requirements, the mock plumbing is negotiable.

- [ ] **Step 6: Run to verify RED**

Run: `python3 -m pytest tests/test_scan_rescan_item.py -q`
Expected: FAIL — route doesn't exist yet (404 on all, or import error).

- [ ] **Step 7: Implement the route**

In `backend/api/routes/scanner.py`, add near `ScanRequest` (line ~50):

```python
class RescanItemRequest(BaseModel):
    url: str
```

Add the route (near the other `/scan/*` routes, e.g. after `scan_stop`):

```python
@router.post("/rescan-item")
def rescan_item(
    req: RescanItemRequest,
    reg: ServiceRegistry = Depends(get_registry),
):
    """Force-refresh a single cached scan result: re-fetch its detail page
    and re-run TMDB/OMDb/RT enrichment, bypassing the normal scan's
    skip-already-cached-URLs optimization. Reuses the exact same scraping/
    enrichment pipeline the bulk scan uses — no new matching logic."""
    if not req.url:
        raise HTTPException(status_code=400, detail="No URL provided")
    db = reg.db
    scanner = reg.scanner
    if not db or not scanner:
        raise HTTPException(status_code=503, detail="Scanner not available")

    existing = db.get_background_cache_by_url(req.url)
    if not existing:
        raise HTTPException(status_code=404, detail="Item not found in cache")

    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
    try:
        details = scanner.scrapers.scrape_details(req.url, headers)
    except Exception as e:
        logger.exception("Rescan failed for %s", req.url)
        raise HTTPException(status_code=502, detail=f"Rescan failed: {e}")
    if not details:
        raise HTTPException(status_code=502, detail="Could not fetch a fresh copy of this page")

    post_source = existing.get("source_category") or "hdencode"
    details['source'] = post_source
    details['category'] = existing.get("source_category") or ""

    item = scanner._create_media_item({
        'details': details, 'is_tv': details.get('is_tv', False), 'url': req.url,
    })
    if not item:
        raise HTTPException(status_code=502, detail="Could not parse the refreshed page")

    import asyncio
    asyncio.run(scanner._enricher.enrich([item]))

    d = _media_item_to_dict(item)
    db.upsert_background_cache([{
        "url": req.url,
        "title": d.get("title"),
        "year": d.get("year"),
        "status": str(d.get("status", "")),
        "source_category": post_source,
        "data": __import__("json").dumps(d, default=str),
    }])
    return {"status": "ok", "item": d}
```

(Use a proper top-of-file `import json` and `import asyncio` instead of the inline `__import__`/local-`import asyncio` shown above if the file doesn't already import them — check the existing import block first and place them there normally; the inline form above is just illustrative of what's needed.)

- [ ] **Step 8: Run to verify GREEN**

Re-copy `backend/.`, run: `python3 -m pytest tests/test_scan_rescan_item.py -q`
Expected: all pass. Then run the wider suite touched: `python3 -m pytest tests/test_scan_rescan_item.py tests/test_scanner_service.py tests/test_api_rename.py -q` (or whichever file Step 5 actually landed the DB test in) to confirm no regressions.

- [ ] **Step 9: Commit**

```bash
git add backend/database.py backend/api/routes/scanner.py tests/test_scan_rescan_item.py <the DB test file from Step 1/5>
git commit -m "feat(scan): rescan-item endpoint for on-demand metadata refresh"
```

---

### Task 2: Frontend — `api.rescanItem` + result-store merge + Rescan button

**Files:**
- Modify: `frontend/src/lib/api/client.ts` (new client method, near `scrapeLinks`)
- Modify: `frontend/src/lib/stores/results.ts` (new `updateResultFromRescan` function, near `markDownloaded`)
- Modify: `frontend/src/lib/components/DetailPanel.svelte` (new button + handler)
- Test: `frontend/src/lib/stores/results.test.ts` (add case for `updateResultFromRescan`)

**Interfaces:**
- Consumes: Task 1's `POST /scan/rescan-item`.
- Produces: `api.rescanItem(url: string): Promise<{status: string; item: ScanResult}>`; `updateResultFromRescan(url: string, patch: Partial<ScanResult>): void`.

- [ ] **Step 1: Write the failing store test**

Add to `frontend/src/lib/stores/results.test.ts` (check its existing imports/setup for the `results` store's test pattern — reuse the same fixture style as neighboring tests, e.g. how `markDownloaded` or `dismissItem` are tested there):

```typescript
import { get } from 'svelte/store';
import { results, updateResultFromRescan } from './results';

describe('updateResultFromRescan', () => {
  it('merges the patch into the matching item by url', () => {
    results.set([
      { url: 'https://x/1', title: 'Old', imdb_id: null, poster_url: '', rating: 0 } as any,
      { url: 'https://x/2', title: 'Other' } as any,
    ]);
    updateResultFromRescan('https://x/1', { title: 'New', imdb_id: 'tt0064519', rating: 6.3 } as any);
    const items = get(results);
    expect(items[0]).toMatchObject({ url: 'https://x/1', title: 'New', imdb_id: 'tt0064519', rating: 6.3 });
    expect(items[1]).toMatchObject({ url: 'https://x/2', title: 'Other' });
  });

  it('no-ops when the url is not present', () => {
    results.set([{ url: 'https://x/1', title: 'Old' } as any]);
    updateResultFromRescan('https://x/999', { title: 'New' } as any);
    expect(get(results)[0].title).toBe('Old');
  });
});
```

- [ ] **Step 2: Run to verify RED**

Run: `cd frontend && npx vitest run src/lib/stores/results.test.ts`
Expected: FAIL — `updateResultFromRescan` not exported.

- [ ] **Step 3: Implement `updateResultFromRescan`**

In `frontend/src/lib/stores/results.ts`, near `markDownloaded` (ends ~line 1013+):

```typescript
/** Merge a rescanned item's fresh fields (poster/rating/genres/imdb_id/etc.)
 *  into the matching row by url, in place — no-op if the url isn't present
 *  (e.g. it scrolled out of a paged view since the rescan started). */
export function updateResultFromRescan(url: string, patch: Partial<ScanResult>) {
  if (!url) return;
  results.update((items) =>
    items.map((it) => (it.url === url ? { ...it, ...patch } : it))
  );
}
```

(Confirm `ScanResult` is already imported/available in this file — it almost certainly is, given the rest of the store is typed against it; if not, add `import type { ScanResult } from '$lib/api/types';`.)

- [ ] **Step 4: Run to verify GREEN**

Run: `npx vitest run src/lib/stores/results.test.ts` → pass.

- [ ] **Step 5: Add the API client method**

In `frontend/src/lib/api/client.ts`, near `scrapeLinks` (line ~188):

```typescript
  rescanItem: (url: string) =>
    request<{ status: string; item: ScanResult }>('/scan/rescan-item', {
      method: 'POST',
      body: JSON.stringify({ url })
    }),
```

(Confirm `ScanResult` is imported in this file already — matches the return type of other client methods here.)

- [ ] **Step 6: Add the Rescan button to DetailPanel.svelte**

In `frontend/src/lib/components/DetailPanel.svelte`, add the import (alongside the existing `results, markDownloaded` import, line ~6):

```svelte
  import { results, markDownloaded, updateResultFromRescan } from '$lib/stores/results';
```

Add state + handler near `copyingLinks`/`copyLinks` (line ~112):

```svelte
  let rescanning = $state(false);
  async function rescanItem() {
    if (!item.url || rescanning) return;
    rescanning = true;
    try {
      const { item: fresh } = await api.rescanItem(item.url);
      updateResultFromRescan(item.url, fresh);
      addToast('Rescanned', `Refreshed metadata for ${fresh.title || item.title}`);
    } catch (e) {
      addToast('Error', e instanceof Error ? e.message : 'Rescan failed', 'error');
    } finally {
      rescanning = false;
    }
  }
```

Add the button in the action row, next to "Copy Links" (around line 396-411 — the block containing the `copyLinks` button):

```svelte
          <button
            onclick={rescanItem}
            disabled={rescanning}
            aria-label="Rescan this item"
            title="Re-fetch this page and refresh its poster/rating/genres"
            class="flex items-center gap-1.5 px-3 py-1.5 rounded text-xs font-medium bg-[var(--bg-tertiary)] text-[var(--text-secondary)] hover:text-[var(--text-primary)] transition-colors disabled:opacity-50"
          >
            {rescanning ? 'Rescanning…' : 'Rescan'}
          </button>
```

Use STRAIGHT ASCII quotes throughout — no curly/smart quotes in any attribute or string literal you write.

- [ ] **Step 7: Verify + run full checks**

```bash
cd frontend && npm run check && npm run build && npx vitest run
```
Expected: 0 ERRORS, build succeeds, all tests pass. Grep your new/changed lines for U+201C/U+201D before considering this done.

- [ ] **Step 8: Commit**

```bash
git add frontend/src/lib/api/client.ts frontend/src/lib/stores/results.ts frontend/src/lib/stores/results.test.ts frontend/src/lib/components/DetailPanel.svelte
git commit -m "feat(scan): Rescan button in item detail panel (desktop + mobile)"
```

---

### Task 3: Frontend — shared `selectedScanSource` store + `searchThisSite` helper

**Files:**
- Modify: `frontend/src/lib/stores/scanner.ts` (new store + helper)
- Modify: `frontend/src/lib/components/ScanControls.svelte` (switch `selectedSource` from local state to the shared store)
- Test: `frontend/src/lib/stores/scanner.test.ts` (new)

**Interfaces:**
- Consumes: `results.ts`'s `ScanSource` type, `clearResults()`; `scanner.ts`'s existing `startScan`.
- Produces: `selectedScanSource: Writable<ScanSource>` (default `'HDEncode'`); `searchThisSite(query: string, source: ScanSource): void`.

- [ ] **Step 1: Write the failing tests**

Create `frontend/src/lib/stores/scanner.test.ts`:

```typescript
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { get } from 'svelte/store';

vi.mock('$lib/api/client', () => ({
  api: { scanStart: vi.fn().mockResolvedValue({ status: 'started' }) }
}));
vi.mock('$lib/stores/results', () => ({
  clearResults: vi.fn(),
}));

import { selectedScanSource, searchThisSite } from './scanner';
import { clearResults } from '$lib/stores/results';

describe('selectedScanSource', () => {
  it('defaults to HDEncode', () => {
    expect(get(selectedScanSource)).toBe('HDEncode');
  });
});

describe('searchThisSite', () => {
  beforeEach(() => vi.clearAllMocks());
  it('clears results then starts a search scan for the given query and source', async () => {
    searchThisSite('Journey to the Far Side of the Sun', 'DDLBase');
    expect(clearResults).toHaveBeenCalled();
    const { api } = await import('$lib/api/client');
    expect(api.scanStart).toHaveBeenCalledWith(
      'search', 'Journey to the Far Side of the Sun', 1, 'DDLBase', undefined
    );
  });
});
```

(Adjust the exact `api.scanStart` call-argument assertion once Step 3 is implemented, if `startScan`'s real signature passes `flags` as `{}` rather than `undefined` — match whatever `searchThisSite` actually calls, the point of the test is proving `clearResults` then `startScan('search', query, 1, source, ...)` happens, in that order and with the right query/source.)

- [ ] **Step 2: Run to verify RED**

Run: `cd frontend && npx vitest run src/lib/stores/scanner.test.ts`
Expected: FAIL — `selectedScanSource`/`searchThisSite` not exported.

- [ ] **Step 3: Implement the store + helper**

In `frontend/src/lib/stores/scanner.ts`, add near the top (after the existing `scanState`/`scanProgress` writable declarations, line ~12):

```typescript
import type { ScanSource } from '$lib/stores/results';

/** The scan source currently selected in the toolbar (ScanControls) — lifted
 *  out of that component so other UI (the empty-state search fallback) can
 *  read "what source would a scan run against right now" without a prop
 *  drill. ScanControls reads/writes this instead of local state. */
export const selectedScanSource = writable<ScanSource>('HDEncode');
```

Add near the end of the file (after `startScan`):

```typescript
/** Run a live Site Search for `query` against `source`, replacing the
 *  current browse view — the same action as manually switching ScanControls
 *  to "Site Search" mode and hitting Scan. Flags are irrelevant for Site
 *  Search (the backend's _build_sources never reads them for that mode). */
export function searchThisSite(query: string, source: ScanSource) {
  clearResults();
  startScan('search', query, 1, source);
}
```

Add the `clearResults` import at the top of the file: `import { clearResults } from '$lib/stores/results';` — but FIRST check whether importing from `results.ts` into `scanner.ts` creates a circular import (grep `stores/results.ts` for any existing import FROM `stores/scanner.ts`; if one exists, this would cycle — in that case, move `searchThisSite` into `results.ts` instead, next to `clearResults`, and import `startScan`/`ScanType` from `scanner.ts` there instead, whichever direction avoids the cycle).

- [ ] **Step 4: Run to verify GREEN**

Run: `npx vitest run src/lib/stores/scanner.test.ts` → pass.

- [ ] **Step 5: Switch ScanControls.svelte to the shared store**

In `frontend/src/lib/components/ScanControls.svelte`:
- Add the import: `import { selectedScanSource } from '$lib/stores/scanner';`
- Replace `let selectedSource = $state<Source>('HDEncode');` (line ~18) — remove this local declaration entirely.
- Replace every remaining reference to `selectedSource` in this file with `$selectedScanSource` for reads, and `onSourceChange` (line ~35-37) becomes:

```typescript
  function onSourceChange(src: Source) {
    selectedScanSource.set(src);
  }
```

- Update every other read site (`flags = $derived(flagsFor(selectedSource, ...))`, `categories = $derived(sourceCategories[selectedSource])`, the `handleStart()` call, the template's `{selectedSource}` interpolations, and the `<select>`/button bindings) to read `$selectedScanSource` instead of the old local `selectedSource`. Grep the file for every remaining `selectedSource` occurrence after your edit and confirm none are left referencing the deleted local variable.

- [ ] **Step 6: Verify + full checks**

```bash
cd frontend && npm run check && npm run build && npx vitest run
```
Expected: 0 ERRORS, build succeeds, all tests pass (no regression in existing ScanControls behavior — manually reason through: does switching source in the toolbar UI still work identically, just now backed by a store instead of local state?).

- [ ] **Step 7: Commit**

```bash
git add frontend/src/lib/stores/scanner.ts frontend/src/lib/stores/scanner.test.ts frontend/src/lib/components/ScanControls.svelte
git commit -m "refactor(scan): lift selectedSource into a shared store; add searchThisSite helper"
```

---

### Task 4: Frontend — empty-state "Search {source} for '{query}'" button (desktop + mobile)

**Files:**
- Modify: `frontend/src/routes/+page.svelte` (empty-state block, ~line 615-634)
- Modify: `frontend/src/lib/components/mobile/MobileScanView.svelte` (empty-state block, ~line 148-161)

**Interfaces:**
- Consumes: Task 3's `selectedScanSource`, `searchThisSite`; existing `searchFilter` (`stores/results.ts`).

- [ ] **Step 1: Desktop**

In `frontend/src/routes/+page.svelte`:
- Add the import: `import { selectedScanSource, searchThisSite } from '$lib/stores/scanner';`
- Inside the existing `{#if $hiddenByFiltersCount > 0}` block (line ~615-634), immediately after the "Clear filters" button, add:

```svelte
          {#if $searchFilter}
            <button
              onclick={() => searchThisSite($searchFilter, $selectedScanSource)}
              class="px-3 py-1.5 rounded-lg text-xs font-medium text-white bg-[var(--accent)] hover:opacity-90 transition-opacity"
            >Search {$selectedScanSource} for "{$searchFilter}" &rarr;</button>
          {/if}
```

Confirm `searchFilter` is already imported in this file (it is — used throughout for the search box binding).

- [ ] **Step 2: Mobile**

In `frontend/src/lib/components/mobile/MobileScanView.svelte`, add the same import and, inside its equivalent `{#if $hiddenByFiltersCount > 0}` block (line ~149-161), immediately after its "Clear filters" button, add the identical button block from Step 1 (match this file's existing button styling exactly rather than copy-pasting desktop's classes verbatim if they differ — check the surrounding "Clear filters" button's classes here first and mirror those).

- [ ] **Step 3: Verify + full checks**

```bash
cd frontend && npm run check && npm run build && npx vitest run
```
Expected: 0 ERRORS, build succeeds, all pass. Grep both changed files for curly quotes — the `"{$searchFilter}"` interpolation above uses straight double-quotes as literal characters inside the button label, which is fine (they're just displayed text, not Svelte attribute syntax) but double-check no editor auto-smartened them.

- [ ] **Step 4: Manual verification (browser)**

Since this is a user-facing empty-state UI change, start the dev server and verify by hand: type a search term with zero local matches, confirm the button appears with the correct source name and query, click it, confirm a Site Search scan starts (view clears, scan progress shows). This step is for the implementer/reviewer to do via the project's browser preview tooling if available in that environment; note in the report if it wasn't possible to verify visually and rely on the automated checks instead.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/routes/+page.svelte frontend/src/lib/components/mobile/MobileScanView.svelte
git commit -m "feat(scan): empty-state Site Search fallback button (desktop + mobile)"
```

---

## Deployment

This plan does NOT deploy. Joins the existing batch (flat-movie-folders, split-part-suffix fix, skipped-items-manager) already awaiting a combined deploy after user review.
