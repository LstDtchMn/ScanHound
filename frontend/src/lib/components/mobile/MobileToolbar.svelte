<script lang="ts">
  import { get } from 'svelte/store';
  import { searchFilter, selectedKeys, deselectAll, filteredResults, phoneColumns } from '$lib/stores/results';
  import { downloadHost } from '$lib/stores/downloads';
  import { api } from '$lib/api/client';
  import { addToast } from '$lib/stores/notifications';
  import { success } from './haptics';

  interface Props {
    onfilters: () => void;
    ondeck: () => void;
  }
  let { onfilters, ondeck }: Props = $props();

  let searchOpen = $state(false);
  let searchEl: HTMLInputElement | undefined = $state();

  function openSearch() {
    searchOpen = true;
    setTimeout(() => searchEl?.focus(), 30);
  }
  function onSearchBlur() {
    if (!$searchFilter) searchOpen = false;
  }

  let selCount = $derived($selectedKeys.size);
  let selItems = $derived($filteredResults.filter((i) => $selectedKeys.has(i.url)));

  async function grabAll() {
    const items = selItems;
    let sent = 0;
    for (const item of items) {
      if (!item.url) continue;
      try {
        await api.download(item.url, item.title, get(downloadHost), item.year,
                           item.resolution || '', item.size || '', item.hdr || '', item.dovi ?? false,
                           item.season);
        // Don't optimistically mark grabbed: the POST only returns "started".
        // Each item flips to Downloaded via its download:complete
        // (method=jdownloader) WS event once it truly reaches JDownloader;
        // failures stay Missing.
        sent++;
      } catch { /* per-item failure tolerated; summarized below */ }
    }
    success();
    addToast('Sending', `${sent} of ${items.length} to JDownloader…`);
    deselectAll();
  }
</script>

<div
  class="shrink-0 flex md:hidden items-center gap-1 h-11 px-2 border-t border-[var(--border)] bg-[var(--bg-secondary)]"
  role="toolbar" aria-label="Scan actions"
>
  {#if selCount > 0}
    <span class="text-xs font-semibold text-[var(--text-primary)] px-1">{selCount} selected</span>
    <div class="flex-1"></div>
    <button class="px-3 py-1.5 rounded-lg bg-[var(--accent)] text-xs font-semibold text-white" onclick={grabAll}>Grab all</button>
    <button class="px-3 py-1.5 rounded-lg bg-[var(--bg-tertiary)] text-xs text-[var(--text-primary)]" onclick={() => deselectAll()}>Clear</button>
  {:else if searchOpen}
    <input
      bind:this={searchEl}
      bind:value={$searchFilter}
      onblur={onSearchBlur}
      type="search"
      placeholder="Search titles…"
      class="flex-1 h-8 px-3 rounded-lg bg-[var(--bg-tertiary)] border border-[var(--border)] text-sm text-[var(--text-primary)] outline-none focus:border-[var(--accent)]"
      aria-label="Search titles"
    />
    <button class="p-2 text-[var(--text-secondary)]" aria-label="Close search"
      onclick={() => { searchFilter.set(''); searchOpen = false; }}>&times;</button>
  {:else}
    <button class="flex-1 flex items-center justify-center gap-1.5 py-2 text-xs text-[var(--text-secondary)]" onclick={openSearch} aria-label="Search">
      <svg class="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2"><path stroke-linecap="round" stroke-linejoin="round" d="M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0z"/></svg>
      Search
    </button>
    <button class="flex-1 flex items-center justify-center gap-1.5 py-2 text-xs text-[var(--text-secondary)]" onclick={onfilters} aria-label="Filters and sort">
      <svg class="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2"><path stroke-linecap="round" stroke-linejoin="round" d="M3 4a1 1 0 011-1h16a1 1 0 011 1v2.586a1 1 0 01-.293.707l-6.414 6.414a1 1 0 00-.293.707V17l-4 4v-6.586a1 1 0 00-.293-.707L3.293 7.293A1 1 0 013 6.586V4z"/></svg>
      Filters
    </button>
    <button class="flex-1 flex items-center justify-center gap-1.5 py-2 text-xs text-[var(--text-secondary)]" onclick={ondeck} aria-label="Triage deck">
      <svg class="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2"><path stroke-linecap="round" stroke-linejoin="round" d="M19 11H5m14-4H5m14 8H5m14 4H5"/></svg>
      Deck
    </button>
    <button
      class="shrink-0 p-2 text-[var(--text-secondary)] active:text-[var(--accent)]"
      onclick={() => phoneColumns.set($phoneColumns === 1 ? 2 : 1)}
      aria-label={$phoneColumns === 1 ? 'Switch to 2-column poster wall' : 'Switch to single large poster'}
      aria-pressed={$phoneColumns === 1}
    >
      {#if $phoneColumns === 1}
        <!-- currently 1-up → icon shows the 2-up wall you'd switch to -->
        <svg class="w-5 h-5" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2"><rect x="4" y="4" width="7" height="7" rx="1"/><rect x="13" y="4" width="7" height="7" rx="1"/><rect x="4" y="13" width="7" height="7" rx="1"/><rect x="13" y="13" width="7" height="7" rx="1"/></svg>
      {:else}
        <!-- currently 2-up → icon shows the single large poster you'd switch to -->
        <svg class="w-5 h-5" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2"><rect x="5" y="4" width="14" height="16" rx="1"/></svg>
      {/if}
    </button>
  {/if}
</div>
