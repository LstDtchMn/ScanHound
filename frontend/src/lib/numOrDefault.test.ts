import { describe, it, expect } from 'vitest';
import { numOrDefault } from './numOrDefault';

describe('numOrDefault', () => {
  it('persists a legitimately-typed 0 instead of falling back to the default', () => {
    expect(numOrDefault('0', 30)).toBe(0);
  });

  it('falls back to the default on an empty string', () => {
    expect(numOrDefault('', 30)).toBe(30);
  });

  it('falls back to the default on unparseable input', () => {
    expect(numOrDefault('abc', 30)).toBe(30);
  });

  it('parses a normal positive value', () => {
    expect(numOrDefault('45', 30)).toBe(45);
  });
});
