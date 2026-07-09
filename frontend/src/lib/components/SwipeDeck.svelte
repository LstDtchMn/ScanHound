<script lang="ts">
  import {
    deckGroups, results, selectedKeys, selectedDetail,
    dismissItem, restoreItem, toggleSelect, deselectAll,
    deckNeedsMore, loadResults
  } from '$lib/stores/results';
  import { downloadHost } from '$lib/stores/downloads';
  import { api } from '$lib/api/client';
  import { addToast } from '$lib/stores/notifications';
  import { statusVariant, formatStatus, formatCount } from '$lib/constants';
  import { onDestroy } from 'svelte';
  import Badge from './Badge.svelte';
  import RtBadge from './RtBadge.svelte';
  import type { ScanResult } from '$lib/api/types';

  /** Distinct Plex library copies for a card — deduped by res+size (so two
   *  same-res different-size copies both show), matching ResultTile. */
  interface PlexVersion { res: string; hdr: boolean; dovi: boolean; size: number | string }
  function plexVersionsOf(item: ScanResult): PlexVersion[] {
    try {
      const raw = JSON.parse(item.plex_versions || '[]');
      if (!Array.isArray(raw) || raw.length === 0) return [];
      const seen = new Set<string>();
      return raw.filter((v: PlexVersion) => {
        const key = `${v.res}|${v.size}|${v.hdr}|${v.dovi}`;
        if (seen.has(key)) return false;
        seen.add(key);
        return true;
      });
    } catch { return []; }
  }

  const THRESHOLD = 90;   // px past which a release commits to an action
  const TAP_SLOP = 6;     // movement under this counts as a tap, not a drag

  // One card per TITLE (group), not per release, so you never swipe through
  // duplicates. `resolvedKeys` hides a group the instant it's acted on (its
  // other releases stay actionable but the whole title leaves the deck).
  let resolvedKeys = $state(new Set<string>());
  let groups = $derived($deckGroups.filter((g) => !resolvedKeys.has(g.key)));
  let top = $derived(groups[0] ?? null);

  // Which release of the top group will be grabbed (default = best). Reset
  // whenever the top card changes to a different title.
  let selectedReleaseUrl = $state<string | null>(null);
  let prevTopKey = '';
  $effect(() => {
    if (top && top.key !== prevTopKey) { prevTopKey = top.key; selectedReleaseUrl = top.best.url; }
  });
  let selectedRelease = $derived(
    top ? (top.releases.find((r) => r.url === selectedReleaseUrl) ?? top.best) : null
  );

  // Top up the server-paged pool as the deck runs low (counts GROUPS now).
  $effect(() => {
    if (deckNeedsMore(groups.length)) loadResults(false);
  });

  // Top-card drag state
  let dx = $state(0);
  let dy = $state(0);
  let dragging = $state(false);
  let animating = $state(false);
  let startX = 0;
  let startY = 0;

  let intent = $derived(dx > 30 ? 'select' : dx < -30 ? 'skip' : null);
  let overlayOpacity = $derived(Math.min(Math.abs(dx) / THRESHOLD, 1));

  // Undo stack (most recent last). A group-level action: 'select' queued the
  // chosen release; 'skip' dismissed every release of the title.
  type Swipe = { key: string; title: string; action: 'select' | 'skip'; selectedUrl?: string; urls: string[] };
  let undoStack = $state<Swipe[]>([]);

  // Selected items (pulled from the full result set — selected cards leave the deck)
  let selectedItems = $derived($results.filter((i) => i.url && $selectedKeys.has(i.url)));
  let downloading = $state(false);

  // Pending fly-off / spring-back timer, so we can cancel it if another action
  // starts or the component unmounts mid-animation.
  let actionTimer: ReturnType<typeof setTimeout> | null = null;
  onDestroy(() => {
    if (actionTimer) clearTimeout(actionTimer);
  });

  function onPointerDown(e: PointerEvent) {
    if (!top || animating) return;
    dragging = true;
    startX = e.clientX;
    startY = e.clientY;
    (e.currentTarget as HTMLElement).setPointerCapture(e.pointerId);
  }

  function onPointerMove(e: PointerEvent) {
    if (!dragging) return;
    dx = e.clientX - startX;
    dy = e.clientY - startY;
  }

  function onPointerUp() {
    if (!dragging) return;
    dragging = false;
    const moved = Math.hypot(dx, dy);
    if (moved < TAP_SLOP) {
      // Treated as a tap → open details for the currently-selected release
      const rel = selectedRelease;
      dx = 0;
      dy = 0;
      if (rel) selectedDetail.set(rel);
      return;
    }
    if (dx > THRESHOLD) commit('select');
    else if (dx < -THRESHOLD) commit('skip');
    else springBack();
  }

  function springBack() {
    animating = true;
    dx = 0;
    dy = 0;
    if (actionTimer) clearTimeout(actionTimer);
    actionTimer = setTimeout(() => {
      animating = false;
      actionTimer = null;
    }, 240);
  }

  function commit(action: 'select' | 'skip') {
    // Guard re-entry: an in-flight animation must finish before the next action,
    // otherwise a rapid double-tap on the buttons commits the same card twice.
    if (animating) return;
    const group = top;
    const chosen = selectedRelease;
    if (!group) return;
    animating = true;
    const dir = action === 'select' ? 1 : -1;
    // Fly off-screen, then mutate the stores so the card leaves the deck.
    dx = dir * (typeof window !== 'undefined' ? window.innerWidth : 800);
    dy = dy * 1.2;
    if (actionTimer) clearTimeout(actionTimer);
    actionTimer = setTimeout(() => {
      const urls = group.releases.map((r) => r.url);
      const entry: Swipe = { key: group.key, title: group.title, action, selectedUrl: chosen?.url, urls };
      undoStack = [...undoStack.slice(-9), entry];
      // Resolve the whole title so it leaves the deck (its other releases stay
      // actionable in the wall but won't re-offer here).
      resolvedKeys = new Set(resolvedKeys).add(group.key);
      if (action === 'select') {
        if (chosen?.url) toggleSelect(chosen.url);  // queue the chosen release in the tray
      } else {
        // Skip the whole title → dismiss every release. If NONE persisted
        // (offline), un-resolve so it comes back and drop the stale undo entry.
        Promise.all(
          group.releases.map((r) =>
            dismissItem(r.url, group.title, {
              group_key: r.group_key,
              resolution: r.resolution,
              dovi: r.dovi
            })
          )
        ).then((res) => {
          if (!res.some(Boolean)) {
            undoStack = undoStack.filter((s) => s !== entry);
            resolvedKeys = new Set([...resolvedKeys].filter((k) => k !== group.key));
          }
        });
      }
      dx = 0;
      dy = 0;
      animating = false;
      actionTimer = null;
    }, 220);
  }

  function undo() {
    const last = undoStack[undoStack.length - 1];
    if (!last) return;
    undoStack = undoStack.slice(0, -1);
    resolvedKeys = new Set([...resolvedKeys].filter((k) => k !== last.key)); // un-hide the title
    if (last.action === 'select') {
      if (last.selectedUrl) toggleSelect(last.selectedUrl); // deselect the queued release
    } else {
      last.urls.forEach((u) => restoreItem(u)); // un-dismiss every release
    }
  }

  async function downloadSelected() {
    if (selectedItems.length === 0 || downloading) return;
    downloading = true;
    try {
      await api.downloadBatch(
        selectedItems.map((i) => ({ url: i.url, title: i.title, year: i.year, season: i.season, resolution: i.resolution, size: i.size, hdr: i.hdr, dovi: i.dovi })),
        $downloadHost
      );
      addToast('Download', `Sending ${selectedItems.length} item(s) to JDownloader…`);
      // Do NOT optimistically mark these downloaded: the batch endpoint only
      // returns "started". Each item is marked grabbed by the per-item
      // download:complete (method=jdownloader) WS event once it actually reaches
      // JDownloader; items that fail (Cloudflare block, dead source) correctly
      // stay Missing instead of being falsely archived.
      deselectAll();
    } catch (e) {
      addToast('Error', e instanceof Error ? e.message : 'Failed to start downloads', 'error');
    } finally {
      downloading = false;
    }
  }

  // Style for a card at stack position `i` (0 = top, draggable).
  function cardStyle(i: number): string {
    if (i === 0) {
      const rot = dx * 0.04;
      const transition = dragging ? 'none' : 'transform 0.22s ease-out';
      return `transform: translate(${dx}px, ${dy}px) rotate(${rot}deg); transition: ${transition}; z-index: 30;`;
    }
    const scale = 1 - i * 0.05;
    const offset = i * 10;
    return `transform: translateY(${offset}px) scale(${scale}); z-index: ${30 - i}; transition: transform 0.22s ease-out;`;
  }
