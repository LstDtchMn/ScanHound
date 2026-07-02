# Scan Server-Side Pagination + Infinite Scroll — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the Scan browse view page through the *entire* filtered result set (server-side filter/sort + infinite scroll) instead of a one-shot 500-row window, so all missing titles are reachable and the tab badges agree with the list.

**Architecture:** Move filter/search/sort authority into the existing `/results` + `/results/cached` endpoints (they already paginate) via one shared `_filter_and_sort` helper with full client parity + typed sorting. The client turns the browse path into a page accumulator with a mode-aware `filteredResults` (paged = passthrough of server order; live-scan streaming keeps the existing client pipeline) and an infinite-scroll render window.

**Tech Stack:** FastAPI + pytest (backend); SvelteKit 5 runes + Svelte stores + vitest (frontend). Spec: `docs/superpowers/specs/2026-06-30-scan-server-pagination-design.md`.

## Global Constraints

- A server page must equal what the client previously computed in-memory. Filter parity is exact:
  - **category**: `cat = item.category or ('tv' if item.season is not None else '4k')`; item shows when `cat` **not in** `{4k,remux,tv}` **or** `cat` in the enabled set.
  - **genre**: item shows if any of `item.genres` is in the requested list.
  - **language**: item shows if `item.language` is in the requested list.
  - **quick** (AND-combined): `4k`→`resolution=='4K'`; `hdrdv`→`dovi` truthy or (`hdr` truthy and `hdr!='SDR'`); `inplex`→`len(json.loads(plex_versions or '[]'))>0` (fail-safe False).
- **Typed sort** (not string sort): `size`→bytes (TB/GB/MB/KB/B), `posted_date`→timestamp, `year`/`rating`→numeric, `title`→casefold; `order=='desc'` reverses. Sorting is stable.
- Tab `stats` = whole visible set (after dismissal hide, **before** status/search/category/genre/language/quick). `filtered_stats` and `total` = after all filters.
- Response adds `title_counts` = per-title count over the filtered set.
- One shared `_filter_and_sort` helper is reused by `_shape_results` **and** the filter-aware select-all — they must never diverge.
- Page size default `per_page=100`, cap `le=200`.
- Client keeps live-scan streaming as a separate **live mode**; a `pagedMode` flag toggles the `filteredResults` derivation (paged = passthrough; live = existing client filter/sort).
- Infinite scroll = a client render window (`renderLimit`, +100 per step) that also triggers `loadResults()` append in paged mode near the end of loaded rows; **250 ms debounce** on filter changes; **stale-response query-key guard**.
- Dismiss removes the row optimistically in paged mode. `categoryFilter` default `['4k']`→`['4k','remux','tv']`.
- Deck consumes the accumulated `results`; `deckNeedsMore()` triggers `loadResults()` when actionable cards run low and `hasMore`.
- Deploy in-app changes ONLY via `docker compose up -d --build`, and **only when the user asks** — never during the build.

---

### Task 1: Backend `_filter_and_sort` helper (pure filter parity + typed sort)

**Files:**
- Modify: `backend/api/routes/results.py` (add helpers near the top, after imports)
- Test: `tests/test_api_results.py` (create)

**Interfaces:**
- Produces: `_filter_and_sort(items, *, filter=None, search=None, category=None, genre=None, language=None, quick=None, sort='title', order='asc') -> list[dict]`; `_effective_category(item)->str`; `_has_plex_copy(item)->bool`; `_parse_size_to_bytes(str)->float`; `_parse_posted_date(str)->float`; module const `_KNOWN_CATEGORIES = {'4k','remux','tv'}`; module dict `_SORT_KEYS`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_api_results.py`:

```python
"""Tests for server-side results filtering, sorting, pagination."""
from backend.api.routes.results import (
    _filter_and_sort, _effective_category, _has_plex_copy,
    _parse_size_to_bytes, _parse_posted_date,
)


def _it(**kw):
    base = dict(title="A", status="missing", category=None, season=None,
                genres=[], language="English", resolution="1080p", hdr="",
                dovi=False, plex_versions="[]", year=2020, rating=5.0,
                size="4.5 GB", posted_date="June 8, 2026 at 12:56 AM",
                group_key="a-2020")
    base.update(kw)
    return base


def test_effective_category_rules():
    assert _effective_category(_it(category="remux")) == "remux"
    assert _effective_category(_it(category=None, season=2)) == "tv"
    assert _effective_category(_it(category=None, season=None)) == "4k"


def test_category_filter_shows_enabled_and_unknown():
    items = [_it(title="M", category="remux"), _it(title="T", season=1),
             _it(title="S", category="search")]
    out = _filter_and_sort(items, category=["4k"])
    titles = {i["title"] for i in out}
    assert titles == {"S"}  # remux+tv hidden; unknown 'search' always shows


def test_quick_inplex_and_hdrdv():
    inplex = _it(title="P", plex_versions='[{"v":1}]')
    dv = _it(title="D", dovi=True)
    plain = _it(title="X")
    assert {i["title"] for i in _filter_and_sort([inplex, dv, plain], quick=["inplex"])} == {"P"}
    assert {i["title"] for i in _filter_and_sort([inplex, dv, plain], quick=["hdrdv"])} == {"D"}


