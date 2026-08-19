import { describe, expect, it } from 'vitest';
import { safeHttpUrl } from './url';

describe('safeHttpUrl', () => {
  it('passes through ordinary release-page links', () => {
    expect(safeHttpUrl('https://example.test/a-release')).toBe('https://example.test/a-release');
    expect(safeHttpUrl('http://example.test/a-release')).toBe('http://example.test/a-release');
  });

  it('preserves query and fragment', () => {
    const u = 'https://example.test/r?id=7#links';
    expect(safeHttpUrl(u)).toBe(u);
  });

  // The reason this function exists: every url it sees was scraped from a
  // third-party page and stored verbatim, so binding one into an href without
  // a scheme check turns a stored string into script execution on click.
  it('refuses javascript: however it is dressed up', () => {
    expect(safeHttpUrl('javascript:alert(1)')).toBe('');
    expect(safeHttpUrl('JaVaScRiPt:alert(1)')).toBe('');
    expect(safeHttpUrl('  javascript:alert(1)')).toBe('');
  });

  it('refuses other non-http schemes', () => {
    expect(safeHttpUrl('data:text/html,<script>alert(1)</script>')).toBe('');
    expect(safeHttpUrl('file:///etc/passwd')).toBe('');
    expect(safeHttpUrl('vbscript:msgbox(1)')).toBe('');
  });

  it('returns empty for missing or unparsable values', () => {
    expect(safeHttpUrl(null)).toBe('');
    expect(safeHttpUrl(undefined)).toBe('');
    expect(safeHttpUrl('')).toBe('');
    expect(safeHttpUrl('/relative/path')).toBe('');
    expect(safeHttpUrl('not a url')).toBe('');
  });
});
