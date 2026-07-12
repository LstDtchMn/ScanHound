import { get } from 'svelte/store';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { ScanResult } from '$lib/api/types';

const reconnectHandlers: Array<() => void> = [];
vi.mock('$lib/stores/connection', () => ({
  connection: {
    on: vi.fn(() => () => {}),
    onReconnect: (fn: () => void) => { reconnectHandlers.push(fn); return () => {}; }
  }
}));

vi.mock('$lib/api/client', () => ({
  api: {
    dismissItems: vi.fn().mockResolvedValue({ status: 'ok', dismissed_count: 1 }),
    dismissedList: vi.fn().mockResolvedValue({ items: [], count: 0 }),
    selectAll: vi.fn().mockResolvedValue({}),
    deselectAll: vi.fn().mockResolvedValue({}),
    getCachedResults: vi.fn(),
    getResults: vi.fn(),
    setBookmark: vi.fn().mockResolvedValue({ status: 'ok', bookmarked: true }),
    getBookmarks: vi.fn().mockResolvedValue({ items: [], count: 0 })
  }
}));

const { api } = await import('$lib/api/client');

/** Fire every reconnect handler registered via connection.onReconnect (the
 *  store registers its own at import time), the same way connection.ts's
 *  real ws.onopen-after-a-prior-close path would. */
function triggerReconnect() {
  reconnectHandlers.forEach((fn) => fn());
}
const {
  results,
  selectedDetail,
  selectedKeys,
  dismissedUrls,
  statusFilter,
  searchFilter,
  genreFilter,
  toggleGenreFilter,
  languageFilter,
  quickFilters,
  filteredResults,
  deckResults,
  deckGroups,
  dismissItem,
  restoreItem,
  bookmarkedTitles,
  bookmarkIdentityKey,
  toggleBookmark,
  hydrateBookmarks,
  markGrabbedSiblings,
  updateResultFromRescan,
  stats,
  filteredTotal,
  pagedMode,
  postedAfter,
  postedBefore,
  resolutionFilter,
  categoryFilter,
  toggleCategoryFilter,
  activeNarrowingFilters,
  CATEGORY_KEYS,
  flagsFor,
  computeStatusCounts,
  filteredStats,
  hiddenByFiltersCount,
  clearAllFilters,
  isResultsViewEmpty
} = await import('./results');

function item(overrides: Partial<ScanResult>): ScanResult {
  return {
    title: 'Some Movie',
    year: 2024,
    season: null,
    episodes: null,
    resolution: '1080p',
    size: '4.5 GB',
    status: 'missing',
    status_text: 'Missing',
    color: '',
    url: 'https://example.com/some-movie',
    group_key: 'some-movie-2024',
    rating: null,
    votes: null,
    votes_source: '',
    rt_score: null,
    genres: [],
    language: 'English',
    poster_url: '',
    imdb_id: null,
    description: '',
    hdr: 'SDR',
    dovi: false,
    selected: false,
    plex_info: '',
    plex_versions: '[]',
    plex_rating_key: null,
    posted_date: null,
    host_pref: '',
    is_duplicate_group: false,
    bookmarked: false,
    ...overrides
  };
}

function resetStores() {
  results.set([]);
  selectedDetail.set(null);
  selectedKeys.set(new Set());
  dismissedUrls.set(new Set());
  bookmarkedTitles.set(new Set());
  statusFilter.set('all');
  searchFilter.set('');
  genreFilter.set({ include: [], exclude: [] });
  languageFilter.set([]);
  quickFilters.set([]);
  resolutionFilter.set([]);
  postedAfter.set('');
  postedBefore.set('');
  categoryFilter.set([...CATEGORY_KEYS]);
  // These suites exercise the legacy client-side filter+sort pipeline
  // directly; paged mode (server-side filtering) is covered separately below.
  pagedMode.set(false);
}

describe('filteredResults', () => {
  beforeEach(() => {
    resetStores();
    vi.clearAllMocks();
  });

  it('hides items whose url is in dismissedUrls', () => {
    results.set([item({ url: 'a', title: 'A' }), item({ url: 'b', title: 'B' })]);
    dismissedUrls.set(new Set(['a']));
    const titles = get(filteredResults).map((r) => r.title);
    expect(titles).toEqual(['B']);
  });

  it('never dismisses items with an empty url', () => {
    results.set([item({ url: '', title: 'No URL' })]);
    dismissedUrls.set(new Set(['']));
    expect(get(filteredResults).map((r) => r.title)).toEqual(['No URL']);
  });

  it('filters by status', () => {
    results.set([
      item({ url: 'a', status: 'missing' }),
      item({ url: 'b', status: 'upgrade' })
    ]);
    statusFilter.set('upgrade');
    expect(get(filteredResults).map((r) => r.url)).toEqual(['b']);
  });

  it('filters by title search, case-insensitively', () => {
    results.set([item({ url: 'a', title: 'The Matrix' }), item({ url: 'b', title: 'Inception' })]);
    searchFilter.set('matrix');
    expect(get(filteredResults).map((r) => r.url)).toEqual(['a']);
  });

  it('filters by one or more selected genres (item matches if it has any)', () => {
    results.set([
      item({ url: 'a', genres: ['Action', 'Sci-Fi'] }),
      item({ url: 'b', genres: ['Comedy'] }),
      item({ url: 'c', genres: ['Drama'] })
    ]);
    genreFilter.set({ include: ['Sci-Fi', 'Drama'], exclude: [] });
    expect(get(filteredResults).map((r) => r.url)).toEqual(['a', 'c']);
  });

  it('treats an empty genre selection as "All" (no filter)', () => {
    results.set([item({ url: 'a', genres: ['Action'] }), item({ url: 'b', genres: ['Comedy'] })]);
    genreFilter.set({ include: [], exclude: [] });
    expect(get(filteredResults).map((r) => r.url)).toEqual(['a', 'b']);
  });

  it('excludes items that have any of the excluded genres', () => {
    results.set([
      item({ url: 'a', genres: ['Comedy'] }),
      item({ url: 'b', genres: ['Reality'] }),
      item({ url: 'c', genres: ['Reality', 'Comedy'] }),
      item({ url: 'd', genres: [] })
    ]);
    genreFilter.set({ include: [], exclude: ['Reality'] });
    expect(get(filteredResults).map((r) => r.url)).toEqual(['a', 'd']);
  });

  it('combines include and exclude (item must satisfy include AND not match exclude)', () => {
    results.set([
      item({ url: 'a', genres: ['Comedy'] }),
      item({ url: 'b', genres: ['Comedy', 'Reality'] }),
      item({ url: 'c', genres: ['Drama'] })
    ]);
    genreFilter.set({ include: ['Comedy'], exclude: ['Reality'] });
    expect(get(filteredResults).map((r) => r.url)).toEqual(['a']);
  });

  it('filters by one or more selected languages', () => {
    results.set([
      item({ url: 'a', language: 'English' }),
      item({ url: 'b', language: 'French' }),
      item({ url: 'c', language: 'German' })
    ]);
    languageFilter.set(['French', 'German']);
    expect(get(filteredResults).map((r) => r.url)).toEqual(['b', 'c']);
  });

  it('treats an empty language selection as "All" (no filter)', () => {
    results.set([item({ url: 'a', language: 'English' }), item({ url: 'b', language: 'French' })]);
    languageFilter.set([]);
    expect(get(filteredResults).map((r) => r.url)).toEqual(['a', 'b']);
  });

  it('postedAfter excludes items posted before the bound (live/client-side)', () => {
    results.set([
      item({ url: 'a', posted_date: 'June 1, 2026 at 12:00 AM' }),
      item({ url: 'b', posted_date: 'June 20, 2026 at 12:00 AM' })
    ]);
    postedAfter.set('2026-06-08');
    expect(get(filteredResults).map((r) => r.url)).toEqual(['b']);
  });

  it('postedBefore is inclusive through the end of that day (live/client-side)', () => {
    results.set([
      item({ url: 'a', posted_date: 'June 8, 2026 at 11:59 PM' }),
      item({ url: 'b', posted_date: 'June 9, 2026 at 12:00 AM' })
    ]);
    postedBefore.set('2026-06-08');
    expect(get(filteredResults).map((r) => r.url)).toEqual(['a']);
  });

  it('postedAfter + postedBefore both bounds inclusive on boundary dates (live/client-side)', () => {
    results.set([
      item({ url: 'start', posted_date: 'June 8, 2026 at 12:00 AM' }),
      item({ url: 'end', posted_date: 'June 10, 2026 at 11:30 PM' }),
      item({ url: 'early', posted_date: 'June 7, 2026 at 11:59 PM' }),
      item({ url: 'late', posted_date: 'June 11, 2026 at 12:00 AM' })
    ]);
    postedAfter.set('2026-06-08');
    postedBefore.set('2026-06-10');
    expect(get(filteredResults).map((r) => r.url).sort()).toEqual(['end', 'start']);
  });

  it('excludes date-less items when a bound is set (live/client-side)', () => {
    results.set([
      item({ url: 'dateless', posted_date: null }),
      item({ url: 'dated', posted_date: 'June 8, 2026 at 12:00 AM' })
    ]);
    postedAfter.set('2026-06-01');
    expect(get(filteredResults).map((r) => r.url)).toEqual(['dated']);
  });

  it('includes date-less items when no date bound is set (live/client-side)', () => {
    results.set([
      item({ url: 'dateless', posted_date: null }),
      item({ url: 'dated', posted_date: 'June 8, 2026 at 12:00 AM' })
    ]);
    expect(get(filteredResults).map((r) => r.url).sort()).toEqual(['dated', 'dateless']);
  });

  it('the bookmarked quick filter keeps only items whose identity key is in bookmarkedTitles', () => {
    results.set([
      item({ url: 'a', imdb_id: 'tt1111111', bookmarked: false }),
      item({ url: 'b', imdb_id: 'tt2222222', bookmarked: false })
    ]);
    bookmarkedTitles.set(new Set(['imdb:tt1111111']));
    quickFilters.set(['bookmarked']);
    expect(get(filteredResults).map((r) => r.url)).toEqual(['a']);
  });

  it('the bookmarked quick filter reflects a bookmark toggled mid-session, not the stale item.bookmarked snapshot', () => {
    // item.bookmarked here is frozen at false (as if fetched before the user
    // starred it) -- toggleBookmark only ever updates bookmarkedTitles (see
    // its own doc comment), never patches the results array in place, so this
    // filter must read bookmarkedTitles directly or a freshly-starred item
    // would incorrectly stay hidden until the next refetch.
    results.set([item({ url: 'a', imdb_id: 'tt1111111', bookmarked: false })]);
    quickFilters.set(['bookmarked']);
    expect(get(filteredResults).map((r) => r.url)).toEqual([]);

    bookmarkedTitles.set(new Set(['imdb:tt1111111']));
    expect(get(filteredResults).map((r) => r.url)).toEqual(['a']);
  });
});

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

