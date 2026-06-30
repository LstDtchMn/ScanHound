<script lang="ts">
  import Badge from './Badge.svelte';
  import { toggleSelect, selectedKeys, selectedDetail, markDownloaded } from '$lib/stores/results';
  import { settings } from '$lib/stores/settings';
  import { api } from '$lib/api/client';
  import { addToast } from '$lib/stores/notifications';
  import { downloadHost, activeDownload, downloadingTitles } from '$lib/stores/downloads';
  import { density } from '$lib/stores/results';
  import { statusVariant, statusBorderColor, statusBarStyle, formatStatus, formatCount, resolutionRank, sizeToGB, DOWNLOAD_HOSTS } from '$lib/constants';
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

  interface Props {
    item: ScanResult;
    focused?: boolean;
    zebra?: boolean;
    nested?: boolean;
    owned?: { resolution: string; size: string; hdr: string; dovi: boolean } | null;
    oncontextmenu?: (e: MouseEvent) => void;
  }
  let { item, focused = false, zebra = false, nested = false, owned = null, oncontextmenu: ctxHandler }: Props = $props();

  // Compare this (not-yet-owned) release against the version already downloaded
  // in the same group, so upgrades are obvious (e.g. +DV, +7.6 GB over your 4K).
  // Ranking/size helpers are shared with the group-level comparison (constants).
  const resRank = resolutionRank;
  const parseGB = sizeToGB;
  let isOwnedStatus = $derived(['downloaded', 'in_library', 'library'].some(s => (item.status ?? '').toLowerCase().includes(s)));
  let comparison = $derived.by(() => {
    if (!owned || isOwnedStatus) return null;
    const reasons: string[] = [];
    const downsides: string[] = [];
    const ir = resRank(item.resolution), or = resRank(owned.resolution);
    if (ir > or) reasons.push(`+${item.resolution}`);
    else if (ir < or && item.resolution) downsides.push(`−${item.resolution}`);
    if (item.dovi && !owned.dovi) reasons.push('+DV');
    else if (!item.dovi && owned.dovi) downsides.push('−DV');
    const iHDR = !!item.hdr && item.hdr !== 'SDR';
    const oHDR = !!owned.hdr && owned.hdr !== 'SDR';
    if (iHDR && !oHDR && !item.dovi) reasons.push('+HDR');
    else if (!iHDR && oHDR && !owned.dovi) downsides.push('−HDR');
    const ig = parseGB(item.size), og = parseGB(owned.size);
    if (ir === or && og > 0 && ig > 0) {
      if (ig > og) reasons.push(`+${(ig - og).toFixed(1)} GB`);
      else if (ig < og) downsides.push(`−${(og - ig).toFixed(1)} GB`);
    }
    // Distinguish a genuine upgrade from an equal or strictly-worse release, so
    // "same" and "downgrade" no longer render identically.
    const verdict = reasons.length ? 'upgrade' : (downsides.length ? 'downgrade' : 'same');
    return { reasons, downsides, verdict, isUpgrade: verdict === 'upgrade' };
  });

  // Select by unique url, not group_key (same-title releases share group_key)
  let selected = $derived($selectedKeys.has(item.url));

  // Per-item host override (defaults to global)
  let itemHost = $state('');
  let effectiveHost = $derived(itemHost || $downloadHost);

  // Show a spinner on this row's download button while it is being scraped/sent
  let isDownloading = $derived(
    !!item.url && $activeDownload?.url === item.url && $activeDownload?.status !== 'complete'
  );

  let effectiveStatus = $derived($downloadingTitles.has(item.title) ? 'downloading' : item.status);

  // Poster hover-to-enlarge state
  let posterHovered = $state(false);
  let enlargedStyle = $state('');

  function onPosterEnter(e: MouseEvent) {
    const r = (e.currentTarget as HTMLElement).getBoundingClientRect();
    const scale = 2.5;
    const w = r.width * scale;
    const h = r.height * scale;
    enlargedStyle = `left:${r.left + r.width / 2 - w / 2}px;top:${r.top + r.height / 2 - h / 2}px;width:${w}px;height:${h}px;`;
    posterHovered = true;
  }
  function onPosterLeave() { posterHovered = false; }

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
      api.download(item.url, item.title, effectiveHost, item.year,
                   item.resolution || '', item.size || '', item.hdr || '', item.dovi ?? false)
        .catch((e) => addToast('Error', e instanceof Error ? e.message : 'Download failed', 'error'));
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
  style="{statusBarStyle([statusBorderColor(effectiveStatus)])}"
  onclick={() => selectedDetail.set(item)}
  oncontextmenu={ctxHandler}
