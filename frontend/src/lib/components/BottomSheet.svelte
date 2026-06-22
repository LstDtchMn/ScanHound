<script lang="ts">
  import { fly, fade } from 'svelte/transition';
  import type { Snippet } from 'svelte';

  interface Props {
    open: boolean;
    title?: string;
    onclose: () => void;
    children: Snippet;
  }
  let { open, title, onclose, children }: Props = $props();

  // Drag-down-to-dismiss on the grab handle.
  let dragY = $state(0);
  let dragging = false;
  let startY = 0;

  function onPointerDown(e: PointerEvent) {
    dragging = true;
    startY = e.clientY;
    (e.currentTarget as HTMLElement).setPointerCapture(e.pointerId);
  }
  function onPointerMove(e: PointerEvent) {
    if (!dragging) return;
    dragY = Math.max(0, e.clientY - startY);
  }
  function onPointerUp() {
    if (!dragging) return;
    dragging = false;
    if (dragY > 120) onclose();
    dragY = 0;
  }

  function onKey(e: KeyboardEvent) {
    if (e.key === 'Escape') onclose();
  }
</script>

<svelte:window onkeydown={open ? onKey : undefined} />

{#if open}
  <div class="fixed inset-0 z-50 flex flex-col justify-end">
    <!-- svelte-ignore a11y_click_events_have_key_events -->
    <!-- svelte-ignore a11y_no_static_element_interactions -->
    <div class="absolute inset-0 bg-[var(--bg-overlay)]" transition:fade={{ duration: 150 }} onclick={onclose}></div>
    <div
      class="relative bg-[var(--bg-secondary)] border-t border-[var(--border)] rounded-t-2xl shadow-2xl max-h-[85vh] flex flex-col"
      transition:fly={{ y: 400, duration: 220 }}
      style="{dragY ? `transform: translateY(${dragY}px);` : ''} padding-bottom: env(safe-area-inset-bottom);"
    >
      <!-- svelte-ignore a11y_no_static_element_interactions -->
      <div
        class="shrink-0 pt-2.5 pb-1.5 flex justify-center cursor-grab touch-none"
        onpointerdown={onPointerDown}
        onpointermove={onPointerMove}
        onpointerup={onPointerUp}
        onpointercancel={onPointerUp}
      >
        <div class="w-10 h-1 rounded-full bg-[var(--border)]"></div>
      </div>

      {#if title}
        <div class="shrink-0 flex items-center justify-between px-4 pb-2">
          <h2 class="text-base font-semibold text-[var(--text-primary)]">{title}</h2>
          <button onclick={onclose} aria-label="Close" class="p-1.5 -mr-1.5 text-xl leading-none text-[var(--text-secondary)] hover:text-[var(--text-primary)]">&times;</button>
        </div>
      {/if}

      <div class="overflow-y-auto px-4 pb-4">
        {@render children()}
      </div>
    </div>
  </div>
{/if}
