import { describe, it, expect, vi, beforeEach } from 'vitest';
import { get } from 'svelte/store';

vi.mock('$lib/api/client', () => ({
  api: { scanStart: vi.fn().mockResolvedValue({ status: 'started' }) }
}));
vi.mock('$lib/stores/results', () => ({
  clearResults: vi.fn(),
}));

import { selectedScanSource, searchThisSite } from './scanner';
import { clearResults } from '$lib/stores/results';

describe('selectedScanSource', () => {
  it('defaults to HDEncode', () => {
    expect(get(selectedScanSource)).toBe('HDEncode');
  });
});

describe('searchThisSite', () => {
  beforeEach(() => vi.clearAllMocks());
  it('clears results then starts a search scan for the given query and source', async () => {
    searchThisSite('Journey to the Far Side of the Sun', 'DDLBase');
    expect(clearResults).toHaveBeenCalled();
    const { api } = await import('$lib/api/client');
    expect(api.scanStart).toHaveBeenCalledWith(
      'search', 'Journey to the Far Side of the Sun', 1, 'DDLBase', undefined
    );
  });
});
