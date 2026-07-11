import { describe, it, expect } from 'vitest';
import { filterSkipped, relativeTime, type SkippedItem } from './skipped';

const item = (o: Partial<SkippedItem>): SkippedItem => ({
  url: 'u', title: 'A Movie', dismissed_at: null, ...o,
});

describe('filterSkipped', () => {
  const items = [
    item({ url: 'a', title: 'Sinners' }),
    item({ url: 'b', title: 'The Batman' }),
    item({ url: 'c', title: null }),
  ];
  it('empty query returns all', () => {
    expect(filterSkipped(items, '')).toHaveLength(3);
    expect(filterSkipped(items, '   ')).toHaveLength(3);
  });
  it('case-insensitive title substring match', () => {
    expect(filterSkipped(items, 'batman').map((i) => i.url)).toEqual(['b']);
    expect(filterSkipped(items, 'SIN').map((i) => i.url)).toEqual(['a']);
  });
  it('falls back to url match when title is null', () => {
    expect(filterSkipped(items, 'c').map((i) => i.url)).toEqual(['c']);
  });
  it('no match returns empty', () => {
    expect(filterSkipped(items, 'zzz')).toEqual([]);
  });
});

describe('relativeTime', () => {
  const now = Date.parse('2026-07-11T12:00:00Z');
  it('null / empty returns empty string', () => {
    expect(relativeTime(null, now)).toBe('');
    expect(relativeTime('', now)).toBe('');
  });
  it('under a minute is "just now"', () => {
    expect(relativeTime('2026-07-11T11:59:30Z', now)).toBe('just now');
  });
  it('hours ago', () => {
    expect(relativeTime('2026-07-11T09:00:00Z', now)).toBe('3h ago');
  });
  it('days ago', () => {
    expect(relativeTime('2026-07-08T12:00:00Z', now)).toBe('3d ago');
  });
  it('older than 30d returns a date (not "Nd ago")', () => {
    expect(relativeTime('2026-01-01T12:00:00Z', now)).not.toMatch(/ago/);
  });
});
