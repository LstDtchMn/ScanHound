<script lang="ts">
  import Badge from './Badge.svelte';
  import { toggleSelect, selectedKeys, selectedDetail, markDownloaded, posterAspect, POSTER_ASPECT_CLASS, tileShowMeta } from '$lib/stores/results';
  import { settings } from '$lib/stores/settings';
  import { api } from '$lib/api/client';
  import { addToast } from '$lib/stores/notifications';
  import { downloadHost, activeDownload, downloadingTitles } from '$lib/stores/downloads';
  import { statusVariant, formatStatus, formatCount, DOWNLOAD_HOSTS } from '$lib/constants';
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

  // Overlay "Downloading" status for this item and all same-title siblings
  let effectiveStatus = $derived($downloadingTitles.has(item.title) ? 'downloading' : item.status);

  // Poster hover-to-enlarge state
  let posterHovered = $state(false);
  let enlargedStyle = $state('');

  function onPosterEnter(e: MouseEvent) {
    const r = (e.currentTarget as HTMLElement).getBoundingClientRect();
    const scale = 2.2;
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
      // Include release specs so a later "already grabbed" chip isn't blank.
      api.download(item.url, item.title, effectiveHost, item.year,
                   item.resolution || '', item.size || '', item.hdr || '', item.dovi ?? false)
        .catch(() => addToast('Error', 'Download failed', 'error'));
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
  class="relative min-w-0 bg-[var(--bg-secondary)] rounded-lg overflow-hidden border cursor-pointer group
    transition-[transform,box-shadow,border-color,background-color] duration-200 ease-out hover:shadow-lg hover:scale-[1.02]
    {selected ? 'border-[var(--accent)] bg-[var(--accent)]/10' : 'border-[var(--border)] hover:border-[var(--text-secondary)]'}
    {focused ? 'ring-2 ring-[var(--accent)] ring-offset-1 ring-offset-[var(--bg-primary)]' : ''}"
  onclick={() => selectedDetail.set(item)}
  role="button"
  tabindex="0"
  onkeydown={(e) => e.key === 'Enter' && selectedDetail.set(item)}
>
  <div class="{POSTER_ASPECT_CLASS[$posterAspect]} bg-[var(--bg-tertiary)] relative overflow-hidden">
    {#if item.poster_url}
      <img
        src={item.poster_url}
        alt={item.title}
        class="w-full h-full object-cover cursor-zoom-in"
        loading="lazy"
        onmouseenter={onPosterEnter}
        onmouseleave={onPosterLeave}
      />
      {#if posterHovered}
        <img
          src={item.poster_url}
          alt=""
          aria-hidden="true"
          style="position:fixed;{enlargedStyle}z-index:9999;pointer-events:none;border-radius:10px;object-fit:cover;box-shadow:0 25px 60px -8px rgba(0,0,0,0.8);"
          loading="eager"
        />
      {/if}
    {:else}
      <div class="flex flex-col items-center justify-center gap-2 h-full px-3 text-center bg-gradient-to-b from-[var(--bg-tertiary)] to-[color-mix(in_srgb,var(--bg-tertiary)_60%,black)]">
        <svg class="w-9 h-9 text-[var(--text-secondary)] opacity-40" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
          <rect x="3" y="7" width="18" height="14" rx="1.5"/>
          <path d="M3 7l2.2-3.6a1 1 0 0 1 .85-.4h11.9a1 1 0 0 1 .85.4L21 7"/>
          <path d="M7.5 3.3L9 7M13 3l1.8 4M3 11h18"/>
        </svg>
        <span class="text-[var(--text-secondary)] text-xs opacity-70 line-clamp-2">{item.title}</span>
      </div>
    {/if}

    <!-- Status badge — top right -->
    <div class="absolute top-1.5 right-1.5">
      <Badge label={formatStatus(effectiveStatus)} variant={statusVariant(effectiveStatus)} />
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

    <!-- Selection checkbox — top left; custom chip with an accessible native input underneath -->
    <div
      class="absolute top-1.5 left-1.5 w-6 h-6 rounded-full flex items-center justify-center transition-all
        {selected
          ? 'bg-[var(--accent)] opacity-100 scale-100'
          : 'bg-black/60 backdrop-blur-sm opacity-0 group-hover:opacity-100 focus-within:opacity-100 scale-90 group-hover:scale-100'}"
    >
      <svg
        class="w-3.5 h-3.5 text-white transition-opacity {selected ? 'opacity-100' : 'opacity-70'}"
        viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"
      >
        <path d="M3.5 8.5l3 3 6-7"/>
      </svg>
      <input
        type="checkbox"
        checked={selected}
        aria-label="{selected ? 'Deselect' : 'Select'} {item.title}"
        class="absolute inset-0 w-full h-full opacity-0 cursor-pointer"
        onclick={(e) => { e.stopPropagation(); toggleSelect(item.url); }}
      />
    </div>
  </div>

  <!-- Info section (hidden in poster-only mode; overlays stay on the poster) -->
  {#if $tileShowMeta}
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
        <span class="font-semibold text-[var(--accent)]">Plex:</span>
        {#each plexVersions as pv, i}
          {#if i > 0}<span class="text-[var(--text-secondary)] opacity-30">&middot;</span>{/if}
          <span class="inline-flex items-center gap-0.5 text-[var(--text-secondary)]">
            <Badge label={pv.res} variant={pv.res === '4K' ? 'warning' : 'default'} size="xs" />
            {#if pv.dovi}<Badge label="DV" variant="accent" size="xs" />{/if}
            {#if pv.hdr && !pv.dovi}<Badge label="HDR" variant="warning" size="xs" />{/if}
            {#if pv.size}<span class="opacity-60">{pv.size}GB</span>{/if}
          </span>
        {/each}
      </div>
    {/if}

    <!-- Previously grabbed (different resolution) -->
    {#if item.prior_grab}
      <div class="flex items-center gap-1 mt-0.5 text-[10px] text-amber-500 truncate" title="A different version of this title was already sent to JDownloader">
        <svg class="w-3 h-3 flex-shrink-0" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M8 3v7m0 0-3-3m3 3 3-3"/><rect x="2" y="11.5" width="12" height="2" rx="1"/></svg>
        <span class="font-medium">Grabbed:</span>
        <span>{item.prior_grab.resolution}</span>
        {#if item.prior_grab.size}<span class="opacity-75">· {item.prior_grab.size}</span>{/if}
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
          {#each DOWNLOAD_HOSTS as h}<option value={h.value}>{h.short}</option>{/each}
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
  {/if}
</div>
