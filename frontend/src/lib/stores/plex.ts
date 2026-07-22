import { writable } from 'svelte/store';
import { api } from '$lib/api/client';
import { connection } from './connection';
import type { PlexMetadataScanStatus } from '$lib/api/types';

export const plexConnected = writable(false);
export const plexServer = writable('');
export const plexMovieCount = writable(0);
export const plexTvCount = writable(0);

connection.on('plex:status', (data) => {
  plexConnected.set(data.connected as boolean);
  plexServer.set(data.server as string);
  if (data.movie_count !== undefined) plexMovieCount.set(data.movie_count as number);
  if (data.tv_count !== undefined) plexTvCount.set(data.tv_count as number);
});

export async function connectPlex() {
  try {
    await api.plexConnect();
  } catch (e) {
    throw e; // Re-throw for callers that handle it (e.g., settings page)
  }
}

export async function refreshPlexStatus() {
  try {
    const status = await api.plexStatus();
    plexConnected.set(status.connected);
    plexServer.set(status.server);
    plexMovieCount.set(status.movie_count);
    plexTvCount.set(status.tv_count);
  } catch {
    // Silently fail — Plex may not be configured yet
  }
}

// ── Bulk library metadata scan (probe_specs + DV FEL/MEL, movies only) ──
// Mirrors the dv:scan_progress / dv:scan_done pair in renames.ts, but the
// backend broadcasts a single full status_dict on every state change
// (including the terminal done/cancelled/error transition), so one handler
// covers running + terminal states.
export const plexMetadataScanStatus = writable<PlexMetadataScanStatus>({
  status: 'idle',
  processed: 0,
  succeeded: 0,
  failed: 0,
  total: 0,
  current_files: [],
  elapsed_seconds: 0,
  eta_seconds: null,
  error: null
});

connection.on('plex:metadata_scan_progress', (data) => {
  plexMetadataScanStatus.set(data as unknown as PlexMetadataScanStatus);
});

// Re-sync on reconnect -- a scan can run for hours; if the socket drops and
// reopens mid-run, don't wait for the next per-file progress tick to learn
// the current state (same rationale as renames.ts's resyncAfterReconnect).
connection.onReconnect(() => {
  refreshPlexMetadataScanStatus();
});

export async function refreshPlexMetadataScanStatus() {
  try {
    plexMetadataScanStatus.set(await api.plexScanMetadataStatus());
  } catch {
    // Silently fail — same as refreshPlexStatus
  }
}

export async function startPlexMetadataScan(scope: 'all' | 'selected', ids?: string[]) {
  const result = await api.plexScanMetadata(scope, ids);
  await refreshPlexMetadataScanStatus();
  return result;
}

export async function cancelPlexMetadataScan() {
  await api.plexScanMetadataCancel();
  await refreshPlexMetadataScanStatus();
}
