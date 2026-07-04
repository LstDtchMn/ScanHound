import { describe, it, expect, vi, beforeEach } from 'vitest';
import { get } from 'svelte/store';

vi.mock('./connection', () => ({ connection: { on: vi.fn() } }));

describe('addToast action support', () => {
  beforeEach(() => vi.resetModules());

  it('carries an optional action and stays back-compatible', async () => {
    const { addToast, toasts } = await import('./notifications');
    addToast('Plain', 'no action');
    const run = vi.fn();
    addToast('Dismissed', 'Boxcar Bertha', 'normal', { label: 'Undo', run });
    const list = get(toasts);
    expect(list[1].action).toBeUndefined();
    expect(list[0].action?.label).toBe('Undo');
    list[0].action?.run();
    expect(run).toHaveBeenCalled();
  });
});
