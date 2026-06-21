<script lang="ts">
  import { statusFilter, searchFilter, genreFilter, languageFilter, viewMode, stats, selectedKeys, selectAll, deselectAll, filteredResults, sortBy, availableGenres, availableLanguages, density, quickFilters, toggleQuickFilter, visibleColumns } from '$lib/stores/results';
  import { downloadHost } from '$lib/stores/downloads';
  import { api } from '$lib/api/client';
  import { addToast } from '$lib/stores/notifications';
  import type { StatusFilter, SortOption } from '$lib/stores/results';

  const quickChips = [
    { key: '4k', label: '4K' },
    { key: 'hdrdv', label: 'HDR/DV' },
    { key: 'inplex', label: 'In Plex' },
  ];
  const columnDefs = [
    { key: 'rating', label: 'Rating' },
    { key: 'res', label: 'Res' },
    { key: 'size', label: 'Size' },
    { key: 'status', label: 'Status' },
  ] as const;
  function toggleColumn(key: 'rating' | 'res' | 'size' | 'status') {
    visibleColumns.update((c) => ({ ...c, [key]: !c[key] }));
  }

  const sortOptions: { value: SortOption; label: string }[] = [
    { value: 'title-asc', label: 'Title (A–Z)' },
    { value: 'title-desc', label: 'Title (Z–A)' },
    { value: 'year-desc', label: 'Year (Newest)' },
    { value: 'year-asc', label: 'Year (Oldest)' },
    { value: 'size-desc', label: 'Size (Largest)' },
    { value: 'size-asc', label: 'Size (Smallest)' },
    { value: 'rating-desc', label: 'Rating (Highest)' },
    { value: 'rating-asc', label: 'Rating (Lowest)' },
    { value: 'posted-desc', label: 'Posted (Newest)' },
    { value: 'posted-asc', label: 'Posted (Oldest)' }
  ];

  const filters: { value: StatusFilter; label: string }[] = [
    { value: 'all', label: 'All' },
    { value: 'missing', label: 'Missing' },
    { value: 'upgrade', label: 'Upgrades' },
    { value: 'library', label: 'In Library' }
  ];

  let search = $state('');
  let debounceTimer: ReturnType<typeof setTimeout> | undefined;

  function onSearch() {
    clearTimeout(debounceTimer);
    debounceTimer = setTimeout(() => searchFilter.set(search), 200);
  }

  let resultCount = $derived($filteredResults.length);
  let selectedCount = $derived($selectedKeys.size);
  let addingToWatchlist = $state(false);
  let copyingLinks = $state(false);

  let downloadingAll = $state(false);
  async function bulkDownloadAll() {
    const selected = $filteredResults.filter(i => $selectedKeys.has(i.url) && i.url);
    if (selected.length === 0) {
      addToast('Info', 'Select items with a source URL first');
      return;
    }
    downloadingAll = true;
    try {
      await api.downloadBatch(selected.map(i => ({ url: i.url, title: i.title, year: i.year })), $downloadHost);
      addToast('Download All', `Sending ${selected.length} item(s) to JDownloader…`);
    } catch (e) {
      addToast('Error', e instanceof Error ? e.message : 'Failed to start downloads', 'error');
    } finally {
      downloadingAll = false;
    }
  }

  async function bulkCopyLinks() {
    const selected = $filteredResults.filter(i => $selectedKeys.has(i.url) && i.url);
    if (selected.length === 0) {
      addToast('Info', 'Select items with a source URL first');
      return;
    }
    copyingLinks = true;
    try {
      await api.copyLinksBatch(selected.map(i => ({ url: i.url, title: i.title, resolution: i.resolution })), $downloadHost);
      addToast('Copy Links', `Scraping ${selected.length} item(s) — links will be copied to clipboard when ready`);
    } catch (e) {
      addToast('Error', e instanceof Error ? e.message : 'Failed to start link copy', 'error');
    } finally {
      copyingLinks = false;
    }
  }

  async function bulkAddToWatchlist() {
    const selected = $filteredResults.filter(i => $selectedKeys.has(i.url) && i.status?.includes('missing'));
    if (selected.length === 0) {
      addToast('Info', 'Select missing items to add to watchlist');
      return;
    }
    addingToWatchlist = true;
    let added = 0;
    for (const item of selected) {
      try {
        await api.watchlistAdd({ title: item.title, year: item.year ?? undefined, imdb_id: item.imdb_id ?? undefined, item_type: 'movie' });
        added++;
      } catch { /* skip duplicates */ }
    }
    addingToWatchlist = false;
    addToast('Watchlist', `Added ${added} of ${selected.length} items to watchlist`);
  }

  async function handleExport() {
    try {
      const result = await api.exportCsv();
      addToast('Exported', `CSV saved to ${result.filepath}`);
    } catch {
      addToast('Error', 'Failed to export CSV', 'error');
    }
  }
</script>

