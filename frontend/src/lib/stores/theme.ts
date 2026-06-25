import { writable, get } from 'svelte/store';

const KEY = 'scanhound-theme';
export type Theme = 'dark' | 'light';

export const theme = writable<Theme>('dark');

export function applyTheme(t: Theme): void {
  theme.set(t);
  if (typeof document === 'undefined') return;
  if (t === 'light') document.documentElement.setAttribute('data-theme', 'light');
  else document.documentElement.removeAttribute('data-theme');
  try {
    localStorage.setItem(KEY, t);
  } catch {
    /* ignore */
  }
}

/** Resolve the initial theme from storage or the OS preference. */
export function initTheme(): void {
  if (typeof window === 'undefined') return;
  const saved = localStorage.getItem(KEY);
  if (saved === 'light' || saved === 'dark') {
    applyTheme(saved);
  } else {
    applyTheme(window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light');
  }
}

export function toggleTheme(): void {
  applyTheme(get(theme) === 'dark' ? 'light' : 'dark');
}
