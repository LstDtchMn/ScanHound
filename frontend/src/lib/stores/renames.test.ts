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
  move_method: 'move', llm_enabled: false, counts: {}, needs_review: 0, archived: 0,
});
const bulkArchive = vi.fn();
const bulkUnarchive = vi.fn();
vi.mock('$lib/api/client', () => ({
  api: {
    getDvScans: vi.fn().mockResolvedValue({ scans: [], counts: {} }),
    getRenameJobs,
    getRenameStatus,
    bulkArchive,
    bulkUnarchive,
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

  it('resyncAfterReconnect does NOT clear the queue banner while the applying-filter snapshot proves a bulk run is still active', async () => {
    // Regression test for a review finding: the previous unconditional
    // renameQueue.set(null) fired even when this same function's own
    // applying-filter fetch just proved a run is genuinely still going (e.g.
    // mid-copy on one large file, between per-job queue_progress
    // broadcasts) — hiding the Stop button and re-enabling every
    // applyActive-gated control until the next broadcast, potentially
    // minutes away.
    const mod = await import('./renames');
    const { get } = await import('svelte/store');
    const queueBefore = { done: 1, total: 3, current_title: 'Some Movie' };
    mod.renameQueue.set(queueBefore);
    mod.applyCancelling.set(true);
    const allJobs = [{ id: 1, status: 'applying' }];
    mockJobsByStatus(allJobs, allJobs);

    await mod.resyncAfterReconnect();

    expect(get(mod.renameQueue)).toEqual(queueBefore);
    expect(get(mod.applyCancelling)).toBe(true);
  });

  it('resyncAfterReconnect never regresses a locally-known terminal job back to a stale non-terminal status from the REST snapshot', async () => {
    // Regression test for review findings (Important): the two REST reads
    // inside fetchResyncSnapshot (general + status=applying) are independent
    // queries with no shared transaction, and the live WS socket is already
    // dispatching while they're in flight. A job can go applying -> applied
    // (via the live rename:job handler) entirely within that window, making
    // the snapshot stale for that job. The merge must never let a stale
    // snapshot clobber a locally-held terminal status back to 'applying'.
    const mod = await import('./renames');
    const { get } = await import('svelte/store');
    // The live WS handler already applied job 7's completion before the
    // snapshot resolves (simulating the race window).
    handlers['rename:job']({ id: 7, status: 'applied', title: 'Some Movie' });
    expect(get(mod.renameJobs).find((j: { id: number }) => j.id === 7)).toMatchObject({ status: 'applied' });
    // Both REST reads are stale for job 7 — read before its completion committed.
    const allJobs = [{ id: 7, status: 'applying' }];
    mockJobsByStatus(allJobs, allJobs);

    await mod.resyncAfterReconnect();

    const job7 = get(mod.renameJobs).find((j: { id: number }) => j.id === 7);
    expect(job7).toMatchObject({ status: 'applied' });
  });

  it('resyncAfterReconnect still lets a genuinely-fresh snapshot move a job from applying to applied', async () => {
    // Guards the previous test's fix from over-correcting: when the client
    // has NO locally-known terminal status for a job (the ordinary "missed
    // the broadcast entirely while offline" case this resync exists for),
    // the snapshot must still be trusted normally.
    const mod = await import('./renames');
    const { get } = await import('svelte/store');
    mod.renameJobs.set([{ id: 8, status: 'applying' }] as unknown as RenameJob[]);
    const allJobs = [{ id: 8, status: 'applied' }];
    mockJobsByStatus(allJobs, allJobs);

    await mod.resyncAfterReconnect();

    expect(get(mod.renameJobs).find((j) => j.id === 8)).toMatchObject({ status: 'applied' });
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

describe('Archived rename jobs', () => {
  beforeEach(() => {
    for (const k of Object.keys(handlers)) delete handlers[k];
    reconnectHandlers.length = 0;
    getRenameJobs.mockReset().mockResolvedValue({ jobs: [] });
    getRenameStatus.mockReset().mockResolvedValue({
      enabled: true, require_confirmation: true, confidence_threshold: 70,
      move_method: 'move', llm_enabled: false, counts: {}, needs_review: 0, archived: 0,
    });
    bulkArchive.mockReset();
    bulkUnarchive.mockReset();
    vi.resetModules();
  });

  it('loadArchivedRenameJobs fetches archived=true jobs into a separate store', async () => {
    const mod = await import('./renames');
    const { get } = await import('svelte/store');
    const fakeJob = { id: 1, status: 'applied', archived_at: '2026-07-12T00:00:00Z' } as unknown as RenameJob;
    getRenameJobs.mockImplementation((status?: string, archived?: boolean) =>
      Promise.resolve({ jobs: archived ? [fakeJob] : [] })
    );

    await mod.loadArchivedRenameJobs();

    expect(getRenameJobs).toHaveBeenCalledWith(undefined, true);
    expect(get(mod.archivedRenameJobs)).toEqual([fakeJob]);
    // The default (non-archived) renameJobs store must be untouched.
    expect(get(mod.renameJobs)).toEqual([]);
  });

  it('bulkArchive calls the archive endpoint with selected ids, toasts, and clears selection', async () => {
    const mod = await import('./renames');
    const { get } = await import('svelte/store');
    const { toasts } = await import('./notifications');
    mod.selectedJobIds.set(new Set([1, 2]));
    bulkArchive.mockResolvedValue({ archived: 2 });

    await mod.bulkArchive();

    expect(bulkArchive).toHaveBeenCalledWith([1, 2]);
    expect(get(mod.selectedJobIds).size).toBe(0);
    expect(get(toasts)[0]).toMatchObject({ title: 'Archived', body: 'Archived 2 job(s).' });
    // runBulk's finally block refreshes the active queue.
    expect(getRenameJobs).toHaveBeenCalled();
    expect(getRenameStatus).toHaveBeenCalled();
  });

  it('bulkUnarchive calls the unarchive endpoint with selected ids and refreshes the Archived store', async () => {
    const mod = await import('./renames');
    const { get } = await import('svelte/store');
    const { toasts } = await import('./notifications');
    mod.selectedJobIds.set(new Set([3]));
    bulkUnarchive.mockResolvedValue({ unarchived: 1 });
    const fakeArchivedAfter = [{ id: 99, status: 'applied' }] as unknown as RenameJob[];
    getRenameJobs.mockImplementation((status?: string, archived?: boolean) =>
      Promise.resolve({ jobs: archived ? fakeArchivedAfter : [] })
    );

    await mod.bulkUnarchive();

    expect(bulkUnarchive).toHaveBeenCalledWith([3]);
    expect(get(toasts)[0]).toMatchObject({ title: 'Restored', body: 'Restored 1 job(s) to the queue.' });
    // bulkUnarchive must explicitly refresh archivedRenameJobs — runBulk's own
    // finally-block refresh() only touches renameJobs/renameStatus, not the
    // separate Archived-tab store the user is currently viewing.
    expect(get(mod.archivedRenameJobs)).toEqual(fakeArchivedAfter);
  });

  it('a rename:job broadcast carrying archived_at evicts the job from renameJobs instead of upserting it (live auto-archive-on-apply)', async () => {
    // Regression test for review finding (Important): both apply() success
    // paths set archived_at server-side in the very same update that sets
    // status="applied", and queue_apply runs the actual move on a background
    // thread — so the ONLY signal a live session gets that a job just left
    // the active (non-archived) queue is this broadcast. Previously the
    // handler unconditionally upserted, leaving the job visibly stuck in the
    // active list (e.g. the Applied/All tab) until a full page reload.
    const mod = await import('./renames');
    const { get } = await import('svelte/store');
    mod.renameJobs.set([{ id: 5, status: 'applying' }] as unknown as RenameJob[]);
    mod.renameProgress.set(new Map([
      [5, { pct: 60, bytes_done: 600, bytes_total: 1000, bytes_per_sec: 100, eta_seconds: 4, updatedAt: Date.now() }],
    ]));

    handlers['rename:job']({ id: 5, status: 'applied', archived_at: '2026-07-12T00:00:00Z' });

    expect(get(mod.renameJobs).find((j: { id: number }) => j.id === 5)).toBeUndefined();
    expect(get(mod.renameProgress).has(5)).toBe(false);
    // The Archived StatCard's count is still sourced from renameStatus, so
    // eviction must not skip the status reload.
    expect(getRenameStatus).toHaveBeenCalled();
  });

  it('a rename:job broadcast without archived_at still upserts normally (not archived)', async () => {
    const mod = await import('./renames');
    const { get } = await import('svelte/store');
    mod.renameJobs.set([{ id: 6, status: 'applying' }] as unknown as RenameJob[]);

    handlers['rename:job']({ id: 6, status: 'failed' });

    expect(get(mod.renameJobs).find((j: { id: number }) => j.id === 6)).toMatchObject({ status: 'failed' });
  });

  it('a rename:job broadcast carrying archived_at upserts into archivedRenameJobs too (rematching an archived job stays live)', async () => {
    // Regression test for the Minor finding left open by Task 3's second fix
    // round: rematch() changes title/tmdb_id/season/episode/status without
    // touching archived_at, and its broadcast lands on this same handler —
    // previously the archived branch only evicted renameJobs, leaving an open
    // Archived tab showing the job's pre-rematch title/filename until the tab
    // was left and re-entered.
    const mod = await import('./renames');
    const { get } = await import('svelte/store');
    mod.archivedRenameJobs.set([
      { id: 8, status: 'applied', title: 'Old Title', archived_at: '2026-07-12T00:00:00Z' },
    ] as unknown as RenameJob[]);

    handlers['rename:job']({
      id: 8, status: 'needs_review', title: 'New Title', archived_at: '2026-07-12T00:00:00Z',
    });

    expect(get(mod.archivedRenameJobs).find((j: { id: number }) => j.id === 8)).toMatchObject({
      title: 'New Title', status: 'needs_review',
    });
  });

  it('a rename:job broadcast carrying archived_at inserts a not-yet-known job into archivedRenameJobs', async () => {
    const mod = await import('./renames');
    const { get } = await import('svelte/store');
    mod.archivedRenameJobs.set([]);

    handlers['rename:job']({ id: 9, status: 'applied', archived_at: '2026-07-12T00:00:00Z' });

    expect(get(mod.archivedRenameJobs).find((j: { id: number }) => j.id === 9)).toBeDefined();
  });

  it('a rename:job broadcast WITHOUT archived_at evicts the job from archivedRenameJobs (live unarchive elsewhere)', async () => {
    const mod = await import('./renames');
    const { get } = await import('svelte/store');
    mod.archivedRenameJobs.set([
      { id: 10, status: 'matched', archived_at: '2026-07-12T00:00:00Z' },
    ] as unknown as RenameJob[]);

    handlers['rename:job']({ id: 10, status: 'matched' });

    expect(get(mod.archivedRenameJobs).find((j: { id: number }) => j.id === 10)).toBeUndefined();
  });

  it('resyncAfterReconnect never resurrects a locally-known-archived job into the active queue', async () => {
    // Regression test for review finding (Important): the merge loop that
    // protects a locally-known TERMINAL (applied/failed) job from a stale/
    // paginated-out snapshot must not also replant a job the client already
    // knows is archived — the archived-excluding snapshot correctly omits
    // it, and that omission must be trusted, not overridden.
    const mod = await import('./renames');
    const { get } = await import('svelte/store');
    mod.renameJobs.set([
      { id: 42, status: 'applied', archived_at: '2026-07-12T00:00:00Z' },
    ] as unknown as RenameJob[]);
    mockJobsByStatus([], []); // fresh snapshot correctly omits the archived job

    await mod.resyncAfterReconnect();

    expect(get(mod.renameJobs).find((j) => j.id === 42)).toBeUndefined();
  });

  it('a finished apply queue reconciles rows stuck on a missed terminal event (self-heal)', async () => {
    // The exact bug reported: a bulk apply completes, but one or more per-job
    // terminal rename:job broadcasts never reach this tab, leaving those rows
    // frozen at 'applying' with a full progress bar. The queue-done broadcast
    // must trigger a reconcile that sweeps them.
    vi.useFakeTimers();
    try {
      const mod = await import('./renames');
      const { get } = await import('svelte/store');
      mod.renameJobs.set([{ id: 99, status: 'applying' }] as unknown as RenameJob[]);
      mod.renameProgress.set(new Map([
        [99, { pct: 100, bytes_done: 100, bytes_total: 100, bytes_per_sec: null, eta_seconds: null, updatedAt: 1 }],
      ]));
      // Authoritative fresh snapshot: job 99 is done + archived — absent from
      // both the general page and the status=applying filter.
      mockJobsByStatus([], []);

      handlers['rename:queue_progress']({ active: false, done: 1, total: 1 });
      await vi.advanceTimersByTimeAsync(2100); // past the ~2s reconcile delay

      expect(get(mod.renameJobs).find((j) => j.id === 99)).toBeUndefined();
      expect(get(mod.renameProgress).has(99)).toBe(false);
    } finally {
      vi.useRealTimers();
    }
  });

  // --- shift-click range selection (desktop list view) ---
  it('shift-select: plain click sets the anchor, shift-click fills the range', async () => {
    const mod = await import('./renames');
    const { get } = await import('svelte/store');
    mod.setOrderedVisibleIds([1, 2, 3, 4, 5]);
    mod.selectClick(2, false); // anchor = 2, {2}
    mod.selectClick(4, true);  // range 2..4
    expect([...get(mod.selectedJobIds)].sort((a, b) => a - b)).toEqual([2, 3, 4]);
  });

  it('shift-select is additive and re-extends from the same anchor', async () => {
    const mod = await import('./renames');
    const { get } = await import('svelte/store');
    mod.setOrderedVisibleIds([1, 2, 3, 4, 5]);
    mod.selectClick(2, false);
    mod.selectClick(4, true); // {2,3,4}
    mod.selectClick(1, true); // range 2..1 -> {1,2}, unioned with existing
    expect([...get(mod.selectedJobIds)].sort((a, b) => a - b)).toEqual([1, 2, 3, 4]);
  });

  it('shift-click with no anchor falls back to a single toggle', async () => {
    const mod = await import('./renames');
    const { get } = await import('svelte/store');
    mod.setOrderedVisibleIds([1, 2, 3]);
    mod.selectClick(3, true);
    expect([...get(mod.selectedJobIds)]).toEqual([3]);
  });

  it('a plain click on an already-selected row toggles it off', async () => {
    const mod = await import('./renames');
    const { get } = await import('svelte/store');
    mod.setOrderedVisibleIds([1, 2, 3]);
    mod.selectClick(2, false);
    mod.selectClick(2, false);
    expect(get(mod.selectedJobIds).size).toBe(0);
  });

  it('clearSelection resets the anchor so the next shift-click has nothing to extend from', async () => {
    const mod = await import('./renames');
    const { get } = await import('svelte/store');
    mod.setOrderedVisibleIds([1, 2, 3, 4]);
    mod.selectClick(2, false);
    mod.clearSelection();
    mod.selectClick(4, true); // no anchor -> single toggle
    expect([...get(mod.selectedJobIds)]).toEqual([4]);
  });
});
