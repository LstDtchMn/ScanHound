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
    getResults: vi.fn()
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
  selectedKeys,
  dismissedUrls,
  statusFilter,
  searchFilter,
  genreFilter,
  languageFilter,
  quickFilters,
  filteredResults,
  deckResults,
  deckGroups,
  dismissItem,
  restoreItem,
  markGrabbedSiblings,
  stats,
  filteredTotal,
  pagedMode,
  postedAfter,
  postedBefore
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
    ...overrides
  };
}

function resetStores() {
  results.set([]);
  selectedKeys.set(new Set());
  dismissedUrls.set(new Set());
  statusFilter.set('all');
  searchFilter.set('');
  genreFilter.set([]);
  languageFilter.set([]);
  quickFilters.set([]);
  postedAfter.set('');
  postedBefore.set('');
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
    genreFilter.set(['Sci-Fi', 'Drama']);
    expect(get(filteredResults).map((r) => r.url)).toEqual(['a', 'c']);
  });

  it('treats an empty genre selection as "All" (no filter)', () => {
    results.set([item({ url: 'a', genres: ['Action'] }), item({ url: 'b', genres: ['Comedy'] })]);
    genreFilter.set([]);
    expect(get(filteredResults).map((r) => r.url)).toEqual(['a', 'b']);
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
