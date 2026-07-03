<script lang="ts">
  import Badge from './Badge.svelte';
  import { toggleSelect, selectedKeys, selectedDetail, posterAspect, POSTER_ASPECT_CLASS, tileShowMeta } from '$lib/stores/results';
  import { settings } from '$lib/stores/settings';
  import { api } from '$lib/api/client';
  import { addToast } from '$lib/stores/notifications';
  import { downloadHost, activeDownload, downloadingTitles } from '$lib/stores/downloads';
  import { statusVariant, formatStatus, formatCount } from '$lib/constants';
  import type { ScanResult } from '$lib/api/types';
  import { fly } from 'svelte/transition';

  let showRating = $derived($settings.show_rating ?? true);
  let showVotes = $derived($settings.show_votes ?? true);
  let showGenres = $derived($settings.show_genres ?? true);

  interface Props {
    item: ScanResult;
    focused?: boolean;
    onmore?: () => void;
  }
  let { item, focused = false, onmore }: Props = $props();

  // Select by unique url, not group_key (same-title releases share group_key)
  let selected = $derived($selectedKeys.has(item.url));

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
      api.download(item.url, item.title, $downloadHost, item.year,
                   item.resolution || '', item.size || '', item.hdr || '', item.dovi ?? false)
        .catch(() => addToast('Error', 'Download failed', 'error'));
    }
  }
</script>