describe('deckResults', () => {
  beforeEach(() => {
    resetStores();
    vi.clearAllMocks();
  });

  it('includes only actionable (missing/upgrade) items with a url', () => {
    results.set([
      item({ url: 'a', status: 'missing' }),
      item({ url: 'b', status: 'library' }),
      item({ url: '', status: 'missing' })
    ]);
    expect(get(deckResults).map((r) => r.url)).toEqual(['a']);
  });

  it('excludes items already in selectedKeys, but a left-swipe dismissal leaves the deck too', () => {
    results.set([item({ url: 'a', status: 'missing' }), item({ url: 'b', status: 'upgrade' })]);
    selectedKeys.set(new Set(['a']));
    expect(get(deckResults).map((r) => r.url)).toEqual(['b']);

    dismissItem('b');
    expect(get(deckResults)).toEqual([]);
  });
});

describe('deckGroups', () => {
  beforeEach(() => {
    resetStores();
    vi.clearAllMocks();
  });

  it('collapses same-title releases into one group, best resolution first', () => {
    results.set([
      item({ url: 'hd', title: 'Dune', group_key: 'dune|2021', resolution: '1080p', status: 'missing', size: '8 GB' }),
      item({ url: 'uhd', title: 'Dune', group_key: 'dune|2021', resolution: '4K', status: 'missing', size: '20 GB' }),
      item({ url: 'blade', title: 'Blade', group_key: 'blade|1998', resolution: '1080p', status: 'missing' })
    ]);
    const groups = get(deckGroups);
    expect(groups.length).toBe(2); // two titles, not three releases
    const dune = groups.find((g) => g.key === 'dune|2021')!;
    expect(dune.releases.map((r) => r.url)).toEqual(['uhd', 'hd']); // best (4K) first
    expect(dune.best.url).toBe('uhd');
  });

  it('ranks Dolby Vision above SDR at the same resolution when picking best', () => {
    results.set([
      item({ url: 'sdr', title: 'X', group_key: 'x', resolution: '4K', dovi: false, status: 'missing' }),
      item({ url: 'dv', title: 'X', group_key: 'x', resolution: '4K', dovi: true, status: 'missing' })
    ]);
    expect(get(deckGroups)[0].best.url).toBe('dv');
  });

  it('only groups actionable releases (grabbed/library siblings drop out)', () => {
    results.set([
      item({ url: 'a', title: 'Y', group_key: 'y', resolution: '4K', status: 'downloaded' }),
      item({ url: 'b', title: 'Y', group_key: 'y', resolution: '1080p', status: 'missing' })
    ]);
    const groups = get(deckGroups);
    expect(groups.length).toBe(1);
    expect(groups[0].releases.map((r) => r.url)).toEqual(['b']); // only the still-missing one
  });
});

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

describe('toggleBookmark', () => {
  beforeEach(() => {
    resetStores();
    vi.clearAllMocks();
  });

  it('optimistically adds to bookmarkedTitles and calls the API', async () => {
    const i = item({ imdb_id: 'tt1234567', title: 'Dune', year: 2024, bookmarked: false });
    const p = toggleBookmark(i);
    expect(get(bookmarkedTitles).has('imdb:tt1234567')).toBe(true);
    await p;
    expect(api.setBookmark).toHaveBeenCalledWith('tt1234567', 'Dune', 2024, 'movie', true);
  });

  it('toggles off (removes) when the item is already bookmarked', async () => {
    bookmarkedTitles.set(new Set(['imdb:tt1234567']));
    const i = item({ imdb_id: 'tt1234567', title: 'Dune', year: 2024, bookmarked: true });
    const p = toggleBookmark(i);
    expect(get(bookmarkedTitles).has('imdb:tt1234567')).toBe(false);
    await p;
    expect(api.setBookmark).toHaveBeenCalledWith('tt1234567', 'Dune', 2024, 'movie', false);
  });

  it('reverts the optimistic update if the API call fails', async () => {
    vi.mocked(api.setBookmark).mockRejectedValueOnce(new Error('network'));
    const i = item({ imdb_id: 'tt1234567', title: 'Dune', year: 2024, bookmarked: false });
    const ok = await toggleBookmark(i);
    expect(ok).toBe(false);
    expect(get(bookmarkedTitles).has('imdb:tt1234567')).toBe(false);
  });

  it('a second toggle on the SAME still-mounted item object actually flips it back off (does not re-read the stale item.bookmarked flag)', async () => {
    // Rows/panels render off bookmarkedTitles, not item.bookmarked (see
    // ResultRow.svelte's `bookmarked` derivation) -- toggleBookmark must
    // agree, or clicking a star twice in a row (same item reference, no
    // refetch in between) would just re-send "bookmarked: true" both times.
    const i = item({ imdb_id: 'tt1234567', title: 'Dune', year: 2024, bookmarked: false });
    await toggleBookmark(i);
    expect(get(bookmarkedTitles).has('imdb:tt1234567')).toBe(true);
    expect(api.setBookmark).toHaveBeenLastCalledWith('tt1234567', 'Dune', 2024, 'movie', true);

    await toggleBookmark(i); // same object, item.bookmarked is still false
    expect(get(bookmarkedTitles).has('imdb:tt1234567')).toBe(false);
    expect(api.setBookmark).toHaveBeenLastCalledWith('tt1234567', 'Dune', 2024, 'movie', false);
  });

  it('falls back to the title key for items without an imdb_id', async () => {
    const i = item({ imdb_id: null, title: 'Some Obscure Show', year: 2020, season: 1, bookmarked: false });
    await toggleBookmark(i);
    expect(api.setBookmark).toHaveBeenCalledWith(null, 'Some Obscure Show', 2020, 'tv', true);
    expect(get(bookmarkedTitles).has('title:some obscure show:2020:tv')).toBe(true);
  });
});

describe('hydrateBookmarks', () => {
  beforeEach(() => {
    resetStores();
    vi.clearAllMocks();
  });

  it('populates bookmarkedTitles from the server list', async () => {
    vi.mocked(api.getBookmarks).mockResolvedValueOnce({
      items: [
        { id: 1, imdb_id: 'tt1234567', title: 'Dune', year: 2024, media_type: 'movie', created_at: '' },
        { id: 2, imdb_id: null, title: 'Some Show', year: 2020, media_type: 'tv', created_at: '' }
      ],
      count: 2
    });
    await hydrateBookmarks();
    const keys = get(bookmarkedTitles);
    expect(keys.has('imdb:tt1234567')).toBe(true);
    expect(keys.has('title:some show:2020:tv')).toBe(true);
  });
});