>
  <td class="{cellPad} w-8" style={nested ? 'padding-left: 1.75rem;' : ''}>
    <input type="checkbox" checked={selected} class="accent-[var(--accent)]" onclick={(e) => { e.stopPropagation(); toggleSelect(item.url); }} />
  </td>
  <td class="{cellPad} hidden sm:table-cell" style="width:{posterW + 8}px; min-width:{posterW + 8}px;">
    {#if item.poster_url}
      <img src={item.poster_url} alt="" class="object-cover rounded shadow-sm cursor-zoom-in" style="width:{posterW}px; height:{posterH}px;" loading="lazy"
        onmouseenter={onPosterEnter} onmouseleave={onPosterLeave} />
      {#if posterHovered}
        <img src={item.poster_url} alt="" aria-hidden="true"
          style="position:fixed;{enlargedStyle}z-index:9999;pointer-events:none;border-radius:10px;object-fit:cover;box-shadow:0 25px 60px -8px rgba(0,0,0,0.8);"
          loading="eager" />
      {/if}
    {:else}
      <div class="bg-[var(--bg-tertiary)] rounded" style="width:{posterW}px; height:{posterH}px;"></div>
    {/if}
  </td>
  <td class="{cellPad} w-full overflow-hidden">
    <!-- Title with the year folded in, status label right after it -->
    <div class="flex items-center gap-2 min-w-0">
      <span class="text-sm truncate min-w-0" title={item.title}>
        <span class="font-semibold">{item.title}</span>{#if item.year}<span class="text-[var(--text-secondary)] font-normal">&nbsp;({item.year})</span>{/if}
      </span>
      <span class="flex-shrink-0"><Badge label={formatStatus(effectiveStatus)} variant={statusVariant(effectiveStatus)} /></span>
    </div>
    <!-- Decision stat line: rating · RT · resolution/HDR · size · status — everything needed to grab at a glance -->
    <div class="flex items-center flex-wrap gap-x-2.5 gap-y-1 mt-1 text-[12px]">
      {#if showRating && item.rating != null}
        <span class="inline-flex items-center gap-1 whitespace-nowrap">
          <span aria-hidden="true">⭐</span>
          <span class="font-medium text-[var(--text-primary)]">{item.rating.toFixed(1)}</span>
          {#if showVotes && item.votes}<span class="text-[10px] text-[var(--text-secondary)] opacity-60">({formatCount(item.votes)})</span>{/if}
        </span>
      {/if}
      {#if showRt && item.rt_score != null}
        <span class="inline-flex items-center gap-1 whitespace-nowrap text-[var(--text-secondary)]">
          <span aria-hidden="true" title={item.rt_score >= 60 ? 'Fresh' : 'Rotten'}>{item.rt_score >= 60 ? '🍅' : '🤢'}</span>
          <span>{item.rt_score}%</span>
        </span>
      {/if}
      {#if item.resolution || item.dovi || (item.hdr && item.hdr !== 'SDR')}
        <span class="inline-flex items-center gap-1">
          {#if item.resolution}<span class="text-[10px] px-1.5 py-0.5 rounded bg-[var(--bg-tertiary)] text-[var(--text-primary)] font-medium">{item.resolution}</span>{/if}
          {#if item.dovi}<span class="text-[10px] px-1.5 py-0.5 rounded bg-purple-500/20 text-purple-400 font-medium">DV</span>{/if}
          {#if item.hdr && item.hdr !== 'SDR' && !item.dovi}<span class="text-[10px] px-1.5 py-0.5 rounded bg-amber-500/20 text-amber-400 font-medium">HDR</span>{/if}
        </span>
      {/if}
      {#if item.size}<span class="text-[var(--text-secondary)] whitespace-nowrap">{item.size}</span>{/if}
      {#if plexVersions.length > 0}
        <!-- Versions already in the Plex library: a distinct gold "Plex" capsule,
             captioned "In Library", placed just right of the release size. -->
        <span class="inline-flex flex-col items-start leading-none ml-1 pl-2 border-l border-[var(--border)]">
          <span class="text-[8px] uppercase tracking-wide text-[var(--text-secondary)] opacity-70 mb-0.5">In Library</span>
          <span class="inline-flex items-center gap-1 px-1.5 py-0.5 rounded border border-amber-500/50 bg-amber-500/10 whitespace-nowrap"
                title="Versions already in your Plex library">
            <svg class="w-3 h-3 flex-shrink-0 text-amber-500" viewBox="0 0 24 24" fill="currentColor" aria-label="In Plex"><path d="M5 2h6l7 10-7 10H5l7-10L5 2z"/></svg>
            {#each plexVersions as pv, i}
              <span class="inline-flex items-center gap-1 {i > 0 ? 'pl-1 border-l border-amber-500/25' : ''}">
                <span class="font-semibold {pv.res === '4K' ? 'text-yellow-500' : 'text-[var(--text-primary)]'}">{pv.res}</span>
                {#if pv.dovi}<span class="text-purple-400 font-bold text-[9px]">DV</span>{/if}
                {#if pv.hdr && !pv.dovi}<span class="text-amber-400 font-bold text-[9px]">HDR</span>{/if}
                {#if pv.size}<span class="text-[var(--text-secondary)] text-[9px]">{pv.size}GB</span>{/if}
              </span>
            {/each}
          </span>
        </span>
      {/if}
      {#if comparison}
        <!-- Comparison against the version already downloaded in this group -->
        <span class="inline-flex items-center gap-1.5 px-1.5 py-0.5 rounded text-[11px] whitespace-nowrap
              {comparison.verdict === 'upgrade'
                ? 'border border-[var(--success)]/45 bg-[var(--success)]/10 text-[var(--success)]'
                : comparison.verdict === 'downgrade'
                ? 'border border-[var(--warning)]/45 bg-[var(--warning)]/10 text-[var(--warning)]'
                : 'text-[var(--text-secondary)]'}"
              title="Compared to the version you already downloaded in this group">
          {#if comparison.verdict === 'upgrade'}
            <span class="font-semibold">↑ Upgrade</span>
            <span>{comparison.reasons.join(' · ')}</span>
          {:else if comparison.verdict === 'downgrade'}
            <span class="font-semibold">↓ Lower</span>
            <span>{comparison.downsides.join(' · ')}</span>
          {:else}
            <span class="font-semibold">= Same</span>
          {/if}
          <span class="opacity-80">vs your {owned?.resolution} · {owned?.size}</span>
        </span>
      {/if}
    </div>
    <!-- Meta: season/episodes · genres · language · posted date -->
    <div class="flex items-center gap-1.5 text-[11px] text-[var(--text-secondary)] truncate mt-0.5">
      {#if item.season != null}
        <span class="whitespace-nowrap font-medium text-[var(--text-primary)]">S{String(item.season).padStart(2, '0')}</span>
        {#if item.episodes != null}<span class="opacity-75 whitespace-nowrap">&middot; {item.episodes} ep{item.episodes !== 1 ? 's' : ''}</span>{/if}
      {/if}
      {#if showGenres && item.genres?.length}<span class="truncate {item.season != null ? 'opacity-75' : ''}">{item.genres.slice(0, 3).join(', ')}</span>{/if}
      {#if item.language && item.language !== 'English'}<span class="opacity-75 whitespace-nowrap">&middot; {item.language}</span>{/if}
      {#if item.posted_date}
        <span class="inline-flex items-center gap-1 opacity-85 whitespace-nowrap">
          <svg class="w-3 h-3 flex-shrink-0" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5"><rect x="2.5" y="3.5" width="11" height="10" rx="1.5"/><path d="M2.5 6.5h11M5.5 2v3M10.5 2v3"/></svg>
          {item.posted_date}
        </span>
      {/if}
    </div>
    <!-- Description excerpt (normal density only) -->
    {#if !compact && item.description}
      <p class="text-[11px] text-[var(--text-secondary)] mt-1 leading-relaxed line-clamp-2 opacity-80">{item.description}</p>
    {/if}
    <!-- Previously grabbed (different resolution) -->
    {#if item.prior_grab && !comparison}
      {@const g = item.prior_grab}
      {@const gRes = g.resolution && g.resolution !== '?' ? (g.resolution === '2160p' ? '4K' : g.resolution) : ''}
      {@const gSize = g.size && g.size !== '?' ? g.size : ''}
      {@const gHdr = g.hdr && g.hdr !== 'SDR' ? g.hdr : ''}
      <div class="flex items-center gap-1 mt-1 flex-wrap" title="A different resolution of this title was previously sent to JDownloader">
        <span class="inline-flex items-center gap-1 px-1.5 py-0.5 rounded border border-amber-500/40 bg-amber-500/10 text-[10px] text-amber-500 font-medium whitespace-nowrap">
          <svg class="w-2.5 h-2.5 flex-shrink-0" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M8 2v8m0 0-2.5-2.5M8 10l2.5-2.5"/><rect x="2.5" y="12" width="11" height="1.5" rx="0.75"/></svg>
          Grabbed
        </span>
        {#if gRes}<span class="text-[10px] px-1 py-0.5 rounded bg-[var(--bg-tertiary)] text-[var(--text-primary)] font-medium whitespace-nowrap">{gRes}</span>{/if}
        {#if g.dovi}<span class="text-[10px] px-1 py-0.5 rounded bg-purple-500/20 text-purple-400 font-medium">DV</span>{/if}
        {#if gHdr && !g.dovi}<span class="text-[10px] px-1 py-0.5 rounded bg-amber-500/20 text-amber-400 font-medium">{gHdr}</span>{/if}
        {#if gSize}<span class="text-[10px] text-[var(--text-secondary)] whitespace-nowrap">{gSize}</span>{/if}
      </div>
    {/if}
  </td>
  <!-- rating/res/size/status now live in the title-cell stat line above -->
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
          {#each DOWNLOAD_HOSTS as h}<option value={h.value}>{h.short}</option>{/each}
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
