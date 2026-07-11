<script lang="ts">
  import RenameReviewCard from './RenameReviewCard.svelte';
  import RematchModal from './RematchModal.svelte';
  import { api } from '$lib/api/client';
  import { addToast } from '$lib/stores/notifications';
  import {
    applyJob, deleteJob, acceptCombinedJob, acceptCorrectionJob, refreshRenames, applyActive
  } from '$lib/stores/renames';
  import { deckQueue, partitionJobs, type ReviewScope } from '$lib/renames/review';
  import { createDragTracker } from '$lib/components/mobile/gestures';
  import { tap } from '$lib/components/mobile/haptics';
  import type { RenameJob } from '$lib/api/types';

  let {
    jobs,
    initialScope = 'needsReview',
    onClose
  }: {
    jobs: RenameJob[];
    initialScope?: ReviewScope;
    onClose: () => void;
  } = $props();

  // svelte-ignore state_referenced_locally
  let scope = $state<ReviewScope>(initialScope);
  let index = $state(0);
  let busyId = $state<number | null>(null);
  let rematchJob = $state<RenameJob | null>(null);
  let applyAllBusy = $state(false);

  let queue = $derived(deckQueue(jobs, scope));
  let counts = $derived(partitionJobs(jobs));
  let current = $derived(queue[index] ?? null);

  // Keep index in range as the queue shrinks — a resolving action removes the
  // job from `jobs` via the rename:job WS upsert (reactive), which shrinks
  // `queue` and auto-advances onto the next item at the same index.
  $effect(() => {
    if (index >= queue.length) index = Math.max(0, queue.length - 1);
  });

  async function act(fn: () => Promise<void>, ok: string) {
    if (!current) return;
    busyId = current.id;
    try {
      await fn();
      addToast('Renames', ok);
    } catch (e) {
      addToast('Renames', e instanceof Error ? e.message : 'Action failed', 'error');
    } finally {
      busyId = null;
    }
  }

  function prev() {
    if (busyId !== null) return;
    if (index > 0) index -= 1;
  }
  function next() {
    if (busyId !== null) return;
    if (index < queue.length - 1) index += 1;
  }
  function skip() {
    if (busyId !== null) return;
    index = Math.min(index + 1, Math.max(queue.length - 1, 0));
  }

  async function applyAllReady() {
    const ids = counts.ready.map((j) => j.id);
    if (applyAllBusy || ids.length === 0) return;
    applyAllBusy = true;
    try {
      const r = await api.bulkApply(ids);
      if (r.busy) {
        // The button is disabled while $applyActive is true, so this should
        // be unreachable from a single tab — but without this check, a busy
        // rejection (r.queued === 0) would otherwise show the confusing
        // "Applying 0 in background" instead of explaining why nothing
        // happened (the same trap bulkApply()/applyConfident() in
        // stores/renames.ts already guard against for their own callers).
        addToast('Renames', 'Another apply is in progress — try again once it finishes.', 'warning');
        return;
      }
      addToast('Renames', `Applying ${r.queued ?? ids.length} in background`);
      await refreshRenames();
    } catch (e) {
      addToast('Renames', e instanceof Error ? e.message : 'Apply all failed', 'error');
    } finally {
      applyAllBusy = false;
    }
  }

  // --- Horizontal swipe nav — mirrors SwipeableTile.svelte's use of the same
  // axis-locked drag tracker, so vertical scrolling inside a tall card (the
  // conflict compare table) is never hijacked by the swipe.
  const dragTracker = createDragTracker({ axis: 'x', threshold: 70 });
  let dx = $state(0);
  let dragging = $state(false);
  let crossed = false;

  function onPointerDown(e: PointerEvent) {
    if (!current) return;
    dragging = true;
    dragTracker.start(e.clientX, e.clientY);
  }
  function onPointerMove(e: PointerEvent) {
    if (!dragging) return;
    const s = dragTracker.move(e.clientX, e.clientY);
    if (s.locked === 'x') {
      dx = s.dx;
      const over = Math.abs(dx) >= 70;
      if (over && !crossed) tap();
      crossed = over;
    } else if (s.locked === 'y') {
      dx = 0;
    }
  }
  function onPointerUp() {
    if (!dragging) return;
    dragging = false;
    crossed = false;
    const { committed, direction } = dragTracker.end();
    if (committed && direction === 'left') next();
    else if (committed && direction === 'right') prev();
    dx = 0;
  }

  function handleKeydown(e: KeyboardEvent) {
    if (rematchJob) return; // RematchModal is open — let it own the keyboard
    const target = e.target as HTMLElement | null;
    if (target) {
      const tag = target.tagName;
      if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT' || target.isContentEditable) {
        return;
      }
    }
    if (e.key === 'Escape') { onClose(); return; }
    if (e.key === 'ArrowLeft') prev();
    else if (e.key === 'ArrowRight') next();
  }
</script>

<svelte:window onkeydown={handleKeydown} />

<div
  role="dialog"
  aria-modal="true"
  aria-label="Review renames"
  class="fixed inset-0 z-50 flex flex-col bg-[var(--bg-primary)]"
  style="padding-top: env(safe-area-inset-top); padding-bottom: env(safe-area-inset-bottom);"
