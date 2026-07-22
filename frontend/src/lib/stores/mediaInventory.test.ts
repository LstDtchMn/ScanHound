import { beforeEach, describe, expect, it, vi } from 'vitest';

const getMediaInventory = vi.fn().mockResolvedValue({
  items: [], total: 0, page: 1, page_size: 50
});
const getMediaInventoryFacets = vi.fn().mockResolvedValue({});

vi.mock('$lib/api/client', () => ({
  api: { getMediaInventory, getMediaInventoryFacets }
}));

describe('media inventory store', () => {
  beforeEach(() => {
    vi.resetModules();
    getMediaInventory.mockClear();
    getMediaInventoryFacets.mockClear();
  });

  it('serializes evidence filters into the inventory request', async () => {
    const store = await import('./mediaInventory');

    await store.setInventoryFilters({
      hdr10plus_state: 'present', dv_layer: 'fel', discrepancy: 'verified'
    });

    expect(getMediaInventory).toHaveBeenCalledWith(expect.objectContaining({
      hdr10plus_state: 'present', dv_layer: 'fel', discrepancy: 'verified', page: '1'
    }));
  });

  it('does not serialize empty filters', async () => {
    const store = await import('./mediaInventory');

    await store.setInventoryFilters({ hdr: '', scan_state: 'failed' });

    const params = getMediaInventory.mock.calls[0][0] as Record<string, string>;
    expect(params.hdr).toBeUndefined();
    expect(params.scan_state).toBe('failed');
  });
});
