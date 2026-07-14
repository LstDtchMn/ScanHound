import { describe, expect, it } from 'vitest';
import { CATEGORY_VARIANT, POSTER_CATEGORIES, checkedAgo, categoryColor } from './pipelineDisplay';

describe('CATEGORY_VARIANT', () => {
  it('covers all nine categories', () => {
    expect(Object.keys(CATEGORY_VARIANT)).toHaveLength(9);
  });
  it('maps failures to error and verified to success', () => {
    expect(CATEGORY_VARIANT.verified).toBe('success');
    expect(CATEGORY_VARIANT.download_failed).toBe('error');
    expect(CATEGORY_VARIANT.rename_failed).toBe('error');
    expect(CATEGORY_VARIANT.not_in_plex).toBe('error');
  });
});

describe('POSTER_CATEGORIES', () => {
  it('excludes pre-rename categories', () => {
    expect(POSTER_CATEGORIES.has('downloading')).toBe(false);
    expect(POSTER_CATEGORIES.has('never_started')).toBe(false);
    expect(POSTER_CATEGORIES.has('unknown')).toBe(false);
  });
  it('includes post-rename categories', () => {
    for (const c of ['pending_rename', 'rename_failed', 'awaiting_plex_refresh', 'verified', 'not_in_plex']) {
      expect(POSTER_CATEGORIES.has(c)).toBe(true);
    }
  });
});

describe('checkedAgo', () => {
  const now = new Date('2026-07-12T12:00:00Z');
  it('minutes', () => expect(checkedAgo('2026-07-12 11:55:00', now)).toBe('5m ago'));
  it('hours', () => expect(checkedAgo('2026-07-12 09:00:00', now)).toBe('3h ago'));
  it('days', () => expect(checkedAgo('2026-07-10 12:00:00', now)).toBe('2d ago'));
  it('clamps future skew to 0m', () => expect(checkedAgo('2026-07-12 12:05:00', now)).toBe('0m ago'));
  it('empty timestamp renders nothing, not NaN', () => expect(checkedAgo('', now)).toBe(''));
  it('malformed timestamp renders nothing, not NaN', () => {
    expect(checkedAgo('garbage', now)).toBe('');
    expect(checkedAgo('2026-7-12 11:59:00', now)).toBe(''); // non-zero-padded month
  });
  it('parses ISO-8601 timestamps with a UTC offset (rename_jobs.processed_at '
    + 'format, from Python\'s datetime.now(timezone.utc).isoformat())', () => {
    expect(checkedAgo('2026-07-12T11:55:00.000000+00:00', now)).toBe('5m ago');
    expect(checkedAgo('2026-07-12T09:00:00.000000+00:00', now)).toBe('3h ago');
    // Real _now() output carries fractional seconds; confirm they don't break parsing.
    expect(checkedAgo('2026-07-12T11:54:00.946949+00:00', now)).toBe('5m ago');
  });
  it('parses ISO-8601 timestamps with a Z suffix', () => {
    expect(checkedAgo('2026-07-12T09:00:00Z', now)).toBe('3h ago');
  });
  it('parses ISO-8601 timestamps with a non-UTC offset', () => {
    // 14:55:00+03:00 == 11:55:00Z == 5 minutes before `now`.
    expect(checkedAgo('2026-07-12T14:55:00+03:00', now)).toBe('5m ago');
  });
});

describe('categoryColor', () => {
  it('maps known categories to their CSS var', () => {
    expect(categoryColor('verified')).toBe('var(--success)');
    expect(categoryColor('downloading')).toBe('var(--accent)');
    expect(categoryColor('rename_failed')).toBe('var(--error)');
  });
  it('falls back to secondary text color for unknown/null categories', () => {
    expect(categoryColor('unknown')).toBe('var(--text-secondary)');
    expect(categoryColor(null)).toBe('var(--text-secondary)');
  });
});
