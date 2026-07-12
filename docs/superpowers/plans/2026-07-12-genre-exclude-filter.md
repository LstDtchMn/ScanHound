# Genre Exclude Filter Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The existing genre filter (include-only) gains an exclude mode — "never show Reality" without having to include every other genre — both server-side (paged mode) and client-side (live mode).

**Architecture:** Backend `_filter_and_sort` (`backend/api/routes/results.py`) gains a `genre_exclude` parameter alongside the existing `genre` (include) one, threaded through `_shape_results`, both GET endpoints' query params, and `SelectAllRequest`. Frontend `genreFilter` (`frontend/src/lib/stores/results.ts`) changes shape from `string[]` to `{include: string[]; exclude: string[]}`; `toggleGenreFilter` becomes a 3-state cycle (neutral -> include -> exclude -> neutral); the client-side live-mode filter in `filteredResults` and the paged-mode URL param builder both gain exclude handling; `FilterBar.svelte`'s two genre-chip blocks gain a 3-state visual indicator.

**Tech Stack:** Python (FastAPI/pydantic), pytest; SvelteKit 5 (runes), vitest.

## Global Constraints

- `genreFilter` is confirmed **session-only, never persisted** to localStorage (unlike `resolutionFilter`, which uses the `persisted()` helper) — there is no stored-value migration to write; a page reload always starts at `{include: [], exclude: []}`. Do not add persistence as part of this plan (out of scope, matches existing behavior).
- An item with no genre data must never be excluded by an exclude rule (it doesn't carry the excluded genre, so it isn't affirmatively excluded) but still fails an include rule when one is active (matches today's existing include behavior for genre-less items — this is a **regression guard**, not new behavior to build).
- Both include and exclude can be active simultaneously; each is evaluated independently (an item must satisfy include AND not match exclude).
- Backend tests: throwaway container pattern (`docker run -d --name <c> --entrypoint sleep scanhound:latest infinity`, `docker cp backend/. tests/. <c>:/app/...`, `pip install -q pytest httpx`, run, `docker rm -f`). Frontend tests: host node (`cd frontend && npm run check && npm run build && npx vitest run`).
- Work directly on `main`. Commit only when genuinely green.
- Smart/curly-quote hazard: plain ASCII quotes only in all new/changed source; grep before committing.

---

### Task 1: Backend `_filter_and_sort` exclude support

**Files:**
- Modify: `backend/api/routes/results.py`
- Test: `tests/test_results_routes.py` (check `ls tests/ | grep results` first; if a differently-named file already covers `_filter_and_sort`, add there instead to avoid duplicate fixtures)

**Interfaces:**
- Produces: `_filter_and_sort(items, *, ..., genre=None, genre_exclude=None, ...)` (new `genre_exclude: Optional[List[str]] = None` kwarg, `backend/api/routes/results.py:230`). `_shape_results(..., genre=None, genre_exclude=None, ...)` (same new kwarg, `backend/api/routes/results.py:431`). Both GET `/results` and GET `/results/cached` gain a `genre_exclude: Optional[str] = Query(None)` param, CSV-parsed the same way as the existing `genre` param. `SelectAllRequest` (line 364) gains `genre_exclude: Optional[str] = None`.

- [ ] **Step 1: Write the failing tests**

First run `ls tests/ | grep -i result` and read whichever existing test file already imports `_filter_and_sort` (there should be one, given this function has existing include-mode tests) to match its exact fixture/import style. Add these tests there:

```python
def test_filter_and_sort_genre_exclude_hides_matching_items():
    items = [
        {"title": "A", "genres": ["Comedy"]},
        {"title": "B", "genres": ["Reality"]},
        {"title": "C", "genres": ["Reality", "Comedy"]},
        {"title": "D", "genres": []},
    ]
    result = _filter_and_sort(items, genre_exclude=["Reality"])
    titles = {i["title"] for i in result}
    assert titles == {"A", "D"}


def test_filter_and_sort_genre_include_and_exclude_combined():
    items = [
        {"title": "A", "genres": ["Comedy"]},
        {"title": "B", "genres": ["Comedy", "Reality"]},
        {"title": "C", "genres": ["Drama"]},
    ]
    result = _filter_and_sort(items, genre=["Comedy"], genre_exclude=["Reality"])
    titles = {i["title"] for i in result}
    assert titles == {"A"}


def test_filter_and_sort_genre_exclude_never_hides_genre_less_items():
    items = [
        {"title": "NoGenres", "genres": []},
        {"title": "NoGenresKey"},
    ]
    result = _filter_and_sort(items, genre_exclude=["Reality"])
    titles = {i["title"] for i in result}
    assert titles == {"NoGenres", "NoGenresKey"}


def test_filter_and_sort_genre_include_only_regression_unchanged():
    """Existing include-only behavior must be byte-identical."""
    items = [
        {"title": "A", "genres": ["Comedy"]},
        {"title": "B", "genres": ["Drama"]},
    ]
    result = _filter_and_sort(items, genre=["Comedy"])
    assert [i["title"] for i in result] == ["A"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_results_routes.py -v -k genre_exclude` (adjust filename to whatever Step 1 found)
Expected: FAIL — `_filter_and_sort()` raises `TypeError: unexpected keyword argument 'genre_exclude'` for the first two tests (the third/fourth pass trivially since they don't use the new kwarg yet, but keep all four — they document the full contract).

- [ ] **Step 3: Write the implementation**

In `backend/api/routes/results.py`, modify `_filter_and_sort`'s signature (line 230) and genre block (lines 272-274):

```python
def _filter_and_sort(items, *, filter=None, search=None, category=None,
                     genre=None, genre_exclude=None, language=None, quick=None, resolution=None,
                     posted_after=None, posted_before=None,
                     sort="title", order="asc"):
```

```python
    if genre:
        gset = set(genre)
        result = [i for i in result if any(g in gset for g in (i.get("genres") or []))]

    if genre_exclude:
        gxset = set(genre_exclude)
        result = [i for i in result if not any(g in gxset for g in (i.get("genres") or []))]
```

Update the docstring's Args block to add:
```
        genre_exclude: list of genres; item must have NONE of these
```

In `_shape_results` (line 431), add `genre_exclude: Optional[List[str]] = None` to the signature next to the existing `genre` param, and pass it through to whatever internal call it makes to `_filter_and_sort` (read the function body between lines 431 and ~600 to find that call site and add `genre_exclude=genre_exclude` to it — mirror exactly how the existing `genre=genre` argument is already passed there).

In `SelectAllRequest` (line 364), add:
```python
    genre_exclude: Optional[str] = None
```

At the two GET endpoint definitions (`/results` around a `genre: Optional[List[str]] = None` FastAPI dependency near line 443 area — read that endpoint's full signature first — and `/results/cached` at line 672), add a matching `genre_exclude: Optional[str] = Query(None)` parameter and pass `genre_exclude=_csv(genre_exclude)` into the `_shape_results(...)` call the same way `genre=_csv(genre)` is already passed (line 693 and the `/results` endpoint's equivalent call).

At the `select_all` handler (line ~728-733), add `genre_exclude=_csv(req.genre_exclude)` to its `_filter_and_sort(...)` call alongside the existing `genre=_csv(req.genre)`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_results_routes.py -v -k genre` (adjust filename per Step 1)
Expected: PASS, all genre-related tests green (new + pre-existing include-only ones). Also run the full file to confirm no regressions: `pytest tests/test_results_routes.py -v`.

- [ ] **Step 5: Commit**

```bash
git add backend/api/routes/results.py tests/test_results_routes.py
git commit -m "feat(results): server-side genre exclude filter alongside existing include filter"
```

(Adjust the test filename in the `git add` to whatever Step 1 actually found/used.)

---

### Task 2: Frontend `genreFilter` store — include/exclude shape + 3-state toggle

**Files:**
- Modify: `frontend/src/lib/stores/results.ts`
- Modify: `frontend/src/lib/stores/results.test.ts`

**Interfaces:**
- Produces: `genreFilter: Writable<{include: string[]; exclude: string[]}>` (was `Writable<string[]>`, `frontend/src/lib/stores/results.ts:40`). `toggleGenreFilter(genre: string): void` — cycles a single genre through neutral -> include -> exclude -> neutral (removing it from whichever list currently holds it before adding it to the next state; a genre-less/neutral call with the genre in neither list moves it to include).
- Consumes (must update in the same task, or a type error blocks build): the `filteredResults` derived store's genre block (`results.ts:679-681`), `buildResultParams`'s genre param builder (`results.ts:535`), `filterQueryKey`'s genre reference (`results.ts:525` — no code change needed there, `get(genreFilter)` still returns the store's value whatever its shape, `JSON.stringify` handles the object fine), and any place in `results.test.ts` that calls `genreFilter.set([...])` with a bare array (must become `genreFilter.set({include: [...], exclude: []})`).

- [ ] **Step 1: Write the failing tests**

Add to `frontend/src/lib/stores/results.test.ts` (near existing genre-filter tests — read the file's existing `describe` block structure around the lines the earlier grep found: 109, 161, 167, 1542, 1208 — to place these consistently):

```typescript
import { genreFilter, toggleGenreFilter } from './results';
import { get } from 'svelte/store';

describe('genreFilter 3-state toggle', () => {
	beforeEach(() => {
		genreFilter.set({ include: [], exclude: [] });
	});

	it('starts neutral, first toggle includes', () => {
		toggleGenreFilter('Comedy');
		expect(get(genreFilter)).toEqual({ include: ['Comedy'], exclude: [] });
	});

	it('second toggle moves from include to exclude', () => {
		toggleGenreFilter('Comedy');
		toggleGenreFilter('Comedy');
		expect(get(genreFilter)).toEqual({ include: [], exclude: ['Comedy'] });
	});

	it('third toggle returns to neutral', () => {
		toggleGenreFilter('Comedy');
		toggleGenreFilter('Comedy');
		toggleGenreFilter('Comedy');
		expect(get(genreFilter)).toEqual({ include: [], exclude: [] });
	});

	it('toggling one genre does not affect another', () => {
		genreFilter.set({ include: ['Drama'], exclude: ['Reality'] });
		toggleGenreFilter('Comedy');
		expect(get(genreFilter)).toEqual({ include: ['Drama', 'Comedy'], exclude: ['Reality'] });
	});
});
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `npx vitest run src/lib/stores/results.test.ts -t "genreFilter 3-state toggle"`
Expected: FAIL — `toggleGenreFilter` still does 2-state array logic, `get(genreFilter)` is still an array, assertion mismatches.

- [ ] **Step 3: Write the implementation**

In `frontend/src/lib/stores/results.ts`, replace lines 39-44:

```typescript
/** Selected genres to include/exclude; both empty means "All" (no filter).
 *  Session-only (never persisted) -- a filter that narrows *content* must
 *  never silently outlive the session (see resolutionFilter's comment for
 *  the same rule applied there). */
export interface GenreFilterState {
	include: string[];
	exclude: string[];
}
export const genreFilter = writable<GenreFilterState>({ include: [], exclude: [] });
/** Cycles one genre through neutral -> include -> exclude -> neutral. */
export function toggleGenreFilter(genre: string) {
	genreFilter.update(({ include, exclude }) => {
		if (include.includes(genre)) {
			return { include: include.filter((g) => g !== genre), exclude: [...exclude, genre] };
		}
		if (exclude.includes(genre)) {
			return { include, exclude: exclude.filter((g) => g !== genre) };
		}
		return { include: [...include, genre], exclude };
	});
}
```

Find every other `genreFilter.set([...])` call in `results.ts` itself (the earlier investigation found one in a "clear all filters" reset function, around line 91) and update it to `genreFilter.set({ include: [], exclude: [] })`.

Update the `filteredResults` derived store's genre block (currently lines 679-681):

```typescript
    if ($genre.include.length > 0) {
      items = items.filter((i) => i.genres?.some((g) => $genre.include.includes(g)));
    }
    if ($genre.exclude.length > 0) {
      items = items.filter((i) => !i.genres?.some((g) => $genre.exclude.includes(g)));
    }
```

Update `buildResultParams` (currently line 535):

```typescript
  const g = get(genreFilter);
  if (g.include.length) p.genre = g.include.join(',');
  if (g.exclude.length) p.genre_exclude = g.exclude.join(',');
```

Read the small badge-count expression the earlier investigation found in `+page.svelte` (`($genreFilter.length > 0 ? 1 : 0)`, near line 19) and update it to `(($genreFilter.include.length + $genreFilter.exclude.length) > 0 ? 1 : 0)`.

- [ ] **Step 4: Fix pre-existing test call sites**

Search `frontend/src/lib/stores/results.test.ts` for every `genreFilter.set([` call (the earlier investigation found these around lines 109, 161, 167, 1542) and convert each bare-array argument to the new shape, e.g. `genreFilter.set(['Sci-Fi', 'Drama'])` becomes `genreFilter.set({ include: ['Sci-Fi', 'Drama'], exclude: [] })`. Also check the destructuring around line 1208 (`genreFilter: gf, ...`) — if `gf` is asserted against an array shape anywhere nearby, update that assertion to the new object shape too.

- [ ] **Step 5: Run tests to verify they pass**

Run:
```bash
cd frontend
npx vitest run src/lib/stores/results.test.ts
npm run check
```
Expected: full `results.test.ts` suite green (no regressions in the pre-existing tests you touched in Step 4), `npm run check` reports 0 errors (this catches any remaining `string[]`-shaped usage of `genreFilter` you missed, since TypeScript will flag it).

- [ ] **Step 6: Commit**

```bash
git add frontend/src/lib/stores/results.ts frontend/src/lib/stores/results.test.ts frontend/src/routes/+page.svelte
git commit -m "feat(results): genreFilter gains include/exclude shape + 3-state toggle"
```

---

### Task 3: `FilterBar.svelte` 3-state genre chip UI + full verification

**Files:**
- Modify: `frontend/src/lib/components/FilterBar.svelte`

**Interfaces:**
- Consumes: `genreFilter` (now `{include: string[]; exclude: string[]}`), `toggleGenreFilter(genre)` (now a 3-state cycle) — both from Task 2.

**Note:** `FilterBar.svelte` has **two** genre-chip UI blocks (a dropdown-style one around line 258-275, and a chip-row one around line 578-593, per the earlier investigation) — both must be updated identically. Read both blocks in full before editing since their surrounding markup/class conventions differ slightly.

- [ ] **Step 1: Update the dropdown block (~line 258-275)**

The existing checked-state expression `checked={$genreFilter.includes(genre)}` and the "clear all" `genreFilter.set([])` no longer type-check against the new shape. Replace:

```svelte
<button
  onclick={() => genreFilter.set({ include: [], exclude: [] })}
  class="... {($genreFilter.include.length === 0 && $genreFilter.exclude.length === 0) ? 'bg-[var(--accent)]/15 text-[var(--accent)]' : 'text-[var(--text-secondary)] hover:bg-[var(--bg-tertiary)]'}"
>
  All
</button>
{#each $availableGenres as genre}
  {@const state = $genreFilter.include.includes(genre) ? 'include' : $genreFilter.exclude.includes(genre) ? 'exclude' : 'neutral'}
  <button
    type="button"
    onclick={() => toggleGenreFilter(genre)}
    class="flex items-center gap-1.5 px-2 py-1 rounded
      {state === 'include' ? 'bg-[var(--accent)]/15 text-[var(--accent)]' : ''}
      {state === 'exclude' ? 'bg-red-500/15 text-red-500 line-through' : ''}
      {state === 'neutral' ? 'text-[var(--text-secondary)] hover:bg-[var(--bg-tertiary)]' : ''}"
  >
    {genre}
  </button>
{/each}
```

Match the exact surrounding wrapper markup (the `<div>`/`<label>` structure the existing block uses) — only the checked-state logic and the resulting classes need to change; do not restructure the block's layout. The header count badge (`Genres{#if $genreFilter.length > 0}...`) becomes `Genres{#if ($genreFilter.include.length + $genreFilter.exclude.length) > 0}<span class="ml-0.5 text-[var(--accent)]">({$genreFilter.include.length + $genreFilter.exclude.length})</span>{/if}`.

- [ ] **Step 2: Update the chip-row block (~line 578-593)**

Apply the same `state` derivation and 3-class pattern to this block's existing chip markup (its classes differ slightly from the dropdown block's — e.g. `border-[var(--accent)]` instead of a background tint — preserve that block's own existing visual language, just add the third `exclude` state using a red/struck-through treatment consistent with Step 1's choice). The "All" reset button here follows the same `genreFilter.set({ include: [], exclude: [] })` change.

- [ ] **Step 3: Verify manually in the browser**

Start the dev server, open the app, click a genre chip once (include, highlighted), click again (exclude, red/struck-through), click again (neutral). Confirm the results list actually narrows/excludes accordingly in both live and paged mode (toggle whichever mode switch this app exposes, if any, to check both).

- [ ] **Step 4: Run the full verification suite**

```bash
cd frontend
npm run check
npm run build
npx vitest run
```
Backend (throwaway container, confirm no regression from Task 1):
```bash
pytest tests/test_results_routes.py -v
```
Expected: all green. Grep every file touched across Tasks 1-3 for curly/smart quotes and confirm zero matches.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/lib/components/FilterBar.svelte
git commit -m "feat(results): 3-state genre chip UI (include/exclude/neutral) + full verification"
```
