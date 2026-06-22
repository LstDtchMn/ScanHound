<script lang="ts">
  import Badge from './Badge.svelte';
  import { toggleSelect, selectedKeys, selectedDetail, markDownloaded } from '$lib/stores/results';
  import { settings } from '$lib/stores/settings';
  import { api } from '$lib/api/client';
  import { addToast } from '$lib/stores/notifications';
  import { downloadHost, activeDownload } from '$lib/stores/downloads';
  import { statusVariant, formatStatus, formatCount } from '$lib/constants';
  import type { ScanResult } from '$lib/api/types';
  import { fly } from 'svelte/transition';

  let showRating = $derived($settings.show_rating ?? true);
  let showVotes = $derived($settings.show_votes ?? true);
  let showGenres = $derived($settings.show_genres ?? true);
  let showLinks = $derived($settings.show_links ?? true);

  interface Props {
    item: ScanResult;
    focused?: boolean;
    onmore?: () => void;
  }
  let { item, focused = false, onmore }: Props = $props();

  // Select by unique url, not group_key (same-title releases share group_key)
  let selected = $derived($selectedKeys.has(item.url));

  // Per-item host override (defaults to global)
  let itemHost = $state('');
  let effectiveHost = $derived(itemHost || $downloadHost);

  let isDownloading = $derived(
    !!item.url && $activeDownload?.url === item.url && $activeDownload?.status !== 'complete'
  );

  // Parse plex_versions JSON into badge data
  interface PlexVersion { res: string; hdr: string; dovi: boolean; size: string }
  let plexVersions: PlexVersion[] = $derived.by(() => {
    try {
      const raw = JSON.parse(item.plex_versions || '[]');
      if (!Array.isArray(raw) || raw.length === 0) return [];
      const seen = new Set<string>();
      return raw.filter((v: PlexVersion) => {
        const key = `${v.res}|${v.hdr}|${v.dovi}`;
        if (seen.has(key)) return false;
        seen.add(key);
        return true;
      }).slice(0, 4);
    } catch { return []; }
  });

  function handleDownload(e: Event) {
    e.stopPropagation();
    if (item.url) {
      api.download(item.url, item.title, effectiveHost, item.year).catch(() => addToast('Error', 'Download failed', 'error'));
    }
  }

  function openImdb(e: Event) {
    e.stopPropagation();
    if (item.imdb_id) window.open(`https://www.imdb.com/title/${item.imdb_id}`, '_blank');
  }

  function openSource(e: Event) {
    e.stopPropagation();
    if (item.url) window.open(item.url, '_blank');
  }

  function openInPlex(e: Event) {
    e.stopPropagation();
    api.openInPlex(item.title, item.imdb_id ?? undefined, item.plex_rating_key ?? undefined).catch(() => addToast('Error', 'Failed to open in Plex', 'error'));
  }

  function copyUrl(e: Event) {
    e.stopPropagation();
    if (item.url) {
      navigator.clipboard.writeText(item.url).then(
        () => addToast('Copied', 'URL copied to clipboard'),
        () => addToast('Error', 'Failed to copy URL', 'error')
      );
    }
  }

  let copyingLinks = $state(false);
  async function copyLinks(e: Event) {
    e.stopPropagation();
    if (!item.url || copyingLinks) return;
    copyingLinks = true;
    try {
      const { links } = await api.scrapeLinks(item.url, effectiveHost, item.title, item.resolution);
      if (!links.length) {
        addToast('No Links', `No ${effectiveHost} links found on the page`, 'warning');
        return;
      }
      await navigator.clipboard.writeText(links.join('\n'));
      markDownloaded([item.url]);
      addToast('Copied', `${links.length} ${effectiveHost} link(s) copied — JDownloader should grab them`);
    } catch (err) {
      addToast('Error', err instanceof Error ? err.message : 'Failed to scrape links', 'error');
    } finally {
      copyingLinks = false;
    }
  }
</script>

