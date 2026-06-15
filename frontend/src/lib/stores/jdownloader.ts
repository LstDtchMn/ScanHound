import { writable } from 'svelte/store';
import { api } from '$lib/api/client';

export interface JdConnection {
  connected: boolean;
  device?: string;
  error?: string;
  checking: boolean;
}

/** Shared JDownloader connection state, used by the scan checklist + Settings. */
export const jdConnection = writable<JdConnection>({ connected: false, checking: false });

let inFlight = false;

export async function refreshJdConnection() {
  if (inFlight) return;
  inFlight = true;
  jdConnection.update((s) => ({ ...s, checking: true }));
  try {
    const r = await api.jdTest();
    jdConnection.set({ connected: r.connected, device: r.device, error: r.error, checking: false });
  } catch (e) {
    jdConnection.set({
      connected: false,
      error: e instanceof Error ? e.message : 'JDownloader check failed',
      checking: false
    });
  } finally {
    inFlight = false;
  }
}