def test_typed_sort_size_and_posted():
    a = _it(title="A", size="9 GB", posted_date="June 8, 2026 at 12:00 AM")
    b = _it(title="B", size="10 GB", posted_date="July 3, 2026 at 12:00 AM")
    by_size = _filter_and_sort([a, b], sort="size", order="desc")
    assert [i["title"] for i in by_size] == ["B", "A"]  # 10GB > 9GB (not lexical)
    by_posted = _filter_and_sort([a, b], sort="posted_date", order="desc")
    assert [i["title"] for i in by_posted] == ["B", "A"]  # July after June
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_api_results.py -v`
Expected: FAIL with `ImportError: cannot import name '_filter_and_sort'`.

- [ ] **Step 3: Write minimal implementation**

In `backend/api/routes/results.py`, after the existing imports add `import re` and `from datetime import datetime` (only if not present), then:

```python
_KNOWN_CATEGORIES = {"4k", "remux", "tv"}


def _effective_category(item: Dict[str, Any]) -> str:
    cat = item.get("category")
    if cat:
        return cat
    return "tv" if item.get("season") is not None else "4k"


def _has_plex_copy(item: Dict[str, Any]) -> bool:
    try:
        return len(json.loads(item.get("plex_versions") or "[]")) > 0
    except (ValueError, TypeError):
        return False


def _parse_size_to_bytes(size: str) -> float:
    if not size:
        return 0.0
    m = re.search(r"([\d.]+)\s*(TB|GB|MB|KB|B)", size, re.IGNORECASE)
    if not m:
        return 0.0
    mult = {"B": 1, "KB": 1024, "MB": 1024 ** 2, "GB": 1024 ** 3, "TB": 1024 ** 4}
    return float(m.group(1)) * mult.get(m.group(2).upper(), 0)


def _parse_posted_date(s: str) -> float:
    if not s:
        return 0.0
    txt = s.replace(" at ", " ").strip()
    for fmt in ("%B %d, %Y %I:%M %p", "%B %d, %Y"):
        try:
            return datetime.strptime(txt, fmt).timestamp()
        except ValueError:
            continue
    return 0.0


_SORT_KEYS = {
    "title": lambda i: str(i.get("title", "")).casefold(),
    "year": lambda i: float(i.get("year") or 0),
    "rating": lambda i: float(i.get("rating") or 0),
    "size": lambda i: _parse_size_to_bytes(i.get("size", "") or ""),
    "posted_date": lambda i: _parse_posted_date(i.get("posted_date", "") or ""),
}


def _filter_and_sort(items, *, filter=None, search=None, category=None,
                     genre=None, language=None, quick=None,
                     sort="title", order="asc"):
    result = list(items)
    if filter:
        fl = filter.lower()
        result = [i for i in result if fl in str(i.get("status", "")).lower()]
    if search:
        sl = search.lower()
        result = [i for i in result if sl in str(i.get("title", "")).lower()]
    if category:
        enabled = set(category)
        result = [i for i in result
                  if _effective_category(i) not in _KNOWN_CATEGORIES
                  or _effective_category(i) in enabled]
    if genre:
        gset = set(genre)
        result = [i for i in result if any(g in gset for g in (i.get("genres") or []))]
    if language:
        lset = set(language)
        result = [i for i in result if i.get("language") in lset]
    if quick:
        q = set(quick)
        if "4k" in q:
            result = [i for i in result if i.get("resolution") == "4K"]
        if "hdrdv" in q:
            result = [i for i in result
                      if i.get("dovi") or (i.get("hdr") and i.get("hdr") != "SDR")]
        if "inplex" in q:
            result = [i for i in result if _has_plex_copy(i)]
    keyfn = _SORT_KEYS.get(sort)
    if keyfn:
        result = sorted(result, key=keyfn, reverse=(order == "desc"))
    return result
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_api_results.py -v`
Expected: PASS (5 tests).

- [ ] **Step 5: Commit**

```bash
git add backend/api/routes/results.py tests/test_api_results.py
git commit -m "results: shared _filter_and_sort helper with filter parity + typed sort"
```

---

### Task 2: Wire `_filter_and_sort` + `_load_items` into `_shape_results` and both GET endpoints (+ `title_counts`, per_page defaults)

**Files:**
- Modify: `backend/api/routes/results.py` (`_shape_results`, `get_results`, `get_cached_results`; add `_load_items`)
- Test: `tests/test_api_results.py`

**Interfaces:**
- Consumes: `_filter_and_sort` (Task 1).
- Produces: `_load_items(source: str, reg) -> tuple[list[dict], Optional[str]]` (`source` in `{"live","cache"}`, returns items + `last_updated`); `_shape_results(..., category=None, genre=None, language=None, quick=None)` now also returns `title_counts`; endpoints accept `category/genre/language/quick` Query params and `per_page` default 100 `le=200`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_api_results.py`:

