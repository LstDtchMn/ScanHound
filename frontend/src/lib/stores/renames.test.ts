import { describe, it, expect, vi, beforeEach } from 'vitest';
import type { RenameJob } from '$lib/api/types';

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

/** Makes `getRenameJobs` behave like the real `GET /rename/jobs` endpoint:
 *  an unfiltered call returns `page` (simulating the general, capped,
 *  detected_at-ordered listing) and a `status=` call filters `allJobs` by
 *  status (simulating the backend's real `WHERE status = ?` query, which is
 *  authoritative regardless of the general page's cap). Letting `page` and
 *  `allJobs` differ is what lets tests simulate "a job is genuinely still
 *  applying but fell off the capped/paginated general listing". */
function mockJobsByStatus(page: unknown[], allJobs: Array<{ id: number; status: string }>) {
  getRenameJobs.mockImplementation((status?: string) =>
    Promise.resolve({
      jobs: status ? allJobs.filter((j) => j.status === status) : page,
    })
  );
}

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

  it('a late/reordered rename:progress tick arriving after the terminal broadcast does not resurrect the cleared bar', async () => {
    // Regression test for review finding (Important #2): broadcast_sync has
    // no per-connection send serialization, so a final progress tick and the
    // terminal rename:job broadcast have no hard wire-order guarantee. This
    // reproduces the reviewer's exact repro: progress tick, then terminal
    // event (correctly clears it), then a duplicate/late copy of the SAME
    // progress tick arriving after — it must stay cleared, not reappear.
    const mod = await import('./renames');
    const { get } = await import('svelte/store');
    handlers['rename:job']({ id: 7, status: 'applying' });
    handlers['rename:progress']({ id: 7, pct: 94, bytes_done: 9400, bytes_total: 10000 });
    expect(get(mod.renameProgress).has(7)).toBe(true);
    handlers['rename:job']({ id: 7, status: 'applied' });
    expect(get(mod.renameProgress).has(7)).toBe(false);
    // The late/duplicate tick — same payload, arrives after the job has
    // already left 'applying'.
    handlers['rename:progress']({ id: 7, pct: 94, bytes_done: 9400, bytes_total: 10000 });
    expect(get(mod.renameProgress).has(7)).toBe(false);
  });

  it('a rename:progress tick for a job never yet seen locally is still accepted (nothing to contradict it)', async () => {
    // The stale-tick guard must not suppress the ordinary case: a job's very
    // first progress tick can legitimately arrive before any rename:job
    // snapshot has told the client about it.
    const mod = await import('./renames');
    const { get } = await import('svelte/store');
    handlers['rename:progress']({ id: 42, pct: 3, bytes_done: 30, bytes_total: 1000 });
    expect(get(mod.renameProgress).has(42)).toBe(true);
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
    const allJobs = [
      { id: 1, status: 'applied' },
      { id: 2, status: 'applying' },
    ];
    mockJobsByStatus(allJobs, allJobs);

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

  it('resyncAfterReconnect keeps a still-applying job\'s progress AND row even when it fell off the general (capped) jobs page', async () => {
    // Regression test for review finding (Important #1): the general jobs
    // page is capped (limit=200) and ordered by detected_at DESC — a job's
    // original detection time, not its apply time. A job sitting in a large
    // backlog before finally being applied can be genuinely still mid-copy
    // yet absent from that page once a busy scanner has detected 200+ newer
    // items. Simulate that directly: the general page (`page`) does NOT
    // include job 555 at all, but the status=applying filter (`allJobs`)
    // does — resync must trust the filtered fetch, not treat "absent from
    // the general page" as "confirmed finished".
    const mod = await import('./renames');
    const { get } = await import('svelte/store');
    mod.renameProgress.set(new Map([
      [555, { pct: 40, bytes_done: 400, bytes_total: 1000, bytes_per_sec: 100, eta_seconds: 6, updatedAt: Date.now() }],
    ]));
    const page = [{ id: 999, status: 'matched' }]; // job 555 fell off this page
    const allJobs = [
      { id: 999, status: 'matched' },
      { id: 555, status: 'applying' }, // genuinely still applying
    ];
    mockJobsByStatus(page, allJobs);

    await mod.resyncAfterReconnect();

    const progress = get(mod.renameProgress);
    expect(progress.has(555)).toBe(true); // still applying — must survive
    const jobs = get(mod.renameJobs);
    expect(jobs.find((j) => j.id === 555)).toEqual({ id: 555, status: 'applying' }); // row restored, not vanished
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
    const allJobs = [{ id: 9, status: 'applied' }];
    mockJobsByStatus(allJobs, allJobs);

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

  it('resyncAfterReconnect retries once after a failed REST fetch, then succeeds without wiping local state on the failed attempt', async () => {
    // Regression test for review finding (Important #3): a transient REST
    // failure right at reconnect must not silently no-op forever on exactly
    // the stale state this function exists to fix.
    vi.useFakeTimers();
    try {
      const mod = await import('./renames');
      const { get } = await import('svelte/store');
      mod.renameProgress.set(new Map([
        [1, { pct: 50, bytes_done: 500, bytes_total: 1000, bytes_per_sec: 100, eta_seconds: 5, updatedAt: Date.now() }],
      ]));
      const allJobs = [{ id: 1, status: 'applying' }];
      let callCount = 0;
      getRenameJobs.mockImplementation((status?: string) => {
        callCount++;
        // fetchResyncSnapshot fires 2 getRenameJobs calls per attempt
        // (unfiltered + status=applying) — fail both calls of the first
        // attempt, succeed both calls of the retry.
        if (callCount <= 2) return Promise.reject(new Error('network blip'));
        return Promise.resolve({ jobs: status ? allJobs.filter((j) => j.status === status) : allJobs });
      });

      const p = mod.resyncAfterReconnect();
      // First attempt fails (all 3 parallel calls reject) synchronously-ish;
      // let those microtasks settle, then advance past the retry delay.
      await Promise.resolve();
      await Promise.resolve();
      await vi.advanceTimersByTimeAsync(2000);
      await p;

      // The retry succeeded (2nd wave of 3 calls all resolve) — state reflects it.
      expect(get(mod.renameJobs)).toEqual([{ id: 1, status: 'applying' }]);
      expect(get(mod.renameProgress).has(1)).toBe(true); // still applying — kept, never wiped
    } finally {
      vi.useRealTimers();
    }
  });

  it('resyncAfterReconnect warns and leaves state untouched when both the initial fetch AND the retry fail', async () => {
    vi.useFakeTimers();
    try {
      const mod = await import('./renames');
      const { get } = await import('svelte/store');
      const { toasts } = await import('./notifications');
      mod.renameProgress.set(new Map([
        [1, { pct: 50, bytes_done: 500, bytes_total: 1000, bytes_per_sec: 100, eta_seconds: 5, updatedAt: Date.now() }],
      ]));
      mod.renameJobs.set([{ id: 1, status: 'applying' }] as unknown as RenameJob[]);
      getRenameJobs.mockRejectedValue(new Error('backend unreachable'));
      const warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => {});

      const p = mod.resyncAfterReconnect();
      await Promise.resolve();
      await Promise.resolve();
      await vi.advanceTimersByTimeAsync(2000);
      await p;

      // Nothing was wiped by the failed resync — the stale-but-unproven
      // state is left exactly as it was rather than guessed away.
      expect(get(mod.renameProgress).has(1)).toBe(true);
      expect(get(mod.renameJobs)).toEqual([{ id: 1, status: 'applying' }]);
      // But the failure is surfaced, not silent.
      expect(warnSpy).toHaveBeenCalled();
      expect(get(toasts)[0]?.title).toBe('Reconnected, but refresh failed');

      warnSpy.mockRestore();
    } finally {
      vi.useRealTimers();
    }
  });
});
