<script lang="ts">
  import {
    deckResults, results, selectedKeys, selectedDetail,
    dismissItem, restoreItem, toggleSelect, deselectAll, markDownloaded,
    deckNeedsMore, loadResults
  } from '$lib/stores/results';
  import { downloadHost } from '$lib/stores/downloads';
  import { api } from '$lib/api/client';
  import { addToast } from '$lib/stores/notifications';
  import { statusVariant, formatStatus, formatCount } from '$lib/constants';
  import { onDestroy } from 'svelte';
  import Badge from './Badge.svelte';

  const THRESHOLD = 90;   // px past which a release commits to an action
  const TAP_SLOP = 6;     // movement under this counts as a tap, not a drag

  let deck = $derived($deckResults);
  let top = $derived(deck[0] ?? null);

  // Top up the server-paged pool as the deck runs low, so swiping never
  // outpaces what's loaded. Re-runs as $deckResults.length shrinks (cards
  // consumed/selected) or grows (a page lands).
  $effect(() => {
    if (deckNeedsMore($deckResults.length)) loadResults(false);
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

  // Undo stack (most recent last)
  type Swipe = { url: string; title: string; action: 'select' | 'skip' };
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
      // Treated as a tap → open details
      const item = top;
      dx = 0;
      dy = 0;
      if (item) selectedDetail.set(item);
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
    const item = top;
    if (!item) return;
    animating = true;
    const dir = action === 'select' ? 1 : -1;
    // Fly off-screen, then mutate the stores so the card leaves the deck.
    dx = dir * (typeof window !== 'undefined' ? window.innerWidth : 800);
    dy = dy * 1.2;
    if (actionTimer) clearTimeout(actionTimer);
    actionTimer = setTimeout(() => {
      const entry: Swipe = { url: item.url, title: item.title, action };
      undoStack = [...undoStack.slice(-9), entry];
      if (action === 'select') {
        toggleSelect(item.url);
      } else {
        // If the dismiss didn't persist, the store already reverted the card
        // back into the deck on its own — drop the now-stale undo entry so
        // "Undo" doesn't sit there as a no-op.
        dismissItem(item.url, item.title).then((persisted) => {
          if (!persisted) undoStack = undoStack.filter((s) => s !== entry);
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
    if (last.action === 'select') toggleSelect(last.url); // deselect → reappears
    else restoreItem(last.url);
  }

  async function downloadSelected() {
    if (selectedItems.length === 0 || downloading) return;
    downloading = true;
    try {
      const urls = selectedItems.map((i) => i.url);
      await api.downloadBatch(
        selectedItems.map((i) => ({ url: i.url, title: i.title, year: i.year, resolution: i.resolution, size: i.size, hdr: i.hdr, dovi: i.dovi })),
        $downloadHost
      );
      addToast('Download', `Sending ${selectedItems.length} item(s) to JDownloader…`);
      // Mark as downloaded (status → non-actionable) so they leave the deck for
      // good, then clear the tray. Without this, deselecting would re-surface
      // the just-downloaded cards.
      markDownloaded(urls);
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
        {#each deck.slice(0, 3) as item, i (item.url)}
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
            {#if item.poster_url}
              <img src={item.poster_url} alt={item.title} class="absolute inset-0 w-full h-full object-cover" draggable="false" />
            {:else}
              <div class="absolute inset-0 flex items-center justify-center text-[var(--text-secondary)]">No poster</div>
            {/if}
            <div class="absolute inset-0 bg-gradient-to-t from-black/85 via-black/20 to-black/40"></div>

            <!-- Status badge -->
            <div class="absolute top-3 left-3">
              <Badge label={formatStatus(item.status)} variant={statusVariant(item.status)} />
            </div>
            <div class="absolute top-3 right-3 flex gap-1">
              {#if item.dovi}<Badge label="DV" variant="accent" />{/if}
              {#if item.hdr && !item.dovi}<Badge label="HDR" variant="warning" />{/if}
            </div>

            <!-- Swipe-intent overlays (top card only) -->
            {#if i === 0}
              <div class="absolute top-6 left-6 px-3 py-1 rounded-lg border-2 border-[var(--success)] text-[var(--success)] font-extrabold text-2xl rotate-[-12deg]" style="opacity: {intent === 'select' ? overlayOpacity : 0}">ADD</div>
              <div class="absolute top-6 right-6 px-3 py-1 rounded-lg border-2 border-[var(--error)] text-[var(--error)] font-extrabold text-2xl rotate-[12deg]" style="opacity: {intent === 'skip' ? overlayOpacity : 0}">SKIP</div>
            {/if}

            <!-- Info -->
            <div class="absolute bottom-0 left-0 right-0 p-4 text-white">
              <p class="text-lg font-bold leading-tight">{item.title}{#if item.year}<span class="font-normal opacity-80"> ({item.year})</span>{/if}</p>
              <div class="flex flex-wrap items-center gap-x-2 gap-y-0.5 mt-1 text-sm opacity-90">
                {#if item.resolution}<span class="font-semibold">{item.resolution}</span>{/if}
                {#if item.size}<span>· {item.size}</span>{/if}
                {#if item.rating}<span>· ★ {item.rating.toFixed(1)}{#if item.votes}<span class="opacity-70 text-xs"> ({formatCount(item.votes)})</span>{/if}</span>{/if}
              </div>
              {#if item.genres?.length}
                <p class="text-xs opacity-70 mt-0.5 truncate">{item.genres.slice(0, 3).join(' · ')}</p>
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
        onclick={() => top && selectedDetail.set(top)}
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