<div
  transition:fly={{ y: 10, duration: 200 }}
  class="bg-[var(--bg-secondary)] rounded-lg overflow-hidden border transition-colors cursor-pointer group
    {selected ? 'border-[var(--accent)]' : 'border-[var(--border)] hover:border-[var(--text-secondary)]'}
    {focused ? 'ring-2 ring-[var(--accent)] ring-offset-1 ring-offset-[var(--bg-primary)]' : ''}"
  onclick={() => selectedDetail.set(item)}
  role="button"
  tabindex="0"
  onkeydown={(e) => e.key === 'Enter' && selectedDetail.set(item)}
>
  <div class="aspect-[2/3] bg-[var(--bg-tertiary)] relative overflow-hidden">
    {#if item.poster_url}
      <img
        src={item.poster_url}
        alt={item.title}
        class="w-full h-full object-cover transition-transform group-hover:scale-105"
        loading="lazy"
      />
    {:else}
      <div class="flex items-center justify-center h-full text-[var(--text-secondary)] text-xs">
        No poster
      </div>
    {/if}

    <!-- Status badge — top right -->
    <div class="absolute top-1.5 right-1.5">
      <Badge label={formatStatus(item.status)} variant={statusVariant(item.status)} />
    </div>

    <!-- DV/HDR badges — bottom left -->
    <div class="absolute bottom-1.5 left-1.5 flex gap-1">
      {#if item.dovi}<Badge label="DV" variant="accent" />{/if}
      {#if item.hdr && !item.dovi}<Badge label="HDR" variant="warning" />{/if}
    </div>

    <!-- Mobile actions trigger -->
    {#if onmore}
      <button
        onclick={(e) => { e.stopPropagation(); onmore?.(); }}
        aria-label="Actions"
        class="md:hidden absolute bottom-1.5 right-1.5 w-8 h-8 rounded-full bg-black/55 text-white flex items-center justify-center text-lg leading-none"
      >⋯</button>
    {/if}

    <!-- Selection checkbox — top left -->
    <input
      type="checkbox"
      checked={selected}
      aria-label="{selected ? 'Deselect' : 'Select'} {item.title}"
      class="absolute top-1.5 left-1.5 w-4 h-4 accent-[var(--accent)] cursor-pointer rounded
        {selected ? 'opacity-100' : 'opacity-40 group-hover:opacity-80 focus-visible:opacity-100'} transition-opacity"
      onclick={(e) => { e.stopPropagation(); toggleSelect(item.url); }}
    />
  </div>

  <!-- Info section -->
  <div class="p-2">
    <!-- Title + year + resolution on one line -->
    <p class="text-sm font-semibold truncate" title={item.title}>
      {item.title}{#if item.year}<span class="font-normal text-[var(--text-secondary)]">&nbsp;({item.year})</span>{/if}
    </p>

    <!-- Metadata row: size, rating, genres -->
    <div class="flex items-center gap-1.5 mt-0.5 text-xs text-[var(--text-secondary)] truncate">
      {#if item.resolution}<span class="font-medium text-[var(--text-primary)]">{item.resolution}</span>{/if}
      {#if item.size}<span>&middot; {item.size}</span>{/if}
      {#if showRating && item.rating}<span>&middot; {item.rating.toFixed(1)}{#if showVotes && item.votes}<span class="text-[10px] opacity-60">({formatCount(item.votes)})</span>{/if}</span>{/if}
      {#if showGenres && item.genres?.length}<span class="opacity-60 truncate">&middot; {item.genres.slice(0, 2).join(', ')}</span>{/if}
    </div>
    <!-- Plex library versions -->
    {#if plexVersions.length > 0}
      <div class="flex items-center gap-1 mt-0.5 text-[10px] truncate">
        <span class="font-semibold text-orange-400">Plex:</span>
        {#each plexVersions as pv, i}
          {#if i > 0}<span class="text-[var(--text-secondary)] opacity-30">&middot;</span>{/if}
          <span class="inline-flex items-center gap-0.5 text-[var(--text-secondary)]">
            <span class="font-medium {pv.res === '4K' ? 'text-yellow-500' : 'text-[var(--text-primary)]'}">{pv.res}</span>
            {#if pv.dovi}<span class="text-purple-400 font-bold">DV</span>{/if}
            {#if pv.hdr && !pv.dovi}<span class="text-amber-400 font-bold">HDR</span>{/if}
            {#if pv.size}<span class="opacity-60">{pv.size}GB</span>{/if}
          </span>
        {/each}
      </div>
    {/if}

    <!-- Secondary row: RT score, language, posted date -->
    {#if item.rt_score || (item.language && item.language !== 'English') || item.posted_date}
      <div class="flex items-center gap-1.5 text-[10px] text-[var(--text-secondary)] truncate mt-0.5">
        {#if item.posted_date}<span class="opacity-80">{item.posted_date}</span>{/if}
        {#if item.rt_score}<span class="opacity-60">&middot; RT {item.rt_score}%</span>{/if}
        {#if item.language && item.language !== 'English'}<span class="opacity-60">&middot; {item.language}</span>{/if}
      </div>
    {/if}

    <!-- Action buttons with host selector -->
    <div class="flex items-center gap-0.5 mt-1.5 -ml-0.5">
      {#if item.url}
        <!-- Per-item host selector + download -->
        <select
          value={effectiveHost}
          onclick={(e) => e.stopPropagation()}
          onchange={(e) => { e.stopPropagation(); itemHost = e.currentTarget.value; }}
          class="h-5 px-0.5 rounded-l text-[9px] bg-[var(--bg-tertiary)] border border-r-0 border-[var(--border)] text-[var(--text-secondary)] focus:outline-none cursor-pointer"
          title="Download host"
        >
          <option value="Rapidgator">RG</option>
          <option value="Nitroflare">NF</option>
          <option value="1Fichier">1F</option>
        </select>
        <button
          onclick={handleDownload}
          disabled={isDownloading}
          aria-label="Download"
          title="Send to JDownloader ({effectiveHost})"
          class="h-5 px-1 rounded-r bg-[var(--accent)]/80 hover:bg-[var(--accent)] flex items-center justify-center text-white text-xs transition-colors disabled:opacity-80"
        >{#if isDownloading}<span class="inline-block animate-spin">⟳</span>{:else}&#8595;{/if}</button>
        <button
          onclick={copyLinks}
          disabled={copyingLinks}
          aria-label="Copy download links"
          title="Copy {effectiveHost} links (for JDownloader)"
          class="w-5 h-5 rounded flex items-center justify-center text-[var(--text-secondary)] hover:text-[var(--text-primary)] hover:bg-[var(--bg-tertiary)] text-[10px] transition-colors disabled:opacity-50"
        >{copyingLinks ? '…' : '\u{1F517}'}</button>
        <button
          onclick={openSource}
          aria-label="Open source page"
          title="Open source page"
          class="w-5 h-5 rounded flex items-center justify-center text-[var(--text-secondary)] hover:text-[var(--text-primary)] hover:bg-[var(--bg-tertiary)] text-xs transition-colors"
        >&#8599;</button>
        <button
          onclick={copyUrl}
          aria-label="Copy URL"
          title="Copy page URL"
          class="w-5 h-5 rounded flex items-center justify-center text-[var(--text-secondary)] hover:text-[var(--text-primary)] hover:bg-[var(--bg-tertiary)] text-[10px] transition-colors"
        >&#128203;</button>
      {/if}
      {#if showLinks}
        {#if item.imdb_id}
          <button
            onclick={openImdb}
            aria-label="Open on IMDb"
            title="IMDb"
            class="w-5 h-5 rounded flex items-center justify-center text-[var(--text-secondary)] hover:text-yellow-500 hover:bg-yellow-500/10 text-[8px] font-bold transition-colors"
          >IMDb</button>
        {/if}
        {#if item.plex_rating_key}
          <button
            onclick={openInPlex}
            aria-label="Open in Plex"
            title="Open in Plex"
            class="w-5 h-5 rounded flex items-center justify-center text-[var(--text-secondary)] hover:text-orange-400 hover:bg-orange-400/10 text-[8px] font-bold transition-colors"
          >Plex</button>
        {/if}
      {/if}
    </div>
  </div>
</div>
