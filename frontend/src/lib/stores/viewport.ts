import { readable } from 'svelte/store';

const NARROW = '(max-width: 767px)';
const COARSE = '(pointer: coarse)';

/** True on phone-class devices: narrow viewport AND coarse (touch) pointer.
 *  A narrow desktop window stays desktop. SSR-safe (false without window).
 *  Single source of truth for the phone/desktop fork — components must not
 *  re-derive their own media queries. */
export const isPhone = readable(false, (set) => {
  if (typeof window === 'undefined' || typeof window.matchMedia !== 'function') {
    set(false);
    return;
  }
  const narrow = window.matchMedia(NARROW);
  const coarse = window.matchMedia(COARSE);
  const update = () => set(narrow.matches && coarse.matches);
  update();
  narrow.addEventListener('change', update);
  coarse.addEventListener('change', update);
  return () => {
    narrow.removeEventListener('change', update);
    coarse.removeEventListener('change', update);
  };
});
