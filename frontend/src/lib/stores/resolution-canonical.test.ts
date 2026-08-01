/**
 * Resolution facet canonicalisation — frontend twin of
 * backend/tests/test_resolution_canonical.py. Both sides filter independently,
 * so a fix on one side alone leaves the other wrong; these suites are
 * deliberately parallel so a future divergence shows up as one of them failing.
 *
 * Regression cover for the 2026-07-30 finding: UHD is stored as both '4K' and
 * '2160p', every filter compared the raw string, and the chip is labelled '4K'
 * — so 242 of 395 4K movies (61%, measured on the production DB) could not be
 * reached through the 4K facet. The parser writes '2160p', so that share was
 * growing. 1080p was unaffected because parser and chip agree on the spelling,
 * which is why the bug went unnoticed.
 */
import { describe, expect, it } from 'vitest';
import { canonicalResolution, resolutionKeysFor } from './results';
import type { ScanResult } from '$lib/api/types';

const movie = (resolution: string | null): ScanResult =>
  ({ url: 'u', title: 't', resolution } as unknown as ScanResult);
const tvShow = (resolution: string | null): ScanResult =>
  ({ url: 'u', title: 't', resolution, category: 'tv' } as unknown as ScanResult);

describe('canonicalResolution', () => {
  it.each([
    ['2160p', '4K'],
    ['4K', '4K'],
    ['4k', '4K'],
    ['UHD', '4K'],
    ['uhd', '4K'],
    ['1080p', '1080p'],
    ['1080i', '1080p'],
    ['720p', '720p'],
    ['480p', '480p']
  ])('folds %s to %s', (raw, expected) => {
    expect(canonicalResolution(raw)).toBe(expected);
  });

  it('returns null for an absent resolution', () => {
    expect(canonicalResolution(null)).toBeNull();
    expect(canonicalResolution(undefined)).toBeNull();
    expect(canonicalResolution('')).toBeNull();
  });

  it('passes an unknown spelling through unchanged', () => {
    // Mapping the unknown to null would recreate the original defect in a new
    // form — items that quietly cannot be filtered at all.
    expect(canonicalResolution('1440p')).toBe('1440p');
    expect(canonicalResolution('?')).toBe('?');
  });

  it('ignores case and surrounding whitespace', () => {
    expect(canonicalResolution('  2160P  ')).toBe('4K');
  });
});

describe('resolutionKeysFor', () => {
  it('keys a movie by its canonical resolution', () => {
    expect(resolutionKeysFor(movie('2160p'))).toEqual(['4K']);
    expect(resolutionKeysFor(movie('4K'))).toEqual(['4K']);
  });

  it('THE REGRESSION: the 4K chip matches both spellings', () => {
    const selected = new Set(['4K']);
    expect(resolutionKeysFor(movie('4K')).some((k) => selected.has(k))).toBe(true);
    // This assertion failed before the fix.
    expect(resolutionKeysFor(movie('2160p')).some((k) => selected.has(k))).toBe(true);
  });

  it('does not over-merge: a 4K item is not matched by the 1080p chip', () => {
    const selected = new Set(['1080p']);
    expect(resolutionKeysFor(movie('2160p')).some((k) => selected.has(k))).toBe(false);
  });

  it('leaves the TV rule untouched — TV keys only as TV', () => {
    // A separate, deliberate design decision (4K/1080p chips are movies-only),
    // explicitly NOT changed by this defect fix.
    expect(resolutionKeysFor(tvShow('2160p'))).toEqual(['TV']);
    expect(resolutionKeysFor(tvShow('1080p'))).toEqual(['TV']);
  });

  it('yields no keys when resolution is missing', () => {
    expect(resolutionKeysFor(movie(null))).toEqual([]);
  });
});
