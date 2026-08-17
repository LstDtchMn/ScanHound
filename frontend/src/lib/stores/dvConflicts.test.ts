import { describe, it, expect, vi, beforeEach } from 'vitest';
import { get } from 'svelte/store';
import { api } from '$lib/api/client';
import { dvConflicts, loadDvConflicts } from './renames';

/**
 * Recovery of DV conflict state after a missed event.
 *
 * The unattended alert fires once per distinct conflict set and dedups. Its
 * in-app broadcast reaches only the clients connected at that instant, and the
 * durable host-detector ingest (POST /rename/dv-host-rows) emits no
 * dv:scan_done at all. So a tab that was open and holding count 0 while the
 * socket was down had nothing to bring it back to the truth: resyncAfterReconnect
 * refreshes rename state only (peer review round 3).
 *
 * The fix is that conflict state is DERIVED server-side and can simply be
 * re-read. These tests pin the re-read, since that is the whole recovery path.
 */

function jsonResponse(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'content-type': 'application/json' }
  });
}

const TWO_CONFLICTS = {
  count: 2,
  sample: [
    { path: 'C:/4K Drives/4K Columbo/Movies 2/Alpha (2001).mkv', layers: ['fel', 'mel'] },
    { path: 'C:/4K Drives/4K Columbo/Movies 2/Beta (2002).mkv', layers: ['none', 'fel'] }
  ],
  truncated: false
};

describe('dv conflict current-state recovery', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    dvConflicts.set({ count: 0, sample: [], truncated: false });
  });

  it('reads the narrow conflict endpoint, not the inventory', async () => {
    const fetchMock = vi
      .spyOn(globalThis, 'fetch')
      .mockResolvedValue(jsonResponse(TWO_CONFLICTS));

    await api.getDvConflicts();

    const [url] = fetchMock.mock.calls[0];
    expect(String(url)).toContain('/rename/dv-conflicts');
    // Refreshing attention state must not drag the 500-row inventory along.
    expect(String(url)).not.toContain('/rename/dv-scans');
  });

  it('a stale tab holding 0 recovers the real count on refresh', async () => {
    expect(get(dvConflicts).count).toBe(0); // the stale state after a missed event

    vi.spyOn(globalThis, 'fetch').mockResolvedValue(jsonResponse(TWO_CONFLICTS));
    await loadDvConflicts();

    expect(get(dvConflicts).count).toBe(2);
    expect(get(dvConflicts).sample[0].layers).toEqual(['fel', 'mel']);
  });

  it('a failed refresh keeps the last known value rather than clearing it', async () => {
    dvConflicts.set(TWO_CONFLICTS);

    vi.spyOn(globalThis, 'fetch').mockRejectedValue(new Error('offline'));
    await loadDvConflicts();

    // Clearing on failure would silently retract a real warning — the same
    // "absence of evidence read as evidence of absence" shape the backend
    // guards against by never treating a failed scan as proof of no DV.
    expect(get(dvConflicts).count).toBe(2);
  });

  it('clears once the conflicts are genuinely resolved', async () => {
    dvConflicts.set(TWO_CONFLICTS);

    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      jsonResponse({ count: 0, sample: [], truncated: false })
    );
    await loadDvConflicts();

    // The negative control for the test above: "keep the old value on failure"
    // must not become "never clear", or the badge would be permanent.
    expect(get(dvConflicts).count).toBe(0);
  });

  it('surfaces truncation so a capped sample is never read as the whole set', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      jsonResponse({ count: 400, sample: TWO_CONFLICTS.sample, truncated: true })
    );
    await loadDvConflicts();

    const s = get(dvConflicts);
    expect(s.count).toBe(400); // exact, not the sample length
    expect(s.sample).toHaveLength(2);
    expect(s.truncated).toBe(true);
  });
});
