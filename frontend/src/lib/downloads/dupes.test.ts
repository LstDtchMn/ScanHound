import { describe, it, expect } from 'vitest';
import { normalizeTitle, resRank, groupDownloads } from './dupes';
import type { DownloadResult } from '$lib/api/types';

function r(over: Partial<DownloadResult>): DownloadResult {
  return { name: 'X [1080p]', title: 'X', host: 'rg.net', bytes_total: 100, bytes_loaded: 0,
    downloaded: 0, extraction: 'na', state: 'downloading', error: null, updated_at: '', ...over };
}

describe('normalizeTitle', () => {
  it('lowercases and strips year + punctuation', () => {
    expect(normalizeTitle('Killing Faith (2025)')).toBe('killing faith');
    expect(normalizeTitle('Dr. Quinn, Medicine Woman')).toBe('dr quinn medicine woman');
  });

  it('does not collapse a bare-year title to an empty string', () => {
    // Regression: the whole title IS a year (e.g. the movies "1917", "2012",
    // "1984", "2010") — stripping standalone years must not erase them, or
    // unrelated movies would all normalize to the same '' key.
    expect(normalizeTitle('1917')).not.toBe('');
    expect(normalizeTitle('2012')).not.toBe('');
    expect(normalizeTitle('1917')).not.toBe(normalizeTitle('2012'));
  });

  it('normalizes a title with no year at all', () => {
    expect(normalizeTitle('The Matrix')).toBe('the matrix');
  });
});

describe('resRank', () => {
  it('ranks 4K > 1080p > 720p > other', () => {
    expect(resRank('Foo [4K]')).toBeGreaterThan(resRank('Foo [1080p]'));
    expect(resRank('Foo [1080p]')).toBeGreaterThan(resRank('Foo [720p]'));
  });
});

describe('groupDownloads', () => {
  it('groups same-title releases and flags duplicates, picking best', () => {
    const items = [
      r({ name: 'Heat (1995) [1080p]', title: 'Heat', bytes_total: 10 }),
      r({ name: 'Heat (1995) [4K]', title: 'Heat', bytes_total: 40 }),
      r({ name: 'Solo (2018) [1080p]', title: 'Solo' }),
    ];
    const groups = groupDownloads(items);
    const heat = groups.find((g) => g.title === 'Heat')!;
    expect(heat.items).toHaveLength(2);
    expect(heat.isDuplicate).toBe(true);
    expect(heat.best.name).toBe('Heat (1995) [4K]');   // higher res wins
    const solo = groups.find((g) => g.title === 'Solo')!;
    expect(solo.isDuplicate).toBe(false);
  });

  it('flags exact-same-name packages as duplicate', () => {
    const items = [r({ name: 'Foo [1080p]', title: 'Foo' }), r({ name: 'Foo [1080p]', title: 'Foo' })];
    const g = groupDownloads(items)[0];
    expect(g.isDuplicate).toBe(true);
    expect(g.items).toHaveLength(2);
  });

  it('breaks ties by size when resRank is equal', () => {
    const items = [
      r({ name: 'Foo (2020) [1080p]', title: 'Foo', bytes_total: 500 }),
      r({ name: 'Foo (2020) [1080p]', title: 'Foo', bytes_total: 2000 }),
    ];
    const g = groupDownloads(items)[0];
    expect(g.best.bytes_total).toBe(2000);
  });
});
