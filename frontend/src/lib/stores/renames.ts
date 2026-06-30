import { writable, derived } from 'svelte/store';
import { api } from '$lib/api/client';
import { connection } from './connection';
import type { RenameJob, RenameStatus, DvScan } from '$lib/api/types';

export const renameJobs = writable<RenameJob[]>([]);
export const renameStatus = writable<RenameStatus | null>(null);

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

export async function applyJob(id: number) {
  await api.applyRename(id);
  await refresh();
}

export async function undoJob(id: number) {
  await api.undoRename(id);
  await refresh();
}

export async function rematchJob(id: number, tmdbId: number, mediaType?: string) {
  await api.rematchRename(id, tmdbId, mediaType);
  await refresh();
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
