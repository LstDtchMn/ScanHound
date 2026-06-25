<script lang="ts">
  import { statusFilter, searchFilter, genreFilter, languageFilter, toggleGenreFilter, toggleLanguageFilter, viewMode, setViewMode, stats, selectedKeys, selectAll, deselectAll, filteredResults, sortBy, availableGenres, availableLanguages, density, quickFilters, toggleQuickFilter, visibleColumns } from '$lib/stores/results';
  import { downloadHost } from '$lib/stores/downloads';
  import { api } from '$lib/api/client';
  import { addToast } from '$lib/stores/notifications';
  import { DOWNLOAD_HOSTS } from '$lib/constants';
  import BottomSheet from './BottomSheet.svelte';
  import type { StatusFilter, SortOption } from '$lib/stores/results';

  let filterSheet = $state(false);
  let activeFilterCount = $derived(
    ($statusFilter !== 'all' ? 1 : 0) +
    ($searchFilter ? 1 : 0) +
    ($genreFilter.length > 0 ? 1 : 0) +
    ($languageFilter.length > 0 ? 1 : 0) +
    $quickFilters.length
  );

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

<!-- Desktop toolbar -->
<div class="hidden md:flex items-center gap-2 px-3 py-1 border-b border-[var(--border)]">
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
    {#each DOWNLOAD_HOSTS as h}<option value={h.value}>{h.value}</option>{/each}
  </select>

  {#if $availableGenres.length > 0}
    <details class="relative">
      <summary class="list-none cursor-pointer px-2 py-1 rounded text-[11px] text-[var(--text-secondary)] bg-[var(--bg-tertiary)] border border-[var(--border)] hover:border-[var(--accent)] select-none">
        Genres{#if $genreFilter.length > 0}<span class="ml-0.5 text-[var(--accent)]">({$genreFilter.length})</span>{/if} &#9662;
      </summary>
      <div class="absolute right-0 mt-1 z-20 w-44 max-h-72 overflow-y-auto bg-[var(--bg-secondary)] border border-[var(--border)] rounded-lg shadow-lg p-1.5">
        <button
          onclick={() => genreFilter.set([])}
          class="w-full text-left px-2 py-1 rounded text-xs font-medium mb-1
            {$genreFilter.length === 0 ? 'bg-[var(--accent)]/15 text-[var(--accent)]' : 'text-[var(--text-secondary)] hover:bg-[var(--bg-tertiary)]'}"
        >
          All genres
        </button>
        {#each $availableGenres as genre}
          <label class="flex items-center gap-2 px-2 py-1 rounded text-xs cursor-pointer hover:bg-[var(--bg-tertiary)]">
            <input type="checkbox" checked={$genreFilter.includes(genre)} onchange={() => toggleGenreFilter(genre)} class="accent-[var(--accent)]" />
            {genre}
          </label>
        {/each}
      </div>
    </details>
  {/if}

  {#if $availableLanguages.length > 1}
    <details class="relative">
      <summary class="list-none cursor-pointer px-2 py-1 rounded text-[11px] text-[var(--text-secondary)] bg-[var(--bg-tertiary)] border border-[var(--border)] hover:border-[var(--accent)] select-none">
        Languages{#if $languageFilter.length > 0}<span class="ml-0.5 text-[var(--accent)]">({$languageFilter.length})</span>{/if} &#9662;
      </summary>
      <div class="absolute right-0 mt-1 z-20 w-44 max-h-72 overflow-y-auto bg-[var(--bg-secondary)] border border-[var(--border)] rounded-lg shadow-lg p-1.5">
        <button
          onclick={() => languageFilter.set([])}
          class="w-full text-left px-2 py-1 rounded text-xs font-medium mb-1
            {$languageFilter.length === 0 ? 'bg-[var(--accent)]/15 text-[var(--accent)]' : 'text-[var(--text-secondary)] hover:bg-[var(--bg-tertiary)]'}"
        >
          All languages
        </button>
        {#each $availableLanguages as lang}
          <label class="flex items-center gap-2 px-2 py-1 rounded text-xs cursor-pointer hover:bg-[var(--bg-tertiary)]">
            <input type="checkbox" checked={$languageFilter.includes(lang)} onchange={() => toggleLanguageFilter(lang)} class="accent-[var(--accent)]" />
            {lang}
          </label>
        {/each}
      </div>
    </details>
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
      onclick={() => setViewMode('grid')}
      aria-label="Grid view"
      class="p-1 rounded text-xs transition-colors
        {$viewMode === 'grid' ? 'bg-[var(--accent)] text-white' : 'text-[var(--text-secondary)]'}"
    >
      <svg class="w-3.5 h-3.5" fill="currentColor" viewBox="0 0 20 20">
        <path d="M5 3a2 2 0 00-2 2v2a2 2 0 002 2h2a2 2 0 002-2V5a2 2 0 00-2-2H5zM5 11a2 2 0 00-2 2v2a2 2 0 002 2h2a2 2 0 002-2v-2a2 2 0 00-2-2H5zM11 5a2 2 0 012-2h2a2 2 0 012 2v2a2 2 0 01-2 2h-2a2 2 0 01-2-2V5zM11 13a2 2 0 012-2h2a2 2 0 012 2v2a2 2 0 01-2 2h-2a2 2 0 01-2-2v-2z" />
      </svg>
    </button>
    <button
      onclick={() => setViewMode('list')}
      aria-label="List view"
      class="p-1 rounded text-xs transition-colors
        {$viewMode === 'list' ? 'bg-[var(--accent)] text-white' : 'text-[var(--text-secondary)]'}"
    >
      <svg class="w-3.5 h-3.5" fill="currentColor" viewBox="0 0 20 20">
        <path fill-rule="evenodd" d="M3 4a1 1 0 011-1h12a1 1 0 110 2H4a1 1 0 01-1-1zm0 4a1 1 0 011-1h12a1 1 0 110 2H4a1 1 0 01-1-1zm0 4a1 1 0 011-1h12a1 1 0 110 2H4a1 1 0 01-1-1zm0 4a1 1 0 011-1h12a1 1 0 110 2H4a1 1 0 01-1-1z" clip-rule="evenodd" />
      </svg>
    </button>
    <button
      onclick={() => setViewMode('swipe')}
      aria-label="Swipe view"
      title="Swipe deck — triage with swipe right (add) / left (skip)"
      class="p-1 rounded text-xs transition-colors
        {$viewMode === 'swipe' ? 'bg-[var(--accent)] text-white' : 'text-[var(--text-secondary)]'}"
    >
      <svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" stroke-width="1.7" viewBox="0 0 20 20">
        <rect x="5" y="3.5" width="10" height="13" rx="2" transform="rotate(8 10 10)" />
        <rect x="5" y="3.5" width="10" height="13" rx="2" />
      </svg>
    </button>
  </div>
</div>

<!-- Mobile toolbar: scrollable status chips + a Filters button → bottom sheet -->
<div class="flex md:hidden items-center gap-1.5 px-2 py-1.5 border-b border-[var(--border)]">
  <div class="flex-1 min-w-0 overflow-x-auto flex gap-1">
    {#each filters as f}
      <button
        onclick={() => statusFilter.set(f.value)}
        class="shrink-0 px-2.5 py-1.5 rounded-full text-xs font-medium transition-colors
          {$statusFilter === f.value ? 'bg-[var(--accent)] text-white' : 'bg-[var(--bg-tertiary)] text-[var(--text-secondary)]'}"
      >
        {f.label}
        {#if f.value === 'all' && $stats.total > 0}<span class="ml-0.5 opacity-70">{$stats.total}</span>
        {:else if f.value === 'missing' && $stats.missing > 0}<span class="ml-0.5 opacity-70">{$stats.missing}</span>
        {:else if f.value === 'upgrade' && $stats.upgrade > 0}<span class="ml-0.5 opacity-70">{$stats.upgrade}</span>
        {:else if f.value === 'library' && $stats.library > 0}<span class="ml-0.5 opacity-70">{$stats.library}</span>{/if}
      </button>
    {/each}
  </div>
  <button
    onclick={() => (filterSheet = true)}
    class="relative shrink-0 flex items-center gap-1 px-3 py-1.5 rounded-lg text-xs font-medium bg-[var(--bg-tertiary)] text-[var(--text-primary)] border border-[var(--border)]"
  >
    <svg class="w-4 h-4" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" d="M3 4h18M6 12h12M10 20h4" /></svg>
    Filters
    {#if activeFilterCount > 0}
      <span class="absolute -top-1.5 -right-1.5 min-w-[1rem] h-4 px-1 rounded-full bg-[var(--accent)] text-white text-[10px] flex items-center justify-center">{activeFilterCount}</span>
    {/if}
  </button>
</div>

<BottomSheet open={filterSheet} title="View & filters" onclose={() => (filterSheet = false)}>
  <div class="space-y-4">
    <!-- View switch -->
    <div>
      <p class="text-xs font-medium text-[var(--text-secondary)] mb-1.5">View</p>
      <div class="grid grid-cols-3 gap-1 p-1 rounded-lg bg-[var(--bg-tertiary)]">
        {#each [['swipe', 'Swipe'], ['grid', 'Grid'], ['list', 'List']] as [val, label]}
          <button
            onclick={() => setViewMode(val as 'swipe' | 'grid' | 'list')}
            class="py-2 rounded-md text-sm font-medium transition-colors {$viewMode === val ? 'bg-[var(--accent)] text-white' : 'text-[var(--text-secondary)]'}"
          >{label}</button>
        {/each}
      </div>
    </div>

    <!-- Search -->
    <div>
      <label for="fb-search" class="text-xs font-medium text-[var(--text-secondary)] mb-1.5 block">Search</label>
      <input
        id="fb-search"
        type="text"
        bind:value={search}
        oninput={onSearch}
        placeholder="Filter by title…"
        class="w-full bg-[var(--bg-tertiary)] text-[var(--text-primary)] px-3 py-2.5 rounded-lg border border-[var(--border)] text-sm focus:outline-none focus:border-[var(--accent)]"
      />
    </div>

    <!-- Sort -->
    <div>
      <label for="fb-sort" class="text-xs font-medium text-[var(--text-secondary)] mb-1.5 block">Sort</label>
      <select
        id="fb-sort"
        value={$sortBy}
        onchange={(e) => sortBy.set((e.target as HTMLSelectElement).value as SortOption)}
        class="w-full bg-[var(--bg-tertiary)] text-[var(--text-primary)] px-3 py-2.5 rounded-lg border border-[var(--border)] text-sm focus:outline-none focus:border-[var(--accent)]"
      >
        {#each sortOptions as opt}<option value={opt.value}>{opt.label}</option>{/each}
      </select>
    </div>

    <!-- Quick filters -->
    <div>
      <p class="text-xs font-medium text-[var(--text-secondary)] mb-1.5">Quick filters</p>
      <div class="flex flex-wrap gap-2">
        {#each quickChips as chip}
          <button
            onclick={() => toggleQuickFilter(chip.key)}
            class="px-3 py-1.5 rounded-full text-sm border transition-colors
              {$quickFilters.includes(chip.key) ? 'bg-[var(--accent)]/15 border-[var(--accent)] text-[var(--accent)]' : 'border-[var(--border)] text-[var(--text-secondary)]'}"
          >{chip.label}</button>
        {/each}
      </div>
    </div>

    <!-- Genre / Language / Host -->
    {#if $availableGenres.length > 0}
      <div>
        <p class="text-xs font-medium text-[var(--text-secondary)] mb-1.5">Genre</p>
        <div class="flex flex-wrap gap-2">
          <button
            onclick={() => genreFilter.set([])}
            class="px-3 py-1.5 rounded-full text-sm border transition-colors
              {$genreFilter.length === 0 ? 'bg-[var(--accent)]/15 border-[var(--accent)] text-[var(--accent)]' : 'border-[var(--border)] text-[var(--text-secondary)]'}"
          >All</button>
          {#each $availableGenres as g}
            <button
              onclick={() => toggleGenreFilter(g)}
              class="px-3 py-1.5 rounded-full text-sm border transition-colors
                {$genreFilter.includes(g) ? 'bg-[var(--accent)]/15 border-[var(--accent)] text-[var(--accent)]' : 'border-[var(--border)] text-[var(--text-secondary)]'}"
            >{g}</button>
          {/each}
        </div>
      </div>
    {/if}
    {#if $availableLanguages.length > 1}
      <div>
        <p class="text-xs font-medium text-[var(--text-secondary)] mb-1.5">Language</p>
        <div class="flex flex-wrap gap-2">
          <button
            onclick={() => languageFilter.set([])}
            class="px-3 py-1.5 rounded-full text-sm border transition-colors
              {$languageFilter.length === 0 ? 'bg-[var(--accent)]/15 border-[var(--accent)] text-[var(--accent)]' : 'border-[var(--border)] text-[var(--text-secondary)]'}"
          >All</button>
          {#each $availableLanguages as l}
            <button
              onclick={() => toggleLanguageFilter(l)}
              class="px-3 py-1.5 rounded-full text-sm border transition-colors
                {$languageFilter.includes(l) ? 'bg-[var(--accent)]/15 border-[var(--accent)] text-[var(--accent)]' : 'border-[var(--border)] text-[var(--text-secondary)]'}"
            >{l}</button>
          {/each}
        </div>
      </div>
    {/if}
    <div>
      <label for="fb-host" class="text-xs font-medium text-[var(--text-secondary)] mb-1.5 block">Download host</label>
      <select id="fb-host" value={$downloadHost} onchange={(e) => downloadHost.set((e.target as HTMLSelectElement).value)} class="w-full bg-[var(--bg-tertiary)] text-[var(--text-primary)] px-3 py-2.5 rounded-lg border border-[var(--border)] text-sm">
        {#each DOWNLOAD_HOSTS as h}<option value={h.value}>{h.value}</option>{/each}
      </select>
    </div>

    <!-- List options -->
    {#if $viewMode === 'list'}
      <div>
        <p class="text-xs font-medium text-[var(--text-secondary)] mb-1.5">Columns</p>
        <div class="flex flex-wrap gap-2">
          {#each columnDefs as c}
            <button
              onclick={() => toggleColumn(c.key)}
              class="px-3 py-1.5 rounded-full text-sm border transition-colors {$visibleColumns[c.key] ? 'bg-[var(--accent)]/15 border-[var(--accent)] text-[var(--accent)]' : 'border-[var(--border)] text-[var(--text-secondary)]'}"
            >{c.label}</button>
          {/each}
        </div>
      </div>
      <div>
        <p class="text-xs font-medium text-[var(--text-secondary)] mb-1.5">Density</p>
        <div class="grid grid-cols-2 gap-1 p-1 rounded-lg bg-[var(--bg-tertiary)]">
          <button onclick={() => density.set('comfortable')} class="py-2 rounded-md text-sm {$density === 'comfortable' ? 'bg-[var(--accent)] text-white' : 'text-[var(--text-secondary)]'}">Comfortable</button>
          <button onclick={() => density.set('compact')} class="py-2 rounded-md text-sm {$density === 'compact' ? 'bg-[var(--accent)] text-white' : 'text-[var(--text-secondary)]'}">Compact</button>
        </div>
      </div>
    {/if}

    <!-- Selection / batch actions -->
    {#if resultCount > 0}
      <div class="pt-2 border-t border-[var(--border)] space-y-2">
        <div class="flex items-center justify-between text-sm">
          <span class="text-[var(--text-secondary)]">{resultCount} results{selectedCount > 0 ? ` · ${selectedCount} selected` : ''}</span>
          <div class="flex gap-3">
            <button onclick={() => selectAll($filteredResults.map(i => i.url))} class="text-[var(--accent)]">Select all</button>
            {#if selectedCount > 0}<button onclick={() => deselectAll()} class="text-[var(--text-secondary)]">Clear</button>{/if}
          </div>
        </div>
        {#if selectedCount > 0}
          <div class="grid grid-cols-2 gap-2">
            <button onclick={bulkDownloadAll} disabled={downloadingAll} class="py-2.5 rounded-lg text-sm font-semibold text-white bg-[var(--accent)] disabled:opacity-50">{downloadingAll ? '…' : '⬇ Download'}</button>
            <button onclick={bulkCopyLinks} disabled={copyingLinks} class="py-2.5 rounded-lg text-sm font-medium text-[var(--accent)] border border-[var(--border)] disabled:opacity-50">{copyingLinks ? '…' : '🔗 Copy links'}</button>
            <button onclick={bulkAddToWatchlist} disabled={addingToWatchlist} class="py-2.5 rounded-lg text-sm font-medium text-[var(--accent)] border border-[var(--border)] disabled:opacity-50">{addingToWatchlist ? '…' : '+ Watchlist'}</button>
            <button onclick={handleExport} class="py-2.5 rounded-lg text-sm font-medium text-[var(--text-secondary)] border border-[var(--border)]">Export CSV</button>
          </div>
        {:else}
          <button onclick={handleExport} class="w-full py-2.5 rounded-lg text-sm font-medium text-[var(--text-secondary)] border border-[var(--border)]">Export CSV</button>
        {/if}
      </div>
    {/if}
  </div>
</BottomSheet>
