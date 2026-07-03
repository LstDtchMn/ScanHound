import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';

// Minimal fake WebSocket so connect()/doConnect() can run in jsdom without a
// real socket. Tracks all instances so a test can drive onopen/onclose.
class FakeWebSocket {
  static instances: FakeWebSocket[] = [];
  static OPEN = 1;
  static CONNECTING = 0;
  readyState = FakeWebSocket.CONNECTING;
  onopen: (() => void) | null = null;
  onclose: (() => void) | null = null;
  onmessage: ((event: { data: string }) => void) | null = null;
  onerror: (() => void) | null = null;
  url: string;
  constructor(url: string) {
    this.url = url;
    FakeWebSocket.instances.push(this);
  }
  close() {
    this.readyState = 3; // CLOSED
    this.onclose?.();
  }
  send() {}
  // Test helper — simulate the server accepting the connection.
  open() {
    this.readyState = FakeWebSocket.OPEN;
    this.onopen?.();
  }
}

vi.mock('$lib/api/client', () => ({ getAuthNonce: () => null }));
vi.mock('$lib/api/endpoint', () => ({ wsBase: () => 'ws://test/ws' }));

describe('connection reconnect hook', () => {
  let originalWebSocket: unknown;

  beforeEach(() => {
    vi.resetModules();
    FakeWebSocket.instances = [];
    originalWebSocket = (globalThis as any).WebSocket;
    (globalThis as any).WebSocket = FakeWebSocket as unknown as typeof WebSocket;
  });

  afterEach(() => {
    (globalThis as any).WebSocket = originalWebSocket;
  });

  it('does not fire onReconnect for the very first connection', async () => {
    const { connection } = await import('./connection');
    const cb = vi.fn();
    connection.onReconnect(cb);
    connection.connect();

    const first = FakeWebSocket.instances[0];
    first.open();

    expect(cb).not.toHaveBeenCalled();
    connection.disconnect();
  });

  it('fires onReconnect handlers when a connection reopens after a prior close', async () => {
    vi.useFakeTimers();
    try {
      const { connection } = await import('./connection');
      const cb = vi.fn();
      connection.onReconnect(cb);
      connection.connect();

      const first = FakeWebSocket.instances[0];
      first.open();
      expect(cb).not.toHaveBeenCalled();

      // Simulate the socket dropping — this schedules a reconnect timer.
      first.onclose?.();
      await vi.advanceTimersByTimeAsync(2000);
      expect(FakeWebSocket.instances.length).toBeGreaterThan(1);
      const second = FakeWebSocket.instances[FakeWebSocket.instances.length - 1];
      second.open();

      expect(cb).toHaveBeenCalledTimes(1);
      connection.disconnect();
    } finally {
      vi.useRealTimers();
    }
  });

  it('a handler unsubscribed via the returned cleanup is not called on a later reconnect', async () => {
    vi.useFakeTimers();
    try {
      const { connection } = await import('./connection');
      const cb = vi.fn();
      const unsub = connection.onReconnect(cb);
      connection.connect();

      const first = FakeWebSocket.instances[0];
      first.open();
      unsub();

      first.onclose?.();
      await vi.advanceTimersByTimeAsync(2000);
      const second = FakeWebSocket.instances[FakeWebSocket.instances.length - 1];
      second.open();

      expect(cb).not.toHaveBeenCalled();
      connection.disconnect();
    } finally {
      vi.useRealTimers();
    }
  });

  it('a manual disconnect()+connect() cycle does not count as a reconnect (fresh session)', async () => {
    const { connection } = await import('./connection');
    const cb = vi.fn();
    connection.onReconnect(cb);
    connection.connect();
    FakeWebSocket.instances[0].open();

    connection.disconnect();
    connection.connect();
    FakeWebSocket.instances[FakeWebSocket.instances.length - 1].open();

    expect(cb).not.toHaveBeenCalled();
  });
});
