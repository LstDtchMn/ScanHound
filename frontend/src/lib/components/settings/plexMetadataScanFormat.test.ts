import { describe, expect, it } from 'vitest';
import { formatEta } from './plexMetadataScanFormat';

describe('formatEta', () => {
  it('returns -- for null', () => {
    expect(formatEta(null)).toBe('--');
  });

  it('formats seconds under a minute', () => {
    expect(formatEta(45)).toBe('45s');
  });

  it('formats minutes and seconds', () => {
    expect(formatEta(65)).toBe('1m 5s');
  });

  it('formats hours and minutes, dropping seconds', () => {
    expect(formatEta(3661)).toBe('1h 1m');
  });

  it('rounds down fractional seconds', () => {
    expect(formatEta(45.9)).toBe('45s');
  });
});
