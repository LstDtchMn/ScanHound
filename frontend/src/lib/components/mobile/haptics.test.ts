import { describe, it, expect, vi, afterEach } from 'vitest';
import { tap, success, warning } from './haptics';

afterEach(() => vi.unstubAllGlobals());

describe('haptics', () => {
  it('tap vibrates 10ms when supported', () => {
    const vibrate = vi.fn();
    vi.stubGlobal('navigator', { vibrate });
    tap();
    expect(vibrate).toHaveBeenCalledWith(10);
  });

  it('success uses a pattern', () => {
    const vibrate = vi.fn();
    vi.stubGlobal('navigator', { vibrate });
    success();
    expect(vibrate).toHaveBeenCalledWith([15, 60, 15]);
  });

  it('silently no-ops without navigator.vibrate', () => {
    vi.stubGlobal('navigator', {});
    expect(() => { tap(); success(); warning(); }).not.toThrow();
  });
});
