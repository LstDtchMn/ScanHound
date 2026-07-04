/** Haptic feedback via navigator.vibrate — works in the Android WebView
 *  (with VIBRATE manifest permission) and Android Chrome; silently no-ops
 *  everywhere else (desktop, iOS Safari, SSR). Deliberately NOT the Tauri
 *  haptics plugin: same effect, zero cargo/capability surface. */

function vibrate(pattern: number | number[]): void {
  try {
    if (typeof navigator !== 'undefined' && typeof navigator.vibrate === 'function') {
      navigator.vibrate(pattern);
    }
  } catch {
    /* never let a haptic break a gesture */
  }
}

/** Tiny tick — swipe crossed its commit threshold, pull-to-refresh armed. */
export function tap(): void { vibrate(10); }

/** Double-pulse — action succeeded (grab sent, refresh done). */
export function success(): void { vibrate([15, 60, 15]); }

/** Single firmer buzz — destructive-ish commit (dismiss) or error. */
export function warning(): void { vibrate(35); }
