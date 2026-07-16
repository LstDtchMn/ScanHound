import { describe, it, expect } from 'vitest';
import {
  groupResults, computeSiblingCounts, isDuplicateGroup,
  groupSizeRange, groupDateRange, groupStatusSummary, groupFormats
} from './grouping';
import type { ScanResult } from './api/types';

const r = (over: Partial<ScanResult>): ScanResult => ({
  url: over.url ?? Math.random().toString(36), title: 'Dune', year: 2021, status: 'missing',
  resolution: '2160p', size: '20 GB', group_key: over.url ?? 'k', rating: null,
  poster_url: '', hdr: 'HDR10', posted_date: null, ...over
} as ScanResult);

describe('grouping', () => {
  it('groups same-title items preserving order', () => {
    const groups = groupResults([
      r({ title: 'Dune', url: 'a', group_key: 'dune|2021|S0' }),
      r({ title: 'Blade', url: 'b', group_key: 'blade|1982|S0' }),
      r({ title: 'Dune', url: 'c', group_key: 'dune|2021|S0' }),
    ]);
    expect(groups.map(g => g.title)).toEqual(['Dune', 'Blade']);
    expect(groups[0].items.map(i => i.url)).toEqual(['a', 'c']);
  });

  it('groups by group_key, not bare title (same-title/different-year items stay separate)', () => {
    const groups = groupResults([
      r({ title: 'Dune', year: 1984, url: 'a', group_key: 'dune|1984|S0' }),
      r({ title: 'Dune', year: 2021, url: 'b', group_key: 'dune|2021|S0' }),
      r({ title: 'Dune', year: 2021, url: 'c', group_key: 'dune|2021|S0' }),
    ]);
    expect(groups.length).toBe(2);
    expect(groups[0].items.map(i => i.url)).toEqual(['a']);
    expect(groups[1].items.map(i => i.url)).toEqual(['b', 'c']);
  });

  it('groupResults falls back to a composite key for legacy rows with no group_key', () => {
    const groups = groupResults([
      r({ title: 'Dune', year: 1984, season: null, url: 'a', group_key: '' as unknown as string }),
      r({ title: 'Dune', year: 2021, season: null, url: 'b', group_key: '' as unknown as string }),
      r({ title: 'Dune', year: 1984, season: null, url: 'c', group_key: '' as unknown as string }),
    ]);
    expect(groups.length).toBe(2);
    expect(groups[0].items.map(i => i.url)).toEqual(['a', 'c']);
    expect(groups[1].items.map(i => i.url)).toEqual(['b']);
  });

  it('groupResults gives every group a unique key even when titles collide (Dune 1984 vs 2021)', () => {
    const groups = groupResults([
      r({ title: 'Dune', year: 1984, url: 'a', group_key: 'dune|1984|S0' }),
      r({ title: 'Dune', year: 2021, url: 'b', group_key: 'dune|2021|S0' }),
    ]);
    expect(groups.length).toBe(2);
    expect(groups[0].title).toBe('Dune');
    expect(groups[1].title).toBe('Dune');
    // Both groups share the display title, but must have distinct keys so a
    // Svelte keyed {#each (group.key)} never sees a duplicate key.
    expect(groups[0].key).not.toBe(groups[1].key);
    expect(new Set(groups.map(g => g.key)).size).toBe(2);
    expect(groups[0].key).toBe('dune|1984|S0');
    expect(groups[1].key).toBe('dune|2021|S0');
  });

  it('computeSiblingCounts uses server titleCounts in paged mode when present', () => {
    const counts = computeSiblingCounts([r({ title: 'Dune', url: 'a', group_key: 'dune|2021|S0' })], { 'dune|2021|S0': 3 }, true);
    expect(counts.get('dune|2021|S0')).toBe(3);
  });

  it('computeSiblingCounts falls back to a local tally over the full filtered set (paged, empty titleCounts)', () => {
    const counts = computeSiblingCounts(
      [
        r({ title: 'Dune', url: 'a', group_key: 'dune|2021|S0' }),
        r({ title: 'Dune', url: 'b', group_key: 'dune|2021|S0' }),
        r({ title: 'Blade', url: 'c', group_key: 'blade|1982|S0' }),
      ],
      {}, true
    );
    expect(counts.get('dune|2021|S0')).toBe(2);
    expect(counts.get('blade|1982|S0')).toBe(1);
  });

  it('computeSiblingCounts always uses a local tally in live mode, keyed by group_key not bare title', () => {
    const counts = computeSiblingCounts(
      [r({ title: 'Dune', url: 'a', group_key: 'dune|2021|S0' }), r({ title: 'Dune', url: 'b', group_key: 'dune|2021|S0' })],
      { 'dune|2021|S0': 99 }, false
    );
    expect(counts.get('dune|2021|S0')).toBe(2); // titleCounts ignored outside paged mode
  });

  it('computeSiblingCounts does not conflate a same-title/different-year item with unrelated siblings (live mode)', () => {
    // A lone Dune 1984 alongside two Dune 2021 releases must NOT be tallied
    // together just because they share a display title.
    const items = [
      r({ title: 'Dune', year: 1984, url: 'a', group_key: 'dune|1984|S0' }),
      r({ title: 'Dune', year: 2021, url: 'b', group_key: 'dune|2021|S0' }),
      r({ title: 'Dune', year: 2021, url: 'c', group_key: 'dune|2021|S0' }),
    ];
    const counts = computeSiblingCounts(items, {}, false);
    expect(counts.get('dune|1984|S0')).toBe(1);
    expect(counts.get('dune|2021|S0')).toBe(2);
    const groups = groupResults(items);
    const dune1984 = groups.find(g => g.key === 'dune|1984|S0')!;
    expect(isDuplicateGroup(dune1984, counts)).toBe(false); // must NOT be flagged as a duplicate
  });

  it('isDuplicateGroup thresholds on siblingCounts (keyed by group.key), falling back to items.length', () => {
    const group = { key: 'dune|2021|S0', title: 'Dune', items: [r({ url: 'a' })] };
    expect(isDuplicateGroup(group, new Map([['dune|2021|S0', 3]]))).toBe(true);
    expect(isDuplicateGroup(group, new Map())).toBe(false); // 1 item, no count entry
    const twoItemGroup = { key: 'dune|2021|S0', title: 'Dune', items: [r({ url: 'a' }), r({ url: 'b' })] };
    expect(isDuplicateGroup(twoItemGroup, new Map())).toBe(true); // falls back to items.length
  });

  it('groupFormats aggregates resolutions (2160p→4K) + dv/hdr flags, ordered', () => {
    const f = groupFormats([
      r({ resolution: '1080p', hdr: 'SDR', dovi: false } as Partial<ScanResult>),
      r({ resolution: '2160p', hdr: '', dovi: true } as Partial<ScanResult>)
    ]);
    expect(f.res).toEqual(['4K', '1080p']);
    expect(f.dv).toBe(true);
    expect(f.hdr).toBe(false); // SDR / dovi-covered HDR don't count
  });

  it('groupSizeRange returns a single size when sizes are effectively equal, else a range', () => {
    expect(groupSizeRange([r({ size: '20 GB' }), r({ size: '20 GB' })])).toBe('20 GB');
    expect(groupSizeRange([r({ size: '10 GB' }), r({ size: '25 GB' })])).toBe('10.0 GB – 25.0 GB');
    expect(groupSizeRange([r({ size: undefined as unknown as string })])).toBe('');
  });

  it('groupDateRange sorts chronologically (not lexically) and formats a range', () => {
    const items = [
      r({ posted_date: 'July 3, 2026 at 09:00 AM' }),
      r({ posted_date: 'June 25, 2026 at 02:55 PM' })
    ];
    // Plain string sort would put "July" before "June" — must not happen.
    expect(groupDateRange(items)).toBe('Jun 25 – Jul 3');
    expect(groupDateRange([r({ posted_date: null })])).toBe('');
  });

  it('groupStatusSummary orders by severity then appends unknown statuses', () => {
    const summary = groupStatusSummary([
      r({ status: 'in_library' }), r({ status: 'missing' }), r({ status: 'missing' }), r({ status: 'weird_status' })
    ]);
    expect(summary.map(s => s.status)).toEqual(['missing', 'in_library', 'weird_status']);
    expect(summary.find(s => s.status === 'missing')?.count).toBe(2);
  });
});
