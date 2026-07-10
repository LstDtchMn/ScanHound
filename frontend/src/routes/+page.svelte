<script lang="ts">
  import ScanControls from '$lib/components/ScanControls.svelte';
  import FilterBar from '$lib/components/FilterBar.svelte';
  import StatusBar from '$lib/components/StatusBar.svelte';
  import ResultTile from '$lib/components/ResultTile.svelte';
  import GroupTile from '$lib/components/GroupTile.svelte';
  import ResultRow from '$lib/components/ResultRow.svelte';
  import ContextMenu from '$lib/components/ContextMenu.svelte';
  import ResultActionSheet from '$lib/components/ResultActionSheet.svelte';
  import DetailPanel from '$lib/components/DetailPanel.svelte';
  import SwipeDeck from '$lib/components/SwipeDeck.svelte';
  import { filteredResults, viewMode, viewModeExplicit, results, stats, selectedDetail, focusedIndex, toggleSelect, hydrateDismissed, fromCache, cacheUpdatedAt, tileSize, gridGap, gridColumns, TILE_MIN_PX, GRID_GAP_CLASS, loadResults, hasMore, loadingMore, loadError, filteredTotal, titleCounts, pagedMode, statusFilter, searchFilter, genreFilter, languageFilter, quickFilters, categoryFilter, sortBy, hiddenByFiltersCount, clearAllFilters } from '$lib/stores/results';
  import { mobile } from '$lib/stores/media';
  import { addToast } from '$lib/stores/notifications';
  import { get } from 'svelte/store';
  import { scanState, scanProgress, scanPhase } from '$lib/stores/scanner';
  import { settings, settingsLoaded, loadSettings } from '$lib/stores/settings';
  import { plexConnected, plexMovieCount, plexTvCount, refreshPlexStatus } from '$lib/stores/plex';
  import { batchProgress } from '$lib/stores/downloads';
  import Badge from '$lib/components/Badge.svelte';
  import { statusBorderColor, statusVariant, statusBarStyle, formatStatus, resolutionRank, sizeToGB } from '$lib/constants';
  import { jdConnection, refreshJdConnection } from '$lib/stores/jdownloader';
  import { api } from '$lib/api/client';
  import { onMount } from 'svelte';
  import { slide } from 'svelte/transition';
  import type { ScanResult } from '$lib/api/types';
  import {
    groupResults, computeSiblingCounts, isDuplicateGroup as isDupGroup,
    groupSizeRange, groupDateRange, groupStatusSummary, groupFormats,
    type ResultGroup, type GroupFormats
  } from '$lib/grouping';
  import { isPhone } from '$lib/stores/viewport';
  import MobileScanView from '$lib/components/mobile/MobileScanView.svelte';
  import DetailSheet from '$lib/components/mobile/DetailSheet.svelte';

  let tmdbKeyMissing = $state(false);
  let tmdbBannerDismissed = $state(
    typeof localStorage !== 'undefined' && localStorage.getItem('tmdb-banner-dismissed') === 'true'
  );
  // Tracks whether initial Plex status check is still pending
  let plexChecking = $state(true);

  /** Short relative time like "5m ago" from a UTC timestamp string. */
  function relTime(s: string | null): string {
    if (!s) return '';
    // SQLite CURRENT_TIMESTAMP is UTC without a zone marker — treat it as UTC.
    const iso = s.includes('T') ? s : s.replace(' ', 'T') + 'Z';
    const t = Date.parse(iso);
    if (Number.isNaN(t)) return '';
    const secs = Math.max(0, Math.round((Date.now() - t) / 1000));
    if (secs < 60) return 'just now';
    const mins = Math.round(secs / 60);
    if (mins < 60) return `${mins}m ago`;
    const hrs = Math.round(mins / 60);
    if (hrs < 24) return `${hrs}h ago`;
    return `${Math.round(hrs / 24)}d ago`;
  }

  async function scanNow() {
    try {
      await api.triggerBackgroundScan();
      addToast('Background scan', 'Started — cached results will refresh in the background');
    } catch {
      addToast('Background scan', 'Could not start a scan', 'error');
    }
  }

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
  // Track which multi-item groups the user has explicitly expanded; all others start collapsed
  let expandedGroups = $state<Set<string>>(new Set());
  let resultsContainer: HTMLDivElement | undefined = $state();

  // Column count precedence: an explicit per-device choice wins; otherwise the
  // server-wide tile_columns setting; otherwise responsive auto-fill by tile size.
  let effectiveColumns = $derived(
    $gridColumns !== 'auto' ? $gridColumns : (($settings.tile_columns as number) || 0)
  );
  let gridStyle = $derived(
    effectiveColumns > 0
      ? `grid-template-columns: repeat(${effectiveColumns}, 1fr)`
      : `grid-template-columns: repeat(auto-fill, minmax(${TILE_MIN_PX[$tileSize]}px, 1fr))`
  );
  let gridGapClass = $derived(GRID_GAP_CLASS[$gridGap]);

  let renderLimit = $state(100);
  let scrollSentinel: HTMLDivElement | undefined = $state();
  let renderedResults = $derived($filteredResults.slice(0, renderLimit));

  // Group results by title for group headers (pure logic in $lib/grouping —
  // shared with the phone MobileScanView; these are thin store-reading wrappers).
  let groupedResults = $derived(() => groupResults(renderedResults));

  // Sibling counts across ALL filtered results — server counts in paged mode
  // (covers rows not yet loaded into the render window), local tally in live mode.
  let siblingCounts = $derived(() => computeSiblingCounts($filteredResults, $titleCounts, $pagedMode));

  function isDuplicateGroup(group: ResultGroup) {
    return isDupGroup(group, siblingCounts());
  }

  function toggleGroup(title: string) {
    expandedGroups = new Set(expandedGroups);
    if (expandedGroups.has(title)) expandedGroups.delete(title);
    else expandedGroups.add(title);
  }

  function isGroupExpanded(group: ResultGroup): boolean {
    return !isDuplicateGroup(group) || expandedGroups.has(group.title);
  }

  /** Left bar for a collapsed group row. One vertical segment per distinct status
   *  color present (e.g. top-half yellow upgrade, bottom-half green in-library),
   *  so a mixed group reads at a glance. Uses the same shared statusBarStyle as
   *  individual rows so every bar lines up into one continuous vertical strip. */
  function groupBarStyle(items: ScanResult[]): string {
    const seen = new Set<string>();
    const colors: string[] = [];
    for (const { status } of groupStatusSummary(items)) {
      const c = statusBorderColor(status);
      if (!seen.has(c)) { seen.add(c); colors.push(c); }
    }
    return statusBarStyle(colors);
  }

  /** The best release in a group the user already has (downloaded or in Plex),
   *  used to show "upgrade vs what you own" on the still-missing siblings. */
  function groupOwnedSpec(items: ScanResult[]) {
    const owned = items.filter(i => {
      const s = (i.status ?? '').toLowerCase();
      return s.includes('download') || s.includes('library');
    });
    if (owned.length === 0) return null;
    // Shared ranking/size helpers (constants) keep this in lockstep with the
    // per-row upgrade comparison in ResultRow.
    const best = [...owned].sort(
      (a, b) => resolutionRank(b.resolution) - resolutionRank(a.resolution)
        || sizeToGB(b.size) - sizeToGB(a.size))[0];
    return { resolution: best.resolution, size: best.size, hdr: best.hdr, dovi: best.dovi };
  }

  // Reset the render window and keyboard focus whenever the active filter set changes.
  // (The store handles refetching in paged mode — this only resets the client window.)
  $effect(() => {
    $statusFilter; $searchFilter; $genreFilter; $languageFilter; $quickFilters; $categoryFilter; $sortBy;
    renderLimit = 100;
    focusedIndex.set(-1);
    resultsContainer?.scrollTo({ top: 0 });
  });

  // Grow the render window as the bottom sentinel comes into view; in paged mode,
  // also fetch the next server page once the window nears the end of loaded rows.
  $effect(() => {
    if (!scrollSentinel) return;
    const io = new IntersectionObserver((entries) => {
      if (!entries[0].isIntersecting) return;
      renderLimit += 100;
      if ($pagedMode && $hasMore && !$loadingMore && renderLimit >= $filteredResults.length - 100) {
        loadResults(false);
      }
    }, { rootMargin: '600px' });
    io.observe(scrollSentinel);
    return () => io.disconnect();
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
            // A live last-scan set is already fully in memory — use client filtering.
            pagedMode.set(false);
          }
        } catch { /* no previous results */ }
      })(),
    ]);
    // If no live results were available (fresh session / server restart), fall
    // back to server-paginated cached results so the app opens populated.
    if (get(results).length === 0) {
      pagedMode.set(true);
      await loadResults(true);
    }
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
      if (isGroupExpanded(group)) {
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
      if (isGroupExpanded(group)) {
        for (const item of group.items) {
          map.set(item, i++);
        }
      }
    }
    return map;
  });

  /** Scroll the currently-focused row/tile into view (block: 'nearest' so it
   *  only scrolls when the element isn't already fully visible). Runs after
   *  a tick so it picks up a renderLimit-driven DOM update when one just
   *  happened (see handleResultsKeydown's ArrowDown/Up growth below). */
  function scrollFocusedIntoView() {
    requestAnimationFrame(() => {
      resultsContainer?.querySelector('[data-focused="true"]')?.scrollIntoView({ block: 'nearest' });
    });
  }

  function handleResultsKeydown(e: KeyboardEvent) {
    const tag = (e.target as HTMLElement)?.tagName;
    if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT') return;

    let items = flatVisibleItems();
    if (items.length === 0) return;

    switch (e.key) {
      case 'ArrowDown': {
        e.preventDefault();
        // Keyboard nav used to wall at the ~100-item render window (only the
        // scroll IntersectionObserver grew it), stranding keyboard-only users
        // who reached the last rendered row. Grow the window first so moving
        // past the current edge is always possible while more filtered
        // results exist (paged mode also tops up from the server, mirroring
        // the scroll-sentinel observer).
        if ($focusedIndex >= items.length - 1 && renderLimit < $filteredResults.length) {
          renderLimit += 100;
          items = flatVisibleItems();
          if ($pagedMode && $hasMore && !$loadingMore && renderLimit >= $filteredResults.length - 100) {
            loadResults(false);
          }
        }
        focusedIndex.update(i => Math.min(i + 1, items.length - 1));
        scrollFocusedIntoView();
        break;
      }
      case 'ArrowUp': {
        e.preventDefault();
        focusedIndex.update(i => Math.max(i - 1, 0));
        scrollFocusedIntoView();
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

{#if $isPhone}
  <MobileScanView />
{:else}
<FilterBar />

{#if $fromCache}
  <div class="px-4 py-1.5 flex items-center gap-2 text-xs text-[var(--text-secondary)] border-b border-[var(--border)] bg-[color-mix(in_srgb,var(--accent)_4%,var(--bg-primary))]">
    <svg class="w-3.5 h-3.5 flex-shrink-0 opacity-70" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2"><path stroke-linecap="round" stroke-linejoin="round" d="M12 8v4l3 3m6-3a9 9 0 11-18 0 9 9 0 0118 0z" /></svg>
    <span>Showing cached results{#if $cacheUpdatedAt} · updated {relTime($cacheUpdatedAt)}{/if}</span>
    <button onclick={scanNow} class="ml-auto text-[var(--accent)] hover:underline font-medium">Scan now</button>
  </div>
{/if}

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
    <div class="grid {gridGapClass}" style={gridStyle}>
      {#each groupedResults() as group (group.title)}
        {#if isDuplicateGroup(group) && !isGroupExpanded(group)}
          <!-- Collapsed duplicate group: a normal grid cell (stacked-poster card),
               not a full-width row — keeps the poster wall's rhythm intact. -->
          <div class="min-w-0">
            <GroupTile
              title={group.title}
              items={group.items}
              count={siblingCounts().get(group.title) ?? group.items.length}
              formats={groupFormats(group.items)}
              statusSummary={groupStatusSummary(group.items)}
              sizeRange={groupSizeRange(group.items)}
              dateRange={groupDateRange(group.items)}
              onToggle={() => toggleGroup(group.title)}
            />
          </div>
        {:else if isDuplicateGroup(group)}
          <!-- Expanded: slim full-width header strip (click/Enter/Space to collapse) + the run of tiles -->
          <section class="mb-2" style="grid-column: 1 / -1;">
            <button
              type="button"
              class="flex w-full items-center gap-2 mb-2 mt-4 first:mt-0 cursor-pointer select-none text-left rounded-lg border-transparent bg-transparent py-0 transition-colors"
              aria-expanded="true"
              aria-label="{group.title} — {siblingCounts().get(group.title)} releases, collapse"
              onclick={() => toggleGroup(group.title)}
            >
              <span class="text-[10px] text-[var(--text-secondary)] transition-transform rotate-90">&triangleright;</span>
              <span class="text-xs font-semibold text-[var(--text-secondary)]">{group.title}</span>
              <Badge label="{siblingCounts().get(group.title)} releases" />
            </button>
            <div class="grid {gridGapClass}" style={gridStyle} transition:slide={{ duration: 150 }}>
              {#each group.items as item, idx (item.url || item.group_key + '-' + idx)}
                <div class="min-w-0" oncontextmenu={(e) => handleContextMenu(e, item)}>
                  <ResultTile {item} focused={flatIndexMap().get(item) === $focusedIndex} onmore={() => (mobileActionItem = item)} />
                </div>
              {/each}
            </div>
          </section>
        {:else}
          {#each group.items as item, idx (item.url || item.group_key + '-' + idx)}
            <div class="min-w-0" oncontextmenu={(e) => handleContextMenu(e, item)}>
              <ResultTile {item} focused={flatIndexMap().get(item) === $focusedIndex} onmore={() => (mobileActionItem = item)} />
            </div>
          {/each}
        {/if}
      {/each}
    </div>
  {:else}
    <div class="overflow-x-auto">
    <table class="w-full text-left">
      <thead class="text-xs text-[var(--text-secondary)] sticky top-0 z-10 [&_th]:bg-[var(--bg-primary)] [&_th]:border-b [&_th]:border-[var(--border)]">
        <tr>
          <th class="p-2 w-8"></th>
          <th class="p-2 w-10 hidden sm:table-cell"></th>
          <th class="p-2">Title</th>
          <th class="p-2 w-px whitespace-nowrap text-right">Actions</th>
        </tr>
      </thead>
      <tbody>
        {#each groupedResults() as group (group.title)}
          {#if isDuplicateGroup(group)}
            {#if !isGroupExpanded(group)}
              {@const fmtsL = groupFormats(group.items)}
              {@const statusSummary = groupStatusSummary(group.items)}
              <!-- Collapsed: looks like a real result row with range metadata -->
              <tr
                class="cursor-pointer select-none border-b border-[var(--border)] hover:bg-[var(--bg-tertiary)] transition-colors"
                style="{groupBarStyle(group.items)}"
                onclick={() => toggleGroup(group.title)}
                onkeydown={(e) => (e.key === 'Enter' || e.key === ' ') && (e.preventDefault(), toggleGroup(group.title))}
                tabindex="0"
                role="button"
              >
                <td class="p-2 w-8 text-center">
                  <span class="text-[10px] text-[var(--text-secondary)]">&#9654;</span>
                </td>
                <td class="p-2 hidden sm:table-cell" style="width:72px; min-width:72px;">
                  {#if group.items[0].poster_url}
                    <img src={group.items[0].poster_url} alt="" class="object-cover rounded shadow-sm"
                      style="width:64px;height:96px;" loading="lazy" />
                  {:else}
                    <div class="bg-[var(--bg-tertiary)] rounded" style="width:64px;height:96px;"></div>
                  {/if}
                </td>
                <td class="p-2 w-full overflow-hidden">
                  <div class="flex items-center gap-2 flex-wrap">
                    <span class="text-sm font-semibold truncate">{group.title}</span>
                    {#if group.items[0].year}<span class="text-[var(--text-secondary)] text-sm font-normal">({group.items[0].year})</span>{/if}
                    <Badge label="{siblingCounts().get(group.title)} releases" />
                    {#each statusSummary as st}
                      <Badge label={`${st.count} ${formatStatus(st.status)}`} variant={statusVariant(st.status)} />
                    {/each}
                  </div>
                  <!-- Group decision stat line: rating · RT · resolutions · size range -->
                  <div class="flex items-center flex-wrap gap-x-2.5 gap-y-1 mt-1 text-[12px]">
                    {#if group.items[0].rating != null}
                      <span class="inline-flex items-center gap-1 whitespace-nowrap">
                        <span aria-hidden="true">⭐</span>
                        <span class="font-medium text-[var(--text-primary)]">{group.items[0].rating.toFixed(1)}</span>
                      </span>
                    {/if}
                    {#if group.items[0].rt_score != null}
                      <span class="inline-flex items-center gap-1 whitespace-nowrap text-[var(--text-secondary)]">
                        <span aria-hidden="true" title={group.items[0].rt_score >= 60 ? 'Fresh' : 'Rotten'}>{group.items[0].rt_score >= 60 ? '🍅' : '🤢'}</span>
                        <span>{group.items[0].rt_score}%</span>
                      </span>
                    {/if}
                    {#if fmtsL.res.length || fmtsL.dv || fmtsL.hdr}
                      <span class="inline-flex items-center gap-1 flex-wrap">
                        {#each fmtsL.res as r}<Badge label={r} size="xs" />{/each}
                        {#if fmtsL.dv}<Badge label="DV" variant="accent" size="xs" />{/if}
                        {#if fmtsL.hdr}<Badge label="HDR" variant="warning" size="xs" />{/if}
                      </span>
                    {/if}
                    {#if groupSizeRange(group.items)}<span class="text-[var(--text-secondary)] whitespace-nowrap">{groupSizeRange(group.items)}</span>{/if}
                  </div>
                  {#if groupDateRange(group.items)}
                    <div class="text-[11px] text-[var(--text-secondary)] mt-0.5 opacity-60">{groupDateRange(group.items)}</div>
                  {/if}
                  {#if group.items[0].description}
                    <p class="text-[11px] text-[var(--text-secondary)] mt-1 leading-relaxed line-clamp-2 opacity-80">{group.items[0].description}</p>
                  {/if}
                </td>
                <td class="p-2"></td>
              </tr>
            {:else}
              <!-- Expanded header row -->
              <tr
                class="cursor-pointer select-none hover:bg-[var(--bg-tertiary)]"
                onclick={() => toggleGroup(group.title)}
                onkeydown={(e) => (e.key === 'Enter' || e.key === ' ') && (e.preventDefault(), toggleGroup(group.title))}
                tabindex="0"
                role="button"
              >
                <td colspan="99" class="px-2 py-1.5">
                  <div class="flex items-center gap-2">
                    <span class="text-[10px] text-[var(--text-secondary)] transition-transform rotate-90">&triangleright;</span>
                    <span class="text-xs font-semibold text-[var(--text-secondary)]">{group.title}</span>
                    <Badge label="{siblingCounts().get(group.title)} releases" />
                  </div>
                </td>
              </tr>
            {/if}
          {/if}
          {#if isGroupExpanded(group)}
            {@const ownedSpec = isDuplicateGroup(group) ? groupOwnedSpec(group.items) : null}
            {#each group.items as item, idx (item.url || item.group_key + '-' + idx)}
              <ResultRow {item} nested={isDuplicateGroup(group)} owned={ownedSpec} focused={flatIndexMap().get(item) === $focusedIndex} zebra={((flatIndexMap().get(item) ?? 0) % 2) === 1} oncontextmenu={(e) => handleContextMenu(e, item)} />
            {/each}
            {#if isDuplicateGroup(group)}
              <!-- Closes off an expanded version group so its end is unmistakable -->
              <tr aria-hidden="true" class="pointer-events-none">
                <td colspan="99" class="p-0">
                  <div class="h-3 border-b-2 border-[var(--border)]"></div>
                </td>
              </tr>
            {/if}
          {/if}
        {/each}
      </tbody>
    </table>
    </div>
  {/if}

  {#if $filteredTotal === 0 && $filteredResults.length === 0 && $scanState === 'idle'}
    <div class="flex flex-col items-center justify-center min-h-[16rem] py-8 gap-4">
      {#if $results.length > 0}
        <!-- Had results but filter hides them all -->
        <svg class="w-12 h-12 text-[var(--text-secondary)] opacity-30" viewBox="0 0 48 48" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
          <circle cx="20" cy="20" r="14"/>
          <line x1="30" y1="30" x2="42" y2="42"/>
        </svg>
        {#if $hiddenByFiltersCount > 0}
          <!-- Self-diagnosis: the active tab genuinely has matches — filters are
               what's hiding them, not an empty library. See resolutionFilter's
               history (frontend/src/lib/stores/results.ts) for why this matters:
               a filter that quietly narrows content and outlives the session
               (or is just easy to forget about) can silently zero out a tab. -->
          <p class="text-sm text-[var(--text-secondary)]">0 shown &mdash; {$hiddenByFiltersCount} hidden by filters</p>
          <p class="text-xs text-[var(--text-secondary)] opacity-60">Your resolution, genre, language, date, or search filter is hiding every matching item.</p>
          <button
            onclick={() => clearAllFilters()}
            class="px-3 py-1.5 rounded-lg text-xs font-medium text-white bg-[var(--accent)] hover:opacity-90 transition-opacity"
          >Clear filters</button>
        {:else}
          <p class="text-sm text-[var(--text-secondary)]">No results match your filter</p>
          <p class="text-xs text-[var(--text-secondary)] opacity-60">Try adjusting the status filter or search text.</p>
        {/if}
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

  <div bind:this={scrollSentinel} class="h-px"></div>
  {#if $loadingMore}
    <div class="py-4 text-center text-sm text-[var(--text-secondary)]">Loading more…</div>
  {:else if $loadError}
    <div class="py-4 text-center text-sm">
      <button class="underline text-[var(--accent)]" onclick={() => loadResults(false)}>Retry loading more</button>
    </div>
  {/if}
  {#if $filteredTotal > 0}
    <div class="py-3 text-center text-xs text-[var(--text-secondary)] opacity-70">
      showing {Math.min(renderedResults.length, $filteredTotal)} of {$filteredTotal}
    </div>
  {/if}
</div>
{/if}
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

{#if !$isPhone}
  <!-- Desktop-branch action sheet (mobileActionItem is only settable from the
       desktop markup); on phones MobileScanView mounts its own instance. -->
  <ResultActionSheet item={mobileActionItem} onclose={() => (mobileActionItem = null)} />
{/if}

{#if $selectedDetail}
  {#if $isPhone}
    <DetailSheet
      item={$selectedDetail}
      siblings={$results.filter((r) => r.group_key === $selectedDetail!.group_key)}
      onclose={() => selectedDetail.set(null)}
      onselect={(s) => selectedDetail.set(s)}
    />
  {:else}
    <DetailPanel item={$selectedDetail} onclose={() => selectedDetail.set(null)} />
  {/if}
{/if}
