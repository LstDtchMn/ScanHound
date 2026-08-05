import { writable } from 'svelte/store';
import type { WsMessage } from '$lib/api/types';
import { api, getAuthNonce } from '$lib/api/client';
import { wsBase } from '$lib/api/endpoint';

const RECONNECT_DELAY = 2000;
const MAX_RECONNECT_DELAY = 30000;
const MAX_RETRIES = 20;

export type ConnectionState = 'connecting' | 'connected' | 'disconnected' | 'reconnecting' | 'failed';

function createConnection() {
  const state = writable<ConnectionState>('disconnected');
  const version = writable<string>('');
  const handlers = new Map<string, Set<(data: Record<string, unknown>) => void>>();

  let ws: WebSocket | null = null;
  let reconnectDelay = RECONNECT_DELAY;
  let reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  let retryCount = 0;
  let sidecarAlive = true;
  let manualDisconnect = false;
  let tauriListenersRegistered = false;
  /** True once the very first connection of this session has opened — so we
   *  can tell a genuine reconnect (a later open) apart from the initial one. */
  let hasConnectedOnce = false;
  /** Bumped by every connect attempt and by disconnect(). doConnect() captures
   *  it before awaiting the WebSocket ticket and abandons the attempt if it
   *  changed while it was suspended. Without this, the await opens a window in
   *  which a second attempt (a reconnect timer firing, or saveServerConfig's
   *  disconnect()+connect() against a different server) slips past the
   *  readyState guard — `ws` is still null — and we end up with two sockets,
   *  one of them pointed at the old endpoint. */
  let connectGeneration = 0;
  const reconnectHandlers = new Set<() => void>();

  function on(type: string, handler: (data: Record<string, unknown>) => void) {
    if (!handlers.has(type)) handlers.set(type, new Set());
    handlers.get(type)!.add(handler);
    return () => {
      handlers.get(type)?.delete(handler);
    };
  }

  /** Register a callback fired whenever the socket re-opens after having
   *  previously been open (i.e. a real reconnect, not the initial connect).
   *  Consumers use this to re-fetch a snapshot so events missed while
   *  disconnected aren't lost forever. Returns an unsubscribe function. */
  function onReconnect(handler: () => void) {
    reconnectHandlers.add(handler);
    return () => {
      reconnectHandlers.delete(handler);
    };
  }

  function dispatch(msg: WsMessage) {
    const fns = handlers.get(msg.type);
    if (fns) fns.forEach((fn) => fn(msg.data));
    const wild = handlers.get('*');
    if (wild) wild.forEach((fn) => fn({ type: msg.type, ...msg.data }));
  }

  function setupTauriListeners() {
    if (tauriListenersRegistered) return;
    if (typeof window === 'undefined' || !('__TAURI__' in window)) return;
    tauriListenersRegistered = true;

    import('@tauri-apps/api/event').then(({ listen }) => {
      listen('sidecar-terminated', () => {
        sidecarAlive = false;
        if (reconnectTimer) clearTimeout(reconnectTimer);
        ws?.close();
        ws = null;
        state.set('disconnected');
      });
      listen('sidecar-restarting', () => {
        sidecarAlive = true;
        state.set('reconnecting' as ConnectionState);
      });
      listen('sidecar-failed', () => {
        sidecarAlive = false;
        if (reconnectTimer) clearTimeout(reconnectTimer);
        ws?.close();
        ws = null;
        state.set('failed' as ConnectionState);
      });
    }).catch(() => { /* not in Tauri context */ });
  }

  /** Ask the backend for a fresh single-use WebSocket ticket.
   *
   *  A WebSocket handshake cannot carry an Authorization header, so the
   *  credential has to ride in the URL — and NPM/nginx, Cloudflare and
   *  uvicorn all record the full request line, which meant every access log
   *  held a live copy of the 30-day session token. A ticket is minted by an
   *  already-authorized POST, dies in ~30s and on first redemption, so a
   *  logged one is worthless.
   *
   *  Returns '' ONLY when the route is genuinely absent (404/405), i.e. an
   *  older backend that predates ws-tickets; the caller then falls back to the
   *  legacy ?token= form for that case alone.
   *
   *  Falling back on ANY failure would defeat the point. A transient 5xx or a
   *  network blip would re-leak the 30-day token into every proxy log — and a
   *  backend restart is precisely when the ticket POST fails AND when
   *  reconnect attempts are most frequent, so the worst case is also the
   *  likeliest one. Verified as a live behaviour, not a theoretical one,
   *  before this was narrowed. On a transient failure we return null and let
   *  the socket retry rather than downgrade the credential. */
  async function fetchWsTicket(): Promise<string | null> {
    try {
      const res = await api.authWsTicket();
      return typeof res?.ticket === 'string' ? res.ticket : null;
    } catch (e) {
      const status = (e as { status?: number } | null)?.status;
      // Route absent -> this backend cannot mint tickets at all; downgrade.
      if (status === 404 || status === 405) return '';
      // Anything else is transient. Do NOT downgrade.
      return null;
    }
  }

  function connect() {
    manualDisconnect = false;
    retryCount = 0;
    reconnectDelay = RECONNECT_DELAY;
    setupTauriListeners();
    // Returned so callers (and tests) can await the handshake now that
    // minting a ticket makes this asynchronous. It never rejects.
    return doConnect();
  }

  async function doConnect(): Promise<void> {
    if (ws?.readyState === WebSocket.OPEN || ws?.readyState === WebSocket.CONNECTING) return;
    if (!sidecarAlive) return;
    const generation = ++connectGeneration;
    state.set('connecting');

    const base = wsBase();
    const nonce = getAuthNonce();
    let wsUrl = base;
    if (nonce) {
      // Minted per attempt and never cached: the ticket is single-use and
      // expires in seconds, so a reconnect that replayed the previous one
      // would be rejected and the socket would never come back.
      const ticket = await fetchWsTicket();
      if (ticket === null) {
        // Transient failure minting a ticket. Retry rather than fall back to
        // the raw token: a backend restart makes this fire on every attempt of
        // a reconnect storm, which is exactly when the token would be written
        // into the most proxy log lines.
        if (generation === connectGeneration && !manualDisconnect) {
          state.set('disconnected');
          scheduleReconnect();
        }
        return;
      }
      wsUrl = ticket
        ? `${base}?ticket=${encodeURIComponent(ticket)}`
        : `${base}?token=${encodeURIComponent(nonce)}`;
    }
    // Re-check after the await: a disconnect, a dead sidecar or a newer
    // attempt may have landed while the ticket request was in flight.
    if (generation !== connectGeneration || manualDisconnect || !sidecarAlive) return;

    try {
      ws = new WebSocket(wsUrl);
    } catch {
      // A stored server URL that isn't a valid endpoint throws here. This used
      // to surface synchronously to connect()'s caller (server.ts wraps it in
      // try/catch); now it would be an unhandled rejection instead. Retrying
      // cannot fix a malformed URL, so stop rather than schedule a reconnect.
      ws = null;
      state.set('disconnected');
      return;
    }

    ws.onopen = () => {
      reconnectDelay = RECONNECT_DELAY;
      retryCount = 0;
      if (hasConnectedOnce) {
        reconnectHandlers.forEach((fn) => fn());
      }
      hasConnectedOnce = true;
    };

    ws.onmessage = (event) => {
      try {
        const msg: WsMessage = JSON.parse(event.data);
        if (msg.type === 'connected') {
          state.set('connected');
          version.set((msg.data.version as string) || '');
        }
        dispatch(msg);
      } catch {
        console.error('Failed to parse WS message', event.data);
      }
    };

    ws.onclose = () => {
      state.set('disconnected');
      ws = null;
      if (!manualDisconnect) {
        scheduleReconnect();
      }
    };

    ws.onerror = () => {
      ws?.close();
    };
  }

  function scheduleReconnect() {
    if (!sidecarAlive) return;
    retryCount++;
    if (retryCount > MAX_RETRIES) {
      state.set('failed');
      return;
    }
    if (reconnectTimer) clearTimeout(reconnectTimer);
    reconnectTimer = setTimeout(() => {
      void doConnect();
      reconnectDelay = Math.min(reconnectDelay * 1.5, MAX_RECONNECT_DELAY);
    }, reconnectDelay);
  }

  function disconnect() {
    manualDisconnect = true;
    hasConnectedOnce = false;
    // Abandon any attempt currently suspended on its ticket request.
    connectGeneration++;
    if (reconnectTimer) clearTimeout(reconnectTimer);
    ws?.close();
    ws = null;
    state.set('disconnected');
  }

  function send(msg: WsMessage) {
    if (ws?.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify(msg));
    }
  }

  return { state, version, connect, disconnect, send, on, onReconnect };
}

export const connection = createConnection();
