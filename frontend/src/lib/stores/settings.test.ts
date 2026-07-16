import { get } from 'svelte/store';
import { beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('$lib/api/client', () => ({
  api: {
    getSettings: vi.fn(),
    updateSettings: vi.fn()
  }
}));

vi.mock('$lib/stores/notifications', () => ({
  addToast: vi.fn()
}));

const { api } = await import('$lib/api/client');
const { addToast } = await import('$lib/stores/notifications');
const { settings, settingsLoaded, loadSettings, saveSettings } = await import('./settings');

describe('settings store error propagation', () => {
  beforeEach(() => {
    vi.mocked(api.getSettings).mockReset();
    vi.mocked(api.updateSettings).mockReset();
    vi.mocked(addToast).mockReset();
    settingsLoaded.set(false);
    settings.set({});
  });

  it('loadSettings returns true and loads on success', async () => {
    vi.mocked(api.getSettings).mockResolvedValue({ min_size_mb: 200 });
    const ok = await loadSettings();
    expect(ok).toBe(true);
    expect(get(settingsLoaded)).toBe(true);
    expect(get(settings).min_size_mb).toBe(200);
  });

  it('loadSettings returns false (not true) when the API call rejects', async () => {
    vi.mocked(api.getSettings).mockRejectedValue(new Error('network down'));
    const ok = await loadSettings();
    expect(ok).toBe(false);
    expect(get(settingsLoaded)).toBe(false);
  });

  it('saveSettings returns false (not true) when the API call rejects, leaving isDirty true', async () => {
    vi.mocked(api.getSettings).mockResolvedValue({ min_size_mb: 200 });
    await loadSettings();
    settings.update((s) => ({ ...s, min_size_mb: 999 }));
    vi.mocked(api.updateSettings).mockRejectedValue(new Error('save failed'));
    const ok = await saveSettings();
    expect(ok).toBe(false);
    // the change must NOT be silently accepted as the new baseline
    expect(get(settings).min_size_mb).toBe(999);
  });

  it('saveSettings returns true on success', async () => {
    vi.mocked(api.getSettings).mockResolvedValue({ min_size_mb: 200 });
    await loadSettings();
    settings.update((s) => ({ ...s, min_size_mb: 999 }));
    vi.mocked(api.updateSettings).mockResolvedValue({});
    const ok = await saveSettings();
    expect(ok).toBe(true);
  });
});
