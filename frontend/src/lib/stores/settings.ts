import { writable, derived, get } from 'svelte/store';
import { api } from '$lib/api/client';
import type { Settings } from '$lib/api/types';
import { addToast } from './notifications';

export const settings = writable<Settings>({});
export const settingsLoaded = writable(false);
const originalSettings = writable<Settings>({});

export const isDirty = derived(
  [settings, originalSettings],
  ([$settings, $original]) =>
    JSON.stringify($settings) !== JSON.stringify($original)
);

/** Returns true on success, false if the API call failed -- callers must
 *  check this rather than assume the load succeeded (a swallowed rejection
 *  used to look identical to success). */
export async function loadSettings(): Promise<boolean> {
  try {
    const config = await api.getSettings();
    settings.set(config);
    originalSettings.set(structuredClone(config));
    settingsLoaded.set(true);
    return true;
  } catch {
    addToast('Error', 'Failed to load settings', 'error');
    return false;
  }
}

/** Returns true on success, false if the API call failed (or there was
 *  nothing to save) -- callers must check this before treating a save as
 *  applied, e.g. before running a "Test connection" action that depends on
 *  the value just having been persisted. */
export async function saveSettings(): Promise<boolean> {
  // Don't save before settings are loaded — would send empty/default values
  if (!get(settingsLoaded)) return false;
  const current = get(settings);
  const original = get(originalSettings);
  // Only send fields that changed to avoid 422 from legacy keys
  const diff: Record<string, unknown> = {};
  for (const key of Object.keys(current) as (keyof Settings)[]) {
    if (JSON.stringify(current[key]) !== JSON.stringify(original[key])) {
      diff[key] = current[key];
    }
  }
  if (Object.keys(diff).length === 0) return true;
  try {
    await api.updateSettings(diff as Settings);
    originalSettings.set(structuredClone(current));
    addToast('Saved', 'Settings saved successfully');
    return true;
  } catch {
    addToast('Error', 'Failed to save settings', 'error');
    return false;
  }
}

export function resetSettings() {
  const original = get(originalSettings);
  settings.set(structuredClone(original));
}
