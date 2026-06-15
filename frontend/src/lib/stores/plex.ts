import { writable } from 'svelte/store';
import { api } from '$lib/api/client';
import { connection } from './connection';

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
