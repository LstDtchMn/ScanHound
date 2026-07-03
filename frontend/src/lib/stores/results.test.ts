import { get } from 'svelte/store';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { ScanResult } from '$lib/api/types';

vi.mock('$lib/api/client', () => ({
  api: {
    dismissItems: vi.fn().mockResolvedValue({ status: 'ok', dismissed_count: 1 }),
    dismissedList: vi.fn().mockResolvedValue({ items: [], count: 0 }),
    selectAll: vi.fn().mockResolvedValue({}),
    deselectAll: vi.fn().mockResolvedValue({}),
    getCachedResults: vi.fn()
  }
}));

const { api } = await import('$lib/api/client');
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
  dismissItem,
  restoreItem,
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
      true
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

  it('is a no-op for an empty url', () => {
    dismissItem('');
    expect(get(dismissedUrls).size).toBe(0);
    expect(api.dismissItems).not.toHaveBeenCalled();
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
