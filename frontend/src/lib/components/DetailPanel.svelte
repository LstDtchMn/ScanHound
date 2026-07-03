<script lang="ts">
  import Badge from './Badge.svelte';
  import { api } from '$lib/api/client';
  import { addToast } from '$lib/stores/notifications';
  import { downloadQueue, activeDownload, downloadHost } from '$lib/stores/downloads';
  import { results, markDownloaded } from '$lib/stores/results';
  import { statusVariant, formatStatus, DOWNLOAD_HOSTS } from '$lib/constants';
  import type { ScanResult } from '$lib/api/types';
  import { fly } from 'svelte/transition';

  interface Props {
    item: ScanResult;
    onclose: () => void;
  }
  let { item, onclose }: Props = $props();

  let panelEl = $state<HTMLDivElement>();
  let triggerEl: Element | null = null;

  // Mount/unmount only — deliberately reads nothing reactive (not `item`,
  // not `panelEl` beyond the initial capture below) so switching the
  // detail shown (selectedDetail.set(anotherItem) while the panel stays
  // open, which happens without going through onclose — see ResultRow/
  // ResultTile/SwipeDeck) never re-runs this and never fires the restore-
  // focus cleanup early. It only actually fires when the panel unmounts,
  // i.e. a genuine close.
  $effect(() => {
    triggerEl = document.activeElement;
    if (panelEl) {
      requestAnimationFrame(() => panelEl?.focus());
    }
    return () => {
      if (triggerEl instanceof HTMLElement) triggerEl.focus();
    };
  });

  let siblings = $derived(
    $results.filter((r) => r.group_key === item.group_key && r.url !== item.url)
  );

  /** Focusable elements currently inside the panel, in DOM/tab order. Queried
   *  live (not cached) since the set of focusable controls changes with
   *  `item` (siblings list, download buttons, etc.) while the panel stays
   *  mounted across a same-panel item swap. */
  function focusableEls(): HTMLElement[] {
    if (!panelEl) return [];
    return Array.from(
      panelEl.querySelectorAll<HTMLElement>(
        'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])'
      )
    ).filter((el) => el.offsetParent !== null); // skip hidden elements
  }

  /** Focus trap: Tab/Shift+Tab cycle within the panel's focusable elements
   *  instead of escaping into the (dimmed, but still-present) background
   *  list behind the modal. */
  function handleTrapTab(e: KeyboardEvent) {
    if (e.key !== 'Tab') return;
    const els = focusableEls();
    if (els.length === 0) {
      e.preventDefault();
      panelEl?.focus();
      return;
    }
    const first = els[0];
    const last = els[els.length - 1];
    const active = document.activeElement;
    if (e.shiftKey) {
      if (active === first || !els.includes(active as HTMLElement)) {
        e.preventDefault();
        last.focus();
      }
    } else {
      if (active === last || !els.includes(active as HTMLElement)) {
        e.preventDefault();
        first.focus();
      }
    }
  }

  function handleKeydown(e: KeyboardEvent) {
    if (e.key === 'Escape') { onclose(); return; }
    handleTrapTab(e);
  }

  async function handleDownload(
    url: string, title: string, year?: number | null,
    meta?: { resolution?: string; size?: string; hdr?: string; dovi?: boolean }
  ) {
    const id = downloadQueue.add(title);
    try {
      // Pass the release specs so a later scan's "already grabbed" chip shows
      // resolution / HDR / DV instead of blanks.
      await api.download(url, title, $downloadHost, year,
                         meta?.resolution || '', meta?.size || '',
                         meta?.hdr || '', meta?.dovi ?? false);
      downloadQueue.markSent(id);
      addToast('Download', `Sent: ${title} (${$downloadHost})`);
    } catch (e) {
      addToast('Error', e instanceof Error ? e.message : 'Download failed', 'error');
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

  let copyingLinks = $state(false);
  async function copyLinks() {
    if (!item.url || copyingLinks) return;
    copyingLinks = true;
    try {
      const { links } = await api.scrapeLinks(item.url, $downloadHost, item.title, item.resolution);
      if (!links.length) {
        addToast('No Links', `No ${$downloadHost} links found on the page`, 'warning');
        return;
      }
      await navigator.clipboard.writeText(links.join('\n'));
      markDownloaded([item.url]);
      addToast('Copied', `${links.length} ${$downloadHost} link(s) copied — JDownloader should grab them`);
    } catch (e) {
      addToast('Error', e instanceof Error ? e.message : 'Failed to scrape links', 'error');
    } finally {
      copyingLinks = false;
    }
  }

  function openImdb() {
    if (item.imdb_id) window.open(`https://www.imdb.com/title/${item.imdb_id}`, '_blank');
  }

  function openPlex() {
    api.openInPlex(item.title, item.imdb_id ?? undefined, item.plex_rating_key ?? undefined).catch(
      (e) => addToast('Error', e instanceof Error ? e.message : 'Failed to open in Plex', 'error')
    );
  }

  async function addToWatchlist() {
    try {
      await api.watchlistAdd({
        title: item.title,
        year: item.year,
        imdb_id: item.imdb_id,
        item_type: item.season ? 'tv' : 'movie'
      });
      addToast('Watchlist', `Added: ${item.title}`);
    } catch (e) {
      addToast('Error', e instanceof Error ? e.message : 'Failed to add to watchlist', 'error');
    }
  }
</script>

<svelte:window onkeydown={handleKeydown} />

<!-- Backdrop -->
<!-- svelte-ignore a11y_no_static_element_interactions -->
<!-- svelte-ignore a11y_click_events_have_key_events -->
<div class="fixed inset-0 z-50 flex justify-end" onclick={onclose}>
  <!-- Dimmed overlay -->
  <div class="absolute inset-0 bg-[var(--bg-overlay)]"></div>

  <!-- Panel -->
  <div
    bind:this={panelEl}
    role="dialog"
    aria-label="Item details"
    tabindex="-1"
    class="relative w-full max-w-[480px] h-full bg-[var(--bg-primary)] border-l border-[var(--border)] overflow-y-auto shadow-2xl outline-none"
    transition:fly={{ x: 400, duration: 250 }}
    onclick={(e) => e.stopPropagation()}
  >
    <!-- Close button -->
    <button
      onclick={onclose}
      aria-label="Close details panel"
      class="absolute top-3 right-3 z-10 w-8 h-8 rounded-full bg-black/50 hover:bg-black/70 flex items-center justify-center text-white text-sm transition-colors"
    >&times;</button>

    <!-- Hero section -->
    <div class="relative w-full h-[300px] overflow-hidden bg-[var(--bg-tertiary)]">
      {#if item.poster_url}
        <!-- Blurred fill so the wide hero isn't empty around the portrait poster -->
        <img src={item.poster_url} alt="" aria-hidden="true"
          class="absolute inset-0 w-full h-full object-cover blur-2xl scale-110 opacity-40" />
        <!-- Full poster, never cropped (top/bottom no longer cut off) -->
        <img
          src={item.poster_url}
          alt={item.title}
          class="relative block h-[300px] mx-auto object-contain"
        />
      {:else}
        <div class="w-full h-full flex items-center justify-center text-[var(--text-secondary)] text-sm">
          No poster available
        </div>
      {/if}

      <!-- Gradient overlay -->
      <div class="absolute inset-0 bg-gradient-to-t from-black/80 via-transparent to-transparent"></div>

      <!-- Title overlay -->
      <div class="absolute bottom-0 left-0 right-0 p-4">
        <h2 class="text-xl font-bold text-white leading-tight">{item.title}</h2>
        <div class="flex items-center gap-2 mt-1.5 flex-wrap">
          {#if item.year}<span class="text-sm text-white/80">{item.year}</span>{/if}
          <Badge label={item.resolution || '?'} />
          {#if item.dovi}<Badge label="DV" variant="accent" />{/if}
          {#if item.hdr && !item.dovi}<Badge label="HDR" variant="warning" />{/if}
        </div>
      </div>
    </div>

    <!-- Content -->
    <div class="p-4 space-y-5">

      <!-- Status -->
      <div>
        <Badge label={formatStatus(item.status)} variant={statusVariant(item.status)} />
        {#if item.size}
          <span class="ml-2 text-xs text-[var(--text-secondary)]">{item.size}</span>
        {/if}
      </div>

      <!-- Description -->
      {#if item.description}
        <div>
          <h3 class="text-xs font-semibold text-[var(--text-secondary)] uppercase tracking-wider mb-1">Description</h3>
          <p class="text-sm text-[var(--text-primary)] leading-relaxed">{item.description}</p>
        </div>
      {/if}

      <!-- Genres -->
      {#if item.genres?.length}
        <div>
          <h3 class="text-xs font-semibold text-[var(--text-secondary)] uppercase tracking-wider mb-1.5">Genres</h3>
          <div class="flex flex-wrap gap-1.5">
            {#each item.genres as genre}
              <Badge label={genre} />
            {/each}
          </div>
        </div>
      {/if}

      <!-- Metadata grid -->
      <div class="grid grid-cols-2 gap-3">
        {#if item.language}
          <div>
            <span class="text-[10px] font-semibold text-[var(--text-secondary)] uppercase tracking-wider">Language</span>
            <p class="text-sm text-[var(--text-primary)] mt-0.5">{item.language}</p>
          </div>
        {/if}
        {#if item.rating != null}
          <div>
            <span class="text-[10px] font-semibold text-[var(--text-secondary)] uppercase tracking-wider">IMDb</span>
            <p class="text-sm text-[var(--text-primary)] mt-0.5">
              {item.rating.toFixed(1)}
              {#if item.votes != null}
                <span class="text-[10px] text-[var(--text-secondary)]">({item.votes.toLocaleString()} votes)</span>
              {/if}
            </p>
          </div>
        {/if}
        {#if item.rt_score != null}
          <div>
            <span class="text-[10px] font-semibold text-[var(--text-secondary)] uppercase tracking-wider">Rotten Tomatoes</span>
            <p class="text-sm text-[var(--text-primary)] mt-0.5">{item.rt_score}%</p>
          </div>
        {/if}
        {#if item.posted_date}
          <div>
            <span class="text-[10px] font-semibold text-[var(--text-secondary)] uppercase tracking-wider">Posted</span>
            <p class="text-sm text-[var(--text-primary)] mt-0.5">{item.posted_date}</p>
          </div>
        {/if}
      </div>

      <!-- Previously grabbed (different resolution already sent to JDownloader) -->
      {#if item.prior_grab}
        <div class="rounded-lg p-3 border border-amber-500/30 bg-amber-500/10 text-amber-500">
          <h3 class="text-xs font-semibold uppercase tracking-wider mb-1 flex items-center gap-1.5">
            <svg class="w-3.5 h-3.5" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" aria-hidden="true"><path d="M8 3v7m0 0-3-3m3 3 3-3"/><rect x="2" y="11.5" width="12" height="2" rx="1"/></svg>
            Previously Grabbed
          </h3>
          <p class="text-sm">
            {item.prior_grab.resolution}
            {#if item.prior_grab.size}<span class="opacity-75"> &middot; {item.prior_grab.size}</span>{/if}
          </p>
        </div>
      {/if}

      <!-- Plex library info with version badges -->
      {#if item.plex_info && item.plex_info !== '-'}
        <div>
          <h3 class="text-xs font-semibold text-[var(--text-secondary)] uppercase tracking-wider mb-1.5">In Library</h3>
          <p class="text-sm text-[var(--text-primary)] mb-1.5">{item.plex_info}</p>
          {#if item.plex_versions}
            {@const versions = (() => { try { const v = JSON.parse(item.plex_versions); return Array.isArray(v) ? v : []; } catch { return []; } })()}
            {#if versions.length > 0}
              <div class="flex flex-wrap gap-1.5">
                {#each versions as pv}
                  <div class="inline-flex items-center gap-1 px-2 py-1 rounded bg-[var(--bg-tertiary)] border border-[var(--border)]">
                    <span class="text-xs font-semibold {pv.res === '4K' ? 'text-yellow-500' : 'text-[var(--text-primary)]'}">{pv.res}</span>
                    {#if pv.dovi}<span class="text-[10px] font-bold text-purple-400">DV</span>{/if}
                    {#if pv.hdr && !pv.dovi}<span class="text-[10px] font-bold text-amber-400">HDR</span>{/if}
                    {#if pv.size}<span class="text-[10px] text-[var(--text-secondary)]">{pv.size}GB</span>{/if}
                  </div>
                {/each}
              </div>
            {/if}
          {/if}
        </div>
      {/if}

      <!-- Siblings -->
      {#if siblings.length > 0}
        <div>
          <h3 class="text-xs font-semibold text-[var(--text-secondary)] uppercase tracking-wider mb-2">
            Other Releases ({siblings.length})
          </h3>
          <div class="space-y-1.5">
            {#each siblings as sibling}
              <div class="flex items-center justify-between p-2 rounded bg-[var(--bg-secondary)] border border-[var(--border)]">
                <div class="flex items-center gap-2 min-w-0">
                  <Badge label={sibling.resolution || '?'} />
                  {#if sibling.dovi}<Badge label="DV" variant="accent" />{/if}
                  {#if sibling.hdr && !sibling.dovi}<Badge label="HDR" variant="warning" />{/if}
                  <span class="text-xs text-[var(--text-secondary)] truncate">{sibling.size}</span>
                </div>
                {#if sibling.url}
                  <button
                    onclick={() => handleDownload(sibling.url, sibling.title, sibling.year, sibling)}
                    title="Download"
                    aria-label="Download {sibling.resolution || ''} release"
                    class="shrink-0 w-6 h-6 rounded hover:bg-[var(--accent)] flex items-center justify-center text-[var(--text-secondary)] hover:text-white text-xs transition-colors"
                  >&#8595;</button>
                {/if}
              </div>
            {/each}
          </div>
        </div>
      {/if}

      <!-- Download progress -->
      {#if $activeDownload && $activeDownload.title === item.title}
        <div class="rounded-lg p-3 border transition-colors {$activeDownload.status === 'complete' ? 'border-[var(--success)] bg-[color-mix(in_srgb,var(--success)_8%,var(--bg-secondary))]' : 'border-[var(--accent)] bg-[color-mix(in_srgb,var(--accent)_8%,var(--bg-secondary))]'}">
          <div class="flex items-center justify-between mb-1.5">
            <span class="text-xs font-medium">
              {#if $activeDownload.status === 'resolving'}
                Resolving links...
              {:else if $activeDownload.status === 'downloading'}
                Sending to JDownloader...
              {:else if $activeDownload.status === 'complete'}
                Complete
              {:else}
                Processing...
              {/if}
            </span>
            {#if $activeDownload.linkCount > 0}
              <span class="text-[10px] text-[var(--text-secondary)]">{$activeDownload.linkCount} link(s)</span>
            {/if}
          </div>
          <div class="w-full h-1.5 bg-[var(--bg-tertiary)] rounded-full overflow-hidden">
            <div
              class="h-full rounded-full transition-all duration-500"
              style="width: {$activeDownload.status === 'resolving' ? '30' : $activeDownload.status === 'downloading' ? '70' : '100'}%; background: {$activeDownload.status === 'complete' ? 'var(--success)' : 'var(--accent)'};"
            ></div>
          </div>
        </div>
      {/if}

      <!-- Actions -->
      <div class="flex flex-wrap gap-2 pt-2 border-t border-[var(--border)]">
        {#if item.url}
          <!-- Host selector + Download (synced with global FilterBar selector) -->
          <div class="flex items-center gap-0">
            <select
              value={$downloadHost}
              onchange={(e) => downloadHost.set(e.currentTarget.value)}
              class="px-2 py-1.5 rounded-l text-xs bg-[var(--bg-tertiary)] border border-r-0 border-[var(--border)] text-[var(--text-primary)] focus:outline-none"
            >
              {#each DOWNLOAD_HOSTS as h}<option value={h.value}>{h.value}</option>{/each}
            </select>
            <button
              onclick={() => handleDownload(item.url, item.title, item.year, item)}
              aria-label="Download"
              class="flex items-center gap-1.5 px-3 py-1.5 rounded-r text-xs font-medium bg-[var(--accent)] text-white hover:opacity-90 transition-opacity"
            >
              &#8595; Download
            </button>
          </div>
          <button
            onclick={copyLinks}
            disabled={copyingLinks}
            aria-label="Copy download links"
            title="Scrape and copy {$downloadHost} links for JDownloader"
            class="flex items-center gap-1.5 px-3 py-1.5 rounded text-xs font-medium bg-[var(--bg-tertiary)] text-[var(--text-secondary)] hover:text-[var(--text-primary)] transition-colors disabled:opacity-50"
          >
            {copyingLinks ? 'Copying…' : 'Copy Links'}
          </button>
          <button
            onclick={copyUrl}
            aria-label="Copy URL"
            class="flex items-center gap-1.5 px-3 py-1.5 rounded text-xs font-medium bg-[var(--bg-tertiary)] text-[var(--text-secondary)] hover:text-[var(--text-primary)] transition-colors"
          >
            Copy URL
          </button>
          <button
            onclick={() => { if (item.url) window.open(item.url, '_blank'); }}
            aria-label="Open source page"
            class="flex items-center gap-1.5 px-3 py-1.5 rounded text-xs font-medium bg-[var(--bg-tertiary)] text-[var(--text-secondary)] hover:text-[var(--text-primary)] transition-colors"
          >
            Open Page
          </button>
        {/if}
        {#if item.plex_rating_key}
          <button
            onclick={openPlex}
            aria-label="Open in Plex"
            class="flex items-center gap-1.5 px-3 py-1.5 rounded text-xs font-medium bg-[var(--bg-tertiary)] text-[var(--text-secondary)] hover:text-orange-400 transition-colors"
          >
            Open in Plex
          </button>
        {/if}
        {#if item.imdb_id}
          <button
            onclick={openImdb}
            aria-label="Open on IMDb"
            class="flex items-center gap-1.5 px-3 py-1.5 rounded text-xs font-medium bg-[var(--bg-tertiary)] text-[var(--text-secondary)] hover:text-yellow-400 transition-colors"
          >
            IMDb
          </button>
        {/if}
        <button
          onclick={addToWatchlist}
          aria-label="Add to watchlist"
          class="flex items-center gap-1.5 px-3 py-1.5 rounded text-xs font-medium bg-[var(--bg-tertiary)] text-[var(--text-secondary)] hover:text-[var(--accent)] transition-colors"
        >
          + Watchlist
        </button>
      </div>
    </div>
  </div>
</div>