<div class="flex items-center gap-2 px-3 py-1 border-b border-[var(--border)]">
  <!-- Status filter tabs -->
  <div class="flex gap-0.5">
    {#each filters as f}
      <button
        onclick={() => statusFilter.set(f.value)}
        class="px-2 py-1 rounded text-[11px] font-medium transition-colors
          {$statusFilter === f.value
            ? 'bg-[var(--accent)] text-white'
            : 'text-[var(--text-secondary)] hover:bg-[var(--bg-tertiary)]'}"
      >
        {f.label}
        {#if f.value === 'all' && $stats.total > 0}
          <span class="ml-0.5 opacity-70">{$stats.total}</span>
        {:else if f.value === 'missing' && $stats.missing > 0}
          <span class="ml-0.5 opacity-70">{$stats.missing}</span>
        {:else if f.value === 'upgrade' && $stats.upgrade > 0}
          <span class="ml-0.5 opacity-70">{$stats.upgrade}</span>
        {:else if f.value === 'library' && $stats.library > 0}
          <span class="ml-0.5 opacity-70">{$stats.library}</span>
        {/if}
      </button>
    {/each}
  </div>

  <!-- Quick-filter chips -->
  <div class="flex gap-0.5">
    {#each quickChips as chip}
      <button
        onclick={() => toggleQuickFilter(chip.key)}
        class="px-2 py-1 rounded text-[11px] font-medium transition-colors border
          {$quickFilters.includes(chip.key)
            ? 'bg-[var(--accent)]/15 border-[var(--accent)] text-[var(--accent)]'
            : 'border-transparent text-[var(--text-secondary)] hover:bg-[var(--bg-tertiary)]'}"
        title="Show only {chip.label}"
      >
        {chip.label}
      </button>
    {/each}
  </div>

  {#if resultCount > 0}
    <div class="flex items-center gap-0.5">
      <button
        onclick={() => selectAll($filteredResults.map(i => i.url))}
        class="px-1.5 py-0.5 rounded text-[10px] font-medium text-[var(--text-secondary)] hover:bg-[var(--bg-tertiary)] transition-colors"
        title="Select visible results"
      >
        All
      </button>
      {#if selectedCount > 0}
        <button
          onclick={() => deselectAll()}
          class="px-1.5 py-0.5 rounded text-[10px] font-medium text-[var(--text-secondary)] hover:bg-[var(--bg-tertiary)] transition-colors"
        >
          None ({selectedCount})
        </button>
        <button
          onclick={bulkDownloadAll}
          disabled={downloadingAll}
          class="px-1.5 py-0.5 rounded text-[10px] font-medium text-white bg-[var(--accent)] hover:opacity-90 transition-opacity disabled:opacity-50"
          title="Scrape & send all selected items to JDownloader ({$downloadHost})"
        >
          {downloadingAll ? '...' : '⬇ Download All'}
        </button>
        <button
          onclick={bulkCopyLinks}
          disabled={copyingLinks}
          class="px-1.5 py-0.5 rounded text-[10px] font-medium text-[var(--accent)] hover:bg-[var(--accent)]/10 transition-colors disabled:opacity-50"
          title="Scrape & copy {$downloadHost} links for all selected items (for JDownloader)"
        >
          {copyingLinks ? '...' : '🔗 Copy Links'}
        </button>
        <button
          onclick={bulkAddToWatchlist}
          disabled={addingToWatchlist}
          class="px-1.5 py-0.5 rounded text-[10px] font-medium text-[var(--accent)] hover:bg-[var(--accent)]/10 transition-colors disabled:opacity-50"
          title="Add selected missing items to watchlist"
        >
          {addingToWatchlist ? '...' : '+ Watch'}
        </button>
      {/if}
    </div>
  {/if}

  <div class="flex-1"></div>

  {#if resultCount > 0}
    <button
      onclick={handleExport}
      class="px-2 py-1 rounded text-[11px] text-[var(--text-secondary)] hover:bg-[var(--bg-tertiary)] transition-colors"
      title="Export to CSV"
    >
      Export
    </button>
  {/if}

  <select
    value={$downloadHost}
    onchange={(e) => downloadHost.set((e.target as HTMLSelectElement).value)}
    class="bg-[var(--bg-tertiary)] text-[var(--text-primary)] px-2 py-1 rounded border border-[var(--border)] text-[11px] focus:outline-none focus:border-[var(--accent)] cursor-pointer"
    title="Download host"
  >
    <option value="Rapidgator">Rapidgator</option>
    <option value="Nitroflare">Nitroflare</option>
    <option value="1Fichier">1Fichier</option>
  </select>

  {#if $availableGenres.length > 0}
    <select
      value={$genreFilter}
      onchange={(e) => genreFilter.set((e.target as HTMLSelectElement).value)}
      class="bg-[var(--bg-tertiary)] text-[var(--text-primary)] px-2 py-1 rounded border border-[var(--border)] text-[11px] focus:outline-none focus:border-[var(--accent)] cursor-pointer"
      title="Filter by genre"
    >
      <option value="">All Genres</option>
      {#each $availableGenres as genre}
        <option value={genre}>{genre}</option>
      {/each}
    </select>
  {/if}

  {#if $availableLanguages.length > 1}
    <select
      value={$languageFilter}
      onchange={(e) => languageFilter.set((e.target as HTMLSelectElement).value)}
      class="bg-[var(--bg-tertiary)] text-[var(--text-primary)] px-2 py-1 rounded border border-[var(--border)] text-[11px] focus:outline-none focus:border-[var(--accent)] cursor-pointer"
      title="Filter by language"
    >
      <option value="">All Languages</option>
      {#each $availableLanguages as lang}
        <option value={lang}>{lang}</option>
      {/each}
    </select>
  {/if}

  <select
    value={$sortBy}
    onchange={(e) => sortBy.set((e.target as HTMLSelectElement).value as SortOption)}
    class="bg-[var(--bg-tertiary)] text-[var(--text-primary)] px-2 py-1 rounded border border-[var(--border)] text-[11px] focus:outline-none focus:border-[var(--accent)] cursor-pointer"
  >
    {#each sortOptions as opt}
      <option value={opt.value}>{opt.label}</option>
    {/each}
  </select>

  <input
    type="text"
    bind:value={search}
    oninput={onSearch}
    placeholder="Filter results..."
    class="bg-[var(--bg-tertiary)] text-[var(--text-primary)] px-2 py-1 rounded border border-[var(--border)] text-xs w-36 min-w-0 focus:outline-none focus:border-[var(--accent)]"
  />

  {#if $viewMode === 'list'}
    <!-- Density toggle (list view) -->
    <div class="flex gap-0.5 bg-[var(--bg-tertiary)] rounded p-0.5">
      <button onclick={() => density.set('comfortable')} aria-label="Comfortable rows" title="Comfortable rows"
        class="p-1 rounded transition-colors {$density === 'comfortable' ? 'bg-[var(--accent)] text-white' : 'text-[var(--text-secondary)]'}">
        <svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 20 20"><path d="M3 5h14M3 10h14M3 15h14"/></svg>
      </button>
      <button onclick={() => density.set('compact')} aria-label="Compact rows" title="Compact rows"
        class="p-1 rounded transition-colors {$density === 'compact' ? 'bg-[var(--accent)] text-white' : 'text-[var(--text-secondary)]'}">
        <svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 20 20"><path d="M3 4h14M3 8h14M3 12h14M3 16h14"/></svg>
      </button>
    </div>
    <!-- Column show/hide (list view) -->
    <details class="relative">
      <summary class="list-none cursor-pointer px-2 py-1 rounded text-[11px] text-[var(--text-secondary)] bg-[var(--bg-tertiary)] border border-[var(--border)] hover:border-[var(--accent)] select-none">Columns &#9662;</summary>
      <div class="absolute right-0 mt-1 z-20 w-32 bg-[var(--bg-secondary)] border border-[var(--border)] rounded-lg shadow-lg p-1.5">
        {#each columnDefs as c}
          <label class="flex items-center gap-2 px-2 py-1 rounded text-xs cursor-pointer hover:bg-[var(--bg-tertiary)]">
            <input type="checkbox" checked={$visibleColumns[c.key]} onchange={() => toggleColumn(c.key)} class="accent-[var(--accent)]" />
            {c.label}
          </label>
        {/each}
      </div>
    </details>
  {/if}

  <div class="flex gap-0.5 bg-[var(--bg-tertiary)] rounded p-0.5">
    <button
      onclick={() => viewMode.set('grid')}
      aria-label="Grid view"
      class="p-1 rounded text-xs transition-colors
        {$viewMode === 'grid' ? 'bg-[var(--accent)] text-white' : 'text-[var(--text-secondary)]'}"
    >
      <svg class="w-3.5 h-3.5" fill="currentColor" viewBox="0 0 20 20">
        <path d="M5 3a2 2 0 00-2 2v2a2 2 0 002 2h2a2 2 0 002-2V5a2 2 0 00-2-2H5zM5 11a2 2 0 00-2 2v2a2 2 0 002 2h2a2 2 0 002-2v-2a2 2 0 00-2-2H5zM11 5a2 2 0 012-2h2a2 2 0 012 2v2a2 2 0 01-2 2h-2a2 2 0 01-2-2V5zM11 13a2 2 0 012-2h2a2 2 0 012 2v2a2 2 0 01-2 2h-2a2 2 0 01-2-2v-2z" />
      </svg>
    </button>
    <button
      onclick={() => viewMode.set('list')}
      aria-label="List view"
      class="p-1 rounded text-xs transition-colors
        {$viewMode === 'list' ? 'bg-[var(--accent)] text-white' : 'text-[var(--text-secondary)]'}"
    >
      <svg class="w-3.5 h-3.5" fill="currentColor" viewBox="0 0 20 20">
        <path fill-rule="evenodd" d="M3 4a1 1 0 011-1h12a1 1 0 110 2H4a1 1 0 01-1-1zm0 4a1 1 0 011-1h12a1 1 0 110 2H4a1 1 0 01-1-1zm0 4a1 1 0 011-1h12a1 1 0 110 2H4a1 1 0 01-1-1zm0 4a1 1 0 011-1h12a1 1 0 110 2H4a1 1 0 01-1-1z" clip-rule="evenodd" />
      </svg>
    </button>
  </div>
</div>
