import { describe, expect, it, beforeEach, vi } from 'vitest';
import {
  apiBase,
  wsBase,
  getStoredServerUrl,
  setStoredServerUrl,
  getStoredToken,
  setStoredToken
} from './endpoint';

function setLocation(port: string, protocol = 'http:', host = `localhost:${port}`) {
  vi.stubGlobal('window', {
    location: { port, protocol, host }
  });
}

describe('endpoint storage helpers', () => {
  beforeEach(() => {
    localStorage.clear();
  });

  it('stores and trims a server URL, stripping trailing slashes', () => {
    setStoredServerUrl('https://scanhound.example.com///');
    expect(getStoredServerUrl()).toBe('https://scanhound.example.com');
  });

  it('clears the stored URL when set to empty', () => {
    setStoredServerUrl('https://scanhound.example.com');
    setStoredServerUrl('');
    expect(getStoredServerUrl()).toBe('');
  });

  it('stores and clears the auth token', () => {
    setStoredToken('secret');
    expect(getStoredToken()).toBe('secret');
    setStoredToken('');
    expect(getStoredToken()).toBe('');
  });
});

describe('apiBase()', () => {
  beforeEach(() => {
    localStorage.clear();
    vi.unstubAllGlobals();
  });

  it('prefers a stored server URL over everything else', () => {
    setStoredServerUrl('https://scanhound.example.com');
    setLocation('5174');
    expect(apiBase()).toBe('https://scanhound.example.com');
  });

  it('falls back to the dev backend port when served from the Vite dev port', () => {
    setLocation('5174');
    expect(apiBase()).toBe('http://localhost:9721');
  });

  it('falls back to same-origin ("") for any other port (prod / Tauri)', () => {
    setLocation('443', 'https:', 'scanhound.turtleland.us');
    expect(apiBase()).toBe('');
  });
});

describe('wsBase()', () => {
  beforeEach(() => {
    localStorage.clear();
    vi.unstubAllGlobals();
  });

  it('derives ws:// from a stored http:// server URL', () => {
    setStoredServerUrl('http://192.168.1.50:9721');
    expect(wsBase()).toBe('ws://192.168.1.50:9721/ws');
  });

  it('derives wss:// from a stored https:// server URL', () => {
    setStoredServerUrl('https://scanhound.example.com');
    expect(wsBase()).toBe('wss://scanhound.example.com/ws');
  });

  it('uses the dev backend port when served from the Vite dev port', () => {
    setLocation('5174');
    expect(wsBase()).toBe('ws://localhost:9721/ws');
  });

  it('upgrades to wss:// on a same-origin https page', () => {
    setLocation('443', 'https:', 'scanhound.turtleland.us');
    expect(wsBase()).toBe('wss://scanhound.turtleland.us/ws');
  });

  it('stays ws:// on a same-origin http page', () => {
    setLocation('8080', 'http:', 'localhost:8080');
    expect(wsBase()).toBe('ws://localhost:8080/ws');
  });
});
