<script lang="ts">
  import { statusFilter, searchFilter, genreFilter, languageFilter, toggleGenreFilter, toggleLanguageFilter, viewMode, setViewMode, stats, selectedKeys, selectAll, deselectAll, filteredResults, sortBy, availableGenres, availableLanguages, density, quickFilters, toggleQuickFilter, resolutionFilter, toggleResolutionFilter, RESOLUTION_KEYS, categoryFilter, toggleCategoryFilter, CATEGORY_KEYS, CATEGORY_LABELS, tileSize, posterAspect, tileShowMeta, gridGap, gridColumns, GRID_COLUMN_CHOICES, postedAfter, postedBefore, pagedMode, filteredTotal, mobileChromeCollapsed, type TileSize, type PosterAspect, type GridGap } from '$lib/stores/results';
  import { downloadHost } from '$lib/stores/downloads';
  import { api } from '$lib/api/client';
  import { addToast } from '$lib/stores/notifications';
  import { DOWNLOAD_HOSTS } from '$lib/constants';
  import { scanState } from '$lib/stores/scanner';
  import BottomSheet from './BottomSheet.svelte';
  import type { StatusFilter, SortOption } from '$lib/stores/results';

  interface Props {
    sheetOpen?: boolean;
    showMobileTrigger?: boolean;
  }
  let { sheetOpen = $bindable(false), showMobileTrigger = true }: Props = $props();
  let activeFilterCount = $derived(
    ($statusFilter !== 'all' ? 1 : 0) +
    ($searchFilter ? 1 : 0) +
    (($genreFilter.include.length + $genreFilter.exclude.length) > 0 ? 1 : 0) +
    ($languageFilter.length > 0 ? 1 : 0) +
    $quickFilters.length +
    $resolutionFilter.length +
    // A hidden category (chip toggled off in either sheet — see the Category
    // row below) is exactly the kind of narrowing this badge exists to
    // surface; count it same as the other "narrowed from show-everything" terms above.
    ($categoryFilter.length < CATEGORY_KEYS.length ? 1 : 0) +
    ($postedAfter || $postedBefore ? 1 : 0)
  );

  function clearPostedRange() {
    postedAfter.set('');
    postedBefore.set('');
  }

  // Collapses in sync with ScanControls' mobile bar and the layout's mobile
  // title bar (same store, same idle-gate) so the phone's top chrome moves
  // as one coherent unit rather than partially collapsing.
  let hideMobileRow = $derived($mobileChromeCollapsed && $scanState === 'idle');

  const quickChips = [
    { key: '4k', label: '4K' },
    { key: 'hdrdv', label: 'HDR/DV' },
    { key: 'inplex', label: 'In Plex' },
    { key: 'bookmarked', label: 'Bookmarked' },
  ];

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
  // D3: in paged mode, "select all" only selects the rows currently loaded
  // client-side — the server may have many more matches than that. Rather
  // than silently under-selecting, say so explicitly.
  let selectAllLabel = $derived($pagedMode && $filteredTotal > resultCount ? `Select loaded (${resultCount})` : 'All');
  let selectAllTitle = $derived(
    $pagedMode && $filteredTotal > resultCount
      ? `Only the ${resultCount} loaded result(s) will be selected — ${$filteredTotal} match the current filters. Scroll to load more, then select again.`
      : 'Select visible results'
  );
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
      await api.downloadBatch(selected.map(i => ({ url: i.url, title: i.title, year: i.year, season: i.season, resolution: i.resolution, size: i.size, hdr: i.hdr, dovi: i.dovi })), $downloadHost);
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
        title={selectAllTitle}
      >
        {selectAllLabel}
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
        Genres{#if ($genreFilter.include.length + $genreFilter.exclude.length) > 0}<span class="ml-0.5 text-[var(--accent)]">({$genreFilter.include.length + $genreFilter.exclude.length})</span>{/if} &#9662;
      </summary>
      <div class="absolute right-0 mt-1 z-20 w-44 max-h-72 overflow-y-auto bg-[var(--bg-secondary)] border border-[var(--border)] rounded-lg shadow-lg p-1.5">
        <button
          onclick={() => genreFilter.set({ include: [], exclude: [] })}
          class="w-full text-left px-2 py-1 rounded text-xs font-medium mb-1
            {($genreFilter.include.length === 0 && $genreFilter.exclude.length === 0) ? 'bg-[var(--accent)]/15 text-[var(--accent)]' : 'text-[var(--text-secondary)] hover:bg-[var(--bg-tertiary)]'}"
        >
          All genres
        </button>
        {#each $availableGenres as genre}
          {@const state = $genreFilter.include.includes(genre) ? 'include' : $genreFilter.exclude.includes(genre) ? 'exclude' : 'neutral'}
          <button
            type="button"
            onclick={() => toggleGenreFilter(genre)}
            class="flex items-center gap-2 w-full text-left px-2 py-1 rounded text-xs cursor-pointer
              {state === 'include' ? 'bg-[var(--accent)]/15 text-[var(--accent)]' : ''}
              {state === 'exclude' ? 'bg-red-500/15 text-red-500 line-through' : ''}
              {state === 'neutral' ? 'text-[var(--text-secondary)] hover:bg-[var(--bg-tertiary)]' : ''}"
          >
            {genre}
          </button>
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

  <!-- Posted date range -->
  <div class="flex items-center gap-1">
    <label for="fb-posted-after" class="sr-only">Posted after</label>
    <input
      id="fb-posted-after"
      type="date"
      value={$postedAfter}
      onchange={(e) => postedAfter.set((e.target as HTMLInputElement).value)}
      title="Posted after (inclusive)"
      class="bg-[var(--bg-tertiary)] text-[var(--text-primary)] px-1.5 py-1 rounded border border-[var(--border)] text-[11px] focus:outline-none focus:border-[var(--accent)] cursor-pointer w-[8.5rem]
        {$postedAfter ? 'border-[var(--accent)]' : ''}"
    />
    <span class="text-[var(--text-secondary)] text-[11px]">–</span>
    <label for="fb-posted-before" class="sr-only">Posted before</label>
    <input
      id="fb-posted-before"
      type="date"
      value={$postedBefore}
      onchange={(e) => postedBefore.set((e.target as HTMLInputElement).value)}
      title="Posted before (inclusive of that day)"
      class="bg-[var(--bg-tertiary)] text-[var(--text-primary)] px-1.5 py-1 rounded border border-[var(--border)] text-[11px] focus:outline-none focus:border-[var(--accent)] cursor-pointer w-[8.5rem]
        {$postedBefore ? 'border-[var(--accent)]' : ''}"
    />
    {#if $postedAfter || $postedBefore}
      <button
        onclick={clearPostedRange}
        aria-label="Clear posted date range"
        title="Clear posted date range"
        class="px-1 py-1 rounded text-[11px] text-[var(--text-secondary)] hover:bg-[var(--bg-tertiary)] transition-colors"
      >
        &times;
      </button>
    {/if}
  </div>

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
  {/if}

  {#if $viewMode === 'grid'}
    <!-- Grid display options (grid view) -->
    <details class="relative">
      <summary class="list-none cursor-pointer px-2 py-1 rounded text-[11px] text-[var(--text-secondary)] bg-[var(--bg-tertiary)] border border-[var(--border)] hover:border-[var(--accent)] select-none">
        Grid &#9662;
      </summary>
      <div class="absolute right-0 z-30 mt-1 w-52 p-3 rounded-lg border border-[var(--border)] bg-[var(--bg-secondary)] shadow-xl space-y-3">
        <div>
          <p class="text-[10px] uppercase tracking-wide text-[var(--text-secondary)] mb-1">Columns</p>
          <div class="flex flex-wrap gap-1">
            {#each GRID_COLUMN_CHOICES as c}
              <button onclick={() => gridColumns.set(c)} class="px-2 py-1 rounded text-xs transition-colors {$gridColumns === c ? 'bg-[var(--accent)] text-white' : 'bg-[var(--bg-tertiary)] text-[var(--text-secondary)]'}">{c === 'auto' ? 'Auto' : c}</button>
            {/each}
          </div>
        </div>
        <div>
          <p class="text-[10px] uppercase tracking-wide text-[var(--text-secondary)] mb-1">Tile size{#if $gridColumns !== 'auto'}<span class="opacity-50"> (Auto only)</span>{/if}</p>
          <div class="grid grid-cols-3 gap-1">
            {#each [['sm', 'S'], ['md', 'M'], ['lg', 'L']] as [val, label]}
              <button onclick={() => tileSize.set(val as TileSize)} class="py-1 rounded text-xs transition-colors {$tileSize === val ? 'bg-[var(--accent)] text-white' : 'bg-[var(--bg-tertiary)] text-[var(--text-secondary)]'}">{label}</button>
            {/each}
          </div>
        </div>
        <div>
          <p class="text-[10px] uppercase tracking-wide text-[var(--text-secondary)] mb-1">Poster aspect</p>
          <div class="grid grid-cols-3 gap-1">
            {#each [['2/3', '2:3'], ['16/9', '16:9'], ['1/1', '1:1']] as [val, label]}
              <button onclick={() => posterAspect.set(val as PosterAspect)} class="py-1 rounded text-xs transition-colors {$posterAspect === val ? 'bg-[var(--accent)] text-white' : 'bg-[var(--bg-tertiary)] text-[var(--text-secondary)]'}">{label}</button>
            {/each}
          </div>
        </div>
        <div>
          <p class="text-[10px] uppercase tracking-wide text-[var(--text-secondary)] mb-1">Spacing</p>
          <div class="grid grid-cols-3 gap-1">
            {#each [['tight', 'Tight'], ['normal', 'Normal'], ['roomy', 'Roomy']] as [val, label]}
              <button onclick={() => gridGap.set(val as GridGap)} class="py-1 rounded text-xs transition-colors {$gridGap === val ? 'bg-[var(--accent)] text-white' : 'bg-[var(--bg-tertiary)] text-[var(--text-secondary)]'}">{label}</button>
            {/each}
          </div>
        </div>
        <label class="flex items-center justify-between text-xs text-[var(--text-secondary)] cursor-pointer select-none">
          <span>Poster only</span>
          <input type="checkbox" checked={!$tileShowMeta} onchange={(e) => tileShowMeta.set(!(e.currentTarget as HTMLInputElement).checked)} class="accent-[var(--accent)]" />
        </label>
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

<!-- Mobile toolbar: scrollable status chips + a Filters button → bottom sheet.
     Collapses fully (grid-rows 0fr) in sync with the scan bar / title bar. -->
<div
  class="grid md:hidden transition-[grid-template-rows] duration-200 ease-out"
  style="grid-template-rows: {hideMobileRow ? '0fr' : '1fr'};"
>
<div class="overflow-hidden flex items-center gap-1.5 px-2 py-1.5 border-b border-[var(--border)]">
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
    <!-- Resolution/type facet (4K / 1080p / TV) — multi-toggle, OR-combined. -->
    <span class="shrink-0 w-px my-1 bg-[var(--border)]"></span>
    {#each RESOLUTION_KEYS as rk}
      <button
        onclick={() => toggleResolutionFilter(rk)}
        aria-pressed={$resolutionFilter.includes(rk)}
        class="shrink-0 px-2.5 py-1.5 rounded-full text-xs font-medium transition-colors
          {$resolutionFilter.includes(rk) ? 'bg-[var(--accent)] text-white' : 'bg-[var(--bg-tertiary)] text-[var(--text-secondary)]'}"
      >{rk}</button>
    {/each}
  </div>
  {#if showMobileTrigger}
    <button
      onclick={() => (sheetOpen = true)}
      class="relative shrink-0 flex items-center gap-1 px-3 py-1.5 rounded-lg text-xs font-medium bg-[var(--bg-tertiary)] text-[var(--text-primary)] border border-[var(--border)]"
    >
      <svg class="w-4 h-4" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" d="M3 4h18M6 12h12M10 20h4" /></svg>
      Filters
      {#if activeFilterCount > 0}
        <span class="absolute -top-1.5 -right-1.5 min-w-[1rem] h-4 px-1 rounded-full bg-[var(--accent)] text-white text-[10px] flex items-center justify-center">{activeFilterCount}</span>
      {/if}
    </button>
  {/if}
</div>
</div>

<BottomSheet open={sheetOpen} title="View & filters" onclose={() => (sheetOpen = false)}>
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

    <!-- Category filter (4K / Remux / TV) — the SAME categoryFilter store
         ScanControls' Scan-options sheet chips write to (via
         toggleCategoryFilter, results.ts' single writer for this store — see
         its doc comment). Surfaced here too since this sheet is where users
         look for it on mobile: the always-visible row above only shows the
         near-identical *resolution* facet (4K/1080p/TV), and Scan options is
         about what to SCAN next, not what's currently shown. -->
    <div>
      <p class="text-xs font-medium text-[var(--text-secondary)] mb-1.5">Category</p>
      <div class="flex flex-wrap gap-2">
        {#each CATEGORY_KEYS as key}
          <button
            onclick={() => toggleCategoryFilter(key)}
            aria-pressed={$categoryFilter.includes(key)}
            class="px-3 py-1.5 rounded-full text-sm border transition-colors
              {$categoryFilter.includes(key) ? 'bg-[var(--accent)]/15 border-[var(--accent)] text-[var(--accent)]' : 'border-[var(--border)] text-[var(--text-secondary)]'}"
          >{CATEGORY_LABELS[key]}</button>
        {/each}
      </div>
    </div>

    <!-- Genre / Language / Host -->
    {#if $availableGenres.length > 0}
      <div>
        <p class="text-xs font-medium text-[var(--text-secondary)] mb-1.5">Genre</p>
        <div class="flex flex-wrap gap-2">
          <button
            onclick={() => genreFilter.set({ include: [], exclude: [] })}
            class="px-3 py-1.5 rounded-full text-sm border transition-colors
              {$genreFilter.include.length === 0 && $genreFilter.exclude.length === 0 ? 'bg-[var(--accent)]/15 border-[var(--accent)] text-[var(--accent)]' : 'border-[var(--border)] text-[var(--text-secondary)]'}"
          >All</button>
          {#each $availableGenres as g}
            {@const state = $genreFilter.include.includes(g) ? 'include' : $genreFilter.exclude.includes(g) ? 'exclude' : 'neutral'}
            <button
              onclick={() => toggleGenreFilter(g)}
              class="px-3 py-1.5 rounded-full text-sm border transition-colors
                {state === 'include' ? 'bg-[var(--accent)]/15 border-[var(--accent)] text-[var(--accent)]' : ''}
                {state === 'exclude' ? 'bg-red-500/15 border-red-500 text-red-500 line-through' : ''}
                {state === 'neutral' ? 'border-[var(--border)] text-[var(--text-secondary)]' : ''}"
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
    <!-- Posted date range -->
    <div>
      <div class="flex items-center justify-between mb-1.5">
        <p class="text-xs font-medium text-[var(--text-secondary)]">Posted date</p>
        {#if $postedAfter || $postedBefore}
          <button onclick={clearPostedRange} class="text-xs text-[var(--accent)]">Clear</button>
        {/if}
      </div>
      <div class="flex items-center gap-2">
        <div class="flex-1">
          <label for="fb-posted-after-sheet" class="sr-only">Posted after</label>
          <input
            id="fb-posted-after-sheet"
            type="date"
            value={$postedAfter}
            onchange={(e) => postedAfter.set((e.target as HTMLInputElement).value)}
            title="Posted after (inclusive)"
            class="w-full bg-[var(--bg-tertiary)] text-[var(--text-primary)] px-3 py-2.5 rounded-lg border text-sm focus:outline-none focus:border-[var(--accent)]
              {$postedAfter ? 'border-[var(--accent)]' : 'border-[var(--border)]'}"
          />
        </div>
        <span class="text-[var(--text-secondary)] text-sm">to</span>
        <div class="flex-1">
          <label for="fb-posted-before-sheet" class="sr-only">Posted before</label>
          <input
            id="fb-posted-before-sheet"
            type="date"
            value={$postedBefore}
            onchange={(e) => postedBefore.set((e.target as HTMLInputElement).value)}
            title="Posted before (inclusive of that day)"
            class="w-full bg-[var(--bg-tertiary)] text-[var(--text-primary)] px-3 py-2.5 rounded-lg border text-sm focus:outline-none focus:border-[var(--accent)]
              {$postedBefore ? 'border-[var(--accent)]' : 'border-[var(--border)]'}"
          />
        </div>
      </div>
    </div>

    <div>
      <label for="fb-host" class="text-xs font-medium text-[var(--text-secondary)] mb-1.5 block">Download host</label>
      <select id="fb-host" value={$downloadHost} onchange={(e) => downloadHost.set((e.target as HTMLSelectElement).value)} class="w-full bg-[var(--bg-tertiary)] text-[var(--text-primary)] px-3 py-2.5 rounded-lg border border-[var(--border)] text-sm">
        {#each DOWNLOAD_HOSTS as h}<option value={h.value}>{h.value}</option>{/each}
      </select>
    </div>

    <!-- List options -->
    {#if $viewMode === 'list'}
      <div>
        <p class="text-xs font-medium text-[var(--text-secondary)] mb-1.5">Density</p>
        <div class="grid grid-cols-2 gap-1 p-1 rounded-lg bg-[var(--bg-tertiary)]">
          <button onclick={() => density.set('comfortable')} class="py-2 rounded-md text-sm {$density === 'comfortable' ? 'bg-[var(--accent)] text-white' : 'text-[var(--text-secondary)]'}">Comfortable</button>
          <button onclick={() => density.set('compact')} class="py-2 rounded-md text-sm {$density === 'compact' ? 'bg-[var(--accent)] text-white' : 'text-[var(--text-secondary)]'}">Compact</button>
        </div>
      </div>
    {/if}

    <!-- Grid options -->
    {#if $viewMode === 'grid'}
      <div class="space-y-3">
        <div>
          <p class="text-xs font-medium text-[var(--text-secondary)] mb-1.5">Columns</p>
          <div class="flex flex-wrap gap-1">
            {#each GRID_COLUMN_CHOICES as c}
              <button onclick={() => gridColumns.set(c)} class="px-3 py-2 rounded-md text-sm {$gridColumns === c ? 'bg-[var(--accent)] text-white' : 'bg-[var(--bg-tertiary)] text-[var(--text-secondary)]'}">{c === 'auto' ? 'Auto' : c}</button>
            {/each}
          </div>
        </div>
        <div>
          <p class="text-xs font-medium text-[var(--text-secondary)] mb-1.5">Tile size</p>
          <div class="grid grid-cols-3 gap-1 p-1 rounded-lg bg-[var(--bg-tertiary)]">
            {#each [['sm', 'Small'], ['md', 'Medium'], ['lg', 'Large']] as [val, label]}
              <button onclick={() => tileSize.set(val as TileSize)} class="py-2 rounded-md text-sm {$tileSize === val ? 'bg-[var(--accent)] text-white' : 'text-[var(--text-secondary)]'}">{label}</button>
            {/each}
          </div>
        </div>
        <div>
          <p class="text-xs font-medium text-[var(--text-secondary)] mb-1.5">Poster aspect</p>
          <div class="grid grid-cols-3 gap-1 p-1 rounded-lg bg-[var(--bg-tertiary)]">
            {#each [['2/3', '2:3'], ['16/9', '16:9'], ['1/1', '1:1']] as [val, label]}
              <button onclick={() => posterAspect.set(val as PosterAspect)} class="py-2 rounded-md text-sm {$posterAspect === val ? 'bg-[var(--accent)] text-white' : 'text-[var(--text-secondary)]'}">{label}</button>
            {/each}
          </div>
        </div>
        <div>
          <p class="text-xs font-medium text-[var(--text-secondary)] mb-1.5">Spacing</p>
          <div class="grid grid-cols-3 gap-1 p-1 rounded-lg bg-[var(--bg-tertiary)]">
            {#each [['tight', 'Tight'], ['normal', 'Normal'], ['roomy', 'Roomy']] as [val, label]}
              <button onclick={() => gridGap.set(val as GridGap)} class="py-2 rounded-md text-sm {$gridGap === val ? 'bg-[var(--accent)] text-white' : 'text-[var(--text-secondary)]'}">{label}</button>
            {/each}
          </div>
        </div>
        <label class="flex items-center justify-between text-sm text-[var(--text-secondary)] py-1">
          <span>Poster only</span>
          <input type="checkbox" checked={!$tileShowMeta} onchange={(e) => tileShowMeta.set(!(e.currentTarget as HTMLInputElement).checked)} class="accent-[var(--accent)] w-4 h-4" />
        </label>
      </div>
    {/if}

    <!-- Selection / batch actions -->
    {#if resultCount > 0}
      <div class="pt-2 border-t border-[var(--border)] space-y-2">
        <div class="flex items-center justify-between text-sm">
          <span class="text-[var(--text-secondary)]">{resultCount} results{selectedCount > 0 ? ` · ${selectedCount} selected` : ''}</span>
          <div class="flex gap-3">
            <button onclick={() => selectAll($filteredResults.map(i => i.url))} class="text-[var(--accent)]" title={selectAllTitle}>{selectAllLabel}</button>
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
