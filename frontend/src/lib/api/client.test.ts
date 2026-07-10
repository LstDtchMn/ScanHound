import { describe, it, expect, vi, beforeEach } from 'vitest';
import { api } from './client';

describe('conflict apis', () => {
  beforeEach(() => { vi.restoreAllMocks(); });
  it('applyRename sends conflict_strategy body', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(JSON.stringify({ ok: true }), { status: 200, headers: { 'content-type': 'application/json' } }));
    await api.applyRename(5, { conflict_strategy: 'overwrite' });
    const [, opts] = fetchMock.mock.calls[0];
    expect(JSON.parse(opts!.body as string)).toEqual({ conflict_strategy: 'overwrite' });
  });
  it('applyRename with no body sends none', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(JSON.stringify({ ok: true }), { status: 200, headers: { 'content-type': 'application/json' } }));
    await api.applyRename(5);
    const [, opts] = fetchMock.mock.calls[0];
    expect(opts!.body).toBeUndefined();
  });
});
