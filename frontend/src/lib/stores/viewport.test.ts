import { describe, it, expect, vi, beforeEach } from 'vitest';
import { get } from 'svelte/store';

type MqlListener = (e: { matches: boolean }) => void;

function mockMatchMedia(initial: Record<string, boolean>) {
  const listeners = new Map<string, MqlListener[]>();
  const state = { ...initial };
  vi.stubGlobal('matchMedia', (query: string) => ({
    get matches() { return state[query] ?? false; },
    media: query,
    addEventListener: (_: string, cb: MqlListener) => {
      listeners.set(query, [...(listeners.get(query) ?? []), cb]);
    },
    removeEventListener: () => {}
  }));
  return {
    set(query: string, matches: boolean) {
      state[query] = matches;
      for (const cb of listeners.get(query) ?? []) cb({ matches });
    }
  };
}

const NARROW = '(max-width: 767px)';
const COARSE = '(pointer: coarse)';

describe('isPhone', () => {
  beforeEach(() => vi.resetModules());

  it('is true only when narrow AND coarse', async () => {
    mockMatchMedia({ [NARROW]: true, [COARSE]: true });
    const { isPhone } = await import('./viewport');
    expect(get(isPhone)).toBe(true);
  });

  it('is false for a narrow desktop window (fine pointer)', async () => {
    mockMatchMedia({ [NARROW]: true, [COARSE]: false });
    const { isPhone } = await import('./viewport');
    expect(get(isPhone)).toBe(false);
  });

  it('updates live when the viewport changes (rotate/resize)', async () => {
    const mql = mockMatchMedia({ [NARROW]: false, [COARSE]: true });
    const { isPhone } = await import('./viewport');
    let last: boolean | undefined;
    const unsub = isPhone.subscribe((v) => (last = v));
    expect(last).toBe(false);
    mql.set(NARROW, true);
    expect(last).toBe(true);
    unsub();
  });
});
