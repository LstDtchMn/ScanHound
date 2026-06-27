import { writable, derived } from 'svelte/store';
import { api } from '$lib/api/client';
import { connection } from './connection';
import type { RenameJob, RenameStatus } from '$lib/api/types';

export const renameJobs = writable<RenameJob[]>([]);
export const renameStatus = writable<RenameStatus | null>(null);

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
