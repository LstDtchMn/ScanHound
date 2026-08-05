// SH-P2-17: the WebSocket handshake must not carry the 30-day session token in
// the URL, because every proxy in front of this app (NPM/nginx, Cloudflare) and
// uvicorn itself log the full request line verbatim. It carries a single-use,
// ~30s ticket minted by an authorized POST instead.
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { get } from 'svelte/store';

// Minimal fake WebSocket so connect()/doConnect() can run in jsdom without a
// real socket. Tracks all instances so a test can inspect URLs and drive
// onopen/onmessage/onclose.
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
  // Test helper — simulate the server accepting the connection and sending
  // the handshake frame the store waits for before reporting 'connected'.
  open(version = '2.0.0-dev') {
    this.readyState = FakeWebSocket.OPEN;
    this.onopen?.();
    this.onmessage?.({ data: JSON.stringify({ type: 'connected', data: { version } }) });
  }
}

const SESSION_TOKEN = 'sess-30day-abcdef0123456789';

let currentNonce = SESSION_TOKEN;
const wsTicketMock = vi.fn();

vi.mock('$lib/api/client', () => ({
  getAuthNonce: () => currentNonce,
  api: { authWsTicket: () => wsTicketMock() }
}));
vi.mock('$lib/api/endpoint', () => ({ wsBase: () => 'ws://test/ws' }));

function lastSocket(): FakeWebSocket {
  return FakeWebSocket.instances[FakeWebSocket.instances.length - 1];
}

/** Deferred whose resolution the test controls, so an attempt can be held
 *  mid-ticket-request while something else happens. */
function deferred<T>() {
  let resolve!: (v: T) => void;
  const promise = new Promise<T>((r) => { resolve = r; });
  return { promise, resolve };
}

