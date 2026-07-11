import { writable } from 'svelte/store';
import { api } from '$lib/api/client';
import { connection } from './connection';
import { addToast } from './notifications';
import { clearResults, type ScanSource } from '$lib/stores/results';

export type ScanState = 'idle' | 'running' | 'stopping';
export type ScanType = 'deep' | 'incremental' | 'loaded' | 'search';

export const scanState = writable<ScanState>('idle');
export const scanProgress = writable<number>(0);
export const scanPhase = writable<string>('');
export const scanItemCount = writable<number>(0);

/** The scan source currently selected in the toolbar (ScanControls) — lifted
 *  out of that component so other UI (the empty-state search fallback) can
 *  read "what source would a scan run against right now" without a prop
 *  drill. ScanControls reads/writes this instead of local state. */
export const selectedScanSource = writable<ScanSource>('HDEncode');

connection.on('scan:progress', (data) => {
  scanProgress.set(data.progress as number);
  if (data.phase) scanPhase.set(data.phase as string);
  if (data.item_count != null) scanItemCount.set(data.item_count as number);
});

connection.on('scan:complete', (data) => {
  scanState.set('idle');
  scanProgress.set(0);
  scanPhase.set('');
  const count =
    (data.total as number | undefined) ??
    ((data.stats as { total?: number } | undefined)?.total ?? 0);
  addToast('Scan Complete', `Found ${count} result${count !== 1 ? 's' : ''}.`);
});

connection.on('scan:error', (data) => {
  scanState.set('idle');
  scanProgress.set(0);
  scanPhase.set('');
  const msg = (data.message as string) || 'Scan failed unexpectedly.';
  addToast('Scan Error', msg, 'error');
});

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
