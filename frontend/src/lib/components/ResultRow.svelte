<script lang="ts">
  import Badge from './Badge.svelte';
  import { toggleSelect, selectedKeys, selectedDetail, markDownloaded } from '$lib/stores/results';
  import { settings } from '$lib/stores/settings';
  import { api } from '$lib/api/client';
  import { addToast } from '$lib/stores/notifications';
  import { downloadHost, activeDownload } from '$lib/stores/downloads';
  import { statusVariant, formatStatus, formatCount } from '$lib/constants';
  import type { ScanResult } from '$lib/api/types';

  let showRating = $derived($settings.show_rating ?? true);
  let showVotes = $derived($settings.show_votes ?? true);
  let showRt = $derived($settings.show_rt ?? true);
  let showGenres = $derived($settings.show_genres ?? true);
  let showLinks = $derived($settings.show_links ?? true);

  interface Props {
    item: ScanResult;
    focused?: boolean;
    oncontextmenu?: (e: MouseEvent) => void;
  }
  let { item, focused = false, oncontextmenu: ctxHandler }: Props = $props();

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
    {selected ? 'bg-[var(--accent)]/5' : ''}
    {focused ? 'outline outline-2 outline-[var(--accent)] -outline-offset-2' : ''}"
  onclick={() => selectedDetail.set(item)}
  oncontextmenu={ctxHandler}
>
  <td class="p-2 w-8">
    <input type="checkbox" checked={selected} class="accent-[var(--accent)]" onclick={(e) => { e.stopPropagation(); toggleSelect(item.url); }} />
  </td>
  <td class="p-2 hidden sm:table-cell" style="width:56px; min-width:56px;">
    {#if item.poster_url}
      <img src={item.poster_url} alt="" class="h-[72px] object-cover rounded shadow-sm" style="width:48px; min-width:48px;" loading="lazy" />
    {:else}
      <div class="h-[72px] bg-[var(--bg-tertiary)] rounded" style="width:48px; min-width:48px;"></div>
    {/if}
  </td>
  <td class="p-2">
    <div class="text-sm font-medium truncate max-w-xs" title={item.title}>{item.title}</div>
    <div class="flex items-center gap-1.5 text-[10px] text-[var(--text-secondary)] truncate">
      {#if showGenres && item.genres?.length}<span>{item.genres.slice(0, 3).join(', ')}</span>{/if}
      {#if item.language && item.language !== 'English'}<span class="opacity-60">&middot; {item.language}</span>{/if}
    </div>
    <!-- Plex versions + posted date row -->
    {#if plexVersions.length > 0 || item.posted_date}
      <div class="flex items-center gap-1.5 mt-0.5 text-[10px] text-[var(--text-secondary)]">
        {#if plexVersions.length > 0}
          <span class="font-semibold text-orange-400">Plex</span>
          {#each plexVersions as pv, i}
            {#if i > 0}<span class="opacity-40">&middot;</span>{/if}
            <span class="inline-flex items-center gap-0.5">
              <span class="font-medium {pv.res === '4K' ? 'text-yellow-500' : 'text-[var(--text-primary)]'}">{pv.res}</span>
              {#if pv.dovi}<span class="text-purple-400 font-bold">DV</span>{/if}
              {#if pv.hdr && !pv.dovi}<span class="text-amber-400 font-bold">HDR</span>{/if}
              {#if pv.size}<span class="opacity-60">{pv.size}GB</span>{/if}
            </span>
          {/each}
        {/if}
        {#if item.posted_date}
          {#if plexVersions.length > 0}<span class="opacity-30">|</span>{/if}
          <span class="opacity-70">{item.posted_date}</span>
        {/if}
      </div>
    {/if}
  </td>
  <!-- Rating (IMDb + Rotten Tomatoes icon) — promoted to second data column -->
  <td class="p-2 text-sm">
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
  <td class="p-2 text-sm text-[var(--text-secondary)] hidden lg:table-cell">{item.year ?? ''}</td>
  <td class="p-2 hidden md:table-cell">
    <div class="flex items-center gap-1">
      <Badge label={item.resolution || '?'} />
      {#if item.dovi}<Badge label="DV" variant="accent" />{/if}
      {#if item.hdr && item.hdr !== 'SDR' && !item.dovi}<Badge label="HDR" variant="warning" />{/if}
    </div>
  </td>
  <td class="p-2 text-sm text-[var(--text-secondary)] hidden lg:table-cell">{item.size}</td>
  <td class="p-2">
    <Badge label={formatStatus(item.status)} variant={statusVariant(item.status)} />
  </td>
  <td class="p-2">
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
        <button onclick={copyUrl} title="Copy page URL" aria-label="Copy URL" class="w-6 h-6 rounded hover:bg-[var(--bg-tertiary)] flex items-center justify-center text-[var(--text-secondary)] hover:text-white text-xs transition-colors">&#128203;</button>
      {/if}
      {#if showLinks && item.imdb_id}
        <button onclick={openImdb} title="IMDb" aria-label="Open on IMDb" class="w-6 h-6 rounded hover:bg-yellow-600 flex items-center justify-center text-[var(--text-secondary)] hover:text-white text-[8px] font-bold transition-colors">IMDb</button>
      {/if}
    </div>
  </td>
  <!-- Spacer: absorbs remaining width so data columns cluster near the title -->
  <td class="p-2 w-full"></td>
</tr>