```python
import json
from unittest.mock import MagicMock
from fastapi.testclient import TestClient
from backend.api.main import create_app
from backend.api.dependencies import registry


def _row(title, status="missing", category="4k", **kw):
    data = dict(title=title, status=status, category=category, url=f"u/{title}",
                group_key=f"{title}-k", season=None, genres=[], language="English",
                resolution="4K", hdr="", dovi=False, plex_versions="[]",
                year=2020, rating=5.0, size="4.5 GB",
                posted_date="June 8, 2026 at 12:56 AM")
    data.update(kw)
    return {"url": data["url"], "data": json.dumps(data), "last_seen_at": "2026-06-30T00:00:00"}


def _client_with_cache(rows):
    registry.db = MagicMock()
    registry.db.get_background_cache.return_value = rows
    registry.db.get_dismissed_urls.return_value = set()
    return TestClient(create_app())


def test_cached_stats_whole_set_but_filtered_narrows():
    rows = [_row("A", status="missing"), _row("B", status="in_library"),
            _row("C", status="missing")]
    c = _client_with_cache(rows)
    r = c.get("/results/cached", params={"filter": "missing", "per_page": 100}).json()
    assert r["stats"]["total"] == 3          # whole visible set
    assert r["stats"]["missing"] == 2
    assert r["total"] == 2                    # after status filter
    assert {i["title"] for i in r["items"]} == {"A", "C"}


def test_cached_pages_are_disjoint_and_cover_full_set():
    rows = [_row(f"T{n:03d}") for n in range(250)]
    c = _client_with_cache(rows)
    seen = []
    for page in (1, 2, 3):
        r = c.get("/results/cached", params={"per_page": 100, "page": page,
                                             "sort": "title", "order": "asc"}).json()
        seen.extend(i["title"] for i in r["items"])
        assert r["total"] == 250
    assert len(seen) == 250 and len(set(seen)) == 250


def test_cached_title_counts_sum_to_total():
    rows = [_row("Dup"), _row("Dup", url="u/dup2", group_key="Dup-k2"), _row("Solo")]
    c = _client_with_cache(rows)
    r = c.get("/results/cached", params={"per_page": 100}).json()
    assert r["title_counts"]["Dup"] == 2
    assert sum(r["title_counts"].values()) == r["total"]


def test_cached_category_query_param_filters():
    rows = [_row("K", category="4k"), _row("R", category="remux")]
    c = _client_with_cache(rows)
    r = c.get("/results/cached", params={"category": "4k", "per_page": 100}).json()
    assert {i["title"] for i in r["items"]} == {"K"}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_api_results.py -k "cached" -v`
Expected: FAIL (`KeyError: 'title_counts'` and/or category param ignored).

- [ ] **Step 3: Write minimal implementation**

In `backend/api/routes/results.py`:

(a) Add the source loader (place above `get_results`):

```python
def _load_items(source: str, reg: ServiceRegistry):
    """Return (item_dicts, last_updated) for the live last-scan set or the
    pre-cached background-scan rows."""
    if source == "cache":
        items: List[Dict[str, Any]] = []
        last_updated: Optional[str] = None
        if reg.db is not None:
            for row in reg.db.get_background_cache():
                try:
                    data = json.loads(row.get("data") or "{}")
                except (ValueError, TypeError):
                    data = {}
                if not data.get("url"):
                    data["url"] = row.get("url")
                items.append(data)
                seen = row.get("last_seen_at")
                if seen and (last_updated is None or seen > last_updated):
                    last_updated = seen
        return items, last_updated
    return [_media_item_to_dict(i) for i in get_last_scan_items()], None
```

(b) Change `_shape_results` signature and its filter block. Replace the status-filter + search + sort section (the `if filter:` / `if search:` / `items.sort(...)` block) with a single `_filter_and_sort` call and add `title_counts`:

```python
def _shape_results(
    items: List[Dict[str, Any]],
    *,
    filter: Optional[str],
    search: Optional[str],
    sort: str,
    order: str,
    page: int,
    per_page: int,
    include_dismissed: bool,
    reg: ServiceRegistry,
    category: Optional[List[str]] = None,
    genre: Optional[List[str]] = None,
    language: Optional[List[str]] = None,
    quick: Optional[List[str]] = None,
    extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    if not include_dismissed and reg.db is not None:
        dismissed = reg.db.get_dismissed_urls()
        if dismissed:
            items = [i for i in items if i.get("url") not in dismissed]

    visible_items = list(items)  # whole-set stats, before filters

    items = _filter_and_sort(
        items, filter=filter, search=search, category=category, genre=genre,
        language=language, quick=quick, sort=sort, order=order,
    )

    title_counts: Dict[str, int] = {}
    for i in items:
        t = str(i.get("title", ""))
        title_counts[t] = title_counts.get(t, 0) + 1

    total = len(items)
    start = (page - 1) * per_page
    page_items = items[start:start + per_page]

    with _selected_lock:
        selected_snapshot = set(_selected)
    for item in page_items:
        item["selected"] = item.get("group_key", "") in selected_snapshot

    response = {
        "items": page_items,
        "total": total,
        "page": page,
        "per_page": per_page,
        "stats": _compute_status_counts(visible_items),
        "filtered_stats": _compute_status_counts(items),
        "title_counts": title_counts,
    }
    if extra:
        response.update(extra)
    return response
```

(c) Add a comma-split helper and update both endpoints. Add near the top:

