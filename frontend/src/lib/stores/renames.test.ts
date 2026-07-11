import { describe, it, expect, vi, beforeEach } from 'vitest';

// The store module registers connection.on(...)/onReconnect(...) handlers at
// import time, so we must stub ./connection BEFORE importing the store. We
// capture every handler (keyed by event name for `on`, in a list for
// `onReconnect`) so tests can invoke them directly.
const handlers: Record<string, (data: unknown) => void> = {};
const reconnectHandlers: Array<() => void> = [];
vi.mock('$lib/stores/connection', () => ({
  connection: {
    on: (event: string, cb: (data: unknown) => void) => { handlers[event] = cb; return () => {}; },
    onReconnect: (cb: () => void) => { reconnectHandlers.push(cb); return () => {}; }
  }
}));
const getRenameJobs = vi.fn().mockResolvedValue({ jobs: [] });
const getRenameStatus = vi.fn().mockResolvedValue({
  enabled: true, require_confirmation: true, confidence_threshold: 70,
  move_method: 'move', llm_enabled: false, counts: {}, needs_review: 0,
});
vi.mock('$lib/api/client', () => ({
  api: {
    getDvScans: vi.fn().mockResolvedValue({ scans: [], counts: {} }),
    getRenameJobs,
    getRenameStatus,
  }
}));

describe('DV sync stores', () => {
  beforeEach(() => {
    for (const k of Object.keys(handlers)) delete handlers[k];
    reconnectHandlers.length = 0;
    getRenameJobs.mockReset().mockResolvedValue({ jobs: [] });
    getRenameStatus.mockReset().mockResolvedValue({
      enabled: true, require_confirmation: true, confidence_threshold: 70,
      move_method: 'move', llm_enabled: false, counts: {}, needs_review: 0,
    });
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

describe('Apply progress: speed/ETA fields + staleness fixes', () => {
  beforeEach(() => {
    for (const k of Object.keys(handlers)) delete handlers[k];
    reconnectHandlers.length = 0;
    getRenameJobs.mockReset().mockResolvedValue({ jobs: [] });
    getRenameStatus.mockReset().mockResolvedValue({
      enabled: true, require_confirmation: true, confidence_threshold: 70,
      move_method: 'move', llm_enabled: false, counts: {}, needs_review: 0,
    });
    vi.resetModules();
  });

  it('rename:progress stores bytes_per_sec/eta_seconds and a receipt timestamp', async () => {
    const mod = await import('./renames');
    const { get } = await import('svelte/store');
    const before = Date.now();
    handlers['rename:progress']({
      id: 7, pct: 42, bytes_done: 4200, bytes_total: 10000,
      bytes_per_sec: 512_000, eta_seconds: 11.3,
    });
    const entry = get(mod.renameProgress).get(7);
    expect(entry).toBeDefined();
    expect(entry!.bytes_per_sec).toBe(512_000);
    expect(entry!.eta_seconds).toBeCloseTo(11.3);
    expect(entry!.updatedAt).toBeGreaterThanOrEqual(before);
  });

  it('rename:progress with no rate yet stores null bytes_per_sec/eta_seconds, not undefined', async () => {
    const mod = await import('./renames');
    const { get } = await import('svelte/store');
    handlers['rename:progress']({ id: 7, pct: 5, bytes_done: 100, bytes_total: 10000 });
    const entry = get(mod.renameProgress).get(7);
    expect(entry!.bytes_per_sec).toBeNull();
    expect(entry!.eta_seconds).toBeNull();
  });

  it('rename:job leaving "applying" clears that job\'s progress entry (completion always wins over a stale tick)', async () => {
    const mod = await import('./renames');
    const { get } = await import('svelte/store');
    handlers['rename:progress']({ id: 7, pct: 94, bytes_done: 9400, bytes_total: 10000 });
    expect(get(mod.renameProgress).has(7)).toBe(true);
    // The terminal status-change broadcast is a separate channel from
    // progress ticks — it must clear the stale bar even if the job's own
    // 100% progress tick was never received.
    handlers['rename:job']({ id: 7, status: 'applied' });
    expect(get(mod.renameProgress).has(7)).toBe(false);
  });

  it('registers a connection.onReconnect handler', async () => {
    // renames.ts transitively imports stores/results.ts (for `persisted`),
    // which registers its own onReconnect handler too — so this only
    // asserts renames.ts's handler exists among them, not an exact count.
    await import('./renames');
    expect(reconnectHandlers.length).toBeGreaterThanOrEqual(1);
  });

  it('resyncAfterReconnect re-fetches jobs/status and drops progress for jobs no longer applying server-side', async () => {
    const mod = await import('./renames');
    const { get } = await import('svelte/store');
    // Simulate: job 1 finished while this client was disconnected (its
    // terminal rename:job broadcast was missed — the exact bug being
    // fixed); job 2 is a genuinely still-running apply.
    mod.renameProgress.set(new Map([
      [1, { pct: 94, bytes_done: 940, bytes_total: 1000, bytes_per_sec: 100, eta_seconds: 1, updatedAt: Date.now() }],
      [2, { pct: 10, bytes_done: 10, bytes_total: 100, bytes_per_sec: 50, eta_seconds: 2, updatedAt: Date.now() }],
    ]));
    getRenameJobs.mockResolvedValue({
      jobs: [
        { id: 1, status: 'applied' },
        { id: 2, status: 'applying' },
      ],
    });

    await mod.resyncAfterReconnect();

    expect(getRenameJobs).toHaveBeenCalled();
    expect(getRenameStatus).toHaveBeenCalled();
    const progress = get(mod.renameProgress);
    expect(progress.has(1)).toBe(false); // stale — dropped
    expect(progress.has(2)).toBe(true);  // still applying — kept
    expect(get(mod.renameJobs)).toEqual([
      { id: 1, status: 'applied' },
      { id: 2, status: 'applying' },
    ]);
  });

  it('resyncAfterReconnect clears the queue banner and any stuck "Stopping…" state', async () => {
    const mod = await import('./renames');
    const { get } = await import('svelte/store');
    mod.renameQueue.set({ done: 1, total: 3, current_title: 'Some Movie' });
    mod.applyCancelling.set(true);

    await mod.resyncAfterReconnect();

    expect(get(mod.renameQueue)).toBeNull();
    expect(get(mod.applyCancelling)).toBe(false);
  });

  it('the registered reconnect handler actually triggers a resync (end-to-end wiring)', async () => {
    const mod = await import('./renames');
    const { get } = await import('svelte/store');
    mod.renameProgress.set(new Map([
      [9, { pct: 94, bytes_done: 9, bytes_total: 10, bytes_per_sec: null, eta_seconds: null, updatedAt: Date.now() }],
    ]));
    getRenameJobs.mockResolvedValue({ jobs: [{ id: 9, status: 'applied' }] });

    expect(reconnectHandlers.length).toBeGreaterThanOrEqual(1);
    // Mirrors connection.ts's real fan-out (`reconnectHandlers.forEach((fn) => fn())`)
    // — fire every registered handler, not just renames.ts's own.
    reconnectHandlers.forEach((fn) => fn());
    // resyncAfterReconnect is async and connection.ts's real onReconnect
    // fan-out doesn't await handlers either (fire-and-forget) — flush
    // pending microtasks the same way.
    await new Promise((r) => setTimeout(r, 0));

    expect(get(mod.renameProgress).has(9)).toBe(false);
  });
});
