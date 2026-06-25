<script lang="ts">
  import { selectedKeys } from '$lib/stores/results';
  import { downloadHost } from '$lib/stores/downloads';
  import { buildResultActions } from '$lib/resultActions';
  import type { ScanResult } from '$lib/api/types';
  import { onMount } from 'svelte';

  interface Props {
    item: ScanResult;
    x: number;
    y: number;
    onclose: () => void;
  }
  let { item, x, y, onclose }: Props = $props();

  let selected = $derived($selectedKeys.has(item.url));
  let menuEl = $state<HTMLDivElement>();
  let focusedIdx = $state(0);

  // Clamp position to viewport after mount
  let menuRect = $state<{ w: number; h: number } | null>(null);
  let clampedX = $derived(menuRect ? Math.min(x, window.innerWidth - menuRect.w - 8) : x);
  let clampedY = $derived(menuRect ? Math.min(y, window.innerHeight - menuRect.h - 8) : y);

  onMount(() => {
    if (menuEl) {
      const rect = menuEl.getBoundingClientRect();
      menuRect = { w: rect.width, h: rect.height };
      const buttons = menuEl.querySelectorAll<HTMLButtonElement>('button[data-menu-item]');
      buttons[0]?.focus();
    }
  });

  let items = $derived(buildResultActions(item, $downloadHost, selected));

  function handleAction(run: () => void) {
    run();
    onclose();
  }

  function handleKeydown(e: KeyboardEvent) {
    switch (e.key) {
      case 'Escape':
        e.preventDefault();
        onclose();
        break;
      case 'ArrowDown':
        e.preventDefault();
        focusedIdx = Math.min(focusedIdx + 1, items.length - 1);
        focusItem();
        break;
      case 'ArrowUp':
        e.preventDefault();
        focusedIdx = Math.max(focusedIdx - 1, 0);
        focusItem();
        break;
      case 'Enter':
      case ' ':
        e.preventDefault();
        handleAction(items[focusedIdx].run);
        break;
    }
  }

  function focusItem() {
    if (menuEl) {
      const buttons = menuEl.querySelectorAll<HTMLButtonElement>('button[data-menu-item]');
      buttons[focusedIdx]?.focus();
    }
  }
</script>

<svelte:window onclick={onclose} onkeydown={handleKeydown} />

<!-- svelte-ignore a11y_no_static_element_interactions -->
<div
  bind:this={menuEl}
  class="fixed z-50 bg-[var(--bg-secondary)] border border-[var(--border)] rounded-lg shadow-lg py-1 min-w-[160px] text-sm"
  style="left: {clampedX}px; top: {clampedY}px;"
  role="menu"
>
  {#each items as menuItem, i}
    {#if menuItem.separatorBefore}
      <div class="border-t border-[var(--border)] my-1"></div>
    {/if}
    <button
      data-menu-item
      role="menuitem"
      class="w-full px-3 py-1.5 text-left hover:bg-[var(--bg-tertiary)] transition-colors {focusedIdx === i ? 'bg-[var(--bg-tertiary)]' : ''}"
      onclick={() => handleAction(menuItem.run)}
    >
      {menuItem.label}
    </button>
  {/each}
</div>