<div
  transition:fly={{ y: 10, duration: 200 }}
  class="relative min-w-0 bg-[var(--bg-secondary)] rounded-lg overflow-hidden border cursor-pointer group
    transition-[transform,box-shadow,border-color,background-color] duration-200 ease-out hover:shadow-lg hover:scale-[1.02]
    {selected ? 'border-[var(--accent)] bg-[var(--accent)]/10' : 'border-[var(--border)] hover:border-[var(--text-secondary)]'}
    {focused ? 'ring-2 ring-[var(--accent)] ring-offset-1 ring-offset-[var(--bg-primary)]' : ''}"
  data-focused={focused}
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

    <!-- Bottom scrim: gradient wash behind title/meta + chips, keeps them legible over any poster art -->
    {#if $tileShowMeta}
      <div class="pointer-events-none absolute inset-x-0 bottom-0 h-[45%]" style="background: linear-gradient(to top, rgba(0,0,0,.85), transparent);"></div>
    {/if}

    <!-- Status badge — top right -->
    <div class="absolute top-1.5 right-1.5 z-10">
      <Badge label={formatStatus(effectiveStatus)} variant={statusVariant(effectiveStatus)} />
    </div>

    <!-- Selection checkbox — top left; custom chip with an accessible native input underneath -->
    <div
      class="absolute top-1.5 left-1.5 z-10 w-6 h-6 rounded-full flex items-center justify-center transition-all
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

    <!-- Mobile actions trigger -->
    {#if onmore}
      <button
        onclick={(e) => { e.stopPropagation(); onmore?.(); }}
        aria-label="Actions"
        class="md:hidden absolute bottom-1.5 right-1.5 z-10 w-8 h-8 rounded-full bg-black/55 text-white flex items-center justify-center text-lg leading-none"
      >⋯</button>
    {/if}

    <!-- Primary action: single floating Download pill, Jellyfin-style — reveals on hover/focus.
         Everything else (copy links, open source, copy url, IMDb, Plex, watchlist) lives in the
         context-menu (right-click) / mobile action sheet ("⋯"), both already wired by the parent. -->
    {#if item.url}
      <button
        onclick={handleDownload}
        disabled={isDownloading}
        aria-label="Download {item.title} ({$downloadHost})"
        title="Send to JDownloader ({$downloadHost})"
        class="absolute z-10 top-1/2 left-1/2 -translate-x-1/2 -translate-y-1/2 w-11 h-11 rounded-full
          bg-[var(--accent)] text-white shadow-lg flex items-center justify-center text-base
          opacity-0 scale-90 pointer-events-none
          group-hover:opacity-100 group-hover:scale-100 group-hover:pointer-events-auto
          group-focus-within:opacity-100 group-focus-within:scale-100 group-focus-within:pointer-events-auto
          focus:opacity-100 focus:scale-100 focus:pointer-events-auto
          hover:opacity-100 hover:scale-105
          transition-all duration-150 ease-out disabled:opacity-80"
      >{#if isDownloading}<span class="inline-block animate-spin">⟳</span>{:else}&#8595;{/if}</button>
    {/if}

    <!-- Bottom scrim content: DV/HDR chips (left) + title/year (stacked above), hover-reveal secondary meta -->
    {#if $tileShowMeta}
      <div class="absolute inset-x-0 bottom-0 z-10 p-2 pt-6">
        <!-- Hover-reveal secondary metadata: rating/RT/size/genres/plex-versions/prior-grab.
             Not lost — this is a relocation; the same data is always available in the detail panel. -->
        <div
          class="max-h-0 opacity-0 overflow-hidden
            group-hover:max-h-48 group-hover:opacity-100 group-focus-within:max-h-48 group-focus-within:opacity-100
            transition-all duration-200 ease-out"
        >
          <div class="flex items-center gap-1.5 text-[10px] text-white/85 flex-wrap mb-1">
            {#if item.resolution}<span class="font-semibold text-white">{item.resolution}</span>{/if}
            {#if item.size}<span>&middot; {item.size}</span>{/if}
            {#if showRating && item.rating}<span>&middot; &#9733; {item.rating.toFixed(1)}{#if showVotes && item.votes}<span class="opacity-70"> ({formatCount(item.votes)})</span>{/if}</span>{/if}
            {#if item.rt_score}<span>&middot; RT {item.rt_score}%</span>{/if}
          </div>
          {#if showGenres && item.genres?.length}
            <div class="text-[10px] text-white/70 truncate mb-1">{item.genres.slice(0, 3).join(', ')}</div>
          {/if}
          {#if item.posted_date || (item.language && item.language !== 'English')}
            <div class="flex items-center gap-1.5 text-[10px] text-white/70 truncate mb-1">
              {#if item.posted_date}<span class="opacity-80">{item.posted_date}</span>{/if}
              {#if item.language && item.language !== 'English'}<span class="opacity-60">&middot; {item.language}</span>{/if}
            </div>
          {/if}
          {#if plexVersions.length > 0}
            <div class="flex items-center gap-1 flex-wrap text-[9px] mb-1">
              <span class="font-semibold text-[var(--accent)]">Plex:</span>
              {#each plexVersions as pv, i}
                {#if i > 0}<span class="text-white/30">&middot;</span>{/if}
                <span class="inline-flex items-center gap-0.5 text-white/80">
                  <Badge label={pv.res} variant={pv.res === '4K' ? 'warning' : 'default'} size="xs" />
                  {#if pv.dovi}<Badge label="DV" variant="accent" size="xs" />{/if}
                  {#if pv.hdr && !pv.dovi}<Badge label="HDR" variant="warning" size="xs" />{/if}
                  {#if pv.size}<span class="opacity-70">{pv.size}GB</span>{/if}
                </span>
              {/each}
            </div>
          {/if}
          {#if item.prior_grab}
            <div class="flex items-center gap-1 text-[9px] text-amber-400 truncate mb-1" title="A different version of this title was already sent to JDownloader">
              <span class="font-medium">Grabbed:</span>
              <span>{item.prior_grab.resolution}</span>
              {#if item.prior_grab.size}<span class="opacity-75">&middot; {item.prior_grab.size}</span>{/if}
            </div>
          {/if}
        </div>

        <!-- DV/HDR chips (bottom-left) + Title/year (always visible, sits at the very bottom of the scrim) -->
        <div class="flex items-center gap-1 mb-1">
          {#if item.dovi}<Badge label="DV" variant="accent" size="xs" />{/if}
          {#if item.hdr && !item.dovi}<Badge label="HDR" variant="warning" size="xs" />{/if}
        </div>
        <p class="text-sm font-semibold text-white truncate leading-tight" title={item.title}>
          {item.title}{#if item.year}<span class="font-normal text-white/70">&nbsp;({item.year})</span>{/if}
        </p>
      </div>
    {/if}
  </div>
</div>