```python
def _csv(param: Optional[str]) -> Optional[List[str]]:
    if not param:
        return None
    vals = [p.strip() for p in param.split(",") if p.strip()]
    return vals or None
```

Update `get_results`:

```python
@router.get("")
def get_results(
    filter: Optional[str] = Query(None),
    search: Optional[str] = Query(None),
    sort: str = Query("title"),
    order: str = Query("asc"),
    page: int = Query(1, ge=1),
    per_page: int = Query(100, ge=1, le=200),
    category: Optional[str] = Query(None),
    genre: Optional[str] = Query(None),
    language: Optional[str] = Query(None),
    quick: Optional[str] = Query(None),
    include_dismissed: bool = Query(False),
    reg: ServiceRegistry = Depends(get_registry),
):
    items, _ = _load_items("live", reg)
    return _shape_results(
        items, filter=filter, search=search, sort=sort, order=order,
        page=page, per_page=per_page, include_dismissed=include_dismissed, reg=reg,
        category=_csv(category), genre=_csv(genre), language=_csv(language),
        quick=_csv(quick),
    )
```

Update `get_cached_results` the same way, using `_load_items("cache", reg)` and the `extra={"source": "cache", "last_updated": last_updated}`:

```python
@router.get("/cached")
def get_cached_results(
    filter: Optional[str] = Query(None),
    search: Optional[str] = Query(None),
    sort: str = Query("title"),
    order: str = Query("asc"),
    page: int = Query(1, ge=1),
    per_page: int = Query(100, ge=1, le=200),
    category: Optional[str] = Query(None),
    genre: Optional[str] = Query(None),
    language: Optional[str] = Query(None),
    quick: Optional[str] = Query(None),
    include_dismissed: bool = Query(False),
    reg: ServiceRegistry = Depends(get_registry),
):
    items, last_updated = _load_items("cache", reg)
    return _shape_results(
        items, filter=filter, search=search, sort=sort, order=order,
        page=page, per_page=per_page, include_dismissed=include_dismissed, reg=reg,
        category=_csv(category), genre=_csv(genre), language=_csv(language),
        quick=_csv(quick),
        extra={"source": "cache", "last_updated": last_updated},
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_api_results.py -v`
Expected: PASS (all Task 1 + Task 2 tests). Also run the existing suite for regressions:
Run: `python -m pytest tests/test_api_routes.py -v`
Expected: PASS (existing results tests still green).

- [ ] **Step 5: Commit**

```bash
git add backend/api/routes/results.py tests/test_api_results.py
git commit -m "results: server-side filter/sort params + title_counts + shared _load_items"
```

---

### Task 3: Filter-aware select-all

**Files:**
- Modify: `backend/api/routes/results.py` (`select_all` endpoint + request model)
- Test: `tests/test_api_results.py`

**Interfaces:**
- Consumes: `_filter_and_sort`, `_load_items` (Tasks 1-2).
- Produces: `POST /results/select-all` accepting an optional body `SelectAllRequest{source, filter, search, category, genre, language, quick}` → `{status, selected_count, group_keys}`. Absent body = legacy "select all last-scan items".

- [ ] **Step 1: Write the failing test**

Append to `tests/test_api_results.py`:

```python
def test_select_all_filtered_returns_matching_group_keys():
    rows = [_row("A", status="missing", category="4k"),
            _row("B", status="in_library", category="4k"),
            _row("C", status="missing", category="remux")]
    c = _client_with_cache(rows)
    r = c.post("/results/select-all",
               json={"source": "cache", "filter": "missing", "category": "4k"}).json()
    assert r["selected_count"] == 1
    assert r["group_keys"] == ["A-k"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_api_results.py -k select_all -v`
Expected: FAIL (endpoint ignores body / returns no `group_keys`).

- [ ] **Step 3: Write minimal implementation**

Add the request model near the other models and rewrite `select_all`:

```python
class SelectAllRequest(BaseModel):
    source: str = "live"
    filter: Optional[str] = None
    search: Optional[str] = None
    category: Optional[str] = None
    genre: Optional[str] = None
    language: Optional[str] = None
    quick: Optional[str] = None


@router.post("/select-all")
def select_all(req: Optional[SelectAllRequest] = None,
               reg: ServiceRegistry = Depends(get_registry)):
    if req is None:
        raw_items = get_last_scan_items()
        with _selected_lock:
            for item in raw_items:
                gk = getattr(item, "group_key", None) or (
                    item.get("group_key") if isinstance(item, dict) else None)
                if gk:
                    _selected.add(gk)
            return {"status": "ok", "selected_count": len(_selected),
                    "group_keys": sorted(_selected)}
    items, _ = _load_items("cache" if req.source == "cache" else "live", reg)
    matched = _filter_and_sort(
        items, filter=req.filter, search=req.search, category=_csv(req.category),
        genre=_csv(req.genre), language=_csv(req.language), quick=_csv(req.quick),
    )
    keys = [str(i.get("group_key")) for i in matched if i.get("group_key")]
    with _selected_lock:
        _selected.clear()
        _selected.update(keys)
        return {"status": "ok", "selected_count": len(_selected), "group_keys": keys}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_api_results.py -v`
Expected: PASS (all backend tests).

- [ ] **Step 5: Commit**

