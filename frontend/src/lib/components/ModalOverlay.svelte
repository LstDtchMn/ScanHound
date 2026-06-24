<script lang="ts">
  import { fade } from 'svelte/transition';
  import type { Snippet } from 'svelte';

  interface Props {
    onclose: () => void;
    /** Where the panel sits: centered (dialogs) or pinned to the bottom (sheets). */
    align?: 'center' | 'bottom';
    children: Snippet;
  }
  let { onclose, align = 'center', children }: Props = $props();

  function onKeydown(e: KeyboardEvent) {
    if (e.key === 'Escape') onclose();
  }
</script>

<svelte:window onkeydown={onKeydown} />

<div class="fixed inset-0 z-50 flex {align === 'bottom' ? 'flex-col justify-end' : 'items-center justify-center'}">
  <!-- svelte-ignore a11y_click_events_have_key_events -->
  <!-- svelte-ignore a11y_no_static_element_interactions -->
  <div class="absolute inset-0 bg-[var(--bg-overlay)]" transition:fade={{ duration: 150 }} onclick={onclose}></div>
  {@render children()}
</div>
