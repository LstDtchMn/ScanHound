import { get } from 'svelte/store';
import { beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('$lib/api/client', () => ({
  api: {
    authStatus: vi.fn(),
    authLogin: vi.fn(),
    authSetPassword: vi.fn(),
    authLogout: vi.fn().mockResolvedValue({ ok: true })
  },
  setAuthNonce: vi.fn()
}));

const { api, setAuthNonce } = await import('$lib/api/client');
const { getStoredToken, setStoredToken } = await import('$lib/api/endpoint');
const {
  authRequired,
  hasPassword,
  setupRequired,
  authChecked,
  refreshAuthStatus,
  login,
  setPassword,
  logout,
  handleUnauthorized
} = await import('./auth');

beforeEach(() => {
  localStorage.clear();
  vi.clearAllMocks();
  authRequired.set(false);
  hasPassword.set(false);
  setupRequired.set(false);
  authChecked.set(false);
});

describe('auth store', () => {
  it('refreshAuthStatus mirrors server status into the stores', async () => {
    vi.mocked(api.authStatus).mockResolvedValue({
      auth_required: true,
      has_password: true,
      nonce_active: false,
      setup_required: false
    });
    const s = await refreshAuthStatus();
    expect(s?.auth_required).toBe(true);
    expect(get(authRequired)).toBe(true);
    expect(get(hasPassword)).toBe(true);
    expect(get(authChecked)).toBe(true);
  });

  it('refreshAuthStatus treats an unreachable server as open', async () => {
    vi.mocked(api.authStatus).mockRejectedValue(new Error('network'));
    const s = await refreshAuthStatus();
    expect(s).toBeNull();
    expect(get(authRequired)).toBe(false);
    expect(get(authChecked)).toBe(true);
  });

  it('refreshAuthStatus mirrors setup_required into its store (SH-H01)', async () => {
    vi.mocked(api.authStatus).mockResolvedValue({
      auth_required: false,
      has_password: false,
      nonce_active: false,
      setup_required: true
    });
    await refreshAuthStatus();
    expect(get(setupRequired)).toBe(true);
  });

  it('refreshAuthStatus treats an unreachable server as not needing setup', async () => {
    vi.mocked(api.authStatus).mockRejectedValue(new Error('network'));
    await refreshAuthStatus();
    expect(get(setupRequired)).toBe(false);
  });

  it('login stores the token and applies it to the client', async () => {
    vi.mocked(api.authLogin).mockResolvedValue({
      token: 'sess-tok',
      expires_at: '2099-01-01T00:00:00Z'
    });
    await login('hunter2');
    expect(api.authLogin).toHaveBeenCalledWith('hunter2');
    expect(getStoredToken()).toBe('sess-tok');
    expect(setAuthNonce).toHaveBeenCalledWith('sess-tok');
    expect(get(authRequired)).toBe(true);
  });

  it('setPassword forwards both passwords and flips hasPassword', async () => {
    vi.mocked(api.authSetPassword).mockResolvedValue({ ok: true });
    await setPassword('newpw12345', 'oldpw12345');
    expect(api.authSetPassword).toHaveBeenCalledWith('newpw12345', 'oldpw12345');
    expect(get(hasPassword)).toBe(true);
  });

  it('logout and handleUnauthorized clear the stored token', () => {
    setStoredToken('sess-tok');
    logout();
    expect(getStoredToken()).toBe('');
    expect(setAuthNonce).toHaveBeenCalledWith('');

    setStoredToken('sess-tok2');
    handleUnauthorized();
    expect(getStoredToken()).toBe('');
  });
});
