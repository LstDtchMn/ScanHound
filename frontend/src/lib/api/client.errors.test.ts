import { describe, expect, it } from 'vitest';
import { formatErrorDetail } from './client';

describe('formatErrorDetail', () => {
  it('formats a structured closed public error', () => {
    expect(formatErrorDetail({
      code: 'scrape_failed',
      message: 'Links could not be retrieved.',
      correlation_id: 'abc123'
    })).toBe('Links could not be retrieved. (Reference: abc123)');
  });

  it('preserves bounded legacy string validation errors', () => {
    expect(formatErrorDetail('Title must be at least 2 characters'))
      .toBe('Title must be at least 2 characters');
  });

  it('does not stringify unknown objects', () => {
    expect(formatErrorDetail({ raw: '/private/secret' })).toBeUndefined();
  });
});