describe('dismissItem / restoreItem', () => {
  beforeEach(() => {
    resetStores();
    vi.clearAllMocks();
  });

  it('optimistically adds to dismissedUrls and calls the API', () => {
    dismissItem('https://example.com/x', 'X');
    expect(get(dismissedUrls).has('https://example.com/x')).toBe(true);
    expect(api.dismissItems).toHaveBeenCalledWith(
      ['https://example.com/x'],
      { 'https://example.com/x': 'X' },
      true,
      undefined
    );
  });

  it('forwards per-url title-quality meta for title-level skip', () => {
    dismissItem('https://example.com/x', 'X', {
      group_key: 'x|2020',
      resolution: '1080p',
      dovi: false
    });
    expect(api.dismissItems).toHaveBeenCalledWith(
      ['https://example.com/x'],
      { 'https://example.com/x': 'X' },
      true,
      { 'https://example.com/x': { group_key: 'x|2020', resolution: '1080p', dovi: false } }
    );
  });

  it('reverts the dismissal if the API call fails', async () => {
    vi.mocked(api.dismissItems).mockRejectedValueOnce(new Error('network'));
    dismissItem('https://example.com/y');
    expect(get(dismissedUrls).has('https://example.com/y')).toBe(true);
    await vi.waitFor(() => {
      expect(get(dismissedUrls).has('https://example.com/y')).toBe(false);
    });
  });

  it('restoreItem removes a url from dismissedUrls', () => {
    dismissedUrls.set(new Set(['https://example.com/z']));
    restoreItem('https://example.com/z');
    expect(get(dismissedUrls).has('https://example.com/z')).toBe(false);
  });

  it('paged mode: a failed dismiss re-inserts the removed row', async () => {
    pagedMode.set(true);
    vi.mocked(api.dismissItems).mockRejectedValueOnce(new Error('network'));
    const a = item({ url: 'a', title: 'A' });
    results.set([a, item({ url: 'b', title: 'B' })]);
    filteredTotal.set(2);
    dismissItem('a', 'A');
    expect(get(results).map((r) => r.url)).toEqual(['b']); // optimistically removed
    await vi.waitFor(() => {
      expect(get(results).map((r) => r.url)).toContain('a'); // restored on API failure
    });
    expect(get(filteredTotal)).toBe(2);
    expect(get(dismissedUrls).has('a')).toBe(false);
  });

  it('paged mode: a failed dismiss does NOT over-count filteredTotal when a concurrent load already re-added the row', async () => {
    pagedMode.set(true);
    vi.mocked(api.dismissItems).mockRejectedValueOnce(new Error('network'));
    const a = item({ url: 'a', title: 'A' });
    results.set([a, item({ url: 'b', title: 'B' })]);
    filteredTotal.set(2);
    const p = dismissItem('a', 'A');                 // optimistic: removes 'a', filteredTotal -> 1
    expect(get(results).map((r) => r.url)).toEqual(['b']);
    // Simulate a concurrent loadResults(true) landing before the rejection:
    // it repopulates 'a' and resets filteredTotal to the server total.
    results.set([a, item({ url: 'b', title: 'B' })]);
    filteredTotal.set(2);
    await p; // let the rejection-revert run
    expect(get(results).filter((r) => r.url === 'a')).toHaveLength(1); // not double-added
    expect(get(filteredTotal)).toBe(2); // not 3 — the +1 is skipped since we didn't re-insert
  });

  it('is a no-op for an empty url', () => {
    dismissItem('');
    expect(get(dismissedUrls).size).toBe(0);
    expect(api.dismissItems).not.toHaveBeenCalled();
  });

  it('paged mode: dismiss drops the row and restore(item) re-inserts it', () => {
    pagedMode.set(true);
    const a = item({ url: 'a', title: 'A' });
    results.set([a, item({ url: 'b', title: 'B' })]);
    filteredTotal.set(2);

    dismissItem('a', 'A');
    expect(get(results).map((r) => r.url)).toEqual(['b']); // physically removed
    expect(get(filteredTotal)).toBe(1);

    restoreItem('a', a);
    expect(get(results).map((r) => r.url)).toContain('a'); // brought back
    expect(get(filteredTotal)).toBe(2);
    expect(get(dismissedUrls).has('a')).toBe(false);
  });

  it('paged-mode restore does not duplicate an already-present row', () => {
    pagedMode.set(true);
    const a = item({ url: 'a', title: 'A' });
    results.set([a]);
    filteredTotal.set(1);
    restoreItem('a', a); // row never left results
    expect(get(results).filter((r) => r.url === 'a')).toHaveLength(1);
    expect(get(filteredTotal)).toBe(1);
  });
});

describe('markGrabbedSiblings', () => {
  beforeEach(() => {
    resetStores();
    stats.set({ total: 3, missing: 3, upgrade: 0, library: 0 });
    vi.clearAllMocks();
  });

  it('flips equal/lower siblings to downloaded_similar and decrements missing', () => {
    results.set([
      item({ url: 'g', group_key: 'k', resolution: '1080p', dovi: false, status: 'downloaded' }),
      item({ url: 'lo', group_key: 'k', resolution: '720p', dovi: false, status: 'missing' }),
      item({ url: 'other', group_key: 'z', resolution: '1080p', status: 'missing' })
    ]);
    markGrabbedSiblings('g');
    const byUrl = Object.fromEntries(get(results).map((r) => [r.url, r]));
    expect(byUrl.lo.status).toBe('downloaded_similar'); // same title, not an upgrade
    expect(byUrl.other.status).toBe('missing');         // different group, untouched
    expect(get(stats).missing).toBe(2);                 // one sibling left the missing pool
  });

  it('leaves a genuinely-better sibling missing and does not decrement', () => {
    results.set([
      item({ url: 'g', group_key: 'k', resolution: '1080p', dovi: false, status: 'downloaded' }),
      item({ url: 'hi', group_key: 'k', resolution: '2160p', dovi: false, status: 'missing' })
    ]);
    markGrabbedSiblings('g');
    const hi = get(results).find((r) => r.url === 'hi')!;
    expect(hi.status).toBe('missing');       // higher res stays grabbable
    expect(hi.prior_grab).toBeTruthy();       // but annotated with what you have
    expect(get(stats).missing).toBe(3);       // unchanged
  });
});

describe('updateResultFromRescan', () => {
  beforeEach(() => {
    resetStores();
    vi.clearAllMocks();
  });

  it('merges the patch into the matching item by url', () => {
    results.set([
      item({ url: 'https://x/1', title: 'Old', imdb_id: null, poster_url: '', rating: 0 }),
      item({ url: 'https://x/2', title: 'Other' })
    ]);
    updateResultFromRescan('https://x/1', { title: 'New', imdb_id: 'tt0064519', rating: 6.3 });
    const items = get(results);
    expect(items[0]).toMatchObject({ url: 'https://x/1', title: 'New', imdb_id: 'tt0064519', rating: 6.3 });
    expect(items[1]).toMatchObject({ url: 'https://x/2', title: 'Other' });
  });

  it('no-ops when the url is not present', () => {
    results.set([item({ url: 'https://x/1', title: 'Old' })]);
    updateResultFromRescan('https://x/999', { title: 'New' });
    expect(get(results)[0].title).toBe('Old');
  });

  it('also patches selectedDetail when it is showing the rescanned url', () => {
    const original = item({ url: 'https://x/1', title: 'Old', rating: 0 });
    results.set([original]);
    selectedDetail.set(original);
    updateResultFromRescan('https://x/1', { title: 'New', rating: 6.3 });
    expect(get(selectedDetail)).toMatchObject({ url: 'https://x/1', title: 'New', rating: 6.3 });
  });

  it('leaves selectedDetail untouched when it is showing a different url', () => {
    const other = item({ url: 'https://x/2', title: 'Other' });
    results.set([item({ url: 'https://x/1', title: 'Old' }), other]);
    selectedDetail.set(other);
    updateResultFromRescan('https://x/1', { title: 'New' });
    expect(get(selectedDetail)).toMatchObject({ url: 'https://x/2', title: 'Other' });
  });

  it('leaves selectedDetail as null when nothing is selected', () => {
    results.set([item({ url: 'https://x/1', title: 'Old' })]);
    updateResultFromRescan('https://x/1', { title: 'New' });
    expect(get(selectedDetail)).toBeNull();
  });
});

