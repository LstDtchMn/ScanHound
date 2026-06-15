<script lang="ts">
  import { fly } from 'svelte/transition';

  interface Props {
    onclose: () => void;
  }
  let { onclose }: Props = $props();

  const shortcuts = [
    { key: '1-5', action: 'Navigate pages (Scan/DLs/Watch/Stats/Settings)' },
    { key: 'G', action: 'Grid view' },
    { key: 'L', action: 'List view' },
    { key: 'Ctrl+A', action: 'Select all' },
    { key: 'Ctrl+D', action: 'Deselect all' },
    { key: 'Ctrl+L', action: 'Toggle logs' },
    { key: '?', action: 'Show shortcuts' },
    { key: 'Esc', action: 'Close panel/overlay' },
    { key: 'Arrow Up/Down', action: 'Navigate results' },
    { key: 'Enter', action: 'View focused result details' },
    { key: 'Space', action: 'Toggle selection on focused result' }
  ];

  function handleKeydown(e: KeyboardEvent) {
    if (e.key === 'Escape') {
      e.preventDefault();
      onclose();
    }
  }

  function handleBackdropClick(e: MouseEvent) {
    if (e.target === e.currentTarget) {
      onclose();
    }
  }
</script>

<svelte:window onkeydown={handleKeydown} />

<!-- svelte-ignore a11y_no_static_element_interactions -->
<!-- svelte-ignore a11y_click_events_have_key_events -->
<div
  class="fixed inset-0 z-50 flex items-center justify-center bg-[var(--bg-overlay)]"
  onclick={handleBackdropClick}
>
  <div
    transition:fly={{ y: -20, duration: 200 }}
    class="bg-[var(--bg-secondary)] border border-[var(--border)] rounded-xl shadow-2xl p-6 w-full max-w-md"
  >
    <div class="flex items-center justify-between mb-4">
      <h2 class="text-lg font-semibold text-[var(--text-primary)]">Keyboard Shortcuts</h2>
      <button
        onclick={onclose}
        class="w-7 h-7 rounded-lg flex items-center justify-center text-[var(--text-secondary)] hover:bg-[var(--bg-tertiary)] transition-colors"
        title="Close"
      >&times;</button>
    </div>

    <div class="grid grid-cols-[auto_1fr] gap-x-4 gap-y-2">
      {#each shortcuts as shortcut}
        <div class="flex items-center justify-end">
          {#each shortcut.key.split('+') as part, i}
            {#if i > 0}<span class="text-[var(--text-secondary)] text-xs mx-0.5">+</span>{/if}
            <kbd class="inline-block px-1.5 py-0.5 text-xs font-mono rounded bg-[var(--bg-tertiary)] border border-[var(--border)] text-[var(--text-primary)]">{part}</kbd>
          {/each}
        </div>
        <span class="text-sm text-[var(--text-secondary)] flex items-center">{shortcut.action}</span>
      {/each}
    </div>
  </div>
</div>