```bash
git add backend/api/routes/results.py tests/test_api_results.py
git commit -m "results: filter-aware select-all returns matching group_keys"
```

---

### Task 4: Client store — paged state + `loadResults` + mode-aware `filteredResults`

**Files:**
- Modify: `frontend/src/lib/stores/results.ts`
- Test: `frontend/src/lib/stores/results.test.ts`

**Interfaces:**
- Consumes: `api.getCachedResults(params)` returning `{items, total, stats, title_counts, source?, last_updated?}`.
- Produces: exported stores `pagedMode` (writable bool, default true), `hasMore`, `loadingMore`, `loadError` (bool), `filteredTotal` (number), `titleCounts` (`Record<string,number>`); `loadResults(reset: boolean): Promise<void>`; `filteredResults` becomes mode-aware (paged → passthrough of `results`).

- [ ] **Step 1: Write the failing test**

Append to `frontend/src/lib/stores/results.test.ts` (add `getCachedResults` to the `vi.mock` api object, and import the new symbols). New tests:

```typescript
it('loadResults(true) replaces results and sets paged totals', async () => {
  const { loadResults, results, filteredTotal, hasMore, pagedMode } =
    await import('./results');
  (api.getCachedResults as any).mockResolvedValueOnce({
    items: [item({ title: 'A', url: 'a' }), item({ title: 'B', url: 'b' })],
    total: 5, stats: { total: 5, missing: 5, upgrade: 0, library: 0 },
    title_counts: { A: 1, B: 1 }, source: 'cache'
  });
  pagedMode.set(true);
  await loadResults(true);
  expect(get(results).length).toBe(2);
  expect(get(filteredTotal)).toBe(5);
  expect(get(hasMore)).toBe(true);
});

it('loadResults(false) appends the next page and flips hasMore off', async () => {
  const { loadResults, results, hasMore, pagedMode } = await import('./results');
  pagedMode.set(true);
  (api.getCachedResults as any).mockResolvedValueOnce({
    items: [item({ title: 'A', url: 'a' })], total: 2,
    stats: { total: 2, missing: 2, upgrade: 0, library: 0 }, title_counts: { A: 1 }
  });
  await loadResults(true);
  (api.getCachedResults as any).mockResolvedValueOnce({
    items: [item({ title: 'B', url: 'b' })], total: 2,
    stats: { total: 2, missing: 2, upgrade: 0, library: 0 }, title_counts: { B: 1 }
  });
  await loadResults(false);
  expect(get(results).map(r => r.title)).toEqual(['A', 'B']);
  expect(get(hasMore)).toBe(false);
});

it('paged filteredResults passes results through untouched', async () => {
  const { results, filteredResults, pagedMode, statusFilter } = await import('./results');
  pagedMode.set(true);
  statusFilter.set('missing');
  results.set([item({ title: 'Z', status: 'in_library', url: 'z' })]);
  // paged mode must NOT re-apply the status filter client-side
  expect(get(filteredResults).map(r => r.title)).toEqual(['Z']);
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd frontend && npx vitest run src/lib/stores/results.test.ts`
Expected: FAIL (`loadResults`/`pagedMode` undefined).

- [ ] **Step 3: Write minimal implementation**

In `frontend/src/lib/stores/results.ts`:

(a) Add `import { get } from 'svelte/store';` if not present. Add the new stores near `fromCache`:

```typescript
export const pagedMode = writable<boolean>(true);
export const hasMore = writable<boolean>(false);
export const loadingMore = writable<boolean>(false);
export const loadError = writable<boolean>(false);
export const filteredTotal = writable<number>(0);
export const titleCounts = writable<Record<string, number>>({});

const SORT_PARAM: Record<SortOption, { sort: string; order: string }> = {
  'title-asc': { sort: 'title', order: 'asc' },
  'title-desc': { sort: 'title', order: 'desc' },
  'year-desc': { sort: 'year', order: 'desc' },
  'year-asc': { sort: 'year', order: 'asc' },
  'size-desc': { sort: 'size', order: 'desc' },
  'size-asc': { sort: 'size', order: 'asc' },
  'rating-desc': { sort: 'rating', order: 'desc' },
  'rating-asc': { sort: 'rating', order: 'asc' },
  'posted-desc': { sort: 'posted_date', order: 'desc' },
  'posted-asc': { sort: 'posted_date', order: 'asc' }
};

let currentPage = 0;
let currentQueryKey = '';

function filterQueryKey(): string {
  return JSON.stringify([
    get(statusFilter), get(searchFilter), get(genreFilter), get(languageFilter),
    get(quickFilters), get(categoryFilter), get(sortBy)
  ]);
}

function buildResultParams(page: number): Record<string, string> {
  const p: Record<string, string> = { page: String(page), per_page: '100' };
  const s = get(statusFilter); if (s !== 'all') p.filter = s;
  const q = get(searchFilter); if (q) p.search = q;
  const cats = get(categoryFilter); if (cats.length) p.category = cats.join(',');
  const g = get(genreFilter); if (g.length) p.genre = g.join(',');
  const l = get(languageFilter); if (l.length) p.language = l.join(',');
  const qf = get(quickFilters); if (qf.length) p.quick = qf.join(',');
  const so = SORT_PARAM[get(sortBy)]; p.sort = so.sort; p.order = so.order;
  return p;
}

export async function loadResults(reset: boolean): Promise<void> {
  if (!get(pagedMode)) return;
  if (get(loadingMore)) return;
  const key = filterQueryKey();
  if (!reset && key !== currentQueryKey) return; // stale append
  const page = reset ? 1 : currentPage + 1;
  loadingMore.set(true);
  loadError.set(false);
  try {
    const data = await api.getCachedResults(buildResultParams(page));
    if (filterQueryKey() !== key) return; // superseded while awaiting — discard
    const items = (data.items ?? []) as ScanResult[];
    if (reset) { results.set(items); currentPage = 1; currentQueryKey = key; }
    else { results.update((r) => [...r, ...items]); currentPage = page; }
    filteredTotal.set(data.total ?? items.length);
    titleCounts.set(data.title_counts ?? {});
    if (data.stats) stats.set(data.stats);
    hasMore.set(get(results).length < (data.total ?? 0));
    if ((data as { source?: string }).source === 'cache') {
      cacheUpdatedAt.set((data as { last_updated?: string }).last_updated ?? null);
      fromCache.set(true);
    }
  } catch {
    loadError.set(true);
  } finally {
    loadingMore.set(false);
  }
}
```