>
  <!-- Header -->
  <div class="flex items-center justify-between gap-2 px-3 h-12 border-b border-[var(--border)] shrink-0">
    <button
      type="button"
      aria-label="Close"
      onclick={onClose}
      class="p-2 -ml-2 text-lg leading-none text-[var(--text-secondary)] hover:text-[var(--text-primary)]"
    >&times;</button>

    <div class="flex items-center gap-0.5 rounded-full bg-[var(--bg-tertiary)] p-0.5">
      <button
        type="button"
        aria-pressed={scope === 'needsReview'}
        disabled={busyId !== null}
        onclick={() => { scope = 'needsReview'; index = 0; }}
        class="px-3 py-1 rounded-full text-xs font-medium transition-colors disabled:opacity-40
          {scope === 'needsReview' ? 'bg-[var(--accent)] text-white' : 'text-[var(--text-secondary)]'}"
      >Under 100% ({counts.needsReview.length})</button>
      <button
        type="button"
        aria-pressed={scope === 'all'}
        disabled={busyId !== null}
        onclick={() => { scope = 'all'; index = 0; }}
        class="px-3 py-1 rounded-full text-xs font-medium transition-colors disabled:opacity-40
          {scope === 'all' ? 'bg-[var(--accent)] text-white' : 'text-[var(--text-secondary)]'}"
      >All ({counts.ready.length + counts.needsReview.length})</button>
    </div>

    <span class="w-14 text-right text-xs text-[var(--text-secondary)] tabular-nums">
      {queue.length ? `${index + 1} / ${queue.length}` : ''}
    </span>
  </div>

  <!-- Card -->
  <div class="flex-1 min-h-0 overflow-y-auto overscroll-contain p-3">
    {#if current}
      <!-- svelte-ignore a11y_no_static_element_interactions -->
      <div
        onpointerdown={onPointerDown}
        onpointermove={onPointerMove}
        onpointerup={onPointerUp}
        onpointercancel={onPointerUp}
        style="transform: translateX({dx}px); touch-action: pan-y;
          transition: {dragging ? 'none' : 'transform 0.18s ease-out'};"
      >
        <!--
          Keyed by job id: without this, RenameReviewCard reuses its instance
          across items and its per-card lazy state (conflict preview fetch,
          dvScanning) would carry over from the previous job and flash stale
          spec data before re-fetching for the new one.
        -->
        {#key current.id}
          <RenameReviewCard
            job={current}
            busy={busyId === current.id || $applyActive}
            onApply={() => act(() => applyJob(current!.id), 'Applied')}
            onOverwrite={() => act(() => applyJob(current!.id, 'overwrite'), 'Overwriting…')}
            onKeepBoth={() => act(() => applyJob(current!.id, 'keep_both'), 'Keeping both')}
            onSkip={skip}
            onRematch={() => (rematchJob = current)}
            onReidentify={() => act(async () => { await api.reidentifyRename(current!.id); await refreshRenames(); }, 'Re-identifying')}
            onAcceptCombined={() => act(() => acceptCombinedJob(current!.id), 'Accepted')}
            onAcceptCorrection={() => act(() => acceptCorrectionJob(current!.id), 'Correction applied')}
            onRemove={() => act(() => deleteJob(current!.id), 'Removed')}
          />
        {/key}
      </div>
    {:else}
      <div class="h-full flex flex-col items-center justify-center gap-3 text-center px-6">
        <div class="text-4xl">✓</div>
        <p class="text-sm font-medium text-[var(--text-primary)]">All reviewed</p>
        <p class="text-xs text-[var(--text-secondary)] max-w-xs">
          Nothing left in this scope. Switch to All to see confidently-matched jobs, or close the deck.
        </p>
        {#if counts.ready.length > 0}
          <button
            type="button"
            onclick={applyAllReady}
            disabled={applyAllBusy || $applyActive}
            title={$applyActive ? 'A bulk apply is in progress — try again once it finishes.' : undefined}
            class="px-4 py-2 rounded-lg text-sm font-semibold bg-[var(--accent)] text-white disabled:opacity-50 hover:brightness-110 transition-all"
          >
            {applyAllBusy ? 'Applying…' : `Apply all ${counts.ready.length} ready`}
          </button>
        {/if}
        <button
          type="button"
          onclick={onClose}
          class="px-4 py-2 rounded-lg text-sm font-medium border border-[var(--border)] text-[var(--text-secondary)] hover:bg-[var(--bg-tertiary)] transition-colors"
        >Done</button>
      </div>
    {/if}
  </div>

  <!-- Nav -->
  {#if current}
    <div class="flex items-center justify-between gap-2 px-3 py-2 border-t border-[var(--border)] shrink-0">
      <button
        type="button"
        onclick={prev}
        disabled={index === 0 || busyId !== null}
        class="px-3 py-1.5 rounded-lg text-xs font-medium border border-[var(--border)] text-[var(--text-secondary)] disabled:opacity-40 hover:bg-[var(--bg-tertiary)] transition-colors"
      >‹ Prev</button>
      <button
        type="button"
        onclick={next}
        disabled={index >= queue.length - 1 || busyId !== null}
        class="px-3 py-1.5 rounded-lg text-xs font-medium border border-[var(--border)] text-[var(--text-secondary)] disabled:opacity-40 hover:bg-[var(--bg-tertiary)] transition-colors"
      >Next ›</button>
    </div>
  {/if}
</div>

{#if rematchJob}
  <RematchModal job={rematchJob} onClose={() => { rematchJob = null; }} />
{/if}
