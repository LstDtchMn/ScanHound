<script lang="ts">
  import ScanControls from '$lib/components/ScanControls.svelte';
  import FilterBar from '$lib/components/FilterBar.svelte';
  import StatusBar from '$lib/components/StatusBar.svelte';
  import ResultTile from '$lib/components/ResultTile.svelte';
  import ResultRow from '$lib/components/ResultRow.svelte';
  import ContextMenu from '$lib/components/ContextMenu.svelte';
  import ResultActionSheet from '$lib/components/ResultActionSheet.svelte';
  import DetailPanel from '$lib/components/DetailPanel.svelte';
  import SwipeDeck from '$lib/components/SwipeDeck.svelte';
  import { filteredResults, viewMode, viewModeExplicit, results, stats, selectedDetail, focusedIndex, toggleSelect, visibleColumns, hydrateDismissed } from '$lib/stores/results';
  import { mobile } from '$lib/stores/media';
  import { get } from 'svelte/store';
  import { scanState, scanProgress, scanPhase } from '$lib/stores/scanner';
  import { settings, settingsLoaded, loadSettings } from '$lib/stores/settings';
  import { plexConnected, plexMovieCount, plexTvCount, refreshPlexStatus } from '$lib/stores/plex';
  import { batchProgress } from '$lib/stores/downloads';
  import { jdConnection, refreshJdConnection } from '$lib/stores/jdownloader';
  import { api } from '$lib/api/client';
  import { onMount } from 'svelte';
  import { slide } from 'svelte/transition';
  import type { ScanResult } from '$lib/api/types';

  let tmdbKeyMissing = $state(false);
  let tmdbBannerDismissed = $state(
    typeof localStorage !== 'undefined' && localStorage.getItem('tmdb-banner-dismissed') === 'true'
  );
  // Tracks whether initial Plex status check is still pending
  let plexChecking = $state(true);

  // Trending movies for empty state discovery
  let trendingMovies = $state<{ id: number; title: string; year: string | null; poster_url: string; rating: number }[]>([]);

  // Pre-flight checklist state — treat masked value as configured
  const MASK = '\u2022\u2022\u2022\u2022\u2022\u2022\u2022\u2022';
  let tmdbConfigured = $derived($settingsLoaded && (!!$settings.tmdb_api_key || $settings.tmdb_api_key === MASK));
  let omdbConfigured = $derived($settingsLoaded && (!!$settings.omdb_api_key || $settings.omdb_api_key === MASK));
  let metadataConfigured = $derived(tmdbConfigured || omdbConfigured);
  let lastScanFormatted = $derived(() => {
    const ts = $settings.last_scan_time as number | undefined;
    if (!ts) return 'Never';
    const d = new Date(ts * 1000);
    const now = Date.now();
    const diffMs = now - d.getTime();
    const diffH = Math.floor(diffMs / 3600000);
    if (diffH < 1) return 'Just now';
    if (diffH < 24) return `${diffH}h ago`;
    const diffD = Math.floor(diffH / 24);
    return diffD === 1 ? 'Yesterday' : `${diffD}d ago`;
  });

  // HDEncode is always available; DDLBase and Adit-HD are toggleable
  let sources = $derived([
    { name: 'HDEncode', enabled: true },
    { name: 'DDLBase', enabled: !!$settings.ddlbase_enabled },
    { name: 'Adit-HD', enabled: !!$settings.adithd_enabled }
  ]);
  let enabledSourceCount = $derived(sources.filter(s => s.enabled).length);
  let jdEnabled = $derived(!!$settings.jd_enabled);
  let issueCount = $derived(
    (($plexConnected || plexChecking) ? 0 : 1) + (metadataConfigured ? 0 : 1) + (enabledSourceCount === 0 ? 1 : 0)
  );

  let contextMenu = $state<{ item: ScanResult; x: number; y: number } | null>(null);
  let mobileActionItem = $state<ScanResult | null>(null);
  let currentPage = $state(1);
  const perPage = 100;
  let collapsedGroups = $state<Set<string>>(new Set());
  let resultsContainer: HTMLDivElement | undefined = $state();

  let tileColumns = $derived(($settings.tile_columns as number) || 0);
  let gridStyle = $derived(
    tileColumns > 0
      ? `grid-template-columns: repeat(${tileColumns}, 1fr)`
      : 'grid-template-columns: repeat(auto-fill, minmax(160px, 1fr))'
  );

  let totalPages = $derived(Math.max(1, Math.ceil($filteredResults.length / perPage)));
  let paginatedResults = $derived(
    $filteredResults.slice((currentPage - 1) * perPage, currentPage * perPage)
  );

  // Group results by title for group headers
  interface ResultGroup {
    title: string;
    items: ScanResult[];
  }
  let groupedResults = $derived(() => {
    const groups: ResultGroup[] = [];
    const map = new Map<string, ResultGroup>();
    for (const item of paginatedResults) {
      const key = item.title;
      let group = map.get(key);
      if (!group) {
        group = { title: key, items: [] };
        map.set(key, group);
        groups.push(group);
      }
      group.items.push(item);
    }
    return groups;
  });

  // Sibling counts across ALL filtered results (not just current page)
  let siblingCounts = $derived(() => {
    const counts = new Map<string, number>();
    for (const item of $filteredResults) {
      counts.set(item.title, (counts.get(item.title) || 0) + 1);
    }
    return counts;
  });

  function isDuplicateGroup(group: ResultGroup) {
    return (siblingCounts().get(group.title) || group.items.length) > 1;
  }

  function toggleGroup(title: string) {
    collapsedGroups = new Set(collapsedGroups);
    if (collapsedGroups.has(title)) collapsedGroups.delete(title);
    else collapsedGroups.add(title);
  }

  // Reset page and keyboard focus when filter changes
  $effect(() => {
    // eslint-disable-next-line @typescript-eslint/no-unused-expressions
    $filteredResults.length;
    currentPage = 1;
    focusedIndex.set(-1);
  });

  // Scroll to top when page or filters change
  $effect(() => {
    // eslint-disable-next-line @typescript-eslint/no-unused-expressions
    currentPage;
    // eslint-disable-next-line @typescript-eslint/no-unused-expressions
    $filteredResults.length;
    resultsContainer?.scrollTo({ top: 0, behavior: 'smooth' });
  });

  onMount(async () => {
    // Phones default to the swipe deck unless the user has chosen a view.
    if (get(mobile) && !get(viewModeExplicit)) viewMode.set('swipe');
    // Load all status in parallel so checklist shows accurate data on first render
    await Promise.all([
      // Pull the persisted swipe-dismissal set so skipped items stay hidden —
      // awaited alongside results so the deck never briefly shows cards the
      // user already swiped away in an earlier session.
      hydrateDismissed(),
      refreshPlexStatus().finally(() => { plexChecking = false; }),
      loadSettings(),
      (async () => {
        try {
          const resp = await api.getResults({ per_page: '500' });
          if (resp.items && resp.items.length > 0) {
            results.set(resp.items);
            if (resp.stats) stats.set(resp.stats);
          }
        } catch { /* no previous results */ }
      })(),
    ]);
    // Settings store is now populated — check metadata keys for banner
    tmdbKeyMissing = !$settings.tmdb_api_key && !$settings.omdb_api_key;

    // Check JDownloader connection for the checklist indicator
    if ($settings.jd_enabled) refreshJdConnection();

    // Plex auto-connect may still be loading — retry status after a delay
    if (!$plexConnected && $settings.auto_connect_plex) {
      setTimeout(() => refreshPlexStatus(), 5000);
    }

    // Fetch trending movies for discovery section
    if (tmdbConfigured) {
      api.discover('trending').then(r => { trendingMovies = r.items?.slice(0, 10) ?? []; }).catch(() => {});
    }
  });

  // Flat list of visible (non-collapsed) paginated items for keyboard navigation
  let flatVisibleItems = $derived(() => {
    const items: ScanResult[] = [];
    for (const group of groupedResults()) {
      if (!collapsedGroups.has(group.title)) {
        items.push(...group.items);
      }
    }
    return items;
  });

  // Map from item reference to flat index for focused indicator
  let flatIndexMap = $derived(() => {
    const map = new Map<ScanResult, number>();
    let i = 0;
    for (const group of groupedResults()) {
      if (!collapsedGroups.has(group.title)) {
        for (const item of group.items) {
          map.set(item, i++);
        }
      }
    }
    return map;
  });

  function handleResultsKeydown(e: KeyboardEvent) {
    const tag = (e.target as HTMLElement)?.tagName;
    if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT') return;

    const items = flatVisibleItems();
    if (items.length === 0) return;

    switch (e.key) {
      case 'ArrowDown': {
        e.preventDefault();
        focusedIndex.update(i => Math.min(i + 1, items.length - 1));
        break;
      }
      case 'ArrowUp': {
        e.preventDefault();
        focusedIndex.update(i => Math.max(i - 1, 0));
        break;
      }
      case 'Enter': {
        const idx = $focusedIndex;
        if (idx >= 0 && idx < items.length) {
          e.preventDefault();
          selectedDetail.set(items[idx]);
        }
        break;
      }
      case ' ': {
        const idx = $focusedIndex;
        if (idx >= 0 && idx < items.length) {
          e.preventDefault();
          toggleSelect(items[idx].url);
        }
        break;
      }
    }
  }

  function handleContextMenu(e: MouseEvent, item: ScanResult) {
    e.preventDefault();
    // On touch, long-press fires `contextmenu` — show a bottom sheet instead of
    // a cursor-positioned popup.
    if (get(mobile)) {
      mobileActionItem = item;
    } else {
      contextMenu = { item, x: e.clientX, y: e.clientY };
    }
  }
