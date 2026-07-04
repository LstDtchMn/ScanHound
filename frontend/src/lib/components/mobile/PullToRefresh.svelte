<script lang="ts">
  import type { Snippet } from 'svelte';
  import { createDragTracker } from './gestures';
  import { tap, success } from './haptics';

  interface Props {
    onrefresh: () => Promise<void> | void;
    disabled?: boolean;
    children: Snippet;
  }
  let { onrefresh, disabled = false, children }: Props = $props();

  const TRIGGER = 70;   // px of (damped) pull that arms the refresh
  const MAX_PULL = 110;
  const DAMP = 0.45;

  let scroller: HTMLDivElement | undefined = $state();
  let pull = $state(0);          // damped visual offset
  let refreshing = $state(false);
  let armed = $state(false);

  const tracker = createDragTracker({ axis: 'y', threshold: TRIGGER / DAMP });

  function onPointerDown(e: PointerEvent) {
    if (disabled || refreshing) return;
    if ((scroller?.scrollTop ?? 1) > 0) return; // only from the very top
    tracker.start(e.clientX, e.clientY);
  }

  function onPointerMove(e: PointerEvent) {
    if (disabled || refreshing || !tracker.state.active) return;
    const s = tracker.move(e.clientX, e.clientY);
    if (s.locked !== 'y' || s.dy <= 0) { pull = 0; armed = false; return; }
    // Native scroll/overscroll is suppressed by `touch-action: pan-y` +
    // `overscroll-y-contain` on the scroller (preventDefault on pointermove
    // has no effect on scrolling per the pointer-events spec). If Android
    // Chrome ever claims the pan and fires pointercancel mid-pull (resetting
    // it via onpointercancel below), switch to `touch-action: none` while
    // y-locked — gated by the on-device checklist's PTR item.
    pull = Math.min(s.dy * DAMP, MAX_PULL);
    const nowArmed = pull >= TRIGGER;
    if (nowArmed && !armed) tap();
    armed = nowArmed;
  }

  async function onPointerUp() {
    if (!tracker.state.active) return;
    tracker.end();
    if (armed && !refreshing) {
      refreshing = true;
      pull = TRIGGER; // hold at spinner height
      try { await onrefresh(); success(); } finally {
        refreshing = false;
        pull = 0;
        armed = false;
      }
    } else {
      pull = 0;
      armed = false;
    }
  }
</script>

<div class="relative h-full min-h-0 flex flex-col overflow-hidden">
  <!-- Indicator -->
  <div
    class="absolute inset-x-0 top-0 z-10 flex justify-center pointer-events-none transition-opacity"
    style="height: {pull}px; opacity: {pull > 8 ? 1 : 0};"
    aria-hidden="true"
  >
    <div class="flex items-end pb-1">
      {#if refreshing}
        <div class="w-5 h-5 border-2 border-[var(--accent)] border-t-transparent rounded-full animate-spin"></div>
      {:else}
        <svg class="w-5 h-5 text-[var(--text-secondary)] transition-transform {armed ? 'rotate-180 text-[var(--accent)]' : ''}"
          fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2">
          <path stroke-linecap="round" stroke-linejoin="round" d="M19 14l-7 7m0 0l-7-7m7 7V3" />
        </svg>
      {/if}
    </div>
  </div>

  <div
    bind:this={scroller}
    class="flex-1 min-h-0 overflow-y-auto overscroll-y-contain transition-transform duration-150"
    style="transform: translateY({pull}px); touch-action: pan-y;"
    onpointerdown={onPointerDown}
    onpointermove={onPointerMove}
    onpointerup={onPointerUp}
    onpointercancel={onPointerUp}
    role="presentation"
  >
    {@render children()}
  </div>
</div>
