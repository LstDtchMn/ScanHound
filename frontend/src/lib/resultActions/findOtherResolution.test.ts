import { describe, expect, it } from 'vitest';
import { findCachedAlternative, seasonSearchQuery, targetResolution } from './findOtherResolution';
import type { ScanResult } from '$lib/api/types';

function makeItem(overrides: Partial<ScanResult> = {}): ScanResult {
  return {
    title: 'Show Name',
    year: 2020,
    season: 2,
    episodes: 10,
    resolution: '4K',
    size: '20 GB',
    status: 'missing',
    status_text: 'Missing',
    color: '',
    url: 'https://example.com/show',
    group_key: 'show-name-s2',
    rating: null,
    votes: null,
    votes_source: '',
    rt_score: null,
    genres: [],
    language: 'en',
    poster_url: '',
    imdb_id: 'tt1234567',
    description: '',
    hdr: '',
    dovi: false,
    selected: false,
    plex_info: '',
    plex_versions: '',
    plex_rating_key: null,
    posted_date: null,
    host_pref: '',
    is_duplicate_group: false,
    ...overrides
  } as ScanResult;
}

describe('targetResolution', () => {
  it('4K implies 1080p', () => {
    expect(targetResolution('4K')).toBe('1080p');
  });
  it('2160p implies 1080p', () => {
    expect(targetResolution('2160p')).toBe('1080p');
  });
  it('1080p implies 4K', () => {
    expect(targetResolution('1080p')).toBe('4K');
  });
  it('720p implies 4K', () => {
    expect(targetResolution('720p')).toBe('4K');
  });
});

describe('seasonSearchQuery', () => {
  it('formats a single-digit season with a leading zero', () => {
    expect(seasonSearchQuery('Show Name', 2)).toBe('Show Name S02');
  });
  it('formats a double-digit season without truncation', () => {
    expect(seasonSearchQuery('Show Name', 12)).toBe('Show Name S12');
  });
});

describe('findCachedAlternative', () => {
  it('matches by imdb_id, same season, different resolution', () => {
    const target = { imdbId: 'tt1234567', title: 'Show Name', season: 2, excludeResolution: '4K' };
    const alt = makeItem({ resolution: '1080p' });
    const items = [makeItem({ resolution: '4K' }), alt, makeItem({ season: 3, resolution: '1080p' })];
    expect(findCachedAlternative(items, target)).toBe(alt);
  });

  it('falls back to normalized title match when target has no imdb_id', () => {
    const target = { imdbId: null, title: 'Show Name', season: 2, excludeResolution: '4K' };
    const alt = makeItem({ resolution: '1080p', imdb_id: null });
    const items = [alt];
    expect(findCachedAlternative(items, target)).toBe(alt);
  });

  it('never matches a different season', () => {
    const target = { imdbId: 'tt1234567', title: 'Show Name', season: 2, excludeResolution: '4K' };
    const items = [makeItem({ season: 3, resolution: '1080p' })];
    expect(findCachedAlternative(items, target)).toBeNull();
  });

  it('never matches a different show even with the same season', () => {
    const target = { imdbId: 'tt1234567', title: 'Show Name', season: 2, excludeResolution: '4K' };
    const items = [makeItem({ imdb_id: 'tt9999999', title: 'Different Show', resolution: '1080p' })];
    expect(findCachedAlternative(items, target)).toBeNull();
  });

  it('excludes items matching the excluded resolution', () => {
    const target = { imdbId: 'tt1234567', title: 'Show Name', season: 2, excludeResolution: '4K' };
    const items = [makeItem({ resolution: '4K' })];
    expect(findCachedAlternative(items, target)).toBeNull();
  });

  it('returns null when nothing qualifies', () => {
    const target = { imdbId: 'tt1234567', title: 'Show Name', season: 2, excludeResolution: '4K' };
    expect(findCachedAlternative([], target)).toBeNull();
  });

  it('does not cross-match when target has an imdb_id but the candidate does not', () => {
    const target = { imdbId: 'tt1234567', title: 'Show Name', season: 2, excludeResolution: '4K' };
    const items = [makeItem({ imdb_id: null, resolution: '1080p' })];
    expect(findCachedAlternative(items, target)).toBeNull();
  });
});