describe('loadResults / paged mode', () => {
  beforeEach(() => {
    resetStores();
    vi.clearAllMocks();
  });

  it('loadResults(true) replaces results and sets paged totals', async () => {
    const { loadResults, results, filteredTotal, hasMore, pagedMode } =
      await import('./results');
    // total > PAGED_PER_PAGE (100) so there genuinely IS a page 2 — hasMore is
    // derived from page*per_page vs total, not the returned row count.
    (api.getCachedResults as any).mockResolvedValueOnce({
      items: [item({ title: 'A', url: 'a' }), item({ title: 'B', url: 'b' })],
      total: 250, stats: { total: 250, missing: 250, upgrade: 0, library: 0 },
      title_counts: { A: 1, B: 1 }, source: 'cache'
    });
    pagedMode.set(true);
    await loadResults(true);
    expect(get(results).length).toBe(2);
    expect(get(filteredTotal)).toBe(250);
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

  it('selectAll(keys) adds the given keys to selectedKeys, preserving existing selections', async () => {
    const { selectAll, selectedKeys } = await import('./results');
    selectedKeys.set(new Set(['already-selected']));
    await selectAll(['u1', 'u2']);
    expect([...get(selectedKeys)].sort()).toEqual(['already-selected', 'u1', 'u2']);
  });

  it('selectAll() with no args selects every loaded result\'s url', async () => {
    const { selectAll, selectedKeys, results } = await import('./results');
    results.set([item({ title: 'A', url: 'a' }), item({ title: 'B', url: 'b' })]);
    await selectAll();
    expect([...get(selectedKeys)].sort()).toEqual(['a', 'b']);
  });

  it('dismissItem removes the row from results in paged mode', async () => {
    const { results, dismissItem, pagedMode } = await import('./results');
    pagedMode.set(true);
    results.set([item({ title: 'A', url: 'keep' }), item({ title: 'B', url: 'drop' })]);
    await dismissItem('drop', 'B');
    expect(get(results).map(r => r.url)).toEqual(['keep']);
  });

  it('dismissItem decrements filteredTotal in paged mode, but only when a row was actually removed', async () => {
    const { results, dismissItem, pagedMode, filteredTotal } = await import('./results');
    pagedMode.set(true);
    results.set([item({ title: 'A', url: 'u1' }), item({ title: 'B', url: 'u2' })]);
    filteredTotal.set(2);
    await dismissItem('u1', 'A');
    expect(get(filteredTotal)).toBe(1);
    await dismissItem('not-in-results');
    expect(get(filteredTotal)).toBe(1);
  });

  it('categoryFilter defaults to all three categories', async () => {
    // Fresh module import with no persisted value returns the new default.
    const mod = await import('./results');
    expect(get(mod.categoryFilter).sort()).toEqual(['4k', 'remux', 'tv']);
  });

  it('loadResults sends posted_after/posted_before only when set', async () => {
    const { loadResults, pagedMode, postedAfter, postedBefore } = await import('./results');
    pagedMode.set(true);
    (api.getCachedResults as any).mockResolvedValueOnce({
      items: [], total: 0, stats: { total: 0, missing: 0, upgrade: 0, library: 0 }, title_counts: {}
    });
    await loadResults(true);
    let params = (api.getCachedResults as any).mock.calls.at(-1)[0];
    expect(params.posted_after).toBeUndefined();
    expect(params.posted_before).toBeUndefined();

    postedAfter.set('2026-06-01');
    postedBefore.set('2026-06-10');
    (api.getCachedResults as any).mockResolvedValueOnce({
      items: [], total: 0, stats: { total: 0, missing: 0, upgrade: 0, library: 0 }, title_counts: {}
    });
    await loadResults(true);
    params = (api.getCachedResults as any).mock.calls.at(-1)[0];
    expect(params.posted_after).toBe('2026-06-01');
    expect(params.posted_before).toBe('2026-06-10');
  });

  it('D2: a reset load preempts an in-flight append instead of being swallowed', async () => {
    const { loadResults, results, pagedMode, statusFilter } = await import('./results');
    pagedMode.set(true);

    // Seed page 1 so an append (page 2) is a valid next call.
    (api.getCachedResults as any).mockResolvedValueOnce({
      items: [item({ title: 'Page1', url: 'p1' })],
      total: 300, stats: { total: 300, missing: 0, upgrade: 0, library: 0 }, title_counts: {}
    });
    await loadResults(true);

    // Kick off an append (page 2) but don't resolve it yet.
    let resolveAppend!: (v: any) => void;
    const appendPromise = new Promise((resolve) => { resolveAppend = resolve; });
    (api.getCachedResults as any).mockReturnValueOnce(appendPromise);
    const appendCall = loadResults(false); // in-flight, unresolved

    // Filter changes mid-flight — simulate what the debounced refetch would do.
    statusFilter.set('missing');
    (api.getCachedResults as any).mockResolvedValueOnce({
      items: [item({ title: 'FilteredFresh', url: 'ff' })],
      total: 1, stats: { total: 1, missing: 1, upgrade: 0, library: 0 }, title_counts: {}
    });
    const resetCall = loadResults(true); // must NOT be swallowed by the in-flight append

    await resetCall;
    // The reset load must have gone through and won: results reflect the
    // fresh filtered set, not the stale append.
    expect(get(results).map((r) => r.title)).toEqual(['FilteredFresh']);

    // Now let the stale append resolve — its result must be discarded (the
    // existing filterQueryKey superseded-check), not appended after the reset.
    resolveAppend({
      items: [item({ title: 'StaleAppend', url: 'stale' })],
      total: 300, stats: { total: 300, missing: 0, upgrade: 0, library: 0 }, title_counts: {}
    });
    await appendCall;

    expect(get(results).map((r) => r.title)).toEqual(['FilteredFresh']);
  });

  it('selectAll() payload includes posted_after/posted_before only when set', async () => {
    const { selectAll, postedAfter, postedBefore } = await import('./results');
    postedAfter.set('');
    postedBefore.set('');
    await selectAll();
    let payload = (api.selectAll as any).mock.calls.at(-1)[0];
    expect(payload?.posted_after).toBeUndefined();
    expect(payload?.posted_before).toBeUndefined();

    postedAfter.set('2026-06-01');
    postedBefore.set('2026-06-10');
    await selectAll();
    payload = (api.selectAll as any).mock.calls.at(-1)[0];
    expect(payload.posted_after).toBe('2026-06-01');
    expect(payload.posted_before).toBe('2026-06-10');
  });
});

describe('WS reconnect snapshot reload (D1)', () => {
  beforeEach(() => {
    resetStores();
    vi.clearAllMocks();
  });

  it('in paged mode, a reconnect re-runs loadResults(true) (page 1 reload)', async () => {
    const { handleReconnectSnapshot, pagedMode, results, filteredTotal } = await import('./results');
    pagedMode.set(true);
    (api.getCachedResults as any).mockResolvedValueOnce({
      items: [item({ title: 'Fresh', url: 'fresh' })],
      total: 1, stats: { total: 1, missing: 1, upgrade: 0, library: 0 }, title_counts: { Fresh: 1 }
    });

    await handleReconnectSnapshot();

    expect(api.getCachedResults).toHaveBeenCalled();
    expect(get(results).map((r) => r.title)).toEqual(['Fresh']);
    expect(get(filteredTotal)).toBe(1);
  });

  it('outside paged mode (a live snapshot), a reconnect re-fetches api.getResults', async () => {
    const { handleReconnectSnapshot, pagedMode, results, stats } = await import('./results');
    pagedMode.set(false);
    results.set([item({ title: 'Old snapshot', url: 'old' })]);
    (api.getResults as any).mockResolvedValueOnce({
      items: [item({ title: 'Missed while down', url: 'missed' })],
      total: 1, page: 1, per_page: 500,
      stats: { total: 1, missing: 1, upgrade: 0, library: 0 }
    });

    await handleReconnectSnapshot();

    expect(api.getResults).toHaveBeenCalledWith({ per_page: '500' });
    expect(get(results).map((r) => r.title)).toEqual(['Missed while down']);
    expect(get(stats)).toEqual({ total: 1, missing: 1, upgrade: 0, library: 0 });
  });

  it('does not clobber an in-progress live scan stream on reconnect', async () => {
    const { handleScanResult, handleReconnectSnapshot, pagedMode, results } = await import('./results');
    pagedMode.set(false);
    handleScanResult(item({ title: 'Streaming 1', url: 's1' }) as unknown as Record<string, unknown>);

    await handleReconnectSnapshot();

    expect(api.getResults).not.toHaveBeenCalled();
    expect(get(results).map((r) => r.title)).toEqual(['Streaming 1']);
  });

  it('the connection.onReconnect wiring at module load invokes handleReconnectSnapshot', async () => {
    const { pagedMode, results } = await import('./results');
    pagedMode.set(true);
    (api.getCachedResults as any).mockResolvedValueOnce({
      items: [item({ title: 'Reconnected', url: 'r1' })],
      total: 1, stats: { total: 1, missing: 1, upgrade: 0, library: 0 }, title_counts: {}
    });

    triggerReconnect();
    await vi.waitFor(() => {
      expect(get(results).map((r) => r.title)).toEqual(['Reconnected']);
    });
  });
});

describe('bounded growth of accumulated pages (D1)', () => {
  beforeEach(() => {
    resetStores();
    vi.clearAllMocks();
  });

  it('appending pages past the cap evicts the oldest rows, keeping the array bounded', async () => {
    const { loadResults, results, hasMore, pagedMode, PAGED_RESULTS_CAP } = await import('./results');
    pagedMode.set(true);

    // Seed with a first page.
    (api.getCachedResults as any).mockResolvedValueOnce({
      items: [item({ title: 'first', url: 'first' })],
      total: PAGED_RESULTS_CAP + 50,
      stats: { total: PAGED_RESULTS_CAP + 50, missing: 0, upgrade: 0, library: 0 },
      title_counts: {}
    });
    await loadResults(true);

    // Append pages of 100 until well past the cap.
    const pagesNeeded = Math.ceil((PAGED_RESULTS_CAP + 50) / 100) + 1;
    for (let i = 0; i < pagesNeeded; i++) {
      const pageItems = Array.from({ length: 100 }, (_, j) =>
        item({ title: `p${i}-${j}`, url: `p${i}-${j}` })
      );
      (api.getCachedResults as any).mockResolvedValueOnce({
        items: pageItems,
        total: PAGED_RESULTS_CAP + 50,
        stats: { total: PAGED_RESULTS_CAP + 50, missing: 0, upgrade: 0, library: 0 },
        title_counts: {}
      });
      await loadResults(false);
    }

    expect(get(results).length).toBeLessThanOrEqual(PAGED_RESULTS_CAP);
    // The most recently appended page must survive the eviction (it's the
    // oldest rows — the front of the array — that get dropped).
    const lastPageFirstUrl = `p${pagesNeeded - 1}-0`;
    expect(get(results).some((r) => r.url === lastPageFirstUrl)).toBe(true);
    // Once every server page has been fetched, hasMore MUST be false even
    // though results.length is pinned at the cap below total — otherwise the
    // scroll/keyboard top-up loops forever past the true end of the set.
    expect(get(hasMore)).toBe(false);
  });

  it('never evicts a currently-selected row when capping (selection stays consistent)', async () => {
    const { loadResults, results, selectedKeys, pagedMode, PAGED_RESULTS_CAP } =
      await import('./results');
    pagedMode.set(true);

    // Page 1 has a row the user then selects.
    (api.getCachedResults as any).mockResolvedValueOnce({
      items: [item({ title: 'keep-me', url: 'keep-me' })],
      total: PAGED_RESULTS_CAP + 500,
      stats: { total: PAGED_RESULTS_CAP + 500, missing: 0, upgrade: 0, library: 0 },
      title_counts: {}
    });
    await loadResults(true);
    selectedKeys.set(new Set(['keep-me'])); // user selects the oldest row

    // Append enough pages to push well past the cap — 'keep-me' is the very
    // front of the array and would be the first evicted if selection were ignored.
    const pagesNeeded = Math.ceil((PAGED_RESULTS_CAP + 500) / 100) + 2;
    for (let i = 0; i < pagesNeeded; i++) {
      (api.getCachedResults as any).mockResolvedValueOnce({
        items: Array.from({ length: 100 }, (_, j) => item({ title: `q${i}-${j}`, url: `q${i}-${j}` })),
        total: PAGED_RESULTS_CAP + 500,
        stats: { total: PAGED_RESULTS_CAP + 500, missing: 0, upgrade: 0, library: 0 },
        title_counts: {}
      });
      await loadResults(false);
    }

    // The selected row survived eviction, so selectedKeys stays backed by a
    // real loaded row (no phantom selection / over-counted bulk target).
    expect(get(results).some((r) => r.url === 'keep-me')).toBe(true);
  });

  it('live-scan streaming (handleScanResult) is never capped', async () => {
    const { handleScanResult, results, pagedMode } = await import('./results');
    pagedMode.set(true); // first streamed item flips this off, per existing behavior
    const count = 50;
    for (let i = 0; i < count; i++) {
      handleScanResult(item({ title: `live-${i}`, url: `live-${i}` }) as unknown as Record<string, unknown>);
    }
    expect(get(results).length).toBe(count);
  });
});

describe('D3: paged-mode facets use server available_genres/available_languages', () => {
  beforeEach(() => {
    resetStores();
    vi.clearAllMocks();
  });

  it('availableGenres/availableLanguages reflect the server facets in paged mode, not just loaded rows', async () => {
    const { loadResults, pagedMode, availableGenres, availableLanguages } = await import('./results');
    pagedMode.set(true);
    (api.getCachedResults as any).mockResolvedValueOnce({
      // Only one item's worth of genres/languages loaded on this page...
      items: [item({ title: 'A', url: 'a', genres: ['Action'], language: 'English' })],
      total: 500,
      stats: { total: 500, missing: 0, upgrade: 0, library: 0 },
      title_counts: {},
      // ...but the server facets span the whole matching set (500 items).
      available_genres: ['Action', 'Comedy', 'Drama', 'Horror'],
      available_languages: ['English', 'French', 'German']
    });

    await loadResults(true);

    expect(get(availableGenres)).toEqual(['Action', 'Comedy', 'Drama', 'Horror']);
    expect(get(availableLanguages)).toEqual(['English', 'French', 'German']);
  });

  it('in live mode (not paged), facets still derive from loaded results, ignoring any stale server facets', async () => {
    const { results, pagedMode, availableGenres, availableLanguages } = await import('./results');
    pagedMode.set(false);
    results.set([
      item({ url: 'a', genres: ['Sci-Fi'], language: 'Japanese' }),
      item({ url: 'b', genres: ['Comedy'], language: 'Japanese' })
    ]);

    expect(get(availableGenres)).toEqual(['Comedy', 'Sci-Fi']);
    expect(get(availableLanguages)).toEqual(['Japanese']);
  });

  it('a fresh page-1 load replaces stale server facets from a previous filter', async () => {
    const { loadResults, pagedMode, availableGenres } = await import('./results');
    pagedMode.set(true);
    (api.getCachedResults as any).mockResolvedValueOnce({
      items: [], total: 0, stats: { total: 0, missing: 0, upgrade: 0, library: 0 },
      title_counts: {}, available_genres: ['Action'], available_languages: []
    });
    await loadResults(true);
    expect(get(availableGenres)).toEqual(['Action']);

    (api.getCachedResults as any).mockResolvedValueOnce({
      items: [], total: 0, stats: { total: 0, missing: 0, upgrade: 0, library: 0 },
      title_counts: {}, available_genres: ['Horror', 'Thriller'], available_languages: []
    });
    await loadResults(true);
    expect(get(availableGenres)).toEqual(['Horror', 'Thriller']);
  });
});

describe('D3: select-all in paged mode selects only loaded rows (honest label)', () => {
  beforeEach(() => {
    resetStores();
    vi.clearAllMocks();
  });

  it('selectAll() in paged mode still only selects the urls actually passed/loaded — not the full server match set', async () => {
    // This documents the D3 decision: rather than silently claiming to select
    // every server-matched row (which would require plumbing group_key->url
    // mapping from a backend response, since selectedKeys is url-keyed while
    // the server's /select-all match set is group_key-keyed), paged mode's
    // "Select all" button is relabeled "Select loaded (N)" in the UI and the
    // store keeps exactly its existing (loaded-rows) semantics.
    const { selectAll, selectedKeys, results, filteredResults, pagedMode, filteredTotal } = await import('./results');
    pagedMode.set(true);
    filteredTotal.set(500); // server says 500 total matches
    results.set([item({ title: 'A', url: 'a' }), item({ title: 'B', url: 'b' })]); // only 2 loaded

    await selectAll(get(filteredResults).map((r) => r.url));

    expect([...get(selectedKeys)].sort()).toEqual(['a', 'b']);
  });
});

describe('debounced refetch on filter change (paged mode)', () => {
  // NOTE: the module-level _filterKey subscription is primed the moment this
  // module is first imported, long before these tests run — so we never rely
  // on "the first fire is skipped" here, only on debounce timing/coalescing/gating.
  //
  // Each test also sets an explicit baseline value for the store it's about to
  // change (while already on fake timers) and then clears timers before the
  // real assertions start. This guards against a prior suite's resetStores()
  // (which runs under REAL timers) having left a stray in-flight debounce, and
  // against a same-value .set() being a no-op (svelte's writable only notifies
  // subscribers when the new value is actually different).

  it('does not refetch before 250ms, and refetches exactly once after', async () => {
    vi.useFakeTimers();
    try {
      pagedMode.set(true);
      statusFilter.set('all'); // baseline (may itself schedule a fake timer)
      vi.clearAllTimers(); // drop anything scheduled by the baseline above
      (api.getCachedResults as any).mockClear();
      (api.getCachedResults as any).mockResolvedValue({
        items: [],
        total: 0,
        stats: { total: 0, missing: 0, upgrade: 0, library: 0 },
        title_counts: {}
      });

      statusFilter.set('missing');

      vi.advanceTimersByTime(200);
      expect(api.getCachedResults).not.toHaveBeenCalled();

      vi.advanceTimersByTime(100); // total 300ms — past the 250ms debounce
      await vi.runAllTimersAsync();
      expect(api.getCachedResults).toHaveBeenCalledTimes(1);
    } finally {
      vi.useRealTimers();
    }
  });

  it('coalesces rapid successive filter changes into a single refetch', async () => {
    vi.useFakeTimers();
    try {
      pagedMode.set(true);
      searchFilter.set(''); // baseline
      vi.clearAllTimers();
      (api.getCachedResults as any).mockClear();
      (api.getCachedResults as any).mockResolvedValue({
        items: [],
        total: 0,
        stats: { total: 0, missing: 0, upgrade: 0, library: 0 },
        title_counts: {}
      });

      searchFilter.set('a');
      vi.advanceTimersByTime(100);
      searchFilter.set('ab'); // resets the debounce timer before it fires
      vi.advanceTimersByTime(100); // 200ms since the second change's own start — still short of 250ms

      expect(api.getCachedResults).not.toHaveBeenCalled();

      vi.advanceTimersByTime(200); // now well past 250ms since the last change
      await vi.runAllTimersAsync();
      expect(api.getCachedResults).toHaveBeenCalledTimes(1);
    } finally {
      vi.useRealTimers();
    }
  });

  it('changing postedAfter triggers the debounced refetch', async () => {
    vi.useFakeTimers();
    try {
      pagedMode.set(true);
      postedAfter.set(''); // baseline
      vi.clearAllTimers();
      (api.getCachedResults as any).mockClear();
      (api.getCachedResults as any).mockResolvedValue({
        items: [],
        total: 0,
        stats: { total: 0, missing: 0, upgrade: 0, library: 0 },
        title_counts: {}
      });

      postedAfter.set('2026-06-01');

      vi.advanceTimersByTime(200);
      expect(api.getCachedResults).not.toHaveBeenCalled();

      vi.advanceTimersByTime(100);
      await vi.runAllTimersAsync();
      expect(api.getCachedResults).toHaveBeenCalledTimes(1);
    } finally {
      vi.useRealTimers();
    }
  });

  it('clearAllFilters (the empty-state "Clear filters" action) triggers the debounced refetch in paged mode', async () => {
    vi.useFakeTimers();
    try {
      pagedMode.set(true);
      resolutionFilter.set(['4K', '1080p']); // baseline: the "stuck filter" trap state
      vi.clearAllTimers();
      (api.getCachedResults as any).mockClear();
      (api.getCachedResults as any).mockResolvedValue({
        items: [item({ status: 'missing', url: 'tv1' })],
        total: 1,
        stats: { total: 1, missing: 1, upgrade: 0, library: 0 },
        filtered_stats: { total: 1, missing: 1, upgrade: 0, library: 0 },
        title_counts: {}
      });

      clearAllFilters();
      expect(get(resolutionFilter)).toEqual([]);

      vi.advanceTimersByTime(200);
      expect(api.getCachedResults).not.toHaveBeenCalled();

      vi.advanceTimersByTime(100);
      await vi.runAllTimersAsync();
      expect(api.getCachedResults).toHaveBeenCalledTimes(1);
      // The refetch's response is what actually brings the hidden item back —
      // trace confirmed: results (passthrough in paged mode) now holds it.
      expect(get(results).map((i) => i.url)).toEqual(['tv1']);
    } finally {
      vi.useRealTimers();
    }
  });

  it('never refetches while pagedMode is false, even past the debounce window', async () => {
    vi.useFakeTimers();
    try {
      pagedMode.set(false);
      statusFilter.set('all'); // baseline
      vi.clearAllTimers();
      (api.getCachedResults as any).mockClear();
      (api.getCachedResults as any).mockResolvedValue({
        items: [],
        total: 0,
        stats: { total: 0, missing: 0, upgrade: 0, library: 0 },
        title_counts: {}
      });

      statusFilter.set('upgrade');
      vi.advanceTimersByTime(300);
      await vi.runAllTimersAsync();

      expect(api.getCachedResults).not.toHaveBeenCalled();
    } finally {
      vi.useRealTimers();
    }
  });
});

describe('handleScanResult / handleScanComplete (live stream supersedes paged cache)', () => {
  // C1/C2 regression coverage: a live scan's streamed results must never mix
  // with (or be clobbered by) server-paged/cache-loaded rows. The store used
  // to gate this on a dead `fromCacheActive` flag that nothing set anymore
  // (hydrateCache, its only setter, had no callers) so the guard could never
  // fire. handleScanResult/handleScanComplete are exported specifically so
  // this can be tested by calling them directly, the same way the real
  // `connection.on('scan:result', ...)` wiring in results.ts invokes them —
  // this is less invasive than instantiating a real WebSocket in jsdom.
  beforeEach(() => {
    resetStores();
    vi.clearAllMocks();
  });

  it('first streamed item clears cached rows, flips pagedMode off, and appends; second item just appends', async () => {
    const { handleScanResult, results, pagedMode, fromCache } = await import('./results');
    pagedMode.set(true);
    fromCache.set(true);
    results.set([item({ title: 'Cached A', url: 'cached-a' }), item({ title: 'Cached B', url: 'cached-b' })]);

    handleScanResult(item({ title: 'Live 1', url: 'live-1' }) as unknown as Record<string, unknown>);

    expect(get(pagedMode)).toBe(false);
    expect(get(fromCache)).toBe(false);
    expect(get(results).map((r) => r.title)).toEqual(['Live 1']);

    handleScanResult(item({ title: 'Live 2', url: 'live-2' }) as unknown as Record<string, unknown>);

    expect(get(results).map((r) => r.title)).toEqual(['Live 1', 'Live 2']);
  });

  it('does not clear results when already in live mode (not paged)', async () => {
    const { handleScanResult, results, pagedMode } = await import('./results');
    pagedMode.set(false);
    results.set([item({ title: 'Existing Live', url: 'existing' })]);

    handleScanResult(item({ title: 'Next', url: 'next' }) as unknown as Record<string, unknown>);

    expect(get(results).map((r) => r.title)).toEqual(['Existing Live', 'Next']);
  });

  it('handleScanComplete sets stats and clears stale results when the scan produced nothing', async () => {
    const { handleScanComplete, results, stats } = await import('./results');
    results.set([item({ title: 'Stale', url: 'stale' })]);

    handleScanComplete({ stats: { total: 0, missing: 0, upgrade: 0, library: 0 } });

    expect(get(results)).toEqual([]);
    expect(get(stats)).toEqual({ total: 0, missing: 0, upgrade: 0, library: 0 });
  });

  it('handleScanComplete keeps streamed results when the scan produced items', async () => {
    const { handleScanResult, handleScanComplete, results, stats } = await import('./results');
    handleScanResult(item({ title: 'Streamed', url: 'streamed' }) as unknown as Record<string, unknown>);

    handleScanComplete({ stats: { total: 1, missing: 1, upgrade: 0, library: 0 } });

    expect(get(results).map((r) => r.title)).toEqual(['Streamed']);
    expect(get(stats)).toEqual({ total: 1, missing: 1, upgrade: 0, library: 0 });
  });
});

describe('clearResults flips pagedMode off', () => {
  // clearResults() has exactly one caller today: ScanControls.svelte's
  // handleStart (the local Start-Scan button), which calls clearResults()
  // immediately before startScan(...). Since that's the only call site, the
  // C1 fix's belt-and-braces mode flip lives inside clearResults() itself
  // rather than in the .svelte component or the scanner store.
  beforeEach(() => {
    resetStores();
    vi.clearAllMocks();
  });

  it('sets pagedMode to false as part of clearing results for a new scan', async () => {
    const { clearResults, pagedMode, results } = await import('./results');
    pagedMode.set(true);
    results.set([item({ title: 'Old', url: 'old' })]);

    clearResults();

    expect(get(pagedMode)).toBe(false);
    expect(get(results)).toEqual([]);
  });
});

describe('deckNeedsMore', () => {
  beforeEach(() => {
    resetStores();
    vi.clearAllMocks();
  });

  it('deckNeedsMore is true only when paged, has more, and cards run low', async () => {
    const { deckNeedsMore, pagedMode, hasMore, loadingMore } = await import('./results');
    pagedMode.set(true); hasMore.set(true); loadingMore.set(false);
    expect(deckNeedsMore(3)).toBe(true);
    expect(deckNeedsMore(20)).toBe(false);
    hasMore.set(false);
    expect(deckNeedsMore(3)).toBe(false);
  });
});

describe('resolutionFilter is session-only (not persisted)', () => {
  it('is a plain writable — starts empty regardless of any localStorage value', async () => {
    // Regression test for the "zero Missing items" bug: resolutionFilter used
    // to be `persisted('sh-resolution-filter', [])`, so a value written to
    // localStorage by a prior session would silently narrow every future
    // session with no visible indicator. Simulate that prior-session write
    // and confirm a fresh import ignores it.
    localStorage.setItem('sh-resolution-filter', JSON.stringify(['4K', '1080p']));
    expect(get(resolutionFilter)).toEqual([]);
  });
});

describe('computeStatusCounts (pure)', () => {
  it('counts total and each status by substring match, mirroring the backend', () => {
    const counts = computeStatusCounts([
      item({ status: 'missing', url: 'a' }),
      item({ status: 'missing', url: 'b' }),
      item({ status: 'upgrade', url: 'c' }),
      item({ status: 'in_library', url: 'd' }),
      item({ status: 'downloaded', url: 'e' })
    ]);
    expect(counts).toEqual({ total: 5, missing: 2, upgrade: 1, library: 1 });
  });

  it('returns all-zero counts for an empty list', () => {
    expect(computeStatusCounts([])).toEqual({ total: 0, missing: 0, upgrade: 0, library: 0 });
  });
});

describe('filteredStats / hiddenByFiltersCount (empty-state self-diagnosis)', () => {
  beforeEach(() => {
    resetStores();
    vi.clearAllMocks();
  });

  it('live mode: filteredStats derives from filteredResults, and hiddenByFiltersCount is 0 when nothing is hidden', async () => {
    const { results: r, stats: st, statusFilter: sf } = await import('./results');
    r.set([item({ status: 'missing', url: 'a' }), item({ status: 'missing', url: 'b' })]);
    st.set({ total: 2, missing: 2, upgrade: 0, library: 0 });
    sf.set('missing');
    expect(get(filteredStats)).toEqual({ total: 2, missing: 2, upgrade: 0, library: 0 });
    expect(get(hiddenByFiltersCount)).toBe(0);
  });

  it('live mode: a content filter (resolution) hiding every match for the active tab is reflected as hiddenByFiltersCount', async () => {
    // Reproduces the reported bug directly: two TV items are 'missing', but a
    // stuck resolutionFilter of {4K, 1080p} matches neither (TV only ever
    // keys as 'TV' — see resolutionKeysFor), so filteredResults goes to 0
    // while the true baseline (stats) still says 2.
    const { results: r, stats: st, statusFilter: sf, resolutionFilter: rf } = await import('./results');
    r.set([
      item({ status: 'missing', url: 'tv1', season: 1, category: 'tv' }),
      item({ status: 'missing', url: 'tv2', season: 2, category: 'tv' })
    ]);
    st.set({ total: 2, missing: 2, upgrade: 0, library: 0 });
    sf.set('missing');
    rf.set(['4K', '1080p']);
    expect(get(filteredResults)).toEqual([]);
    expect(get(hiddenByFiltersCount)).toBe(2);
  });

  it('clearAllFilters resets resolutionFilter (and the other content filters) so the hidden items reappear', async () => {
    const {
      results: r, stats: st, statusFilter: sf, resolutionFilter: rf,
      genreFilter: gf, languageFilter: lf, postedAfter: pa, postedBefore: pb, searchFilter: sef
    } = await import('./results');
    r.set([item({ status: 'missing', url: 'tv1', season: 1, category: 'tv' })]);
    st.set({ total: 1, missing: 1, upgrade: 0, library: 0 });
    sf.set('missing');
    rf.set(['4K', '1080p']);
    gf.set({ include: ['Horror'], exclude: [] });
    lf.set(['French']);
    pa.set('2020-01-01');
    pb.set('2020-12-31');
    sef.set('nomatch');
    expect(get(filteredResults)).toEqual([]);
    expect(get(hiddenByFiltersCount)).toBe(1);

    clearAllFilters();

    expect(get(rf)).toEqual([]);
    expect(get(gf)).toEqual({ include: [], exclude: [] });
    expect(get(lf)).toEqual([]);
    expect(get(pa)).toBe('');
    expect(get(pb)).toBe('');
    expect(get(sef)).toBe('');
    expect(get(filteredResults).map((i) => i.url)).toEqual(['tv1']);
    expect(get(hiddenByFiltersCount)).toBe(0);
  });

  it('paged mode: filteredStats/hiddenByFiltersCount come from the server filtered_stats field', async () => {
    const { loadResults, pagedMode, stats: st, statusFilter: sf } = await import('./results');
    pagedMode.set(true);
    sf.set('missing');
    (api.getCachedResults as any).mockResolvedValueOnce({
      items: [], total: 0,
      stats: { total: 340, missing: 340, upgrade: 0, library: 0 },
      filtered_stats: { total: 0, missing: 0, upgrade: 0, library: 0 },
      title_counts: {}
    });
    await loadResults(true);
    expect(get(st).missing).toBe(340);
    expect(get(filteredStats).missing).toBe(0);
    expect(get(hiddenByFiltersCount)).toBe(340);
  });

  it('paged mode: hiddenByFiltersCount is 0 once filtered_stats catches up with stats (e.g. after clearing filters)', async () => {
    const { loadResults, pagedMode, stats: st, statusFilter: sf } = await import('./results');
    pagedMode.set(true);
    sf.set('missing');
    (api.getCachedResults as any).mockResolvedValueOnce({
      items: [item({ status: 'missing', url: 'a' })], total: 340,
      stats: { total: 340, missing: 340, upgrade: 0, library: 0 },
      filtered_stats: { total: 340, missing: 340, upgrade: 0, library: 0 },
      title_counts: {}
    });
    await loadResults(true);
    expect(get(hiddenByFiltersCount)).toBe(0);
  });
});

describe('hiddenByFiltersCount does not conflate dismissed items with filter-hidden ones (live mode)', () => {
  // Regression coverage for the finding: in live mode, `stats` was populated
  // straight from the scanner (handleScanResult/handleScanComplete) and never
  // dismissal-filtered, while filteredStats (via filteredResults) DOES exclude
  // dismissed items. So swiping away every item matching the active tab used
  // to leave `stats[key]` inflated relative to `filteredStats[key]` forever —
  // a false "N hidden by filters" with a Clear filters button that can't fix
  // it (clearAllFilters never touches dismissedUrls).
  beforeEach(() => {
    resetStores();
    vi.clearAllMocks();
  });

  it('every item matching the active tab dismissed (not content-filtered): hiddenByFiltersCount is 0, not a false positive', () => {
    results.set([
      item({ status: 'missing', url: 'a' }),
      item({ status: 'missing', url: 'b' })
    ]);
    stats.set({ total: 2, missing: 2, upgrade: 0, library: 0 });
    statusFilter.set('missing');
    // Nothing dismissed yet, no content filters — baseline sanity check.
    expect(get(hiddenByFiltersCount)).toBe(0);

    // Swipe-to-skip every item on this tab (normal triage, not a filter).
    dismissedUrls.set(new Set(['a', 'b']));

    expect(get(filteredResults)).toEqual([]); // dismissal really does hide them
    // The old bug: this would read 2 (stats.missing=2 vs filteredStats.missing=0)
    // even though no CONTENT filter is hiding anything — Clear filters is a no-op
    // here, so the UI must not claim filters are the cause.
    expect(get(hiddenByFiltersCount)).toBe(0);
  });

  it('a mix of dismissed AND content-filtered items: hiddenByFiltersCount reflects only the content-filtered portion', () => {
    results.set([
      item({ status: 'missing', url: 'a', season: 1, category: 'tv' }), // will be dismissed
      item({ status: 'missing', url: 'b', season: 1, category: 'tv' })  // will be content-filtered
    ]);
    stats.set({ total: 2, missing: 2, upgrade: 0, library: 0 });
    statusFilter.set('missing');
    dismissedUrls.set(new Set(['a']));
    resolutionFilter.set(['4K', '1080p']); // TV never keys as 4K/1080p — hides 'b'

    expect(get(filteredResults)).toEqual([]);
    // Only 'b' (content-filtered) should count as "hidden by filters" — 'a'
    // (dismissed) must not inflate the figure or the count would overstate
    // what Clear filters can actually resurrect.
    expect(get(hiddenByFiltersCount)).toBe(1);

    clearAllFilters();
    // 'b' reappears once the content filter is cleared; 'a' stays gone (dismissed).
    expect(get(filteredResults).map((r) => r.url)).toEqual(['b']);
    expect(get(hiddenByFiltersCount)).toBe(0);
  });

  it('restoring a dismissed item brings hiddenByFiltersCount back down correctly (no content filter involved)', () => {
    results.set([item({ status: 'missing', url: 'a' })]);
    stats.set({ total: 1, missing: 1, upgrade: 0, library: 0 });
    statusFilter.set('missing');
    dismissedUrls.set(new Set(['a']));
    expect(get(hiddenByFiltersCount)).toBe(0); // dismissed, not filter-hidden

    restoreItem('a');
    expect(get(filteredResults).map((r) => r.url)).toEqual(['a']);
    expect(get(hiddenByFiltersCount)).toBe(0); // still 0 — it's visible again
  });
});

describe('isResultsViewEmpty (empty-state gate) survives a paged-mode -> live-mode transition', () => {
  // Regression coverage: filteredTotal is only written by loadResults() and
  // the paged branches of dismissItem/restoreItem — never by
  // handleScanResult/handleScanComplete/clearResults. A user browsing paged
  // results (filteredTotal left at some prior value) who then starts a live
  // scan used to keep filteredTotal stuck, so a gate of `filteredTotal === 0`
  // could never become true again for the rest of that session — even when
  // the live view was genuinely empty. isResultsViewEmpty must not consult
  // filteredTotal at all once out of paged mode.
  beforeEach(() => {
    resetStores();
    vi.clearAllMocks();
  });

  it('paged mode: gate is keyed on filteredTotal (matches old semantics)', async () => {
    const { loadResults, pagedMode, filteredTotal: ft, filteredResults: fr } = await import('./results');
    pagedMode.set(true);
    (api.getCachedResults as any).mockResolvedValueOnce({
      items: [item({ title: 'A', url: 'a' })], total: 340,
      stats: { total: 340, missing: 340, upgrade: 0, library: 0 },
      filtered_stats: { total: 340, missing: 340, upgrade: 0, library: 0 },
      title_counts: {}
    });
    await loadResults(true);
    expect(get(ft)).toBe(340);
    expect(isResultsViewEmpty(get(pagedMode), get(fr).length, get(ft))).toBe(false);
  });

  it('paged -> live transition: a stale filteredTotal no longer wedges the gate shut', async () => {
    const {
      loadResults, pagedMode, filteredTotal: ft, filteredResults: fr,
      handleScanResult: scanResult, handleScanComplete: scanComplete
    } = await import('./results');

    // 1. Browse paged results — filteredTotal picks up a large server total.
    pagedMode.set(true);
    (api.getCachedResults as any).mockResolvedValueOnce({
      items: [item({ title: 'Cached', url: 'cached' })], total: 340,
      stats: { total: 340, missing: 340, upgrade: 0, library: 0 },
      filtered_stats: { total: 340, missing: 340, upgrade: 0, library: 0 },
      title_counts: {}
    });
    await loadResults(true);
    expect(get(ft)).toBe(340);

    // 2. Start a live scan that streams in a single result then completes.
    scanResult(item({ title: 'Live', url: 'live', status: 'missing' }) as unknown as Record<string, unknown>);
    expect(get(pagedMode)).toBe(false); // flips out of paged mode on first stream

    // filteredTotal is untouched by the live path — still stuck at 340.
    expect(get(ft)).toBe(340);
    // But the live view genuinely has one item, so the gate must read "not empty".
    expect(isResultsViewEmpty(get(pagedMode), get(fr).length, get(ft))).toBe(false);

    // 3. Dismiss the only live item — the live view is now genuinely empty,
    // and the OLD gate (`filteredTotal === 0`) could never detect this since
    // filteredTotal is still 340. The fixed gate must catch it.
    dismissedUrls.set(new Set(['live']));
    expect(get(fr)).toEqual([]);
    expect(get(ft)).toBe(340); // proves the staleness: still never reset
    expect(isResultsViewEmpty(get(pagedMode), get(fr).length, get(ft))).toBe(true);

    // Sanity: the OLD single-field check would have stayed stuck "not empty"
    // here, which is exactly the bug this replaces.
    expect(get(ft) === 0).toBe(false);

    scanComplete({ stats: { total: 0, missing: 0, upgrade: 0, library: 0 } });
  });

  it('live mode with zero results and zero stale filteredTotal: gate reads empty (baseline, no regression)', () => {
    expect(isResultsViewEmpty(false, 0, 0)).toBe(true);
  });

  it('paged mode with filteredTotal > 0 but no rows loaded yet: gate stays "not empty" (avoids a premature flash)', () => {
    expect(isResultsViewEmpty(true, 0, 340)).toBe(false);
  });
});

describe('toggleCategoryFilter', () => {
  // Regression coverage for the mobile "can't find Remux" fix: categoryFilter
  // is now written from two places (ScanControls' chips AND FilterBar's
  // Category row), both exclusively through this function — see its doc
  // comment in results.ts for why that matters (single writer, no clobber).
  beforeEach(() => {
    resetStores();
    vi.clearAllMocks();
  });

  it('removes a key that is present', () => {
    categoryFilter.set(['4k', 'remux', 'tv']);
    toggleCategoryFilter('remux');
    expect(get(categoryFilter).sort()).toEqual(['4k', 'tv']);
  });

  it('adds a key that is absent', () => {
    categoryFilter.set(['4k']);
    toggleCategoryFilter('remux');
    expect(get(categoryFilter).sort()).toEqual(['4k', 'remux']);
  });

  it('applying it twice is a no-op (pure toggle)', () => {
    const start = [...get(categoryFilter)].sort();
    toggleCategoryFilter('tv');
    toggleCategoryFilter('tv');
    expect(get(categoryFilter).sort()).toEqual(start);
  });

  it('can narrow all the way down to an empty array by toggling off every category', () => {
    categoryFilter.set(['4k', 'remux', 'tv']);
    toggleCategoryFilter('4k');
    toggleCategoryFilter('remux');
    toggleCategoryFilter('tv');
    expect(get(categoryFilter)).toEqual([]);
  });
});

describe('flagsFor (per-source scan-toggle projection of categoryFilter)', () => {
  // Regression coverage for the Critical bug this suite exists to close:
  // ScanControls' `flags` became `$derived(flagsFor(selectedSource,
  // $categoryFilter))`, re-running on every categoryFilter change. flagsFor
  // used to special-case an empty filter back to the source's `default`
  // flags — harmless when flags was only seeded once at mount, but wrong
  // once it re-derives live: toggling every category chip off (a state the
  // test above proves categoryFilter can reach) would silently re-enable the
  // source's default categories in both the checkbox UI and what
  // handleStart() passes to startScan(), scraping categories the user had
  // just turned off. flagsFor is pure (no store reads), so no resetStores()/
  // beforeEach is needed here.

  it('an explicitly empty categoryFilter yields every flag false — NOT the source defaults — for all three sources', () => {
    expect(flagsFor('HDEncode', [])).toEqual({ '4k': false, remux: false, tv: false });
    expect(flagsFor('DDLBase', [])).toEqual({ '4k_webdl': false, '4k_remux': false, '1080p_remux': false });
    expect(flagsFor('Adit-HD', [])).toEqual({ '4k': false, remux: false, tv: false });
  });

  it('all three normalized categories selected turns every per-source key on', () => {
    expect(flagsFor('HDEncode', ['4k', 'remux', 'tv'])).toEqual({ '4k': true, remux: true, tv: true });
  });

  it('DDLBase: both remux sub-keys (4k_remux, 1080p_remux) derive from the single normalized "remux" category; DDLBase has no tv key', () => {
    expect(flagsFor('DDLBase', ['4k', 'remux'])).toEqual({
      '4k_webdl': true,
      '4k_remux': true,
      '1080p_remux': true
    });
  });

  it('only "4k" selected leaves remux/tv off', () => {
    expect(flagsFor('HDEncode', ['4k'])).toEqual({ '4k': true, remux: false, tv: false });
  });
});

describe('clearAllFilters resets categoryFilter to show-all', () => {
  beforeEach(() => {
    resetStores();
    vi.clearAllMocks();
  });

  it('resets a narrowed categoryFilter back to all three categories', () => {
    categoryFilter.set(['4k']);
    clearAllFilters();
    expect(get(categoryFilter).sort()).toEqual(['4k', 'remux', 'tv']);
  });

  it('the reset value actually shows every known-category item again (not [], which filteredResults treats as "hide everything known")', () => {
    results.set([
      item({ url: 'a', category: '4k' }),
      item({ url: 'b', category: 'remux' }),
      item({ url: 'c', category: 'tv', season: 1 })
    ]);
    categoryFilter.set(['4k']); // narrowed — only 'a' should show
    expect(get(filteredResults).map((r) => r.url)).toEqual(['a']);

    clearAllFilters();

    expect(get(filteredResults).map((r) => r.url).sort()).toEqual(['a', 'b', 'c']);
  });
});

describe('activeNarrowingFilters (names the "Hidden by: ..." culprits)', () => {
  beforeEach(() => {
    resetStores();
    vi.clearAllMocks();
  });

  it('is empty when every filter is at its "show everything" default', () => {
    expect(get(activeNarrowingFilters)).toEqual([]);
  });

  it('names the OFF categories when categoryFilter is narrowed', () => {
    categoryFilter.set(['4k']); // remux + tv turned off
    expect(get(activeNarrowingFilters)).toEqual(['Remux, TV hidden (category)']);
  });

  it('names the selected keys when resolutionFilter is set', () => {
    resolutionFilter.set(['1080p']);
    expect(get(activeNarrowingFilters)).toEqual(['1080p (resolution)']);
  });

  it('names category and resolution together, in that order', () => {
    categoryFilter.set(['4k', 'remux']); // tv off
    resolutionFilter.set(['4K', '1080p']);
    expect(get(activeNarrowingFilters)).toEqual([
      'TV hidden (category)',
      '4K, 1080p (resolution)'
    ]);
  });

  it('names genre/language/quick/date/search when set, summarizing lists over 3 as a count', () => {
    genreFilter.set({ include: ['Horror', 'Comedy'], exclude: [] });
    languageFilter.set(['French', 'German', 'Japanese', 'Korean']); // > 3 -> count form
    quickFilters.set(['4k']);
    postedAfter.set('2026-01-01');
    searchFilter.set('dune');

    expect(get(activeNarrowingFilters)).toEqual([
      'Horror, Comedy (genre)',
      '4 languages (language)',
      '4K (quick filter)',
      'date range',
      'search text'
    ]);
  });

  it('names excluded genres distinctly from included ones', () => {
    genreFilter.set({ include: ['Comedy'], exclude: ['Reality'] });

    expect(get(activeNarrowingFilters)).toEqual([
      'Comedy (genre)',
      'Reality excluded (genre)'
    ]);
  });

  it('goes back to empty after clearAllFilters', () => {
    categoryFilter.set(['4k']);
    resolutionFilter.set(['1080p']);
    expect(get(activeNarrowingFilters).length).toBeGreaterThan(0);

    clearAllFilters();

    expect(get(activeNarrowingFilters)).toEqual([]);
  });
});
