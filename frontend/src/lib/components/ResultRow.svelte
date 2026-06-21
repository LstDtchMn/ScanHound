<script lang="ts">
  import Badge from './Badge.svelte';
  import { toggleSelect, selectedKeys, selectedDetail, markDownloaded } from '$lib/stores/results';
  import { settings } from '$lib/stores/settings';
  import { api } from '$lib/api/client';
  import { addToast } from '$lib/stores/notifications';
  import { downloadHost, activeDownload } from '$lib/stores/downloads';
  import { density, visibleColumns } from '$lib/stores/results';
  import { statusVariant, statusBorderColor, formatStatus, formatCount } from '$lib/constants';
  import type { ScanResult } from '$lib/api/types';

  let showRating = $derived($settings.show_rating ?? true);
  let showVotes = $derived($settings.show_votes ?? true);
  let showRt = $derived($settings.show_rt ?? true);
  let showGenres = $derived($settings.show_genres ?? true);
  let showLinks = $derived($settings.show_links ?? true);

  // Density: compact trims padding + shrinks the poster; comfortable is roomier.
  let compact = $derived($density === 'compact');
  let cellPad = $derived(compact ? 'p-1.5' : 'p-2.5');
  let posterW = $derived(compact ? 44 : 64);
  let posterH = $derived(compact ? 66 : 96);
  let cols = $derived($visibleColumns);

  interface Props {
    item: ScanResult;
    focused?: boolean;
    zebra?: boolean;
    oncontextmenu?: (e: MouseEvent) => void;
  }
  let { item, focused = false, zebra = false, oncontextmenu: ctxHandler }: Props = $props();

  // Select by unique url, not group_key (same-title releases share group_key)
  let selected = $derived($selectedKeys.has(item.url));

  // Per-item host override (defaults to global)
  let itemHost = $state('');
  let effectiveHost = $derived(itemHost || $downloadHost);

  // Show a spinner on this row's download button while it is being scraped/sent
  let isDownloading = $derived(
    !!item.url && $activeDownload?.url === item.url && $activeDownload?.status !== 'complete'
  );

  // Parse plex_versions JSON into badge data
  interface PlexVersion { res: string; hdr: string; dovi: boolean; size: string }
  let plexVersions: PlexVersion[] = $derived.by(() => {
    try {
      const raw = JSON.parse(item.plex_versions || '[]');
      if (!Array.isArray(raw) || raw.length === 0) return [];
      // Deduplicate by res+hdr+dovi
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
      api.download(item.url, item.title, effectiveHost, item.year).catch((e) => addToast('Error', e instanceof Error ? e.message : 'Download failed', 'error'));
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

<tr
  class="border-b border-[var(--border)] hover:bg-[var(--bg-tertiary)] transition-colors cursor-pointer
    {selected ? 'bg-[var(--accent)]/5' : (zebra ? 'bg-[var(--bg-secondary)]/40' : '')}
    {focused ? 'outline outline-2 outline-[var(--accent)] -outline-offset-2' : ''}"
  style="border-left: 3px solid {statusBorderColor(item.status)};"
  onclick={() => selectedDetail.set(item)}
  oncontextmenu={ctxHandler}
>
  <td class="{cellPad} w-8">
    <input type="checkbox" checked={selected} class="accent-[var(--accent)]" onclick={(e) => { e.stopPropagation(); toggleSelect(item.url); }} />
  </td>
  <td class="{cellPad} hidden sm:table-cell" style="width:{posterW + 8}px;">
    {#if item.poster_url}
      <img src={item.poster_url} alt="" class="object-cover rounded shadow-sm" style="width:{posterW}px; height:{posterH}px;" loading="lazy" />
    {:else}
      <div class="bg-[var(--bg-tertiary)] rounded" style="width:{posterW}px; height:{posterH}px;"></div>
    {/if}
  </td>
  <td class="{cellPad} max-w-[640px] overflow-hidden">
    <!-- Title with the year folded in -->
    <div class="text-sm truncate" title={item.title}>
      <span class="font-semibold">{item.title}</span>{#if item.year}<span class="text-[var(--text-secondary)] font-normal"> ({item.year})</span>{/if}
    </div>
    <!-- Meta: genres · language · posted date -->
    <div class="flex items-center gap-1.5 text-[11px] text-[var(--text-secondary)] truncate mt-0.5">
      {#if showGenres && item.genres?.length}<span class="truncate">{item.genres.slice(0, 3).join(', ')}</span>{/if}
      {#if item.language && item.language !== 'English'}<span class="opacity-75 whitespace-nowrap">&middot; {item.language}</span>{/if}
      {#if item.posted_date}
        <span class="inline-flex items-center gap-1 opacity-85 whitespace-nowrap">
          <svg class="w-3 h-3 flex-shrink-0" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5"><rect x="2.5" y="3.5" width="11" height="10" rx="1.5"/><path d="M2.5 6.5h11M5.5 2v3M10.5 2v3"/></svg>
          {item.posted_date}
        </span>
      {/if}
    </div>
    <!-- "In Plex" similar copies, as readable chips -->
    {#if plexVersions.length > 0}
      <div class="flex items-center flex-wrap gap-1 mt-1 text-[10px]">
        <span class="text-[var(--text-secondary)]">In Plex:</span>
        {#each plexVersions as pv}
          <span class="inline-flex items-center gap-1 px-1.5 py-0.5 rounded border border-[var(--border)] bg-[var(--bg-tertiary)]/50 whitespace-nowrap">
            <span class="font-semibold {pv.res === '4K' ? 'text-yellow-500' : 'text-[var(--text-primary)]'}">{pv.res}</span>
            {#if pv.dovi}<span class="text-purple-400 font-bold">DV</span>{/if}
            {#if pv.hdr && !pv.dovi}<span class="text-amber-400 font-bold">HDR</span>{/if}
            {#if pv.size}<span class="text-[var(--text-secondary)]">{pv.size}GB</span>{/if}
          </span>
        {/each}
      </div>
    {/if}
  </td>
  <!-- Rating (IMDb + Rotten Tomatoes icon) — promoted to second data column -->
  {#if cols.rating}
  <td class="{cellPad} text-sm">
    {#if showRating && item.rating != null}
      <div class="flex items-center gap-1 whitespace-nowrap">
        <span aria-hidden="true">⭐</span>
        <span class="font-medium text-[var(--text-primary)]">{item.rating.toFixed(1)}</span>
        {#if showVotes && item.votes}<span class="text-[10px] text-[var(--text-secondary)] opacity-60">({formatCount(item.votes)})</span>{/if}
      </div>
    {/if}
    {#if showRt && item.rt_score != null}
      <div class="flex items-center gap-1 text-[10px] text-[var(--text-secondary)] mt-0.5 whitespace-nowrap">
        <span title={item.rt_score >= 60 ? 'Fresh' : 'Rotten'} aria-label={item.rt_score >= 60 ? 'Fresh' : 'Rotten'}>{item.rt_score >= 60 ? '🍅' : '🤢'}</span>
        <span>{item.rt_score}%</span>
      </div>
    {/if}
  </td>
  {/if}
  {#if cols.res}
  <td class="{cellPad} hidden md:table-cell">
    <div class="flex items-center gap-1">
      <Badge label={item.resolution || '?'} />
      {#if item.dovi}<Badge label="DV" variant="accent" />{/if}
      {#if item.hdr && item.hdr !== 'SDR' && !item.dovi}<Badge label="HDR" variant="warning" />{/if}
    </div>
  </td>
  {/if}
  {#if cols.size}
  <td class="{cellPad} text-sm text-[var(--text-secondary)] hidden lg:table-cell">{item.size}</td>
  {/if}
  {#if cols.status}
  <td class="{cellPad}">
    <Badge label={formatStatus(item.status)} variant={statusVariant(item.status)} />
  </td>
  {/if}
  <td class="{cellPad}">
    <div class="flex items-center gap-0.5">
      {#if item.url}
        <!-- Per-item host selector -->
        <select
          value={effectiveHost}
          onclick={(e) => e.stopPropagation()}
          onchange={(e) => { e.stopPropagation(); itemHost = e.currentTarget.value; }}
          class="h-6 px-1 rounded-l text-[10px] bg-[var(--bg-tertiary)] border border-r-0 border-[var(--border)] text-[var(--text-secondary)] focus:outline-none cursor-pointer"
          title="Download host"
        >
          <option value="Rapidgator">RG</option>
          <option value="Nitroflare">NF</option>
          <option value="1Fichier">1F</option>
        </select>
        <button onclick={handleDownload} disabled={isDownloading} title="Send to JDownloader ({effectiveHost})" aria-label="Send to JDownloader" class="h-6 px-1.5 rounded-r bg-[var(--accent)]/80 hover:bg-[var(--accent)] flex items-center justify-center text-white text-xs transition-colors disabled:opacity-80">{#if isDownloading}<span class="inline-block animate-spin">⟳</span>{:else}&#8595;{/if}</button>
        <button onclick={copyLinks} disabled={copyingLinks} title="Copy {effectiveHost} links (for JDownloader)" aria-label="Copy download links" class="w-6 h-6 rounded hover:bg-[var(--bg-tertiary)] flex items-center justify-center text-[var(--text-secondary)] hover:text-white text-xs transition-colors disabled:opacity-50">{copyingLinks ? '…' : '\u{1F517}'}</button>
        <button onclick={openSource} title="Open source page" aria-label="Open source page" class="w-6 h-6 rounded hover:bg-[var(--bg-tertiary)] flex items-center justify-center text-[var(--text-secondary)] hover:text-white text-sm transition-colors">&#8599;</button>
        <button onclick={copyUrl} title="Copy page URL" aria-label="Copy URL" class="w-6 h-6 rounded hover:bg-[var(--bg-tertiary)] flex items-center justify-center text-[var(--text-secondary)] hover:text-white text-xs transition-colors">&#128203;</button>
      {/if}
      {#if showLinks && item.imdb_id}
        <button onclick={openImdb} title="IMDb" aria-label="Open on IMDb" class="w-6 h-6 rounded hover:bg-yellow-600 flex items-center justify-center text-[var(--text-secondary)] hover:text-white text-[8px] font-bold transition-colors">IMDb</button>
      {/if}
    </div>
  </td>
</tr>
