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
    const groups = groupResults([r({ title: 'Dune', url: 'a' }), r({ title: 'Blade', url: 'b' }), r({ title: 'Dune', url: 'c' })]);
    expect(groups.map(g => g.title)).toEqual(['Dune', 'Blade']);
    expect(groups[0].items.map(i => i.url)).toEqual(['a', 'c']);
  });

  it('computeSiblingCounts uses server titleCounts in paged mode when present', () => {
    const counts = computeSiblingCounts([r({ title: 'Dune', url: 'a' })], { Dune: 3 }, true);
    expect(counts.get('Dune')).toBe(3);
  });

  it('computeSiblingCounts falls back to a local tally over the full filtered set (paged, empty titleCounts)', () => {
    const counts = computeSiblingCounts(
      [r({ title: 'Dune', url: 'a' }), r({ title: 'Dune', url: 'b' }), r({ title: 'Blade', url: 'c' })],
      {}, true
    );
    expect(counts.get('Dune')).toBe(2);
    expect(counts.get('Blade')).toBe(1);
  });

  it('computeSiblingCounts always uses a local tally in live mode', () => {
    const counts = computeSiblingCounts(
      [r({ title: 'Dune', url: 'a' }), r({ title: 'Dune', url: 'b' })],
      { Dune: 99 }, false
    );
    expect(counts.get('Dune')).toBe(2); // titleCounts ignored outside paged mode
  });

  it('isDuplicateGroup thresholds on siblingCounts, falling back to items.length', () => {
    const group = { title: 'Dune', items: [r({ url: 'a' })] };
    expect(isDuplicateGroup(group, new Map([['Dune', 3]]))).toBe(true);
    expect(isDuplicateGroup(group, new Map())).toBe(false); // 1 item, no count entry
    const twoItemGroup = { title: 'Dune', items: [r({ url: 'a' }), r({ url: 'b' })] };
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