(b) Make `filteredResults` mode-aware. Add `pagedMode` to its dependency list and short-circuit:

```typescript
export const filteredResults = derived(
  [results, statusFilter, searchFilter, genreFilter, languageFilter, sortBy, quickFilters, dismissedUrls, categoryFilter, pagedMode],
  ([$results, $filter, $search, $genre, $language, $sort, $quick, $dismissed, $category, $paged]) => {
    if ($paged) return $results; // server already filtered + sorted
    let items = $results;
    // ...existing client pipeline unchanged...
    return items;
  }
);
```

(Keep the entire existing body after the `if ($paged)` guard.)

- [ ] **Step 4: Run test to verify it passes**

Run: `cd frontend && npx vitest run src/lib/stores/results.test.ts`
Expected: PASS (existing + 3 new tests).

- [ ] **Step 5: Commit**

```bash
git add frontend/src/lib/stores/results.ts frontend/src/lib/stores/results.test.ts
git commit -m "store: paged loadResults + mode-aware filteredResults"
```

---

### Task 5: Client store — debounced refetch, category default, optimistic dismiss, filter-aware selectAll (+ client payload)

**Files:**
- Modify: `frontend/src/lib/stores/results.ts`, `frontend/src/lib/api/client.ts`
- Test: `frontend/src/lib/stores/results.test.ts`

**Interfaces:**
- Consumes: `loadResults` (Task 4).
- Produces: filter-change subscription (250 ms debounce → `loadResults(true)` in paged mode); `categoryFilter` default `['4k','remux','tv']`; `dismissItem` removes the row from `results` in paged mode; `selectAll()` posts the filter payload and sets `selectedKeys` from returned `group_keys`; `api.selectAll(payload?)` sends a JSON body.

- [ ] **Step 1: Write the failing test**

Append to `frontend/src/lib/stores/results.test.ts`:

```typescript
it('selectAll sets selectedKeys from returned group_keys', async () => {
  const { selectAll, selectedKeys } = await import('./results');
  (api.selectAll as any).mockResolvedValueOnce({ selected_count: 2, group_keys: ['x-k', 'y-k'] });
  await selectAll();
  expect([...get(selectedKeys)].sort()).toEqual(['x-k', 'y-k']);
});

it('dismissItem removes the row from results in paged mode', async () => {
  const { results, dismissItem, pagedMode } = await import('./results');
  pagedMode.set(true);
  results.set([item({ title: 'A', url: 'keep' }), item({ title: 'B', url: 'drop' })]);
  await dismissItem('drop', 'B');
  expect(get(results).map(r => r.url)).toEqual(['keep']);
});

it('categoryFilter defaults to all three categories', async () => {
  // Fresh module import with no persisted value returns the new default.
  const mod = await import('./results');
  expect(get(mod.categoryFilter).sort()).toEqual(['4k', 'remux', 'tv']);
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd frontend && npx vitest run src/lib/stores/results.test.ts`
Expected: FAIL (selectAll doesn't set keys; dismiss doesn't remove; default is `['4k']`).

- [ ] **Step 3: Write minimal implementation**

(a) `results.ts` line 107 — change the default:

```typescript
export const categoryFilter = persisted<string[]>('sh-category-filter', ['4k', 'remux', 'tv']);
```

(b) Rewrite `selectAll` (replace the existing `export async function selectAll`):

```typescript
export async function selectAll() {
  const payload: Record<string, string> = {
    source: get(fromCache) ? 'cache' : 'live'
  };
  const s = get(statusFilter); if (s !== 'all') payload.filter = s;
  const q = get(searchFilter); if (q) payload.search = q;
  const cats = get(categoryFilter); if (cats.length) payload.category = cats.join(',');
  const g = get(genreFilter); if (g.length) payload.genre = g.join(',');
  const l = get(languageFilter); if (l.length) payload.language = l.join(',');
  const qf = get(quickFilters); if (qf.length) payload.quick = qf.join(',');
  try {
    const res = await api.selectAll(payload) as { group_keys?: string[] };
    if (res?.group_keys) selectedKeys.set(new Set(res.group_keys));
  } catch {
    selectedKeys.set(new Set(get(results).map((i) => i.url)));
  }
}
```

