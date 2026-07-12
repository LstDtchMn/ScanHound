# Title Bookmarks Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the user bookmark a title while browsing scan results, independent of which specific release/resolution they're looking at. Per-title (not per-release), keyed the same way the rest of this codebase already establishes identity (imdb_id first, normalized-title+year fallback).

**Architecture:** A new `bookmarks` SQLite table (`backend/database.py`) with a partial-unique index on `imdb_id` (when present) and a second partial-unique index on `(title_key, year, media_type)` (when `imdb_id` is absent) — mirroring the `dismissed_items` table's idempotent-migration style already in that file. `POST /results/bookmark` (toggle, mirrors the existing `POST /results/dismiss`'s explicit boolean-flag shape) and `GET /results/bookmarks` in `backend/api/routes/results.py`. `_shape_results` bulk-fetches the full bookmark identity-key set ONCE per request (mirroring the existing `dismissed`/`skipped_titles` bulk-set pattern already in that function) and annotates every item with `bookmarked: bool` BEFORE filtering, so a new `bookmarked` quick-filter chip can use it the same way the existing `inplex` quick filter already works. Frontend: a `bookmarkedTitles` client store (mirrors `dismissedUrls`'s shape/hydration), a star toggle button on `ResultRow.svelte`/`DetailPanel.svelte`/`DetailSheet.svelte`, and a "Bookmarked" entry added to `FilterBar.svelte`'s existing `quickChips` array (no new UI block needed — the two existing `{#each quickChips as chip}` render loops pick it up automatically).

**Tech Stack:** Python (SQLite), pytest; SvelteKit 5 (runes), vitest.

## Global Constraints

- **Per-title, not per-release.** Bookmarking "Dune Part Two" from a 1080p listing and later seeing the 4K Remux listing must both show the SAME bookmarked state — identity is `imdb_id` when present, else `(normalized_title, year, media_type)`.
- **No bookmark notes/tags/folders** — a plain on/off flag per title (YAGNI, matches the simplicity of the existing dismiss/skip mechanism).
- Use `normalize_title()` (existing, `backend/app_service.py:345`) for all title normalization — do not write a second normalization function.
- Backend tests: throwaway container pattern (`docker run -d --name <c> --entrypoint sleep scanhound:latest infinity`, `docker cp backend/. tests/. <c>:/app/...`, `pip install -q pytest httpx`, run, `docker rm -f`). Frontend tests: host node (`cd frontend && npm run check && npm run build && npx vitest run`).
- Work directly on `main`. Commit only when genuinely green.
- Smart/curly-quote hazard: plain ASCII quotes only; grep new/changed files before committing.
- **File overlap note:** several other workflows may be committing to this same working directory/branch concurrently this session, including one (genre-exclude-filter) that also edits `frontend/src/lib/components/FilterBar.svelte` (a different section of that file — the genre-chip blocks, not the `quickChips` array this plan touches) and `frontend/src/lib/stores/results.ts`. If an `Edit` call fails because the file changed underneath it since your last `Read`, that is expected — re-`Read` the current file content and retry your edit against the current text; it is not a sign of corruption. Always `git add` only the exact files your task lists, never `-A`.

---

### Task 1: `bookmarks` table + `DatabaseManager` CRUD

**Files:**
- Modify: `backend/database.py`
- Test: `tests/test_database.py` (check `ls tests/ | grep database` first; if a differently-named file already covers `DatabaseManager`, add there to match its existing fixture style)

**Interfaces:**
- Produces: `DatabaseManager.add_bookmark(imdb_id: str | None, title: str, year: int | None, media_type: str) -> bool`, `DatabaseManager.remove_bookmark(imdb_id: str | None, title: str, year: int | None, media_type: str) -> bool` (removes whichever row matches the same identity resolution `add_bookmark` would have used), `DatabaseManager.list_bookmarks() -> list[dict]` (each dict: `id, imdb_id, title, year, media_type, created_at`), `DatabaseManager.list_bookmark_keys() -> set[tuple]` (each tuple either `('imdb', imdb_id)` or `('title', normalized_title, year, media_type)` — the bulk-fetched identity set Task 2 will match items against, one query per request instead of one per item).

- [ ] **Step 1: Write the failing tests**

First run `ls tests/ | grep -i database` and read whichever file already tests `DatabaseManager` (e.g. its dismissed-items tests) to match its exact fixture/teardown style (likely an in-memory or tempfile SQLite `DatabaseManager` instance per test). Add:

```python
def test_add_bookmark_with_imdb_id_then_list(db):
    assert db.add_bookmark("tt1234567", "Dune: Part Two", 2024, "movie") is True
    bookmarks = db.list_bookmarks()
    assert len(bookmarks) == 1
    assert bookmarks[0]["imdb_id"] == "tt1234567"
    assert bookmarks[0]["title"] == "Dune: Part Two"


def test_add_bookmark_without_imdb_id_uses_title_key_fallback(db):
    assert db.add_bookmark(None, "Some Obscure Show", 2020, "tv") is True
    bookmarks = db.list_bookmarks()
    assert len(bookmarks) == 1
    assert bookmarks[0]["imdb_id"] is None


def test_add_bookmark_same_imdb_id_twice_is_idempotent(db):
    db.add_bookmark("tt1234567", "Dune: Part Two", 2024, "movie")
    db.add_bookmark("tt1234567", "Dune: Part Two (Remux)", 2024, "movie")
    assert len(db.list_bookmarks()) == 1


def test_add_bookmark_same_title_key_twice_is_idempotent(db):
    db.add_bookmark(None, "Some Obscure Show", 2020, "tv")
    db.add_bookmark(None, "some obscure show", 2020, "tv")
    assert len(db.list_bookmarks()) == 1


def test_remove_bookmark_by_imdb_id(db):
    db.add_bookmark("tt1234567", "Dune: Part Two", 2024, "movie")
    assert db.remove_bookmark("tt1234567", "Dune: Part Two", 2024, "movie") is True
    assert db.list_bookmarks() == []


def test_remove_bookmark_by_title_key_fallback(db):
    db.add_bookmark(None, "Some Obscure Show", 2020, "tv")
    assert db.remove_bookmark(None, "Some Obscure Show", 2020, "tv") is True
    assert db.list_bookmarks() == []


def test_list_bookmark_keys_shape(db):
    db.add_bookmark("tt1234567", "Dune: Part Two", 2024, "movie")
    db.add_bookmark(None, "Some Obscure Show", 2020, "tv")
    keys = db.list_bookmark_keys()
    assert ("imdb", "tt1234567") in keys
    assert any(k[0] == "title" and k[2] == 2020 and k[3] == "tv" for k in keys)


def test_remove_bookmark_nonexistent_returns_false(db):
    assert db.remove_bookmark("tt9999999", "Nothing", 2000, "movie") is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_database.py -v -k bookmark` (adjust filename per Step 1)
Expected: FAIL — `AttributeError: 'DatabaseManager' object has no attribute 'add_bookmark'`.

- [ ] **Step 3: Write the implementation**

In `backend/database.py`, find the table-creation block (near `dismissed_items`/`dv_scan`/`media_probe`, around lines 353-543) and add, in the same `cursor.execute('''CREATE TABLE IF NOT EXISTS ...''')` style:

```python
                # Per-title bookmarks (distinct from watchlist -- this is for
                # titles the user HAS already found and wants to remember, not
                # titles being searched-for). title_key is normalize_title(title),
                # stored so the fallback unique index doesn't need SQLite
                # expression-index support across all deployed versions.
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS bookmarks (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        imdb_id TEXT,
                        title TEXT NOT NULL,
                        title_key TEXT NOT NULL,
                        year INTEGER,
                        media_type TEXT NOT NULL,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                ''')
                cursor.execute(
                    'CREATE UNIQUE INDEX IF NOT EXISTS idx_bookmarks_imdb '
                    'ON bookmarks(imdb_id) WHERE imdb_id IS NOT NULL')
                cursor.execute(
                    'CREATE UNIQUE INDEX IF NOT EXISTS idx_bookmarks_title_key '
                    'ON bookmarks(title_key, year, media_type) WHERE imdb_id IS NULL')
```

Near the `dismissed_items` CRUD methods (`add_dismissed_items`/`remove_dismissed_items`/`get_dismissed_items`, around lines 1521-1624), add:

```python
    def add_bookmark(self, imdb_id, title, year, media_type):
        """Add a per-title bookmark. Idempotent: bookmarking the same
        identity (imdb_id, or normalized-title+year+media_type when no
        imdb_id) twice is a no-op, not a duplicate row. Returns True on
        success."""
        from backend.app_service import normalize_title
        title_key = normalize_title(title or "")
        if imdb_id:
            return self._mutate('''
                INSERT INTO bookmarks (imdb_id, title, title_key, year, media_type)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(imdb_id) DO NOTHING
            ''', (imdb_id, title, title_key, year, media_type), label="add_bookmark") is not None
        return self._mutate('''
            INSERT INTO bookmarks (imdb_id, title, title_key, year, media_type)
            VALUES (NULL, ?, ?, ?, ?)
            ON CONFLICT(title_key, year, media_type) WHERE imdb_id IS NULL DO NOTHING
        ''', (title, title_key, year, media_type), label="add_bookmark") is not None

    def remove_bookmark(self, imdb_id, title, year, media_type):
        """Remove a bookmark by the same identity resolution add_bookmark uses.
        Returns True if a row was actually deleted, False if nothing matched."""
        from backend.app_service import normalize_title
        title_key = normalize_title(title or "")
        conn = None
        try:
            with self._lock:
                conn = self.get_connection()
                if not conn:
                    return False
                cur = conn.cursor()
                if imdb_id:
                    cur.execute('DELETE FROM bookmarks WHERE imdb_id = ?', (imdb_id,))
                else:
                    cur.execute(
                        'DELETE FROM bookmarks WHERE imdb_id IS NULL '
                        'AND title_key = ? AND year IS ? AND media_type = ?',
                        (title_key, year, media_type))
                deleted = cur.rowcount > 0
                conn.commit()
            return deleted
        except Exception as e:
            try:
                if conn:
                    conn.rollback()
            except Exception:
                pass
            logger.error("DB Error (remove_bookmark): %s", e)
            return False

    def list_bookmarks(self):
        """Return every bookmark row (dicts), newest first."""
        return self._query_dicts(
            'SELECT id, imdb_id, title, year, media_type, created_at '
            'FROM bookmarks ORDER BY created_at DESC', default=[])

    def list_bookmark_keys(self):
        """Return the full set of bookmark identity keys in one query, for
        bulk per-item matching (avoids an N+1 query per result row). Each key
        is ('imdb', imdb_id) or ('title', title_key, year, media_type)."""
        rows = self._query_dicts(
            'SELECT imdb_id, title_key, year, media_type FROM bookmarks', default=[])
        keys = set()
        for r in rows:
            if r.get("imdb_id"):
                keys.add(("imdb", r["imdb_id"]))
            else:
                keys.add(("title", r.get("title_key"), r.get("year"), r.get("media_type")))
        return keys
```

Note SQLite's `year IS ?` (not `year = ?`) in the `DELETE` above — required because `year` can be `NULL`, and SQL `NULL = NULL` is never true while `NULL IS NULL` is; without this a bookmark with no year would never be removable.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_database.py -v -k bookmark` (adjust filename per Step 1)
Expected: PASS, all 8 tests green.

- [ ] **Step 5: Commit**

```bash
git add backend/database.py tests/test_database.py
git commit -m "feat(db): bookmarks table + CRUD (per-title, imdb_id-first identity key)"
```

(Adjust the test filename in `git add` to whatever Step 1 actually found/used.)

---

### Task 2: `/results/bookmark` + `/results/bookmarks` endpoints + `bookmarked` item flag

**Files:**
- Modify: `backend/api/routes/results.py`
- Test: same file as Task 1 identified for results-route tests (reuse Task 1's Global Constraints investigation, or check `ls tests/ | grep results` if different)

**Interfaces:**
- Consumes: `db.add_bookmark`, `db.remove_bookmark`, `db.list_bookmarks`, `db.list_bookmark_keys` (Task 1). `normalize_title` (existing, `backend/app_service.py:345`).
- Produces: `class BookmarkRequest(BaseModel): imdb_id: Optional[str] = None; title: str; year: Optional[int] = None; media_type: str; bookmarked: bool = True` (mirrors `DismissRequest`'s explicit boolean-flag shape, not an implicit toggle). `POST /results/bookmark` (calls `add_bookmark`/`remove_bookmark` based on `req.bookmarked`). `GET /results/bookmarks` (returns `{"items": [...], "count": N}`, mirrors `GET /results/dismissed`'s shape). Every item dict `_shape_results` processes gains a `bookmarked: bool` key, computed BEFORE `_filter_and_sort` runs (so the new `bookmarked` quick-filter value in Task 4's frontend has something to filter on) via one `db.list_bookmark_keys()` call per request, not a query per item.

- [ ] **Step 1: Write the failing tests**

```python
def test_bookmark_endpoint_adds_and_lists(client):
    resp = client.post("/results/bookmark", json={
        "imdb_id": "tt1234567", "title": "Dune: Part Two", "year": 2024,
        "media_type": "movie", "bookmarked": True,
    })
    assert resp.status_code == 200
    listed = client.get("/results/bookmarks").json()
    assert listed["count"] == 1
    assert listed["items"][0]["imdb_id"] == "tt1234567"


def test_bookmark_endpoint_removes(client):
    client.post("/results/bookmark", json={
        "imdb_id": "tt1234567", "title": "Dune: Part Two", "year": 2024,
        "media_type": "movie", "bookmarked": True,
    })
    resp = client.post("/results/bookmark", json={
        "imdb_id": "tt1234567", "title": "Dune: Part Two", "year": 2024,
        "media_type": "movie", "bookmarked": False,
    })
    assert resp.status_code == 200
    assert client.get("/results/bookmarks").json()["count"] == 0


def test_shape_results_annotates_bookmarked_flag(client, monkeypatch):
    # Adapt this test to however the existing results-route test file mocks
    # _load_cached_items / reg.db for GET /results/cached -- read a
    # neighboring existing test in this file first and match its mocking
    # style exactly rather than inventing a new one.
    ...
```

Write the third test using this file's existing established mocking pattern for `GET /results/cached` (there should already be at least one such test given `_shape_results`/`_filter_and_sort` are already tested per Task 1 of the genre-exclude-filter plan, which landed on `main` before this task started — read one of those existing tests for the exact fixture shape) — assert that an item whose `imdb_id` matches a bookmarked row comes back with `bookmarked: True` in the response, and a non-bookmarked item comes back with `bookmarked: False`.

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_results_routes.py -v -k bookmark` (adjust filename per investigation)
Expected: FAIL — 404 on `/results/bookmark` (route doesn't exist yet).

- [ ] **Step 3: Write the implementation**

In `backend/api/routes/results.py`, add near `DismissRequest` (line 377):

```python
class BookmarkRequest(BaseModel):
    imdb_id: Optional[str] = None
    title: str
    year: Optional[int] = None
    media_type: str
    bookmarked: bool = True
```

Add routes near the existing `/dismiss`/`/dismissed` routes (after line 808's `clear_dismissed`):

```python
@router.post("/bookmark")
def bookmark_item(req: BookmarkRequest, reg: ServiceRegistry = Depends(get_registry)):
    """Bookmark (or un-bookmark) a title, independent of which release the
    user was looking at when they clicked it."""
    db = reg.db
    if db is None:
        raise HTTPException(status_code=503, detail="Database not available")
    if req.bookmarked:
        db.add_bookmark(req.imdb_id, req.title, req.year, req.media_type)
    else:
        db.remove_bookmark(req.imdb_id, req.title, req.year, req.media_type)
    return {"status": "ok", "bookmarked": req.bookmarked}


@router.get("/bookmarks")
def list_bookmarks(reg: ServiceRegistry = Depends(get_registry)):
    db = reg.db
    if db is None:
        raise HTTPException(status_code=503, detail="Database not available")
    items = db.list_bookmarks()
    return {"items": items, "count": len(items)}
```

In `_shape_results` (line 431), find where `visible_items = list(items)` is set (line 516, right before the existing `_filter_and_sort` call at line 518) and add the bulk-annotation immediately before that line:

```python
    bookmark_keys = reg.db.list_bookmark_keys() if reg.db else set()

    def _item_bookmark_key(i):
        imdb = i.get("imdb_id")
        if imdb:
            return ("imdb", imdb)
        media_type = "tv" if i.get("season") is not None else "movie"
        return ("title", normalize_title(str(i.get("title", ""))), i.get("year"), media_type)

    for i in items:
        i["bookmarked"] = _item_bookmark_key(i) in bookmark_keys
```

Add the import at the top of the file: `from backend.app_service import normalize_title`.

Add a `bookmarked` branch to `_filter_and_sort`'s existing `quick` handling (the block with `if "4k" in q:` / `if "hdrdv" in q:` / `if "inplex" in q:`, around line 284-292):

```python
        if "bookmarked" in q:
            result = [i for i in result if i.get("bookmarked")]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_results_routes.py -v -k bookmark` (adjust filename per investigation)
Expected: PASS. Also run the full file to confirm no regression: `pytest tests/test_results_routes.py -v`.

- [ ] **Step 5: Commit**

```bash
git add backend/api/routes/results.py tests/test_results_routes.py
git commit -m "feat(results): bookmark toggle/list endpoints + bookmarked item flag + quick filter"
```

(Adjust the test filename in `git add` to whatever the investigation actually found/used.)

---

### Task 3: Frontend `bookmarkedTitles` store + star toggle (row + detail panel, desktop + mobile)

**Files:**
- Modify: `frontend/src/lib/api/types.ts`
- Modify: `frontend/src/lib/api/client.ts` (confirm exact filename first: `ls frontend/src/lib/api/`)
- Modify: `frontend/src/lib/stores/results.ts`
- Modify: `frontend/src/lib/components/ResultRow.svelte`
- Modify: `frontend/src/lib/components/DetailPanel.svelte`
- Modify: `frontend/src/lib/components/mobile/DetailSheet.svelte`
- Test: `frontend/src/lib/stores/results.test.ts`

**Interfaces:**
- Consumes: `POST /results/bookmark`, `GET /results/bookmarks` (Task 2). `ScanResult.bookmarked: boolean` (new field, matching the backend's per-item annotation from Task 2).
- Produces: `bookmarkedTitles: Writable<Set<string>>` (`frontend/src/lib/stores/results.ts`, mirrors `dismissedUrls`'s shape at line 297 — the Set holds a client-computed identity-key string, e.g. `` `imdb:${imdbId}` `` or `` `title:${normalizeTitle(title)}:${year}:${mediaType}` ``, so a row can check membership without a round-trip). `bookmarkIdentityKey(item: ScanResult): string` (pure helper, exported for testing). `toggleBookmark(item: ScanResult): Promise<boolean>` (optimistic update against `bookmarkedTitles`, calls the API, reverts on failure — mirrors `dismissItem`'s existing optimistic-update-then-revert-on-failure structure at `results.ts:890-926`, read that function first to match its exact error-handling shape).

- [ ] **Step 1: Add the type field**

In `frontend/src/lib/api/types.ts`, add `bookmarked: boolean;` to the `ScanResult` interface (alongside the other computed/server-annotated fields like `is_duplicate_group` around line 30).

- [ ] **Step 2: Write the failing tests**

Add to `frontend/src/lib/stores/results.test.ts` (near the existing dismiss-related tests, to match established structure):

```typescript
import { bookmarkIdentityKey, bookmarkedTitles, toggleBookmark } from './results';

describe('bookmarkIdentityKey', () => {
	it('uses imdb_id when present', () => {
		const key = bookmarkIdentityKey({ imdb_id: 'tt1234567', title: 'Dune', year: 2024, season: null } as any);
		expect(key).toBe('imdb:tt1234567');
	});

	it('falls back to normalized title + year + media type when imdb_id is absent', () => {
		const key = bookmarkIdentityKey({ imdb_id: null, title: 'Some Show!', year: 2020, season: 1 } as any);
		expect(key).toBe('title:some show:2020:tv');
	});

	it('movies (no season) key as media type movie', () => {
		const key = bookmarkIdentityKey({ imdb_id: null, title: 'Some Movie', year: 2020, season: null } as any);
		expect(key).toBe('title:some movie:2020:movie');
	});
});
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `npx vitest run src/lib/stores/results.test.ts -t bookmarkIdentityKey`
Expected: FAIL — `bookmarkIdentityKey` is not exported yet.

- [ ] **Step 4: Write the implementation**

First read `dismissItem` (`frontend/src/lib/stores/results.ts:890-926`) in full to match its exact optimistic-update/revert-on-failure structure, and check `ls frontend/src/lib/api/` plus a couple of existing client methods (e.g. whatever backs `/results/dismiss`) to match the API client's naming/signature convention before adding the two new client methods.

Add near `dismissedUrls` (after line 297):

```typescript
/** Bookmarked titles, keyed the same way the backend does (imdb_id first,
 *  normalized-title+year+media_type fallback) -- mirrors dismissedUrls's
 *  shape/hydration so a row can check membership without a round-trip. */
export const bookmarkedTitles = writable<Set<string>>(new Set());

function normalizeTitleClient(title: string): string {
	return title.toLowerCase().replace(/[^a-z0-9]+/g, ' ').trim();
}

export function bookmarkIdentityKey(item: Pick<ScanResult, 'imdb_id' | 'title' | 'year' | 'season'>): string {
	if (item.imdb_id) return `imdb:${item.imdb_id}`;
	const mediaType = item.season != null ? 'tv' : 'movie';
	return `title:${normalizeTitleClient(item.title)}:${item.year ?? ''}:${mediaType}`;
}

export function toggleBookmark(item: ScanResult): Promise<boolean> {
	const key = bookmarkIdentityKey(item);
	const wasBookmarked = item.bookmarked;
	const nextBookmarked = !wasBookmarked;
	bookmarkedTitles.update((s) => {
		const next = new Set(s);
		if (nextBookmarked) next.add(key);
		else next.delete(key);
		return next;
	});
	const mediaType = item.season != null ? 'tv' : 'movie';
	return api
		.setBookmark(item.imdb_id, item.title, item.year, mediaType, nextBookmarked)
		.then(() => true)
		.catch((e) => {
			bookmarkedTitles.update((s) => {
				const next = new Set(s);
				if (wasBookmarked) next.add(key);
				else next.delete(key);
				return next;
			});
			addToast('Error', e instanceof Error ? e.message : 'Failed to update bookmark', 'error');
			return false;
		});
}

/** Hydrate bookmarkedTitles from the server on app load -- mirrors however
 *  dismissedUrls is hydrated (search for its own hydration call site, likely
 *  near app init, and add an equivalent call there rather than inventing a
 *  new init hook). */
export async function hydrateBookmarks(): Promise<void> {
	const { items } = await api.getBookmarks();
	const keys = new Set(
		items.map((b: { imdb_id: string | null; title: string; year: number | null; media_type: string }) =>
			b.imdb_id ? `imdb:${b.imdb_id}` : `title:${normalizeTitleClient(b.title)}:${b.year ?? ''}:${b.media_type}`
		)
	);
	bookmarkedTitles.set(keys);
}
```

Add the two client methods to the API client file found via `ls frontend/src/lib/api/`, matching its existing naming/signature convention:
- `setBookmark(imdbId, title, year, mediaType, bookmarked)` -> `POST /results/bookmark`
- `getBookmarks()` -> `GET /results/bookmarks`, returns `{items, count}`

Find wherever `dismissedUrls` is hydrated on app load (grep `dismissedUrls.set` outside of `dismissItem`/`restoreItem` — likely a mount/init function) and add a call to `hydrateBookmarks()` alongside it.

Add a star button to `ResultRow.svelte` (near the existing badges — read the row's existing badge/icon layout first to match spacing) and to `DetailPanel.svelte`/`DetailSheet.svelte`'s action-button row (same placement pattern as the existing Rescan button, read `rescanItem`'s button markup in both files first to match style exactly):

```svelte
<button
	type="button"
	onclick={(e) => { e.stopPropagation?.(); toggleBookmark(item); }}
	aria-label={item.bookmarked ? 'Remove bookmark' : 'Bookmark this title'}
	title={item.bookmarked ? 'Remove bookmark' : 'Bookmark this title'}
	class="{item.bookmarked ? 'text-[var(--accent)]' : 'text-[var(--text-secondary)] hover:text-[var(--text-primary)]'}"
>
	{item.bookmarked ? '★' : '☆'}
</button>
```

(`e.stopPropagation?.()` only matters in `ResultRow.svelte`, where the row itself has a click handler — omit it in `DetailPanel.svelte`/`DetailSheet.svelte` where there is no such conflict, matching whatever the existing Rescan button in each of those two files already does or doesn't do.)

- [ ] **Step 5: Run tests to verify they pass**

Run:
```bash
cd frontend
npx vitest run src/lib/stores/results.test.ts
npm run check
npm run build
npx vitest run
```
Expected: all green, no regressions.

- [ ] **Step 6: Commit**

```bash
git add frontend/src/lib/api/types.ts frontend/src/lib/api/client.ts frontend/src/lib/stores/results.ts \
        frontend/src/lib/stores/results.test.ts frontend/src/lib/components/ResultRow.svelte \
        frontend/src/lib/components/DetailPanel.svelte frontend/src/lib/components/mobile/DetailSheet.svelte
git commit -m "feat(results): bookmarkedTitles store + star toggle (row, detail panel, desktop + mobile)"
```

---

### Task 4: "Bookmarked" quick-filter chip + full verification

**Files:**
- Modify: `frontend/src/lib/components/FilterBar.svelte`

**Interfaces:**
- Consumes: `bookmarkedTitles` (Task 3) is NOT directly needed here — the existing `quick` (`quickFilters` store) mechanism already round-trips to the server in paged mode (Task 2's backend `bookmarked` quick-filter branch) and the client-side live-mode filter in `filteredResults` needs a matching `bookmarked` branch too (see Step 2).

- [ ] **Step 1: Add the chip to `quickChips`**

In `frontend/src/lib/components/FilterBar.svelte`, read the current state of the `quickChips` array (line ~40-44 as of the last investigation — re-read the file first, another workflow may have touched nearby lines in this same file this session; this array itself should be undisturbed since the concurrent genre-exclude-filter plan only touches the separate genre-chip blocks) and add one entry:

```javascript
const quickChips = [
  { key: '4k', label: '4K' },
  { key: 'hdrdv', label: 'HDR/DV' },
  { key: 'inplex', label: 'In Plex' },
  { key: 'bookmarked', label: 'Bookmarked' },
];
```

Both existing `{#each quickChips as chip}` render blocks (the compact toolbar and the full panel) pick this up automatically — no further markup changes needed in this file.

- [ ] **Step 2: Add the client-side live-mode filter branch**

In `frontend/src/lib/stores/results.ts`, find the `filteredResults` derived store's existing quick-filter handling (the `if ($quick.includes('inplex')) items = items.filter(hasPlexCopy);` line, found in the earlier investigation) and add immediately after it:

```typescript
    if ($quick.includes('bookmarked')) {
      items = items.filter((i) => i.bookmarked);
    }
```

- [ ] **Step 3: Verify manually in the browser**

Bookmark a title (star icon on a result row), enable the "Bookmarked" quick filter, confirm only bookmarked items show; toggle both live and paged mode if the app exposes that switch.

- [ ] **Step 4: Run the full verification suite**

```bash
cd frontend
npm run check
npm run build
npx vitest run
```
Backend (throwaway container, confirm no regression from Tasks 1-2):
```bash
pytest tests/test_database.py tests/test_results_routes.py -v
```
(Adjust filenames to whatever earlier tasks' investigations actually found.)
Expected: all green. Grep every file touched across Tasks 1-4 for curly/smart quotes and confirm zero matches.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/lib/components/FilterBar.svelte frontend/src/lib/stores/results.ts
git commit -m "feat(results): Bookmarked quick-filter chip (live + paged mode) + full verification"
```
