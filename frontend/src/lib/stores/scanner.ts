import { get, writable } from 'svelte/store';
import { api } from '$lib/api/client';
import { connection } from './connection';
import { addToast } from './notifications';
import { clearResults, setScanActive, type ScanSource } from '$lib/stores/results';

export type ScanState = 'idle' | 'running' | 'stopping';
export type ScanType = 'deep' | 'incremental' | 'loaded' | 'search';

export const scanState = writable<ScanState>('idle');
// Mirror scan activity into results.ts (see setScanActive's doc comment for
// why the dependency points this way): every scanState writer — startScan,
// scan:complete/error, the idle-reconcile poll — flows through here.
scanState.subscribe((s) => setScanActive(s !== 'idle'));
export const scanProgress = writable<number>(0);
export const scanPhase = writable<string>('');
export const scanItemCount = writable<number>(0);

/** The scan source currently selected in the toolbar (ScanControls) — lifted
 *  out of that component so other UI (the empty-state search fallback) can
 *  read "what source would a scan run against right now" without a prop
 *  drill. ScanControls reads/writes this instead of local state. */
export const selectedScanSource = writable<ScanSource>('HDEncode');

connection.on('scan:progress', (data) => {
  // Progress events arrive for EVERY scan the backend runs — including
  // scheduled scans and scans started by another client — while startScan()
  // only covers this session's Start button. Adopting 'running' here keeps
  // scanState (and the scanActive mirror gating the category-toggle
  // live-mode exit) truthful for backend-originated scans. Never overrides
  // 'stopping': that state is this session's in-flight stop request.
  if (get(scanState) === 'idle') scanState.set('running');
  scanProgress.set(data.progress as number);
  if (data.phase) scanPhase.set(data.phase as string);
  if (data.item_count != null) scanItemCount.set(data.item_count as number);
});

connection.on('scan:complete', (data) => {
  scanState.set('idle');
  // Explicit, not only via the scanState mirror: a remote scan that only
  // ever STREAMED results (scanState never left 'idle', so setting 'idle'
  // again notifies nobody) still set the flag in handleScanResult.
  setScanActive(false);
  scanProgress.set(0);
  scanPhase.set('');
  const count =
    (data.total as number | undefined) ??
    ((data.stats as { total?: number } | undefined)?.total ?? 0);
  addToast('Scan Complete', `Found ${count} result${count !== 1 ? 's' : ''}.`);
});

connection.on('scan:error', (data) => {
  scanState.set('idle');
  setScanActive(false); // same streamed-only case as scan:complete above
  scanProgress.set(0);
  scanPhase.set('');
  const msg = (data.message as string) || 'Scan failed unexpectedly.';
  addToast('Scan Error', msg, 'error');
});

/** Reconcile scan activity with the backend — a session that (re)connects
 *  while a scheduled/remote scan is already mid-flight has seen none of that
 *  scan's WS events and would otherwise sit falsely idle (and, conversely, a
 *  session that missed a scan's completion while disconnected could keep the
 *  streamed-activity flag stuck on). Exported for direct testing; registered
 *  below for reconnects and run once at startup. */
export async function reconcileScanActivity(): Promise<void> {
  try {
    const st = await api.scanStatus?.();
    if (!st) return;
    if (st.state === 'running' && get(scanState) === 'idle') {
      scanState.set('running');
    } else if (st.state === 'idle' && get(scanState) === 'idle') {
      // scanState was already idle so its mirror won't fire — clear the
      // stream-set flag directly (missed-completion case).
      setScanActive(false);
    }
    // scanState 'running' with a backend gone idle is reconciled by the
    // polling safety net below.
  } catch {
    /* transient — the running-state poll and the next reconnect retry */
  }
}
connection.onReconnect(reconcileScanActivity);
reconcileScanActivity();

// Safety net: the backend resets to idle when a scan finishes, but if the
// frontend misses the scan:complete event (e.g. the WebSocket reconnected
// mid-scan), the progress bar sticks at 100%. While we believe a scan is
// running, poll the backend and reconcile if it has actually gone idle.
let _reconcileTimer: ReturnType<typeof setInterval> | null = null;
scanState.subscribe((s) => {
  if (s === 'running') {
    if (_reconcileTimer == null) {
      _reconcileTimer = setInterval(async () => {
        try {
          const st = await api.scanStatus();
          if (st?.state === 'idle') {
            scanState.set('idle');
            scanProgress.set(0);
            scanPhase.set('');
          }
        } catch {
          /* transient — keep polling */
        }
      }, 8000);
    }
  } else if (_reconcileTimer != null) {
    clearInterval(_reconcileTimer);
    _reconcileTimer = null;
  }
});

export async function startScan(
  type: ScanType,
  query = '',
  pages = 1,
  source = 'HDEncode',
  flags?: Record<string, boolean>
) {
  scanState.set('running');
  scanProgress.set(0);
  try {
    await api.scanStart(type, query, pages, source, flags);
  } catch (e) {
    scanState.set('idle');
    scanProgress.set(0);
    scanPhase.set('');
    addToast('Scan Error', e instanceof Error ? e.message : 'Failed to start scan', 'error');
  }
}

/** Run a live Site Search for `query` against `source`, replacing the
 *  current browse view — the same action as manually switching ScanControls
 *  to "Site Search" mode and hitting Scan. Flags are irrelevant for Site
 *  Search (the backend's _build_sources never reads them for that mode). */
export function searchThisSite(query: string, source: ScanSource) {
  clearResults();
  startScan('search', query, 1, source);
}

export async function stopScan() {
  scanState.set('stopping');
  try {
    await api.scanStop();
  } catch (e) {
    scanState.set('idle');
    addToast('Error', e instanceof Error ? e.message : 'Failed to stop scan', 'error');
  }
}

// Auto-grab notifications
connection.on('autograb:started', (data) => {
  const count = (data.count as number) || 0;
  if (count > 0) {
    addToast('Auto-Grab', `Processing ${count} item(s)...`);
  }
});

connection.on('autograb:complete', (data) => {
  const grabbed = (data.grabbed as number) || 0;
  const total = (data.total as number) || 0;
  if (grabbed > 0) {
    addToast('Auto-Grab', `Grabbed ${grabbed} of ${total} item(s).`);
  }
});