describe('WebSocket handshake credential (SH-P2-17)', () => {
  let originalWebSocket: unknown;

  beforeEach(() => {
    vi.resetModules();
    FakeWebSocket.instances = [];
    currentNonce = SESSION_TOKEN;
    wsTicketMock.mockReset();
    originalWebSocket = (globalThis as any).WebSocket;
    (globalThis as any).WebSocket = FakeWebSocket as unknown as typeof WebSocket;
  });

  afterEach(() => {
    (globalThis as any).WebSocket = originalWebSocket;
  });

  // POSITIVE CONTROL. The socket must still work end to end: one socket
  // opened, at the ticket URL, reaching 'connected' with the server version.
  // A "fix" that simply stopped sending a credential, or that broke the
  // handshake, fails here.
  it('opens exactly one socket at ?ticket=<ticket> and reaches connected', async () => {
    wsTicketMock.mockResolvedValue({ ticket: 'TICKET-1', expires_in: 30 });
    const { connection } = await import('./connection');

    await connection.connect();

    expect(wsTicketMock).toHaveBeenCalledTimes(1);
    expect(FakeWebSocket.instances).toHaveLength(1);
    expect(lastSocket().url).toBe('ws://test/ws?ticket=TICKET-1');

    lastSocket().open('2.0.0-dev');
    expect(get(connection.state)).toBe('connected');
    expect(get(connection.version)).toBe('2.0.0-dev');

    connection.disconnect();
  });

  it('never puts the session token in the URL when a ticket is available', async () => {
    wsTicketMock.mockResolvedValue({ ticket: 'TICKET-1', expires_in: 30 });
    const { connection } = await import('./connection');

    await connection.connect();

    const url = lastSocket().url;
    expect(url).not.toContain(SESSION_TOKEN);
    expect(url).not.toContain('token=');
    connection.disconnect();
  });

  it('percent-encodes the ticket it received', async () => {
    // token_urlsafe() can emit '-' and '_' but a ticket is opaque to us; if a
    // future backend returns padding or '+' it must not break the query string.
    wsTicketMock.mockResolvedValue({ ticket: 'a+b/c=', expires_in: 30 });
    const { connection } = await import('./connection');

    await connection.connect();

    expect(lastSocket().url).toBe('ws://test/ws?ticket=a%2Bb%2Fc%3D');
    connection.disconnect();
  });

  // DISAGREEING CASE. An implementation that mints one ticket and caches it
  // passes every other test in this file, and then silently fails to ever
  // reconnect against the real backend, where the ticket is single-use and
  // dies in ~30s. This is the case that separates the two.
  it('mints a FRESH ticket for an automatic reconnect, not the cached one', async () => {
    wsTicketMock
      .mockResolvedValueOnce({ ticket: 'TICKET-1', expires_in: 30 })
      .mockResolvedValueOnce({ ticket: 'TICKET-2', expires_in: 30 });
    vi.useFakeTimers();
    try {
      const { connection } = await import('./connection');

      await connection.connect();
      lastSocket().open();
      expect(lastSocket().url).toBe('ws://test/ws?ticket=TICKET-1');

      // Server drops the socket -> the store schedules a reconnect (2000ms).
      lastSocket().close();
      await vi.advanceTimersByTimeAsync(2000);
      // Second advance yields another macrotask so the awaited ticket request
      // inside the timer callback has certainly settled.
      await vi.advanceTimersByTimeAsync(0);

      expect(wsTicketMock).toHaveBeenCalledTimes(2);
      expect(FakeWebSocket.instances).toHaveLength(2);
      expect(lastSocket().url).toBe('ws://test/ws?ticket=TICKET-2');

      // POSITIVE CONTROL for the reconnect: the new socket still works.
      lastSocket().open();
      expect(get(connection.state)).toBe('connected');
      connection.disconnect();
    } finally {
      vi.useRealTimers();
    }
  });

  /** The shape request() throws: an Error carrying the HTTP status. The
   *  status is what separates "this backend has no such route" from "the
   *  route is there and momentarily unhappy". */
  function apiError(status: number, message: string) {
    const e = new Error(message) as Error & { status?: number };
    e.status = status;
    return e;
  }

  // REVERSED for A-2. This test previously asserted that a 404 downgrades to
  // ?token=<30-day session token>, i.e. it pinned the leak as desired
  // behaviour — so its green result was not evidence that the
  // no-token-in-URL property held. A 404/405 does not prove the backend is
  // old: reverse-proxy drift, a partial deploy, an intermediary error page or
  // a wrong API base all produce one, which made the status code a downgrade
  // oracle. Legacy support is now an operator decision on the SERVER
  // (SCANHOUND_WS_ALLOW_TOKEN_QUERY=1), not an inference from a response.
  it.each([[404, 'Not Found'], [405, 'Method Not Allowed']])(
    'does NOT downgrade to ?token= on a %i', async (status, text) => {
      wsTicketMock.mockRejectedValue(apiError(status, text));
      const { connection } = await import('./connection');

      await connection.connect();

      expect(FakeWebSocket.instances).toHaveLength(0);
      const leaked = FakeWebSocket.instances.filter((s) =>
        s.url.includes(SESSION_TOKEN));
      expect(leaked).toHaveLength(0);
      connection.disconnect();
    });

  it('does not downgrade when the ticket response is malformed', async () => {
    // A 200 with no ticket field means the route EXISTS and misbehaved --
    // a backend bug, not an old backend. Downgrading here would be the same
    // leak as any other transient failure, so this retries instead. (This
    // test previously asserted the downgrade; the contract was tightened
    // after a transient-500 leak was demonstrated.)
    wsTicketMock.mockResolvedValue({ expires_in: 30 });
    const { connection } = await import('./connection');

    await connection.connect();

    expect(FakeWebSocket.instances).toHaveLength(0);
    connection.disconnect();
  });

  // ── transient failures must NOT downgrade the credential ──────────────
  //
  // Falling back on ANY error re-leaks the 30-day token, and the worst case is
  // also the likeliest: a backend restart makes the ticket POST fail AND makes
  // reconnects frequent, so the token lands in the maximum number of proxy log
  // lines exactly when it is least intended. Only a genuinely absent route may
  // downgrade.

  it('does NOT leak the session token when minting fails transiently', async () => {
    wsTicketMock.mockRejectedValue(apiError(500, 'API error: 500 Internal'));
    const { connection } = await import('./connection');

    await connection.connect();

    // No socket at all is correct: retrying is better than downgrading.
    expect(FakeWebSocket.instances).toHaveLength(0);
    connection.disconnect();
  });

  it.each([
    [500, 'Internal Server Error'],
    [502, 'Bad Gateway'],
    [503, 'Service Unavailable'],
    [401, 'Unauthorized'],
  ])('does not downgrade to ?token= on a %i', async (status, text) => {
    wsTicketMock.mockRejectedValue(apiError(status, text));
    const { connection } = await import('./connection');

    await connection.connect();

    const leaked = FakeWebSocket.instances.filter((s) =>
      s.url.includes(SESSION_TOKEN));
    expect(leaked).toHaveLength(0);
    connection.disconnect();
  });

  it('does not downgrade when the request fails with no status at all', async () => {
    // A network-level failure (DNS, offline, CORS) never reaches a status.
    // Treating "unknown" as "route absent" is the same leak by another route.
    wsTicketMock.mockRejectedValue(new Error('Failed to fetch'));
    const { connection } = await import('./connection');

    await connection.connect();

    expect(FakeWebSocket.instances).toHaveLength(0);
    connection.disconnect();
  });

  it('recovers once minting starts working again', async () => {
    // POSITIVE CONTROL for the refusal above: declining to connect must be
    // temporary, or "never leaks the token" is satisfied by never connecting.
    wsTicketMock
      .mockRejectedValueOnce(apiError(503, 'Service Unavailable'))
      .mockResolvedValue({ ticket: 'tkt-recovered', expires_in: 30 });
    const { connection } = await import('./connection');

    await connection.connect();
    expect(FakeWebSocket.instances).toHaveLength(0);

    await connection.connect();          // the retry the scheduler would make

    expect(lastSocket().url).toBe('ws://test/ws?ticket=tkt-recovered');
    lastSocket().open();
    expect(get(connection.state)).toBe('connected');
    connection.disconnect();
  });

  it('requests no ticket and sends no credential when there is no session token', async () => {
    // Dev / SCANHOUND_ALLOW_OPEN posture: nothing to protect, nothing to mint.
    currentNonce = '';
    const { connection } = await import('./connection');

    await connection.connect();

    expect(wsTicketMock).not.toHaveBeenCalled();
    expect(lastSocket().url).toBe('ws://test/ws');
    connection.disconnect();
  });

  // DISAGREEING CASE. Awaiting the ticket opens a window in which `ws` is
  // still null, so the readyState guard cannot stop a second attempt. A
  // straightforward `async doConnect()` with no generation guard opens TWO
  // sockets here and passes every other test in this file.
  it('opens one socket when a second connect() lands while a ticket is in flight', async () => {
    const first = deferred<{ ticket: string; expires_in: number }>();
    const second = deferred<{ ticket: string; expires_in: number }>();
    wsTicketMock
      .mockReturnValueOnce(first.promise)
      .mockReturnValueOnce(second.promise);
    const { connection } = await import('./connection');

    const p1 = connection.connect();
    const p2 = connection.connect();
    expect(FakeWebSocket.instances).toHaveLength(0); // both still awaiting
    first.resolve({ ticket: 'TICKET-1', expires_in: 30 });
    second.resolve({ ticket: 'TICKET-2', expires_in: 30 });
    await Promise.all([p1, p2]);

    expect(FakeWebSocket.instances).toHaveLength(1);
    // The surviving socket is the newest attempt, not the superseded one.
    expect(lastSocket().url).toBe('ws://test/ws?ticket=TICKET-2');
    lastSocket().open();
    expect(get(connection.state)).toBe('connected');
    connection.disconnect();
  });

  it('opens no socket when disconnect() lands while a ticket is in flight', async () => {
    const pending = deferred<{ ticket: string; expires_in: number }>();
    wsTicketMock.mockReturnValueOnce(pending.promise);
    const { connection } = await import('./connection');

    const p = connection.connect();
    connection.disconnect();
    pending.resolve({ ticket: 'TICKET-1', expires_in: 30 });
    await p;

    expect(FakeWebSocket.instances).toHaveLength(0);
    expect(get(connection.state)).toBe('disconnected');
  });
});
