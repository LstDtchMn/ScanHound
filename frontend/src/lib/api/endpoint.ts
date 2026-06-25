// Centralized resolution of the backend HTTP/WS endpoint and auth token.
//
// Three deployment shapes share this code:
//   • Browser / PWA at scanhound.turtleland.us → same-origin (no stored URL).
//   • Desktop Tauri → same-origin (Python sidecar), token from the sidecar.
//   • Android / remote → a stored server URL + token entered by the user, since
//     the bundled app is served from tauri://localhost and is NOT same-origin.
//
// A stored server URL always wins; otherwise we fall back to same-origin (prod)
// or the dev ports.

const SERVER_KEY = 'sh-server-url';
const TOKEN_KEY = 'sh-auth-token';

export function getStoredServerUrl(): string {
  if (typeof localStorage === 'undefined') return '';
  return (localStorage.getItem(SERVER_KEY) || '').replace(/\/+$/, '');
}

export function setStoredServerUrl(url: string): void {
  if (typeof localStorage === 'undefined') return;
  const clean = url.trim().replace(/\/+$/, '');
  if (clean) localStorage.setItem(SERVER_KEY, clean);
  else localStorage.removeItem(SERVER_KEY);
}

export function getStoredToken(): string {
  if (typeof localStorage === 'undefined') return '';
  return localStorage.getItem(TOKEN_KEY) || '';
}

export function setStoredToken(token: string): void {
  if (typeof localStorage === 'undefined') return;
  if (token) localStorage.setItem(TOKEN_KEY, token);
  else localStorage.removeItem(TOKEN_KEY);
}

/** HTTP API base (no trailing slash). '' means same-origin. */
export function apiBase(): string {
  const stored = getStoredServerUrl();
  if (stored) return stored;
  if (typeof window === 'undefined') return 'http://localhost:9721';
  if (window.location.port === '5174') return 'http://localhost:9721';
  return ''; // same origin (Docker / reverse proxy / Cloudflare tunnel)
}

/** WebSocket endpoint, derived from the same source as apiBase(). */
export function wsBase(): string {
  const stored = getStoredServerUrl();
  if (stored) return stored.replace(/^http/i, 'ws') + '/ws';
  if (typeof window === 'undefined') return 'ws://localhost:9721/ws';
  if (window.location.port === '5174') return 'ws://localhost:9721/ws';
  const proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
  return `${proto}//${window.location.host}/ws`;
}
