// Browser / self-hosted password auth. The desktop Tauri sidecar auto-logs-in
// with the nonce (see +layout.svelte initAuth); this store covers the case
// where the server has a settable password and the user logs in via /login.
import { writable } from 'svelte/store';
import { api, setAuthNonce } from '$lib/api/client';
import { setStoredToken } from '$lib/api/endpoint';

/** Whether the server requires a token (a password is set or a nonce is active). */
export const authRequired = writable<boolean>(false);
/** Whether a password has been configured (vs. nonce-only / open). */
export const hasPassword = writable<boolean>(false);
/** True only in the fresh-install / wiped-credential state where the server
 *  fails CLOSED (SH-H01) despite auth_required being false — the frontend
 *  must show the set-password prompt instead of entering the app or bouncing
 *  between '/' and '/login' on 401s. */
export const setupRequired = writable<boolean>(false);
/** Set once the first status check has resolved. */
export const authChecked = writable<boolean>(false);

/** Ask the server whether auth is required. Treats an unreachable / auth-less
 *  server as open so dev and same-origin setups keep working untouched. */
export async function refreshAuthStatus() {
  try {
    const s = await api.authStatus();
    authRequired.set(s.auth_required);
    hasPassword.set(s.has_password);
    setupRequired.set(s.setup_required);
    return s;
  } catch {
    authRequired.set(false);
    hasPassword.set(false);
    setupRequired.set(false);
    return null;
  } finally {
    authChecked.set(true);
  }
}

/** Exchange a password for a session token and apply it to the API client. */
export async function login(password: string) {
  const { token } = await api.authLogin(password);
  setStoredToken(token);
  setAuthNonce(token);
  authRequired.set(true);
}

/** Set or change the password. Omit currentPassword for the first set. */
export async function setPassword(newPassword: string, currentPassword?: string) {
  await api.authSetPassword(newPassword, currentPassword);
  hasPassword.set(true);
}

/** Clear the local token (best-effort server-side invalidation). */
export function logout() {
  api.authLogout().catch(() => {});
  setStoredToken('');
  setAuthNonce('');
}

/** Token missing/expired mid-session — drop it so the app re-authenticates. */
export function handleUnauthorized() {
  setStoredToken('');
  setAuthNonce('');
}