</script>

<div class="flex flex-col h-full">
  <!-- Deck area -->
  <div class="relative flex-1 flex items-center justify-center px-4 py-3 overflow-hidden select-none">
    {#if top}
      <!-- Undo -->
      {#if undoStack.length > 0}
        <button
          onclick={undo}
          class="absolute top-3 right-4 z-40 px-3 py-1.5 rounded-full text-xs font-medium bg-[var(--bg-secondary)] border border-[var(--border)] text-[var(--text-secondary)] hover:text-[var(--text-primary)] shadow-lg"
          title="Undo last swipe"
        >↶ Undo</button>
      {/if}

      <div class="relative w-full max-w-sm aspect-[2/3]">
        {#each groups.slice(0, 3) as group, i (group.key)}
          {@const info = group.best}
          {@const shown = i === 0 ? (selectedRelease ?? group.best) : group.best}
          <!-- svelte-ignore a11y_no_static_element_interactions -->
          <div
            class="absolute inset-0 rounded-2xl overflow-hidden border border-[var(--border)] bg-[var(--bg-secondary)] shadow-xl {i === 0 ? 'cursor-grab active:cursor-grabbing touch-none' : 'pointer-events-none'}"
            style={cardStyle(i)}
            onpointerdown={i === 0 ? onPointerDown : undefined}
            onpointermove={i === 0 ? onPointerMove : undefined}
            onpointerup={i === 0 ? onPointerUp : undefined}
            onpointercancel={i === 0 ? onPointerUp : undefined}
          >
            <!-- Poster -->
            {#if info.poster_url}
              <img src={info.poster_url} alt={info.title} class="absolute inset-0 w-full h-full object-cover" draggable="false" />
            {:else}
              <div class="absolute inset-0 flex items-center justify-center text-[var(--text-secondary)]">No poster</div>
            {/if}
            <div class="absolute inset-0 bg-gradient-to-t from-black/85 via-black/20 to-black/40"></div>

            <!-- Status badge (of the selected release) + version count -->
            <div class="absolute top-3 left-3 flex items-center gap-1.5">
              <Badge label={formatStatus(shown.status)} variant={statusVariant(shown.status)} size="xl" />
              {#if group.releases.length > 1}<Badge label="{group.releases.length} versions" variant="info" size="lg" />{/if}
            </div>
            <div class="absolute top-3 right-3 flex gap-1">
              {#if shown.dovi}<Badge label="DV" variant="accent" size="xl" />{/if}
              {#if shown.hdr && !shown.dovi}<Badge label="HDR" variant="warning" size="xl" />{/if}
            </div>

            <!-- Swipe-intent overlays (top card only) -->
            {#if i === 0}
              <div class="absolute top-6 left-6 px-3 py-1 rounded-lg border-2 border-[var(--success)] text-[var(--success)] font-extrabold text-2xl rotate-[-12deg]" style="opacity: {intent === 'select' ? overlayOpacity : 0}">ADD</div>
              <div class="absolute top-6 right-6 px-3 py-1 rounded-lg border-2 border-[var(--error)] text-[var(--error)] font-extrabold text-2xl rotate-[12deg]" style="opacity: {intent === 'skip' ? overlayOpacity : 0}">SKIP</div>
            {/if}

            <!-- Info — sized big for arm's-length reading in the deck. -->
            <div class="absolute bottom-0 left-0 right-0 p-4 text-white">
              <p class="text-3xl font-bold leading-tight">{info.title}{#if info.season != null}<span class="text-[var(--accent)]"> S{String(info.season).padStart(2, '0')}</span>{/if}{#if info.year}<span class="font-normal opacity-80"> ({info.year})</span>{/if}</p>
              <div class="flex flex-wrap items-center gap-x-2.5 gap-y-1 mt-2 text-xl opacity-95 font-medium">
                {#if shown.resolution}<span class="font-bold">{shown.resolution}</span>{/if}
                {#if shown.size}<span>· {shown.size}</span>{/if}
                {#if info.rating}<span>· ★ {info.rating.toFixed(1)}{#if info.votes}<span class="opacity-70"> ({formatCount(info.votes)})</span>{/if}</span>{/if}
                {#if info.rt_score != null}<span class="flex items-center">·&nbsp;<RtBadge score={info.rt_score} size="xl" /></span>{/if}
              </div>

              <!-- Quality picker (top card only, when there's a choice). Tap a
                   chip to pick which version to grab; the chosen one is what a
                   right-swipe queues. stopPropagation so tapping doesn't drag. -->
              {#if i === 0 && group.releases.length > 1}
                <div class="flex items-center gap-2 mt-2.5 overflow-x-auto whitespace-nowrap scrollbar-none pb-0.5">
                  {#each group.releases as rel (rel.url)}
                    <button
                      onpointerdown={(e) => e.stopPropagation()}
                      onclick={(e) => { e.stopPropagation(); selectedReleaseUrl = rel.url; }}
                      class="shrink-0 px-3 py-1.5 rounded-lg text-base font-semibold border-2 transition-colors
                        {rel.url === selectedReleaseUrl
                          ? 'bg-[var(--accent)] border-[var(--accent)] text-white'
                          : 'bg-black/40 border-white/30 text-white/90'}"
                    >
                      {rel.resolution || '?'}{#if rel.dovi} DV{:else if rel.hdr && rel.hdr !== 'SDR'} HDR{/if}{#if rel.size} · {rel.size}{/if}
                    </button>
                  {/each}
                </div>
              {/if}

              <!-- Ownership context: what you already have in Plex + any prior grab. -->
              {#if plexVersionsOf(info).length > 0 || info.prior_grab}
                <div class="flex items-center gap-2 mt-1.5 text-lg overflow-x-auto whitespace-nowrap scrollbar-none">
                  {#if plexVersionsOf(info).length > 0}
                    <span class="shrink-0 font-semibold text-[var(--accent)]">Plex:</span>
                    {#each plexVersionsOf(info) as pv, j}
                      {#if j > 0}<span class="text-white/30 shrink-0">·</span>{/if}
                      <span class="inline-flex items-center gap-1 shrink-0">
                        <span class="font-bold {pv.res === '4K' ? 'text-yellow-400' : 'text-white/90'}">{pv.res}</span>
                        {#if pv.dovi}<span class="font-bold text-purple-300">DV</span>{:else if pv.hdr}<span class="font-bold text-amber-300">HDR</span>{/if}
                        {#if pv.size}<span class="text-white/60">{pv.size}GB</span>{/if}
                      </span>
                    {/each}
                  {/if}
                  {#if info.prior_grab}
                    <span class="shrink-0 inline-flex items-center gap-1 text-amber-400 font-semibold" title="A different version was already sent to JDownloader">↓ Grabbed {info.prior_grab.resolution}{#if info.prior_grab.size} <span class="font-normal text-amber-400/80">· {info.prior_grab.size}</span>{/if}</span>
                  {/if}
                </div>
              {/if}
              {#if info.genres?.length}
                <p class="text-base opacity-70 mt-1 truncate">{info.genres.slice(0, 3).join(' · ')}</p>
              {/if}
            </div>
          </div>
        {/each}
      </div>
    {:else}
      <div class="flex flex-col items-center gap-3 text-center px-6">
        <div class="text-4xl">🎉</div>
        <p class="text-sm font-medium text-[var(--text-primary)]">All caught up</p>
        <p class="text-xs text-[var(--text-secondary)] max-w-xs">No more items to triage. Run a scan or adjust filters to find more. Selected items are queued below.</p>
      </div>
    {/if}
  </div>

  <!-- Action buttons -->
  {#if top}
    <div class="flex items-center justify-center gap-6 py-3">
      <button
        onclick={() => commit('skip')}
        disabled={animating}
        class="w-14 h-14 rounded-full flex items-center justify-center text-2xl bg-[var(--bg-secondary)] border-2 border-[var(--error)] text-[var(--error)] shadow-lg hover:bg-[var(--error)]/10 active:scale-95 transition disabled:opacity-50"
        aria-label="Skip"
        title="Skip (swipe left)"
      >✕</button>
      <button
        onclick={() => selectedRelease && selectedDetail.set(selectedRelease)}
        class="w-11 h-11 rounded-full flex items-center justify-center bg-[var(--bg-secondary)] border-2 border-[var(--border)] text-[var(--text-secondary)] shadow hover:text-[var(--text-primary)] active:scale-95 transition"
        aria-label="Details"
        title="Details"
      >ℹ</button>
      <button
        onclick={() => commit('select')}
        disabled={animating}
        class="w-14 h-14 rounded-full flex items-center justify-center text-2xl bg-[var(--bg-secondary)] border-2 border-[var(--success)] text-[var(--success)] shadow-lg hover:bg-[var(--success)]/10 active:scale-95 transition disabled:opacity-50"
        aria-label="Add to selection"
        title="Add to selection (swipe right)"
      >✓</button>
    </div>
  {/if}

  <!-- Selection tray footer -->
  <div class="flex items-center gap-3 px-4 py-3 border-t border-[var(--border)] bg-[var(--bg-secondary)]" style="padding-bottom: max(0.75rem, env(safe-area-inset-bottom));">
    <div class="flex-1 text-sm">
      {#if selectedItems.length > 0}
        <span class="font-semibold text-[var(--text-primary)]">{selectedItems.length}</span>
        <span class="text-[var(--text-secondary)]"> selected</span>
      {:else}
        <span class="text-[var(--text-secondary)]">Swipe right to add, left to skip</span>
      {/if}
    </div>
    {#if selectedItems.length > 0}
      <button
        onclick={() => deselectAll()}
        class="px-3 py-2 rounded-lg text-sm text-[var(--text-secondary)] hover:bg-[var(--bg-tertiary)] transition"
      >Clear</button>
    {/if}
    <button
      onclick={downloadSelected}
      disabled={selectedItems.length === 0 || downloading}
      class="px-4 py-2 rounded-lg text-sm font-semibold text-white bg-[var(--accent)] hover:opacity-90 disabled:opacity-40 transition flex items-center gap-1.5"
    >
      {downloading ? 'Sending…' : `⬇ Download ${selectedItems.length || ''}`.trim()}
    </button>
  </div>
</div>