(c) In `dismissItem`, after the optimistic `dismissedUrls` add, also drop the row in paged mode. Insert right after the `dismissedUrls.update(...)` that adds the url:

```typescript
  if (get(pagedMode)) {
    results.update((items) => items.filter((i) => i.url !== url));
  }
```

(d) Add the debounced subscription at the end of the file:

```typescript
const _filterKey = derived(
  [statusFilter, searchFilter, genreFilter, languageFilter, quickFilters, categoryFilter, sortBy],
  (vals) => JSON.stringify(vals)
);
let _filterDebounce: ReturnType<typeof setTimeout> | undefined;
let _filterKeyPrimed = false;
_filterKey.subscribe(() => {
  if (!_filterKeyPrimed) { _filterKeyPrimed = true; return; } // skip initial fire
  if (!get(pagedMode)) return;
  clearTimeout(_filterDebounce);
  _filterDebounce = setTimeout(() => loadResults(true), 250);
});
```

(e) `client.ts` — change `selectAll`:

```typescript
  selectAll: (payload?: Record<string, string>) =>
    request('/results/select-all', {
      method: 'POST',
      body: payload ? JSON.stringify(payload) : undefined
    }),
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd frontend && npx vitest run src/lib/stores/results.test.ts`
Expected: PASS. Also `cd frontend && npm run check` → no new type errors.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/lib/stores/results.ts frontend/src/lib/api/client.ts frontend/src/lib/stores/results.test.ts
git commit -m "store: debounced refetch, all-category default, optimistic dismiss, filter-aware selectAll"
```

---

### Task 6: Page — infinite-scroll render window + sentinel, remove Prev/Next pager, loading/retry footer, select-all + siblingCounts wiring

**Files:**
- Modify: `frontend/src/routes/+page.svelte`

**Interfaces:**
- Consumes: `loadResults`, `hasMore`, `loadingMore`, `loadError`, `filteredTotal`, `titleCounts`, `pagedMode` (Tasks 4-5).

- [ ] **Step 1: Update the imports and onMount**

Add the new stores to the results import (line 11) and replace the `hydrateCache()` fallback (line 308) with `await loadResults(true)`. Import `loadResults, hasMore, loadingMore, loadError, filteredTotal, titleCounts, pagedMode`. In the live-results branch of onMount (after `results.set(resp.items)`), add `pagedMode.set(false);` so a live last-scan set uses client filtering.

- [ ] **Step 2: Replace the client pagination state with a render window**

Remove `currentPage`, `perPage`, `totalPages`, `paginatedResults` (lines 94-95, 112-115). Add:

```svelte
  let renderLimit = $state(100);
  let scrollSentinel: HTMLDivElement | undefined = $state();
  let renderedResults = $derived($filteredResults.slice(0, renderLimit));
```

Point `groupedResults` at `renderedResults` (replace `for (const item of paginatedResults)` with `for (const item of renderedResults)`).

Update `siblingCounts` to prefer server counts in paged mode:

```svelte
  let siblingCounts = $derived(() => {
    if ($pagedMode && Object.keys($titleCounts).length) {
      return new Map(Object.entries($titleCounts));
    }
    const counts = new Map<string, number>();
    for (const item of $filteredResults) counts.set(item.title, (counts.get(item.title) || 0) + 1);
    return counts;
  });
```

- [ ] **Step 3: Reset the window on filter change and observe the sentinel**

Add an effect that resets `renderLimit` when the filtered set identity changes, plus an `IntersectionObserver`:

```svelte
  // Reset the render window whenever the active filter set changes.
  $effect(() => {
    $statusFilter; $searchFilter; $genreFilter; $languageFilter; $quickFilters; $categoryFilter; $sortBy;
    renderLimit = 100;
    resultsContainer?.scrollTo({ top: 0 });
  });

  $effect(() => {
    if (!scrollSentinel) return;
    const io = new IntersectionObserver((entries) => {
      if (!entries[0].isIntersecting) return;
      renderLimit += 100;
      if ($pagedMode && $hasMore && !$loadingMore && renderLimit >= $filteredResults.length - 100) {
        loadResults(false);
      }
    }, { rootMargin: '600px' });
    io.observe(scrollSentinel);
    return () => io.disconnect();
  });
