import { get } from 'svelte/store';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { ScanResult } from '$lib/api/types';

vi.mock('$lib/api/client', () => ({
  api: {
    dismissItems: vi.fn().mockResolvedValue({ status: 'ok', dismissed_count: 1 }),
    dismissedList: vi.fn().mockResolvedValue({ items: [], count: 0 }),
    selectAll: vi.fn().mockResolvedValue({}),
    deselectAll: vi.fn().mockResolvedValue({})
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
  restoreItem
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
  genreFilter.set('');
  languageFilter.set('');
  quickFilters.set([]);
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
