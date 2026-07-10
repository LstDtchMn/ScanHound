import { writable, derived, get } from 'svelte/store';
import { api } from '$lib/api/client';
import { connection } from './connection';
import type { RenameJob, RenameStatus, DvScan } from '$lib/api/types';
import { persisted } from '$lib/stores/results';
import { addToast } from '$lib/stores/notifications';
import type { RenameCategory } from '$lib/renames/category';

export const renameJobs = writable<RenameJob[]>([]);
export const renameStatus = writable<RenameStatus | null>(null);

// --- Multi-select ---
export const selectedJobIds = writable<Set<number>>(new Set());
export function toggleSelect(id: number) {
  selectedJobIds.update((s) => {
    const next = new Set(s);
    if (next.has(id)) next.delete(id);
    else next.add(id);
    return next;
  });
}
export function selectAll(ids: number[]) {
  selectedJobIds.set(new Set(ids));
}
export function clearSelection() {
  selectedJobIds.set(new Set());
}

// --- View / sort / category / search prefs ---
export const viewMode = persisted<'list' | 'grid'>('sh-renames-view', 'list');
export const renameSort = persisted<
  'detected_desc' | 'detected_asc' | 'confidence_desc' | 'title_asc'
>('sh-renames-sort', 'detected_desc');
export const renameCategory = persisted<RenameCategory>('sh-renames-category', 'all');
export const renameQuery = writable<string>('');

// --- Bulk in-flight flag (disables BulkBar during a run) ---
export const bulkBusy = writable<boolean>(false);

export interface FolderPreviewItem {
  path: string;
  filename: string;
  tracked: boolean;
  title: string | null;
  year: number | null;
  confidence: number;
  new_filename: string | null;
  status: string;
}
export interface FolderPreview {
  folder: string;
  found: number;
  would_match?: number;
  previews?: FolderPreviewItem[];
  note?: string;
  error?: string;
}
/** Latest dry-run preview from "Process folder → Preview" (no jobs created). */
export const folderPreview = writable<FolderPreview | null>(null);

/** Count of jobs flagged for manual review — surfaced as a nav badge. */
export const needsReviewCount = derived(renameStatus, ($s) => $s?.needs_review ?? 0);

export async function loadRenameJobs(status?: string) {
  try {
    const { jobs } = await api.getRenameJobs(status);
    renameJobs.set(jobs);
  } catch {
    /* offline / no server */
  }
}

export async function loadRenameStatus() {
  try {
    renameStatus.set(await api.getRenameStatus());
  } catch {
    /* offline */
  }
}

async function refresh() {
  await Promise.all([loadRenameJobs(), loadRenameStatus()]);
}

export async function refreshRenames() {
  await refresh();
}

export async function applyJob(id: number, strategy?: 'overwrite' | 'keep_both' | 'skip') {
  await api.applyRename(id, strategy ? { conflict_strategy: strategy } : undefined);
  await refresh();
}

export async function undoJob(id: number) {
  const r = await api.undoRename(id);
  if (r.restore_warning) {
    addToast('Undo', r.restore_warning, 'warning');
  }
  await refresh();
}

export async function rematchJob(id: number, tmdbId: number, mediaType?: string) {
  await api.rematchRename(id, tmdbId, mediaType);
  await refresh();
}

// --- Bulk actions ---
async function runBulk(label: string, fn: (ids: number[]) => Promise<void>) {
  const ids = [...get(selectedJobIds)];
  if (ids.length === 0) return;
  bulkBusy.set(true);
  try {
    await fn(ids);
  } catch (e) {
    addToast(`${label} failed`, e instanceof Error ? e.message : String(e), 'error');
  } finally {
    await refresh();
    clearSelection();
    bulkBusy.set(false);
  }
}

export function bulkApply() {
  return runBulk('Apply', async (ids) => {
    // Applies are queued server-side (cross-device moves can take minutes);
    // each job reports back over the rename:job WS event as it lands.
    const r = await api.bulkApply(ids);
    addToast(
      `Applying ${r.queued ?? 0} in background`,
      r.skipped ? `${r.skipped} skipped (already applied/in progress)` : 'Progress updates live',
      'info'
    );
  });
}

export function bulkReidentify() {
  return runBulk('Re-identify', async (ids) => {
    const r = await api.bulkReidentify(ids);
    addToast('Re-identify queued', `Queued ${r.queued} for re-identify`, 'info');
  });
}

export function bulkDelete() {
  return runBulk('Delete', async (ids) => {
    const r = await api.bulkDelete(ids);
    addToast('Deleted', `Deleted ${r.deleted}`, 'success');
  });
}

export function bulkSetDestination(root: string) {
  return runBulk('Set destination', async (ids) => {
    const r = await api.bulkSetDestination(ids, root);
    addToast('Destination updated', `Updated destination for ${r.updated}`, 'success');
  });
}

/** Apply-confident. ids omitted = all matched jobs (Matched-card shortcut). */
export async function applyConfident(ids?: number[]) {
  bulkBusy.set(true);
  try {
    const r = await api.applyConfident(ids);
    addToast(
      `Applying ${r.queued ?? 0} confident in background`,
      `${r.skipped ?? 0} skipped — progress updates live`,
      'info'
    );
  } catch (e) {
    addToast(
      'Apply confident failed',
      e instanceof Error ? e.message : String(e),
      'error'
    );
  } finally {
    await refresh();
    clearSelection();
    bulkBusy.set(false);
  }
}

export async function acceptCombinedJob(id: number) {
  await api.acceptCombinedRename(id);
  await refresh();
}

export async function acceptCorrectionJob(id: number) {
  await api.acceptCorrectionRename(id);
  await refresh();
}

