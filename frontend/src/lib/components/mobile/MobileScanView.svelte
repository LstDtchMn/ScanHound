<script lang="ts">
  import { get } from 'svelte/store';
  import {
    filteredResults, filteredTotal, titleCounts, pagedMode, hasMore, loadingMore,
    loadResults, handleReconnectSnapshot, phoneColumns, mobileChromeCollapsed,
    dismissItem, restoreItem
  } from '$lib/stores/results';
  import { onMount } from 'svelte';
  import { downloadHost } from '$lib/stores/downloads';
  import { api } from '$lib/api/client';
  import { addToast } from '$lib/stores/notifications';
  import type { ScanResult } from '$lib/api/types';
  import { groupResults, computeSiblingCounts, isDuplicateGroup, groupFormats, groupStatusSummary, groupSizeRange, groupDateRange } from '$lib/grouping';
  import ResultTile from '../ResultTile.svelte';
  import GroupTile from '../GroupTile.svelte';
  import FilterBar from '../FilterBar.svelte';
  import ResultActionSheet from '../ResultActionSheet.svelte';
  import SwipeDeck from '../SwipeDeck.svelte';
  import PullToRefresh from './PullToRefresh.svelte';
  import SwipeableTile from './SwipeableTile.svelte';
  import MobileToolbar from './MobileToolbar.svelte';
  import { success } from './haptics';

  let filterSheetOpen = $state(false);
  let deckOpen = $state(false);       // LOCAL deck overlay — deliberately NOT viewMode
                                       // ('sh-view-mode' is persisted and shared with desktop;
                                       // entering the deck on the phone must not flip it).
  let actionItem = $state<ScanResult | null>(null);
  let expandedGroups = $state(new Set<string>());
  let renderLimit = $state(60);
  let sentinel: HTMLDivElement | undefined = $state();

  let renderedResults = $derived($filteredResults.slice(0, renderLimit));
  let groups = $derived(groupResults(renderedResults));
  // 1-up = single large poster per row (landscape shows 2); 2-up = the wall.
  let gridClass = $derived($phoneColumns === 1 ? 'grid-cols-1 landscape:grid-cols-2' : 'grid-cols-2 landscape:grid-cols-3');
  let siblingCounts = $derived(computeSiblingCounts($filteredResults, $titleCounts, $pagedMode));

  function toggleGroup(title: string) {
    const next = new Set(expandedGroups);
    next.has(title) ? next.delete(title) : next.add(title);
    expandedGroups = next;
  }

  // Infinite scroll: grow the render window; top up server pages.
  $effect(() => {
    if (!sentinel) return;
    const obs = new IntersectionObserver((entries) => {
      if (!entries[0].isIntersecting) return;
      if (renderLimit < $filteredResults.length) renderLimit += 60;
      else if ($pagedMode && $hasMore && !$loadingMore) loadResults(false);
    }, { rootMargin: '600px' }); // pre-fetch before the user hits the end (desktop parity)
    obs.observe(sentinel);
    return () => obs.disconnect();
  });

  async function refresh() {
    if (get(pagedMode)) await loadResults(true);
    else await handleReconnectSnapshot();
    renderLimit = 60;
  }

  // Auto-hide the scan bar as you scroll down into the wall, reveal on scroll
  // up — like a mobile browser's address bar. Small dead zones (near the top,
  // and a 6px wiggle-room per direction) avoid flicker on tiny scroll jitter.
  // ScanControls itself ignores this while a scan is running, so progress
  // always stays pinned regardless of scroll.
  let lastScrollTop = 0;
  function onWallScroll(scrollTop: number) {
    const delta = scrollTop - lastScrollTop;
    if (scrollTop < 24) mobileChromeCollapsed.set(false);
    else if (delta > 6) mobileChromeCollapsed.set(true);
    else if (delta < -6) mobileChromeCollapsed.set(false);
    lastScrollTop = scrollTop;
  }

  onMount(() => {
    mobileChromeCollapsed.set(false); // start visible
    return () => mobileChromeCollapsed.set(false); // never leave it hidden after navigating away
  });

  function grab(item: ScanResult) {
    if (!item.url) return;
    api.download(item.url, item.title, get(downloadHost), item.year,
                 item.resolution || '', item.size || '', item.hdr || '', item.dovi ?? false,
                 item.season)
      .then(() => {
        // The POST only returns "started"; the item is marked grabbed by the
        // download:complete (method=jdownloader) WS event once it actually
        // reaches JDownloader. A failed scrape/send leaves it Missing.
        success();
        addToast('Sending', item.title);
      })
      .catch(() => addToast('Error', `Grab failed: ${item.title}`, 'error'));
  }

  function dismissWithUndo(item: ScanResult) {
    dismissItem(item.url, item.title, {
      group_key: item.group_key,
      resolution: item.resolution,
      dovi: item.dovi
    });
    addToast('Dismissed', item.title, 'normal', { label: 'Undo', run: () => restoreItem(item.url, item) });
  }
</script>

<FilterBar bind:sheetOpen={filterSheetOpen} showMobileTrigger={false} />

<PullToRefresh onrefresh={refresh} onscroll={onWallScroll}>
  <div class="grid {gridClass} gap-2 p-2">
    {#each groups as group (group.title)}
      {#if isDuplicateGroup(group, siblingCounts) && !expandedGroups.has(group.title)}
        <GroupTile
          title={group.title}
          items={group.items}
          count={siblingCounts.get(group.title) ?? group.items.length}
          formats={groupFormats(group.items)}
          statusSummary={groupStatusSummary(group.items)}
          sizeRange={groupSizeRange(group.items)}
          dateRange={groupDateRange(group.items)}
          onToggle={() => toggleGroup(group.title)}
        />
      {:else}
        {#each group.items as item, idx (item.url || item.group_key + '-' + idx)}
          <SwipeableTile
            onswiperight={() => grab(item)}
            onswipeleft={() => dismissWithUndo(item)}
            onlongpress={() => (actionItem = item)}
          >
            <ResultTile {item} onmore={() => (actionItem = item)} />
          </SwipeableTile>
        {/each}
      {/if}
    {/each}
  </div>
  <div bind:this={sentinel} class="h-8"></div>
  {#if $loadingMore}
    <div class="flex justify-center py-3">
      <div class="w-5 h-5 border-2 border-[var(--accent)] border-t-transparent rounded-full animate-spin"></div>
    </div>
  {/if}
  {#if $filteredTotal === 0}
    <p class="text-center text-sm text-[var(--text-secondary)] py-10">No results — pull to refresh or adjust filters.</p>
  {/if}
</PullToRefresh>

<MobileToolbar onfilters={() => (filterSheetOpen = true)} ondeck={() => (deckOpen = true)} />

{#if deckOpen}
  <div class="fixed inset-0 z-40 bg-[var(--bg-primary)] flex flex-col md:hidden">
    <div class="flex items-center justify-between px-3 h-11 border-b border-[var(--border)]" style="padding-top: env(safe-area-inset-top);">
      <span class="text-sm font-semibold text-[var(--text-primary)]">Triage deck</span>
      <button class="p-2 text-[var(--text-secondary)]" aria-label="Close deck" onclick={() => (deckOpen = false)}>&times;</button>
    </div>
    <div class="flex-1 min-h-0"><SwipeDeck /></div>
  </div>
{/if}

<ResultActionSheet item={actionItem} onclose={() => (actionItem = null)} />
