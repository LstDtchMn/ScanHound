import { writable } from 'svelte/store';
import {
  getStoredServerUrl, setStoredServerUrl, setStoredToken, getStoredToken
} from '$lib/api/endpoint';
import { setAuthNonce, fetchWithTimeout } from '$lib/api/client';
import { connection } from './connection';

/** Reactive mirror of the configured remote server URL ('' = same-origin). */
export const serverUrl = writable<string>(getStoredServerUrl());

/** Whether the app is configured to talk to an explicit remote server. */
export const hasRemoteServer = writable<boolean>(!!getStoredServerUrl());

/** Persist URL + token, apply the token to the API client, and reconnect. */
export function saveServerConfig(url: string, token: string): void {
  setStoredServerUrl(url);
  setStoredToken(token);
  setAuthNonce(token);
  serverUrl.set(getStoredServerUrl());
  hasRemoteServer.set(!!getStoredServerUrl());
  // Re-establish the WebSocket against the new endpoint.
  try {
    connection.disconnect();
    connection.connect();
  } catch {
    /* connection may not be active yet */
  }
}

/** Clear remote config (revert to same-origin / sidecar). */
export function clearServerConfig(): void {
  saveServerConfig('', '');
}

export function currentToken(): string {
  return getStoredToken();
}

/** Probe a candidate server with GET /health (and the token, if the server
 *  enforces auth). Returns the reported version on success. */
export async function testServerConnection(
  url: string,
  token: string
): Promise<{ ok: boolean; version?: string; error?: string }> {
  const base = url.trim().replace(/\/+$/, '');
  if (!base) return { ok: false, error: 'Enter a server URL' };
  if (!/^https?:\/\//i.test(base)) return { ok: false, error: 'URL must start with http:// or https://' };
  try {
    const resp = await fetchWithTimeout(
      `${base}/health`,
      { headers: token ? { Authorization: `Bearer ${token}` } : {} },
      10_000
    );
    if (!resp.ok) {
      return { ok: false, error: resp.status === 401 ? 'Unauthorized — check the token' : `HTTP ${resp.status}` };
    }
    const data = await resp.json().catch(() => ({}));
    return { ok: true, version: (data as { version?: string }).version };
  } catch (e) {
    if (e instanceof Error && e.message.startsWith('Request timed out')) return { ok: false, error: 'Timed out' };
    return { ok: false, error: e instanceof Error ? e.message : 'Connection failed' };
  }
}