```

- [ ] **Step 4: Replace the Prev/Next pager block with sentinel + footer**

Replace the `{#if totalPages > 1}` block (≈ lines 809-828) with:

```svelte
  <div bind:this={scrollSentinel} class="h-px"></div>
  {#if $loadingMore}
    <div class="py-4 text-center text-sm text-[var(--text-secondary)]">Loading more…</div>
  {:else if $loadError}
    <div class="py-4 text-center text-sm">
      <button class="underline text-[var(--accent)]" onclick={() => loadResults(false)}>Retry loading more</button>
    </div>
  {/if}
  {#if $filteredTotal > 0}
    <div class="py-3 text-center text-xs text-[var(--text-secondary)] opacity-70">
      showing {Math.min(renderedResults.length, $filteredTotal)} of {$filteredTotal}
    </div>
  {/if}
```

Update the empty-state condition (line 663) from `$filteredResults.length === 0` to `$filteredTotal === 0 && $filteredResults.length === 0`.

- [ ] **Step 5: Verify build + types, then commit**

Run: `cd frontend && npm run check && npm run build`
Expected: no type errors; build succeeds.
Manual smoke (dev): the list keeps loading as you scroll; changing a filter refetches from the top; the footer shows "showing X of N".

```bash
git add frontend/src/routes/+page.svelte
git commit -m "scan page: infinite-scroll render window replacing the 500-cap pager"
```

---

### Task 7: Swipe deck — load more as cards run low

**Files:**
- Modify: `frontend/src/lib/stores/results.ts`, `frontend/src/lib/components/SwipeDeck.svelte`
- Test: `frontend/src/lib/stores/results.test.ts`

**Interfaces:**
- Consumes: `hasMore`, `loadingMore`, `pagedMode`, `loadResults` (Tasks 4-5).
- Produces: `deckNeedsMore(remainingActionable: number): boolean`.

- [ ] **Step 1: Write the failing test**

Append to `frontend/src/lib/stores/results.test.ts`:

```typescript
it('deckNeedsMore is true only when paged, has more, and cards run low', async () => {
  const { deckNeedsMore, pagedMode, hasMore, loadingMore } = await import('./results');
  pagedMode.set(true); hasMore.set(true); loadingMore.set(false);
  expect(deckNeedsMore(3)).toBe(true);
  expect(deckNeedsMore(20)).toBe(false);
  hasMore.set(false);
  expect(deckNeedsMore(3)).toBe(false);
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd frontend && npx vitest run src/lib/stores/results.test.ts -t deckNeedsMore`
Expected: FAIL (`deckNeedsMore` undefined).

- [ ] **Step 3: Write minimal implementation**

In `results.ts`:

```typescript
export function deckNeedsMore(remainingActionable: number): boolean {
  return get(pagedMode) && get(hasMore) && !get(loadingMore) && remainingActionable < 8;
}
```

In `SwipeDeck.svelte`, import `deckResults, deckNeedsMore, loadResults` and, wherever the current card index advances (after a swipe commits), add:

```svelte
  $effect(() => {
    if (deckNeedsMore($deckResults.length)) loadResults(false);
  });
```

(Place the effect at the top level of the component; `$deckResults.length` shrinks as cards are consumed/selected, so it re-runs and tops up the pool.)

- [ ] **Step 4: Run test + build**

Run: `cd frontend && npx vitest run src/lib/stores/results.test.ts -t deckNeedsMore`
Expected: PASS.
Run: `cd frontend && npm run check`
Expected: no new type errors.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/lib/stores/results.ts frontend/src/lib/components/SwipeDeck.svelte frontend/src/lib/stores/results.test.ts
git commit -m "deck: top up the swipe pool from server pages as cards run low"
```

---

### Task 8: Acceptance — full suites + manual verification

**Files:** none (verification only).

- [ ] **Step 1: Backend suite**

Run: `python -m pytest tests/test_api_results.py tests/test_api_routes.py -v`
Expected: all PASS (new results tests + existing route tests).

- [ ] **Step 2: Frontend suite + build**

Run: `cd frontend && npx vitest run && npm run check && npm run build`
Expected: unit tests PASS; no type errors; build succeeds.

- [ ] **Step 3: Manual acceptance (dev server, not a deploy)**

Bring up the dev frontend against the running backend and confirm:
- Browse/cached view, Missing tab: scrolling loads past 100 → 500 → until "showing N of N" equals the badge's missing count.
- Switching category / typing search / changing sort refetches from page 1 (window resets to top).
- Tab badges keep reflecting the whole set while the list narrows.
- Select-all after filtering selects the full filtered set (count matches `filtered_stats`).
- On mobile swipe deck, swiping keeps topping up cards.

- [ ] **Step 4: Record completion in the ledger** (no code commit)

Note in `.superpowers/sdd/progress.md` that all tasks are complete and acceptance passed; hand off to the whole-branch review.

---

## Self-Review

**Spec coverage:** filter parity (T1), typed sort (T1), stats-whole-set vs filtered + title_counts + per_page (T2), shared `_filter_and_sort`/`_load_items` (T1/T2), filter-aware select-all (T3), paged state + loadResults + mode-aware filteredResults (T4), debounce + stale-guard + category default + optimistic dismiss + selectAll payload (T4/T5), infinite-scroll render window + remove pager + loading/retry/footer + siblingCounts (T6), deck top-up (T7), acceptance (T8). All spec sections mapped.

**Type consistency:** `_filter_and_sort` keyword params identical in T1/T2/T3; `loadResults(reset)` signature identical T4→T7; store names (`pagedMode`, `hasMore`, `loadingMore`, `loadError`, `filteredTotal`, `titleCounts`) consistent across T4-T7; `SORT_PARAM` keys match `SortOption`; `api.selectAll(payload?)` matches store `selectAll` usage.

**Placeholders:** none — every code step carries full code.