</script>

<ScanControls />

{#if $scanState === 'running'}
  <div class="px-3 py-1 border-b border-[var(--border)] bg-[var(--bg-secondary)]">
    <div class="flex items-center gap-2">
      <div class="flex-1 h-1.5 bg-[var(--bg-tertiary)] rounded-full overflow-hidden">
        <div
          class="h-full bg-[var(--accent)] rounded-full transition-all duration-300"
          style="width: {Math.max($scanProgress * 100, 2)}%"
        ></div>
      </div>
      <span class="text-[10px] text-[var(--text-secondary)] whitespace-nowrap">{$scanPhase || 'Starting...'}</span>
    </div>
  </div>
{/if}

<FilterBar />

{#if $batchProgress}
  <div class="px-3 py-1.5 border-b border-[var(--border)] bg-[color-mix(in_srgb,var(--accent)_6%,var(--bg-primary))]">
    <div class="flex items-center justify-between mb-1 text-[11px]">
      <span class="font-medium">
        Processing {$batchProgress.completed} / {$batchProgress.total}
        {#if $batchProgress.currentTitle}<span class="text-[var(--text-secondary)]">&mdash; {$batchProgress.currentTitle}</span>{/if}
      </span>
      {#if $batchProgress.completed >= $batchProgress.total}
        <span class="text-[var(--success)]">Done</span>
      {/if}
    </div>
    <div class="w-full h-1.5 bg-[var(--bg-tertiary)] rounded-full overflow-hidden">
      <div
        class="h-full rounded-full transition-all duration-300"
        style="width: {$batchProgress.total > 0 ? ($batchProgress.completed / $batchProgress.total) * 100 : 0}%; background: {$batchProgress.completed >= $batchProgress.total ? 'var(--success)' : 'var(--accent)'};"
      ></div>
    </div>
  </div>
{/if}

{#if tmdbKeyMissing && !tmdbBannerDismissed && $results.length > 0}
  <div class="mx-4 mt-2 flex items-center gap-2 rounded-lg bg-amber-500/10 border border-amber-500/30 px-4 py-2 text-sm text-amber-600 dark:text-amber-400">
    <span>Configure a TMDB API key in Settings &rarr; Sources for poster images and metadata enrichment.</span>
    <button class="ml-auto text-amber-600 dark:text-amber-400 hover:opacity-70" onclick={() => { tmdbBannerDismissed = true; localStorage.setItem('tmdb-banner-dismissed', 'true'); }}>&times;</button>
  </div>
{/if}

{#if $viewMode === 'swipe'}
  <SwipeDeck />
{:else}
<!-- svelte-ignore a11y_no_static_element_interactions -->
<!-- svelte-ignore a11y_no_noninteractive_tabindex -->
<div
  class="flex-1 overflow-auto p-4 outline-none"
  tabindex="0"
  bind:this={resultsContainer}
  onkeydown={handleResultsKeydown}
>
  {#if $viewMode === 'grid'}
    <div class="grid gap-4" style={gridStyle}>
      {#each groupedResults() as group (group.title)}
        {#if isDuplicateGroup(group)}
          <section class="mb-2" style="grid-column: 1 / -1;">
            <button
              type="button"
              class="flex w-full items-center gap-2 mb-2 mt-4 first:mt-0 cursor-pointer select-none text-left"
              onclick={() => toggleGroup(group.title)}
            >
              <span class="text-[10px] text-[var(--text-secondary)] transition-transform {collapsedGroups.has(group.title) ? '' : 'rotate-90'}">&triangleright;</span>
              <span class="text-xs font-semibold text-[var(--text-secondary)]">{group.title}</span>
              <span class="text-[10px] px-1.5 py-0.5 rounded-full bg-[var(--bg-tertiary)] text-[var(--text-secondary)]">
                {siblingCounts().get(group.title)} releases
              </span>
            </button>
            {#if !collapsedGroups.has(group.title)}
              <div class="grid gap-4" style={gridStyle} transition:slide={{ duration: 150 }}>
                {#each group.items as item, idx (item.url || item.group_key + '-' + idx)}
                  <div oncontextmenu={(e) => handleContextMenu(e, item)}>
                    <ResultTile {item} focused={flatIndexMap().get(item) === $focusedIndex} onmore={() => (mobileActionItem = item)} />
                  </div>
                {/each}
              </div>
            {/if}
          </section>
        {:else}
          {#each group.items as item, idx (item.url || item.group_key + '-' + idx)}
            <div oncontextmenu={(e) => handleContextMenu(e, item)}>
              <ResultTile {item} focused={flatIndexMap().get(item) === $focusedIndex} onmore={() => (mobileActionItem = item)} />
            </div>
          {/each}
        {/if}
      {/each}
    </div>
  {:else}
    {@const cols = $visibleColumns}
    <div class="overflow-x-auto">
    <table class="w-full text-left">
      <thead class="text-xs text-[var(--text-secondary)] sticky top-0 z-10 [&_th]:bg-[var(--bg-primary)] [&_th]:border-b [&_th]:border-[var(--border)]">
        <tr>
          <th class="p-2 w-8"></th>
          <th class="p-2 w-10 hidden sm:table-cell"></th>
          <th class="p-2 max-w-[640px]">Title</th>
          {#if cols.rating}<th class="p-2">Rating</th>{/if}
          {#if cols.res}<th class="p-2 hidden md:table-cell">Res</th>{/if}
          {#if cols.size}<th class="p-2 hidden lg:table-cell">Size</th>{/if}
          {#if cols.status}<th class="p-2">Status</th>{/if}
          <th class="p-2">Actions</th>
        </tr>
      </thead>
      <tbody>
        {#each groupedResults() as group (group.title)}
          {#if isDuplicateGroup(group)}
            <tr
              class="cursor-pointer select-none hover:bg-[var(--bg-tertiary)]"
              onclick={() => toggleGroup(group.title)}
              onkeydown={(e) => (e.key === 'Enter' || e.key === ' ') && (e.preventDefault(), toggleGroup(group.title))}
              tabindex="0"
              role="button"
            >
              <td colspan="99" class="px-2 py-1.5">
                <div class="flex items-center gap-2">
                  <span class="text-[10px] text-[var(--text-secondary)] transition-transform {collapsedGroups.has(group.title) ? '' : 'rotate-90'}">&triangleright;</span>
                  <span class="text-xs font-semibold text-[var(--text-secondary)]">{group.title}</span>
                  <span class="text-[10px] px-1.5 py-0.5 rounded-full bg-[var(--bg-tertiary)] text-[var(--text-secondary)]">
                    {siblingCounts().get(group.title)} releases
                  </span>
                </div>
              </td>
            </tr>
          {/if}
          {#if !collapsedGroups.has(group.title)}
            {#each group.items as item, idx (item.url || item.group_key + '-' + idx)}
              <ResultRow {item} focused={flatIndexMap().get(item) === $focusedIndex} zebra={((flatIndexMap().get(item) ?? 0) % 2) === 1} oncontextmenu={(e) => handleContextMenu(e, item)} />
            {/each}
          {/if}
        {/each}
      </tbody>
    </table>
    </div>
  {/if}

  {#if $filteredResults.length === 0 && $scanState === 'idle'}
    <div class="flex flex-col items-center justify-center min-h-[16rem] py-8 gap-4">
      {#if $results.length > 0}
        <!-- Had results but filter hides them all -->
        <svg class="w-12 h-12 text-[var(--text-secondary)] opacity-30" viewBox="0 0 48 48" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
          <circle cx="20" cy="20" r="14"/>
          <line x1="30" y1="30" x2="42" y2="42"/>
        </svg>
        <p class="text-sm text-[var(--text-secondary)]">No results match your filter</p>
        <p class="text-xs text-[var(--text-secondary)] opacity-60">Try adjusting the status filter or search text.</p>
      {:else}
        <!-- Pre-flight checklist -->
        <div class="flex flex-col items-center gap-5 max-w-sm">
          <svg class="w-10 h-10 text-[var(--accent)] opacity-40" viewBox="0 0 48 48" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
            <circle cx="24" cy="24" r="18" />
            <circle cx="24" cy="24" r="10" />
            <circle cx="24" cy="24" r="3" />
            <line x1="24" y1="6" x2="24" y2="2" />
            <line x1="24" y1="46" x2="24" y2="42" />
            <line x1="6" y1="24" x2="2" y2="24" />
            <line x1="46" y1="24" x2="42" y2="24" />
          </svg>

          <div class="w-full space-y-2">
            <!-- Plex -->
            <div class="flex items-center justify-between px-3 py-2 rounded-lg bg-[var(--bg-secondary)] border border-[var(--border)]">
              <div class="flex items-center gap-2">
                <div class="w-2 h-2 rounded-full {$plexConnected ? 'bg-[var(--success)]' : plexChecking ? 'bg-[var(--warning)] animate-pulse' : 'bg-[var(--error)]'}"></div>
                <span class="text-xs text-[var(--text-primary)]">Plex</span>
              </div>
              <span class="text-[10px] text-[var(--text-secondary)]">
                {#if $plexConnected}
                  {$plexMovieCount} movies, {$plexTvCount} TV
                {:else if plexChecking}
                  Connecting...
                {:else}
                  Not connected
                {/if}
              </span>
            </div>

            <!-- Metadata APIs -->
            <div class="flex items-center justify-between px-3 py-2 rounded-lg bg-[var(--bg-secondary)] border border-[var(--border)]">
              <div class="flex items-center gap-2">
                <div class="w-2 h-2 rounded-full {!$settingsLoaded ? 'bg-[var(--text-secondary)] opacity-40 animate-pulse' : metadataConfigured ? 'bg-[var(--success)]' : 'bg-[var(--warning)]'}"></div>
                <span class="text-xs text-[var(--text-primary)]">Metadata</span>
              </div>
              <div class="flex items-center gap-1.5">
                {#if !$settingsLoaded}
                  <span class="text-[10px] text-[var(--text-secondary)] opacity-40">checking…</span>
                {:else}
                  <span class="text-[10px] {tmdbConfigured ? 'text-[var(--text-secondary)]' : 'text-[var(--text-secondary)] opacity-30'}">TMDB {tmdbConfigured ? '✓' : '✗'}</span>
                  <span class="text-[10px] {omdbConfigured ? 'text-[var(--text-secondary)]' : 'text-[var(--text-secondary)] opacity-30'}">OMDb {omdbConfigured ? '✓' : '✗'}</span>
                {/if}
              </div>
            </div>

            <!-- Sources -->
            <div class="flex items-center justify-between px-3 py-2 rounded-lg bg-[var(--bg-secondary)] border border-[var(--border)]">
              <div class="flex items-center gap-2">
                <div class="w-2 h-2 rounded-full {enabledSourceCount > 0 ? 'bg-[var(--success)]' : 'bg-[var(--error)]'}"></div>
                <span class="text-xs text-[var(--text-primary)]">Sources</span>
              </div>
              <div class="flex items-center gap-1.5">
                {#each sources as src}
                  <span class="text-[10px] {src.enabled ? 'text-[var(--text-secondary)]' : 'text-[var(--text-secondary)] opacity-30 line-through'}">{src.name}</span>
                {/each}
              </div>
            </div>

            <!-- JDownloader -->
            <div class="flex items-center justify-between px-3 py-2 rounded-lg bg-[var(--bg-secondary)] border border-[var(--border)]">
              <div class="flex items-center gap-2">
                <div class="w-2 h-2 rounded-full {!jdEnabled ? 'bg-[var(--text-secondary)] opacity-40' : $jdConnection.checking ? 'bg-[var(--warning)] animate-pulse' : $jdConnection.connected ? 'bg-[var(--success)]' : 'bg-[var(--error)]'}"></div>
                <span class="text-xs text-[var(--text-primary)]">JDownloader</span>
              </div>
              <span class="text-[10px] text-[var(--text-secondary)]" title={$jdConnection.error ?? ''}>
                {#if !jdEnabled}
                  Disabled
                {:else if $jdConnection.checking}
                  Checking…
                {:else if $jdConnection.connected}
                  {$jdConnection.device || 'Connected'}
                {:else}
                  Not connected
                {/if}
              </span>
            </div>

            <!-- Last scan -->
            <div class="flex items-center justify-between px-3 py-2 rounded-lg bg-[var(--bg-secondary)] border border-[var(--border)]">
              <div class="flex items-center gap-2">
                <div class="w-2 h-2 rounded-full bg-[var(--text-secondary)] opacity-40"></div>
                <span class="text-xs text-[var(--text-primary)]">Last scan</span>
              </div>
              <span class="text-[10px] text-[var(--text-secondary)]">{lastScanFormatted()}</span>
            </div>
          </div>

          <!-- Summary -->
          <p class="text-xs text-[var(--text-secondary)]">
            {#if issueCount === 0}
              All systems ready — press Start Scan above
            {:else}
              {issueCount} {issueCount === 1 ? 'issue' : 'issues'} — scan may still work, check <a href="/settings" class="text-[var(--accent)] hover:underline">Settings</a>
            {/if}
          </p>
          <p class="text-[10px] text-[var(--text-secondary)] opacity-50">
            Hit <kbd class="px-1 py-0.5 rounded bg-[var(--bg-tertiary)] font-mono">?</kbd> for keyboard shortcuts
          </p>
        </div>

        <!-- Trending movies discovery -->
        {#if trendingMovies.length > 0}
          <div class="mt-6 w-full max-w-3xl">
            <h3 class="text-xs font-semibold text-[var(--text-secondary)] uppercase tracking-wider mb-3 px-1">Trending This Week</h3>
            <div class="flex gap-3 overflow-x-auto pb-2 scrollbar-thin">
              {#each trendingMovies as movie}
                <a
                  href="https://www.themoviedb.org/movie/{movie.id}"
                  target="_blank"
                  rel="noopener"
                  class="shrink-0 w-28 group"
                  title="{movie.title} ({movie.year})"
                >
                  {#if movie.poster_url}
                    <img
                      src={movie.poster_url}
                      alt={movie.title}
                      class="w-28 h-[168px] object-cover rounded-lg border border-[var(--border)] group-hover:border-[var(--accent)] transition-colors"
                      loading="lazy"
                    />
                  {:else}
                    <div class="w-28 h-[168px] bg-[var(--bg-tertiary)] rounded-lg flex items-center justify-center text-[var(--text-secondary)] text-xs">No poster</div>
                  {/if}
                  <p class="text-[10px] font-medium text-[var(--text-primary)] truncate mt-1">{movie.title}</p>
                  <p class="text-[10px] text-[var(--text-secondary)] opacity-60">{movie.year ?? ''} {movie.rating ? `· ${movie.rating.toFixed(1)}` : ''}</p>
                </a>
              {/each}
            </div>
          </div>
        {/if}
      {/if}
    </div>
  {/if}

  {#if totalPages > 1}
    <div class="flex items-center justify-center gap-2 py-4">
      <button
        disabled={currentPage <= 1}
        onclick={() => currentPage--}
        class="px-3 py-1.5 rounded text-xs bg-[var(--bg-tertiary)] hover:bg-[var(--border)] disabled:opacity-30 transition-colors"
      >
        Prev
      </button>
      <span class="text-xs text-[var(--text-secondary)]">
        Page {currentPage} of {totalPages} ({$filteredResults.length} results)
      </span>
      <button
        disabled={currentPage >= totalPages}
        onclick={() => currentPage++}
        class="px-3 py-1.5 rounded text-xs bg-[var(--bg-tertiary)] hover:bg-[var(--border)] disabled:opacity-30 transition-colors"
      >
        Next
      </button>
    </div>
  {/if}
</div>
{/if}

<StatusBar />

{#if contextMenu}
  <ContextMenu
    item={contextMenu.item}
    x={contextMenu.x}
    y={contextMenu.y}
    onclose={() => (contextMenu = null)}
  />
{/if}

<ResultActionSheet item={mobileActionItem} onclose={() => (mobileActionItem = null)} />

{#if $selectedDetail}
  <DetailPanel item={$selectedDetail} onclose={() => selectedDetail.set(null)} />
{/if}
