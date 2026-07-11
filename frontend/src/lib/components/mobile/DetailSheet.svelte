<script lang="ts">
  import type { ScanResult } from '$lib/api/types';
  import { api } from '$lib/api/client';
  import { downloadHost } from '$lib/stores/downloads';
  import { addToast } from '$lib/stores/notifications';
  import { updateResultFromRescan } from '$lib/stores/results';
  import { copyResultLinks } from '$lib/resultActions';
  import { statusVariant, formatStatus, formatCount } from '$lib/constants';
  import Badge from '../Badge.svelte';
  import RtBadge from '../RtBadge.svelte';
  import { createDragTracker } from './gestures';
  import { success } from './haptics';

  interface Props {
    item: ScanResult;
    siblings?: ScanResult[];
    onclose: () => void;
    onselect?: (s: ScanResult) => void;
  }
  let { item, siblings = [], onclose, onselect }: Props = $props();

  /** Existing library copies, parsed from the same plex_versions JSON the
   *  desktop DetailPanel renders — [{res, dovi, hdr, size(GB)}]. This is the
   *  upgrade-decision context: what you already own, per version. */
  let plexVersions = $derived.by(() => {
    if (!item.plex_versions) return [] as { res?: string; dovi?: boolean; hdr?: boolean; size?: number }[];
    try {
      const v = JSON.parse(item.plex_versions);
      return Array.isArray(v) ? v : [];
    } catch {
      return [];
    }
  });

  let expanded = $state(false);
  let dragY = $state(0);
  let sheetEl = $state<HTMLDivElement>();
  let triggerEl: Element | null = null;
  const tracker = createDragTracker({ axis: 'y', threshold: 60 });

  function onHandleDown(e: PointerEvent) { tracker.start(e.clientX, e.clientY); }
  function onHandleMove(e: PointerEvent) {
    const s = tracker.move(e.clientX, e.clientY);
    if (s.locked === 'y') dragY = s.dy;
  }
  function onHandleUp() {
    const { committed, direction } = tracker.end();
    if (committed && direction === 'down') { expanded ? (expanded = false) : onclose(); }
    else if (committed && direction === 'up') expanded = true;
    dragY = 0;
  }

  function grab() {
    if (!item.url) return;
    api.download(item.url, item.title, $downloadHost, item.year,
                 item.resolution || '', item.size || '', item.hdr || '', item.dovi ?? false,
                 item.season)
      .then(() => {
        // "started" only — the download:complete (method=jdownloader) WS event
        // marks it grabbed once it truly reaches JDownloader; a failed send
        // leaves it Missing rather than falsely archived.
        success();
        addToast('Sending', item.title);
        onclose();
      })
      .catch(() => addToast('Error', 'Download failed', 'error'));
  }

  let rescanning = $state(false);
  async function rescanItem() {
    if (!item.url || rescanning) return;
    rescanning = true;
    try {
      const { item: fresh } = await api.rescanItem(item.url);
      updateResultFromRescan(item.url, fresh);
      addToast('Rescanned', `Refreshed metadata for ${fresh.title || item.title}`);
    } catch (e) {
      addToast('Error', e instanceof Error ? e.message : 'Rescan failed', 'error');
    } finally {
      rescanning = false;
    }
  }

  // Mount/unmount only — deliberately reads nothing reactive (not `item`) so
  // switching the detail shown while the sheet stays mounted (onselect on a
  // sibling swaps `item` without unmounting) never re-runs this and never
  // fires the restore-focus cleanup early. Mirrors DetailPanel.svelte's
  // established pattern for the same reason.
  $effect(() => {
    triggerEl = document.activeElement;
    if (sheetEl) requestAnimationFrame(() => sheetEl?.focus());
    return () => {
      if (triggerEl instanceof HTMLElement) triggerEl.focus();
    };
  });

  /** Focusable elements currently inside the sheet, in DOM/tab order. Queried
   *  live (not cached) since the set changes with `item` (siblings, actions)
   *  while the sheet stays mounted across a same-sheet item swap. */
  function focusableEls(): HTMLElement[] {
    if (!sheetEl) return [];
    return Array.from(
      sheetEl.querySelectorAll<HTMLElement>(
        'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])'
      )
    ).filter((el) => el.offsetParent !== null);
  }

  function handleTrapTab(e: KeyboardEvent) {
    if (e.key !== 'Tab') return;
    const els = focusableEls();
    if (els.length === 0) {
      e.preventDefault();
      sheetEl?.focus();
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
</script>

<svelte:window onkeydown={handleKeydown} />

<!-- Scrim -->
<div class="fixed inset-0 z-40 bg-[var(--bg-overlay)] md:hidden" onclick={onclose} role="presentation"></div>

<!-- Sheet -->
<div
  bind:this={sheetEl}
  tabindex="-1"
  role="dialog"
  aria-modal="true"
  aria-label="{item.title} details"
  class="fixed inset-x-0 bottom-0 z-50 md:hidden flex flex-col rounded-t-2xl border-t border-x border-[var(--border)]
    bg-[var(--bg-secondary)] shadow-2xl transition-[height] duration-200 outline-none"
  style="height: {expanded ? '92vh' : '55vh'}; transform: translateY({Math.max(dragY, 0)}px);
    padding-bottom: env(safe-area-inset-bottom);"
>
  <!-- Drag handle -->
  <div
    class="shrink-0 py-2 flex justify-center cursor-grab touch-none"
    onpointerdown={onHandleDown} onpointermove={onHandleMove} onpointerup={onHandleUp} onpointercancel={onHandleUp}
    role="presentation"
  >
    <div class="w-10 h-1 rounded-full bg-[var(--border)]"></div>
  </div>

  <!-- Content -->
  <div class="flex-1 min-h-0 overflow-y-auto px-4 pb-3">
    <div class="flex gap-3">
      {#if item.poster_url}
        <img src={item.poster_url} alt="" class="w-20 rounded-md shrink-0 self-start" />
      {/if}
      <div class="min-w-0">
        <h2 class="text-base font-bold text-[var(--text-primary)] leading-snug">
          {item.title}{#if item.season != null}<span class="text-[var(--accent)]"> · Season {item.season}</span>{/if}
        </h2>
        <p class="text-xs text-[var(--text-secondary)] mt-0.5 flex items-center flex-wrap gap-x-1">
          <span>{item.year || ''}{#if item.rating} · ★ {item.rating.toFixed(1)}{#if item.votes} ({formatCount(item.votes)}){/if}{/if}{#if item.size} · {item.size}{/if}</span>
          {#if item.rt_score != null}<span class="flex items-center">·&nbsp;<RtBadge score={item.rt_score} size="lg" /></span>{/if}
        </p>
        <div class="flex flex-wrap gap-1 mt-2">
          <Badge label={formatStatus(item.status)} variant={statusVariant(item.status)} />
          {#if item.resolution}<Badge label={item.resolution} />{/if}
          {#if item.dovi}<Badge label="DV" variant="info" />{/if}
          {#if item.hdr}<Badge label={item.hdr} />{/if}
        </div>
      </div>
    </div>

    <!-- In Library: what you already own (the upgrade-decision context) —
         placed above the fold so it's visible at half-height before grabbing.
         Same plex_info/plex_versions data the desktop DetailPanel shows. -->
    {#if item.plex_info && item.plex_info !== '-'}
      <div class="mt-3 p-2.5 rounded-lg bg-[var(--bg-tertiary)] border border-[var(--border)]">
        <h3 class="text-[10px] font-semibold text-[var(--text-secondary)] uppercase tracking-wider mb-1">In Library</h3>
        <p class="text-xs text-[var(--text-primary)]">{item.plex_info}</p>
        {#if plexVersions.length > 0}
          <div class="flex flex-wrap gap-1.5 mt-1.5">
            {#each plexVersions as pv}
              <div class="inline-flex items-center gap-1 px-2 py-1 rounded bg-[var(--bg-secondary)] border border-[var(--border)]">
                <span class="text-xs font-semibold {pv.res === '4K' ? 'text-yellow-500' : 'text-[var(--text-primary)]'}">{pv.res}</span>
                {#if pv.dovi}<span class="text-[10px] font-bold text-purple-400">DV</span>{/if}
                {#if pv.hdr && !pv.dovi}<span class="text-[10px] font-bold text-amber-400">HDR</span>{/if}
                {#if pv.size}<span class="text-[10px] text-[var(--text-secondary)]">{pv.size}GB</span>{/if}
              </div>
            {/each}
          </div>
        {/if}
      </div>
    {/if}

    <!-- A copy already grabbed (sent to JD) but not yet showing in Plex -->
    {#if item.prior_grab}
      <p class="mt-2 text-[11px] text-[var(--text-secondary)]">
        Already grabbed: <span class="font-medium text-[var(--text-primary)]">{item.prior_grab.resolution}</span>
        {#if item.prior_grab.dovi}<span class="font-bold text-purple-400"> DV</span>
        {:else if item.prior_grab.hdr}<span class="font-bold text-amber-400"> HDR</span>{/if}
        {#if item.prior_grab.size} · {item.prior_grab.size}{/if}
      </p>
    {/if}

    {#if item.description}
      <p class="text-xs text-[var(--text-secondary)] mt-3 leading-relaxed">{item.description}</p>
    {/if}

    {#if siblings.length > 1}
      <h3 class="text-xs font-semibold text-[var(--text-secondary)] mt-4 mb-1">Releases ({siblings.length})</h3>
      <div class="flex flex-col gap-1">
        {#each siblings as s (s.url)}
          <button
            class="flex items-center gap-2 px-2 py-1.5 rounded-md text-left text-xs
              {s.url === item.url ? 'bg-[var(--accent)]/15 border border-[var(--accent)]' : 'bg-[var(--bg-tertiary)] border border-transparent'}"
            onclick={() => onselect?.(s)}
          >
            <span class="font-medium text-[var(--text-primary)]">{s.resolution || '?'}</span>
            <span class="text-[var(--text-secondary)]">{s.size}</span>
            {#if s.dovi}<Badge label="DV" variant="info" size="xs" />{/if}
            <span class="flex-1"></span>
            <Badge label={formatStatus(s.status)} variant={statusVariant(s.status)} size="xs" />
          </button>
        {/each}
      </div>
    {/if}
  </div>

  <!-- Pinned action -->
  <div class="shrink-0 px-4 py-3 border-t border-[var(--border)] flex gap-2">
    {#if item.status === 'library'}
      <button class="flex-1 py-2.5 rounded-xl bg-[var(--bg-tertiary)] text-sm font-semibold text-[var(--text-primary)]"
        onclick={() => { copyResultLinks(item, $downloadHost); onclose(); }}>Copy links</button>
    {:else}
      <button class="flex-1 py-2.5 rounded-xl bg-[var(--accent)] text-sm font-semibold text-white" onclick={grab}>
        Grab{#if item.size}&nbsp;· {item.size}{/if}
      </button>
    {/if}
    <button
      onclick={rescanItem}
      disabled={rescanning}
      aria-label="Rescan this item"
      title="Re-fetch this page and refresh its poster/rating/genres"
      class="px-4 py-2.5 rounded-xl bg-[var(--bg-tertiary)] text-sm font-semibold text-[var(--text-primary)] disabled:opacity-50"
    >
      {rescanning ? 'Rescanning…' : 'Rescan'}
    </button>
  </div>
</div>
