<script lang="ts">
  import Badge from './Badge.svelte';
  import RtBadge from './RtBadge.svelte';
  import { posterAspect, POSTER_ASPECT_CLASS, tileShowMeta, phoneColumns } from '$lib/stores/results';
  import { isPhone } from '$lib/stores/viewport';
  import { settings } from '$lib/stores/settings';
  import { statusVariant, formatStatus } from '$lib/constants';
  import type { ScanResult } from '$lib/api/types';

  interface GroupFormats { res: string[]; dv: boolean; hdr: boolean; }
  interface StatusSummaryEntry { status: string; count: number }

  interface Props {
    title: string;
    items: ScanResult[];
    count: number;
    formats: GroupFormats;
    statusSummary: StatusSummaryEntry[];
    sizeRange: string;
    dateRange: string;
    onToggle: () => void;
  }
  let { title, items, count, formats, statusSummary, sizeRange, dateRange, onToggle }: Props = $props();

  let front = $derived(items[0]);
  let showRating = $derived($settings.show_rating ?? true);
  let big = $derived($isPhone && $phoneColumns === 1); // single-tile → bigger text

  function onKeydown(e: KeyboardEvent) {
    if (e.key === 'Enter' || e.key === ' ') {
      e.preventDefault();
      onToggle();
    }
  }
</script>

<!-- Stacked-poster card for a collapsed duplicate/multi-release title group.
     Sits as a normal grid cell (unlike the old full-width row it replaces) so
     duplicate groups no longer break the poster wall's rhythm. Clicking/Enter
     expands the group into individual ResultTiles (handled by the parent). -->
<div
  class="relative min-w-0 cursor-pointer group/tile outline-none"
  role="button"
  tabindex="0"
  aria-expanded="false"
  aria-label="{title} — {count} releases, expand"
  onclick={onToggle}
  onkeydown={onKeydown}