export async function deleteJob(id: number) {
  await api.deleteRenameJob(id);
  renameJobs.update((jobs) => jobs.filter((j) => j.id !== id));
  await loadRenameStatus();
}

// ── Apply progress ───────────────────────────────────────────────────
// Per-job byte progress for an in-flight move (only a cross-device COPY emits
// this; same-device renames finish instantly). Keyed by job id.
export interface RenameProgress { pct: number; bytes_done: number; bytes_total: number; }
export const renameProgress = writable<Map<number, RenameProgress>>(new Map());

// Overall apply-queue progress ("job X of N"). null when nothing is applying.
export interface RenameQueueProgress { done: number; total: number; current_title: string | null; }
export const renameQueue = writable<RenameQueueProgress | null>(null);

connection.on('rename:progress', (data) => {
  const id = data.id as number;
  if (!id) return;
  renameProgress.update((m) => {
    const next = new Map(m);
    next.set(id, {
      pct: (data.pct as number) ?? 0,
      bytes_done: (data.bytes_done as number) ?? 0,
      bytes_total: (data.bytes_total as number) ?? 0,
    });
    return next;
  });
});

let queueClearTimer: ReturnType<typeof setTimeout>;
connection.on('rename:queue_progress', (data) => {
  const active = !!data.active;
  clearTimeout(queueClearTimer);
  if (!active) {
    // Brief "100%" flash, then clear the queue bar.
    renameQueue.set({ done: (data.done as number) ?? 0, total: (data.total as number) ?? 0, current_title: null });
    queueClearTimer = setTimeout(() => renameQueue.set(null), 1500);
    return;
  }
  renameQueue.set({
    done: (data.done as number) ?? 0,
    total: (data.total as number) ?? 0,
    current_title: (data.current_title as string) ?? null,
  });
});

// Live updates: upsert a job whenever the backend broadcasts a change.
connection.on('rename:job', (data) => {
  const job = data as unknown as RenameJob;
  if (!job || !job.id) return;
  renameJobs.update((jobs) => {
    const idx = jobs.findIndex((j) => j.id === job.id);
    if (idx >= 0) {
      const next = [...jobs];
      next[idx] = job;
      return next;
    }
    return [job, ...jobs];
  });
  // Once a job leaves 'applying' (applied/failed/needs_review), drop its
  // per-item progress so the bar disappears.
  if (job.status !== 'applying') {
    renameProgress.update((m) => {
      if (!m.has(job.id)) return m;
      const next = new Map(m);
      next.delete(job.id);
      return next;
    });
  }
  loadRenameStatus();
});

// Dry-run preview result (no jobs are created for a preview).
connection.on('rename:folder_preview', (data) => {
  folderPreview.set(data as unknown as FolderPreview);
});

// ── Dolby Vision scan ────────────────────────────────────────────────
export interface DvScanProgress { done: number; total: number; file: string; layer: string | null; }
export interface DvScanResult {
  folder?: string; found: number; scanned: number; skipped: number;
  by_layer?: Record<string, number>; error?: string;
}
/** True from the moment a DV scan is dispatched until its dv:scan_done arrives —
 *  drives the Scan button's disabled state (the POST returns immediately, so a
 *  timer can't tell when the background scan actually finishes). */
export const dvScanRunning = writable<boolean>(false);
/** Live per-file progress of a running DV scan (null when idle). */
export const dvScanProgress = writable<DvScanProgress | null>(null);
/** Summary of the last completed DV scan. */
export const dvScanResult = writable<DvScanResult | null>(null);
/** The DV inventory (scanned files) + per-layer counts. */
export const dvScans = writable<DvScan[]>([]);
export const dvCounts = writable<Record<string, number>>({});

export async function loadDvScans(layer?: string) {
  try {
    const { scans, counts } = await api.getDvScans(layer);
    dvScans.set(scans);
    dvCounts.set(counts);
  } catch {
    /* offline */
  }
}

connection.on('dv:scan_progress', (data) => {
  dvScanProgress.set(data as unknown as DvScanProgress);
});
connection.on('dv:scan_done', (data) => {
  dvScanResult.set(data as unknown as DvScanResult);
  dvScanProgress.set(null);
  dvScanRunning.set(false);
  loadDvScans();
});

// ── On-demand conflict DV scan (RenameReviewCard's "Scan DV layers") ──
// Distinct from the full-library scan above: scan-dv-conflict broadcasts its
// own dv:conflict_scan_done so it never disturbs the DV-scan panel's state.
// Consumers just watch this tick to know when to re-fetch their conflict
// preview — no shared progress/result payload to model.
export const dvScanTick = writable(0);
connection.on('dv:conflict_scan_done', () => {
  dvScanTick.update((n) => n + 1);
});

// ── Dolby Vision label sync (mirrors the DV scan stores above) ────────
export interface DvSyncProgress { done: number; total: number; }
export interface DvSyncResult {
  total: number; added: number; removed: number; matched: number; dry_run: boolean;
  error?: string;
}
/** True from dispatch of a label sync until its dv:sync_done arrives —
 *  drives the "Sync Plex labels" button's disabled state. */
export const dvSyncRunning = writable<boolean>(false);
/** Live done/total progress of a running label sync (null when idle) — the
 *  backend reports counts only, no per-title text (see dv_sync_labels in
 *  backend/api/routes/rename.py). */
export const dvSyncProgress = writable<DvSyncProgress | null>(null);
/** Summary of the last completed label sync. */
export const dvSyncResult = writable<DvSyncResult | null>(null);

connection.on('dv:sync_progress', (data) => {
  dvSyncProgress.set(data as unknown as DvSyncProgress);
});
connection.on('dv:sync_done', (data) => {
  dvSyncResult.set(data as unknown as DvSyncResult);
  dvSyncProgress.set(null);
  dvSyncRunning.set(false);
});
