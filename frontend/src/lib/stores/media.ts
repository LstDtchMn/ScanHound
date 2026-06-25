import { readable } from 'svelte/store';

/** True on phone-sized viewports (< Tailwind `md`). SSR-safe (false on server).
 *  Use for the few cases that must *mount* different components (bottom sheets);
 *  prefer plain `md:` CSS classes everywhere else. */
export const mobile = readable(false, (set) => {
  if (typeof window === 'undefined' || !window.matchMedia) return;
  const mq = window.matchMedia('(max-width: 767px)');
  set(mq.matches);
  const handler = (e: MediaQueryListEvent) => set(e.matches);
  mq.addEventListener('change', handler);
  return () => mq.removeEventListener('change', handler);
});
