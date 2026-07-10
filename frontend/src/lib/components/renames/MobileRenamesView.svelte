<script lang="ts">
  import RenameReviewDeck from './RenameReviewDeck.svelte';
  import { api } from '$lib/api/client';
  import { addToast } from '$lib/stores/notifications';
  import { renameJobs, renameQuery, refreshRenames } from '$lib/stores/renames';
  import { partitionJobs, matchesQuery, type ReviewScope } from '$lib/renames/review';

  let deckOpen = $state(false);
  let scope = $state<ReviewScope>('needsReview');
  let applyAllBusy = $state(false);

  let filtered = $derived($renameJobs.filter((j) => matchesQuery(j, $renameQuery)));
  let parts = $derived(partitionJobs(filtered));

  // Opens the deck at whatever `scope` is currently set to (defaults to
  // 'needsReview'; the scope toggle below can switch it to 'all' first).
  function openReview() {
    deckOpen = true;
  }

  async function applyAllReady() {
    if (applyAllBusy || parts.ready.length === 0) return;
    applyAllBusy = true;
    try {
      const ids = parts.ready.map((j) => j.id);
      const r = await api.bulkApply(ids);
      addToast('Renames', `Applying ${r.queued ?? ids.length} in background`);
      await refreshRenames();
    } catch (e) {
      addToast('Renames', e instanceof Error ? e.message : 'Apply all failed', 'error');
    } finally {
      applyAllBusy = false;
    }
  }
</script>

<div class="flex flex-col h-full">
  <!-- Search -->
  <div class="px-3 py-2 border-b border-[var(--border)]">
    <input
      type="search"
      aria-label="Search rename jobs"
      placeholder="Search title / filename…"
      value={$renameQuery}
      oninput={(e) => renameQuery.set((e.target as HTMLInputElement).value)}
      class="w-full px-3 py-2 rounded-lg text-sm bg-[var(--bg-tertiary)] border border-[var(--border)] focus:border-[var(--accent)] outline-none"
    />
  </div>

  <div class="flex-1 overflow-y-auto p-3 space-y-3">
    {#if $renameJobs.length === 0}
      <p class="text-center text-sm text-[var(--text-secondary)] mt-10">
        No rename jobs yet. Use Process ▾ to scan a folder.
      </p>
    {:else if parts.needsReview.length === 0}
      <div class="rounded-xl border border-[var(--border)] bg-[var(--bg-secondary)] p-4 text-center space-y-3">
        <p class="text-sm font-medium text-[var(--text-primary)]">
          All clear — {parts.ready.length} ready to apply
        </p>
        <button
          type="button"
          onclick={applyAllReady}
          disabled={applyAllBusy || parts.ready.length === 0}
          class="w-full px-4 py-3 rounded-lg text-sm font-semibold bg-[var(--accent)] text-white disabled:opacity-50 hover:brightness-110 transition-all"
        >
          {applyAllBusy ? 'Applying…' : 'Apply all'}
        </button>
      </div>
    {:else}
      {#if parts.ready.length > 0}
        <div class="rounded-xl border border-[var(--border)] bg-[var(--bg-secondary)] p-4">
          <div class="text-2xl font-bold" style="color: var(--success)">{parts.ready.length}</div>
          <div class="text-xs text-[var(--text-secondary)] mb-3">Ready to apply</div>
          <button
            type="button"
            onclick={applyAllReady}
            disabled={applyAllBusy}
            class="w-full px-4 py-3 rounded-lg text-sm font-semibold bg-[var(--accent)] text-white disabled:opacity-50 hover:brightness-110 transition-all"
          >
            {applyAllBusy ? 'Applying…' : 'Apply all'}
          </button>
        </div>
      {/if}

      <div class="rounded-xl border border-[var(--border)] bg-[var(--bg-secondary)] p-4">
        <div class="text-2xl font-bold" style="color: var(--warning)">{parts.needsReview.length}</div>
        <div class="text-xs text-[var(--text-secondary)] mb-3">Needs review</div>
        <button
          type="button"
          onclick={openReview}
          class="w-full px-4 py-3 rounded-lg text-sm font-semibold border border-[var(--border)] text-[var(--text-primary)] hover:bg-[var(--bg-tertiary)] transition-colors"
        >Review</button>
      </div>
    {/if}

    {#if $renameJobs.length > 0}
      <!-- Scope toggle -->
      <div class="flex items-center gap-0.5 rounded-full bg-[var(--bg-tertiary)] p-0.5 w-fit mx-auto">
        <button
          type="button"
          aria-pressed={scope === 'needsReview'}
          onclick={() => (scope = 'needsReview')}
          class="px-3 py-1.5 rounded-full text-xs font-medium transition-colors
            {scope === 'needsReview' ? 'bg-[var(--accent)] text-white' : 'text-[var(--text-secondary)]'}"
        >Under 100% · {parts.needsReview.length}</button>
        <button
          type="button"
          aria-pressed={scope === 'all'}
          onclick={() => (scope = 'all')}
          class="px-3 py-1.5 rounded-full text-xs font-medium transition-colors
            {scope === 'all' ? 'bg-[var(--accent)] text-white' : 'text-[var(--text-secondary)]'}"
        >All · {parts.ready.length + parts.needsReview.length}</button>
      </div>
    {/if}
  </div>
</div>

{#if deckOpen}
  <RenameReviewDeck jobs={filtered} initialScope={scope} onClose={() => (deckOpen = false)} />
{/if}
