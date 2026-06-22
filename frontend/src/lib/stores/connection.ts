import { writable } from 'svelte/store';
import type { WsMessage } from '$lib/api/types';
import { getAuthNonce } from '$lib/api/client';
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

  function on(type: string, handler: (data: Record<string, unknown>) => void) {
    if (!handlers.has(type)) handlers.set(type, new Set());
    handlers.get(type)!.add(handler);
    return () => {
      handlers.get(type)?.delete(handler);
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

  function connect() {
    manualDisconnect = false;
    retryCount = 0;
    reconnectDelay = RECONNECT_DELAY;
    setupTauriListeners();
    doConnect();
  }

  function doConnect() {
    if (ws?.readyState === WebSocket.OPEN || ws?.readyState === WebSocket.CONNECTING) return;
    if (!sidecarAlive) return;
    state.set('connecting');

    const base = wsBase();
    const nonce = getAuthNonce();
    const wsUrl = nonce ? `${base}?token=${encodeURIComponent(nonce)}` : base;
    ws = new WebSocket(wsUrl);

    ws.onopen = () => {
      reconnectDelay = RECONNECT_DELAY;
      retryCount = 0;
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
      doConnect();
      reconnectDelay = Math.min(reconnectDelay * 1.5, MAX_RECONNECT_DELAY);
    }, reconnectDelay);
  }

  function disconnect() {
    manualDisconnect = true;
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

  return { state, version, connect, disconnect, send, on };
}

export const connection = createConnection();
