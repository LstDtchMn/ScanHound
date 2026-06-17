<script lang="ts">
  import { toggleSelect, selectedKeys } from '$lib/stores/results';
  import { api } from '$lib/api/client';
  import { addToast } from '$lib/stores/notifications';
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

  interface MenuItem {
    label: string;
    action: () => void;
    separator?: boolean;
  }

  let items = $derived.by(() => {
    const list: MenuItem[] = [
      { label: selected ? 'Deselect' : 'Select', action: () => toggleSelect(item.url) }
    ];
    if (item.url) {
      list.push({ label: 'Download', action: download });
      list.push({ label: 'Copy URL', action: copyUrl });
    }
    if (item.imdb_id) {
      list.push({ label: 'Open IMDb', action: openImdb, separator: true });
    }
    if (item.plex_rating_key) {
      list.push({ label: 'Open in Plex', action: openInPlex });
    }
    if (item.url) {
      list.push({ label: 'Open Source Page', action: () => window.open(item.url, '_blank'), separator: true });
    }
    list.push({ label: 'Add to Watchlist', action: addToWatchlist });
    return list;
  });

  function handleAction(action: () => void) {
    action();
    onclose();
  }

  function download() {
    if (item.url) {
      api.download(item.url, item.title, undefined, item.year).catch(() => addToast('Error', 'Download failed', 'error'));
    }
  }

  function copyUrl() {
    if (item.url) {
      navigator.clipboard.writeText(item.url).then(
        () => addToast('Copied', 'URL copied to clipboard'),
        () => addToast('Error', 'Failed to copy URL', 'error')
      );
    }
  }

  function openImdb() {
    if (item.imdb_id) window.open(`https://www.imdb.com/title/${item.imdb_id}`, '_blank');
  }

  function openInPlex() {
    api.openInPlex(item.title, item.imdb_id ?? undefined, item.plex_rating_key ?? undefined)
      .catch(() => addToast('Error', 'Failed to open in Plex', 'error'));
  }

  function addToWatchlist() {
    api.watchlistAdd({
      title: item.title,
      year: item.year,
      imdb_id: item.imdb_id,
      item_type: item.season ? 'tv' : 'movie'
    }).then(() => addToast('Watchlist', `Added: ${item.title}`))
      .catch((e) => addToast('Error', e instanceof Error ? e.message : 'Failed to add', 'error'));
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
        handleAction(items[focusedIdx].action);
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
    {#if menuItem.separator}
      <div class="border-t border-[var(--border)] my-1"></div>
    {/if}
    <button
      data-menu-item
      role="menuitem"
      class="w-full px-3 py-1.5 text-left hover:bg-[var(--bg-tertiary)] transition-colors {focusedIdx === i ? 'bg-[var(--bg-tertiary)]' : ''}"
      onclick={() => handleAction(menuItem.action)}
    >
      {menuItem.label}
    </button>
  {/each}
</div>
