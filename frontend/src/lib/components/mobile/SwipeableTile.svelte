<script lang="ts">
  import type { Snippet } from 'svelte';
  import { createDragTracker } from './gestures';
  import { tap, warning } from './haptics';

  interface Props {
    onswiperight?: () => void;
    onswipeleft?: () => void;
    onlongpress?: () => void;
    disabled?: boolean;
    children: Snippet;
  }
  let { onswiperight, onswipeleft, onlongpress, disabled = false, children }: Props = $props();

  const THRESHOLD = 72;
  const LONGPRESS_MS = 450;

  let dx = $state(0);
  let animating = $state(false);
  let crossed = false;
  let longpressTimer: ReturnType<typeof setTimeout> | null = null;
  let longpressed = false;

  const tracker = createDragTracker({ axis: 'x', threshold: THRESHOLD });

  function clearLongpress() {
    if (longpressTimer) { clearTimeout(longpressTimer); longpressTimer = null; }
  }

  function onPointerDown(e: PointerEvent) {
    if (disabled) return;
    longpressed = false;
    tracker.start(e.clientX, e.clientY);
    if (onlongpress) {
      longpressTimer = setTimeout(() => {
        // still essentially stationary → it's a hold
        if (Math.abs(tracker.state.dx) < 6 && tracker.state.locked === null) {
          longpressed = true;
          onlongpress();
        }
      }, LONGPRESS_MS);
    }
  }

  function onPointerMove(e: PointerEvent) {
    if (disabled || !tracker.state.active) return;
    const s = tracker.move(e.clientX, e.clientY);
    if (s.locked === 'x') {
      clearLongpress();
      dx = s.dx;
      const over = Math.abs(dx) >= THRESHOLD;
      if (over && !crossed) tap();
      crossed = over;
    } else if (s.locked === 'y') {
      clearLongpress();
      dx = 0;
    }
  }

  function onPointerUp() {
    clearLongpress();
    if (!tracker.state.active) return;
    const { committed, direction } = tracker.end();
    crossed = false;
    if (committed && direction === 'right' && onswiperight) {
      animating = true;
      dx = THRESHOLD * 1.4;
      setTimeout(() => { onswiperight(); dx = 0; animating = false; }, 120);
    } else if (committed && direction === 'left' && onswipeleft) {
      warning();
      animating = true;
      dx = -THRESHOLD * 1.4;
      setTimeout(() => { onswipeleft(); dx = 0; animating = false; }, 120);
    } else {
      dx = 0; // spring back
    }
  }

  function onClickCapture(e: MouseEvent) {
    // A long-press or a horizontal swipe must not ALSO count as a tap.
    if (longpressed || Math.abs(dx) > 6) {
      e.stopPropagation();
      e.preventDefault();
    }
  }
</script>

<div class="relative overflow-hidden rounded-lg" role="presentation">
  <!-- Underlays -->
  <div class="absolute inset-0 flex items-center justify-between px-4 rounded-lg
      {dx > 0 ? 'bg-green-600/80' : dx < 0 ? 'bg-amber-600/80' : ''}"
    style="opacity: {Math.min(Math.abs(dx) / THRESHOLD, 1)};" aria-hidden="true">
    <svg class="w-6 h-6 text-white {dx > 0 ? '' : 'invisible'}" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2">
      <path stroke-linecap="round" stroke-linejoin="round" d="M4 16v1a3 3 0 003 3h10a3 3 0 003-3v-1m-4-4l-4 4m0 0l-4-4m4 4V4" />
    </svg>
    <svg class="w-6 h-6 text-white {dx < 0 ? '' : 'invisible'}" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2">
      <path stroke-linecap="round" stroke-linejoin="round" d="M13.875 18.825A10.05 10.05 0 0112 19c-4.478 0-8.268-2.943-9.543-7a9.97 9.97 0 011.563-3.029m5.858.908a3 3 0 114.243 4.243M9.878 9.878l4.242 4.242M9.88 9.88l-3.29-3.29m7.532 7.532l3.29 3.29M3 3l3.59 3.59m0 0A9.953 9.953 0 0112 5c4.478 0 8.268 2.943 9.543 7a10.025 10.025 0 01-4.132 5.411m0 0L21 21" />
    </svg>
  </div>
  <!-- The tile -->
  <div
    class="relative {animating ? 'transition-transform duration-100' : dx === 0 ? 'transition-transform duration-150' : ''}"
    style="transform: translateX({dx}px); touch-action: pan-y;"
    onpointerdown={onPointerDown}
    onpointermove={onPointerMove}
    onpointerup={onPointerUp}
    onpointercancel={onPointerUp}
    onclickcapture={onClickCapture}
    role="presentation"
  >
    {@render children()}
  </div>
</div>
