import { describe, it, expect, vi, beforeEach } from 'vitest';

// The store module registers connection.on(...) handlers at import time, so we
// must stub ./connection BEFORE importing the store. We capture every handler
// keyed by event name so the test can invoke them directly.
const handlers: Record<string, (data: unknown) => void> = {};
vi.mock('$lib/stores/connection', () => ({
  connection: {
    on: (event: string, cb: (data: unknown) => void) => { handlers[event] = cb; return () => {}; },
    onReconnect: (_cb: () => void) => () => {}
  }
}));
vi.mock('$lib/api/client', () => ({
  api: { getDvScans: vi.fn().mockResolvedValue({ scans: [], counts: {} }) }
}));

describe('DV sync stores', () => {
  beforeEach(() => {
    for (const k of Object.keys(handlers)) delete handlers[k];
    // The store module registers its connection.on(...) handlers once, at
    // module-evaluation time. Vitest caches ES module imports per test file,
    // so a plain `await import('./renames')` in a later test would return the
    // already-evaluated module and never re-register into the freshly-cleared
    // `handlers` map above. Reset the module registry so each test's import
    // re-runs the module body (and thus re-registers the handlers).
    vi.resetModules();
  });

  it('registers dv:sync_progress and dv:sync_done handlers', async () => {
    await import('./renames');
    expect(typeof handlers['dv:sync_progress']).toBe('function');
    expect(typeof handlers['dv:sync_done']).toBe('function');
  });

  it('dv:sync_done clears dvSyncRunning and stores the result', async () => {
    const mod = await import('./renames');
    const { get } = await import('svelte/store');
    mod.dvSyncRunning.set(true);
    handlers['dv:sync_done']({ total: 3, added: 1, removed: 0, matched: 2, dry_run: false });
    expect(get(mod.dvSyncRunning)).toBe(false);
    expect(get(mod.dvSyncResult)).toEqual({ total: 3, added: 1, removed: 0, matched: 2, dry_run: false });
    expect(get(mod.dvSyncProgress)).toBeNull();
  });

  it('dv:sync_done with an error payload stores the error', async () => {
    const mod = await import('./renames');
    const { get } = await import('svelte/store');
    mod.dvSyncRunning.set(true);
    handlers['dv:sync_done']({ error: 'Plex not initialized' });
    expect(get(mod.dvSyncRunning)).toBe(false);
    expect(get(mod.dvSyncResult)).toEqual({ error: 'Plex not initialized' });
  });

  it('dv:sync_progress updates dvSyncProgress', async () => {
    const mod = await import('./renames');
    const { get } = await import('svelte/store');
    handlers['dv:sync_progress']({ done: 3, total: 10 });
    expect(get(mod.dvSyncProgress)).toEqual({ done: 3, total: 10 });
  });

  it('registers a dv:conflict_scan_done handler distinct from dv:scan_done', async () => {
    await import('./renames');
    expect(typeof handlers['dv:conflict_scan_done']).toBe('function');
  });

  it('dv:conflict_scan_done bumps dvScanTick without touching the full-library DV scan stores', async () => {
    const mod = await import('./renames');
    const { get } = await import('svelte/store');
    mod.dvScanRunning.set(true);
    expect(get(mod.dvScanTick)).toBe(0);
    handlers['dv:conflict_scan_done']({ job_id: 1, scanned: 2 });
    expect(get(mod.dvScanTick)).toBe(1);
    handlers['dv:conflict_scan_done']({ job_id: 1, scanned: 2 });
    expect(get(mod.dvScanTick)).toBe(2);
    // Untouched — this event must never disturb the full-library scan panel.
    expect(get(mod.dvScanRunning)).toBe(true);
    expect(get(mod.dvScanResult)).toBeNull();
  });
});
