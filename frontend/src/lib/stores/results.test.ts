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
  pagedMode
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
});