>
  <!-- Stack offsets: two poster "edges" peeking out top-right, pure CSS, no new deps -->
  <div
    class="absolute inset-0 rounded-lg bg-[var(--bg-tertiary)] border border-[var(--border)] shadow-sm"
    style="transform: translate(6px, -6px); z-index: 0;"
    aria-hidden="true"
  ></div>
  <div
    class="absolute inset-0 rounded-lg bg-[var(--bg-tertiary)] border border-[var(--border)] shadow-sm"
    style="transform: translate(3px, -3px); z-index: 1;"
    aria-hidden="true"
  ></div>

  <!-- Front card — same visual language as ResultTile (radius, border, hover scale/shadow) -->
  <div
    class="relative z-[2] bg-[var(--bg-secondary)] rounded-lg overflow-hidden border border-[var(--border)]
      transition-[transform,box-shadow,border-color] duration-200 ease-out
      group-hover/tile:shadow-lg group-hover/tile:scale-[1.02] group-hover/tile:border-[var(--text-secondary)]
      group-focus-within/tile:ring-2 group-focus-within/tile:ring-[var(--accent)] group-focus-within/tile:ring-offset-1 group-focus-within/tile:ring-offset-[var(--bg-primary)]"
  >
    <div class="{POSTER_ASPECT_CLASS[$posterAspect]} bg-[var(--bg-tertiary)] relative overflow-hidden">
      {#if front.poster_url}
        <img src={front.poster_url} alt={title} class="w-full h-full object-cover" loading="lazy" />
      {:else}
        <div class="flex flex-col items-center justify-center gap-2 h-full px-3 text-center bg-gradient-to-b from-[var(--bg-tertiary)] to-[color-mix(in_srgb,var(--bg-tertiary)_60%,black)]">
          <svg class="w-9 h-9 text-[var(--text-secondary)] opacity-40" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
            <rect x="3" y="7" width="18" height="14" rx="1.5"/>
            <path d="M3 7l2.2-3.6a1 1 0 0 1 .85-.4h11.9a1 1 0 0 1 .85.4L21 7"/>
            <path d="M7.5 3.3L9 7M13 3l1.8 4M3 11h18"/>
          </svg>
          <span class="text-[var(--text-secondary)] text-xs opacity-70 line-clamp-2">{title}</span>
        </div>
      {/if}

      <!-- Bottom scrim: same gradient wash as ResultTile -->
      {#if $tileShowMeta}
        <div class="pointer-events-none absolute inset-x-0 bottom-0 h-[45%]" style="background: linear-gradient(to top, rgba(0,0,0,.85), transparent);"></div>
      {/if}

      <!-- Count badge — top-left, always visible (this is the "what am I looking at" cue) -->
      <div class="absolute top-1.5 left-1.5 z-10">
        <Badge label="{count} releases" variant="info" size={big ? 'xl' : $isPhone ? 'lg' : 'sm'} />
      </div>

      {#if $tileShowMeta}
        <div class="absolute inset-x-0 bottom-0 z-10 p-2 pt-6">
          {#if $isPhone}
            <!-- Phone: always-visible facts (no hover state on touch — same
                 reasoning as ResultTile). rt_score is title-level so it's
                 valid to show from the front release. -->
            <div class="flex items-center gap-1.5 {big ? 'text-xl' : 'text-sm'} text-white/90 flex-nowrap overflow-hidden whitespace-nowrap mb-1 font-medium">
              {#if showRating && front.rating}<span class="shrink-0">&#9733; {front.rating.toFixed(1)}</span>{/if}
              {#if front.rt_score != null}<span class="shrink-0 flex items-center">&middot;&nbsp;<RtBadge score={front.rt_score} size={big ? 'xl' : 'lg'} /></span>{/if}
              {#if sizeRange}<span class="font-bold text-white shrink-0">&middot; {sizeRange}</span>{/if}
              {#if dateRange}<span class="truncate">&middot; {dateRange}</span>{/if}
            </div>
            {#if statusSummary.length}
              <div class="flex items-center gap-1 flex-nowrap overflow-hidden mb-1">
                {#each statusSummary as st}
                  <span class="shrink-0"><Badge label={`${st.count} ${formatStatus(st.status)}`} variant={statusVariant(st.status)} size={big ? 'xl' : 'lg'} /></span>
                {/each}
              </div>
            {/if}
          {:else}
            <!-- Desktop: unchanged hover-reveal secondary metadata. -->
            <div
              class="max-h-0 opacity-0 overflow-hidden
                group-hover/tile:max-h-48 group-hover/tile:opacity-100 group-focus-within/tile:max-h-48 group-focus-within/tile:opacity-100
                transition-all duration-200 ease-out"
            >
              <div class="flex items-center gap-1.5 text-[10px] text-white/85 flex-wrap mb-1">
                {#if showRating && front.rating}<span>&#9733; {front.rating.toFixed(1)}</span>{/if}
                {#if sizeRange}<span class="font-semibold text-white">{sizeRange}</span>{/if}
                {#if dateRange}<span>&middot; {dateRange}</span>{/if}
              </div>
              {#if statusSummary.length}
                <div class="flex items-center gap-1 flex-wrap mb-1">
                  {#each statusSummary as st}
                    <Badge label={`${st.count} ${formatStatus(st.status)}`} variant={statusVariant(st.status)} size="xs" />
                  {/each}
                </div>
              {/if}
            </div>
          {/if}

          <!-- Aggregate format chips (always visible, like ResultTile's DV/HDR row) -->
          <div class="flex items-center gap-1 mb-1 flex-wrap">
            {#each formats.res as r}<Badge label={r} size={big ? 'xl' : $isPhone ? 'lg' : 'xs'} />{/each}
            {#if formats.dv}<Badge label="DV" variant="accent" size={big ? 'xl' : $isPhone ? 'lg' : 'xs'} />{/if}
            {#if formats.hdr}<Badge label="HDR" variant="warning" size={big ? 'xl' : $isPhone ? 'lg' : 'xs'} />{/if}
          </div>
          <p class="{big ? 'text-2xl' : $isPhone ? 'text-base' : 'text-sm'} font-semibold text-white truncate leading-tight" title={title}>
            {title}{#if front.year}<span class="font-normal text-white/70">&nbsp;({front.year})</span>{/if}
          </p>
        </div>
      {/if}
    </div>
  </div>
</div>
