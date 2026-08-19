import { describe, it, expect, vi, beforeEach } from 'vitest';
import { get } from 'svelte/store';

// Spread the real module so anything else in the import graph keeps working;
// only the toast is replaced, because "did the user get told" is an assertion
// this file needs to make.
vi.mock('$lib/stores/notifications', async (importOriginal) => ({
  ...(await importOriginal<typeof import('$lib/stores/notifications')>()),
  addToast: vi.fn()
}));

import { api } from '$lib/api/client';
import { addToast } from '$lib/stores/notifications';
import { dvConflicts, loadDvConflicts, resyncDvConflictsAfterReconnect } from './renames';

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
    vi.mocked(addToast).mockClear();
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

  // --- reconnect repair -------------------------------------------------
  // A backend restart can bring the WebSocket back before plain HTTP is
  // routable through the reverse proxy — the neighbouring resyncAfterReconnect
  // documents exactly that and retries once. A one-shot here let the stale zero
  // survive silently (peer review round 4).

  it('retries once when the first reconnect read fails, and recovers', async () => {
    // The tab is holding the stale zero left by an alert it never received.
    expect(get(dvConflicts).count).toBe(0);

    const fetchMock = vi
      .spyOn(globalThis, 'fetch')
      .mockRejectedValueOnce(new Error('REST not routable yet'))
      .mockResolvedValueOnce(jsonResponse(TWO_CONFLICTS));

    await resyncDvConflictsAfterReconnect();

    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(get(dvConflicts).count).toBe(2);
    expect(addToast).not.toHaveBeenCalled(); // recovered, so nothing to report
  }, 10_000);

  it('warns when both attempts fail AND the preserved value is a stale zero', async () => {
    // The discriminating case. Preserving the old value is right, but when the
    // old value is 0 the UI renders "nothing needs attention" — indistinguishable
    // from a healthy library. Silence here is the false negative.
    expect(get(dvConflicts).count).toBe(0);
    vi.spyOn(globalThis, 'fetch').mockRejectedValue(new Error('offline'));

    await resyncDvConflictsAfterReconnect();

    expect(get(dvConflicts).count).toBe(0);
    expect(addToast).toHaveBeenCalledTimes(1);
    expect(String(vi.mocked(addToast).mock.calls[0][0])).toMatch(/DV status/i);
  }, 10_000);

  it('keeps a known nonzero warning when both attempts fail', async () => {
    dvConflicts.set(TWO_CONFLICTS);
    vi.spyOn(globalThis, 'fetch').mockRejectedValue(new Error('offline'));

    await resyncDvConflictsAfterReconnect();

    expect(get(dvConflicts).count).toBe(2); // never retract a real warning
    expect(addToast).toHaveBeenCalledTimes(1);
  }, 10_000);

  it('does not retry or warn when the first read succeeds', async () => {
    // Negative control: without it, "retries once" could become "always makes
    // two requests", doubling this call on every reconnect.
    const fetchMock = vi
      .spyOn(globalThis, 'fetch')
      .mockResolvedValue(jsonResponse(TWO_CONFLICTS));

    await resyncDvConflictsAfterReconnect();

    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(get(dvConflicts).count).toBe(2);
    expect(addToast).not.toHaveBeenCalled();
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
