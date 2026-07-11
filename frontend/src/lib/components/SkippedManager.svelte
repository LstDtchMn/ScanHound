<script lang="ts">
  import { onMount } from 'svelte';
  import { api } from '$lib/api/client';
  import { restoreItem, restoreAllDismissed } from '$lib/stores/results';
  import { filterSkipped, relativeTime, type SkippedItem } from '$lib/skipped';
  import ConfirmDialog from './ConfirmDialog.svelte';

  let { onclose }: { onclose: () => void } = $props();

  let items = $state<SkippedItem[]>([]);
  let loading = $state(true);
  let error = $state(false);
  let query = $state('');
  let confirmingClear = $state(false);
  let now = $state(Date.now());

  const visible = $derived(filterSkipped(items, query));

  async function load() {
    loading = true;
    error = false;
    try {
      const res = await api.dismissedList();
      items = res.items;
      now = Date.now();
    } catch {
      error = true;
    } finally {
      loading = false;
    }
  }

  onMount(load);

  async function restoreOne(url: string) {
    const ok = await restoreItem(url);
    if (ok) items = items.filter((i) => i.url !== url);
  }

  async function doClearAll() {
    confirmingClear = false;
    const ok = await restoreAllDismissed();
    if (ok) items = [];
  }
</script>

<div class="flex flex-col gap-3 min-h-0">
  <div class="flex items-center gap-2">
    <h2 class="text-sm font-semibold text-[var(--text-primary)]">Skipped items ({items.length})</h2>
    {#if items.length > 0}
      <button
        class="ml-auto text-xs px-2 py-1 rounded border border-[var(--border)] text-[var(--text-secondary)] hover:bg-[var(--bg-tertiary)]"
        onclick={() => (confirmingClear = true)}
      >Restore all</button>
    {/if}
    <button class="p-1 text-[var(--text-secondary)] hover:text-[var(--text-primary)]" aria-label="Close" onclick={onclose}>&times;</button>
  </div>

  <input
    type="text"
    placeholder="Search skipped titles..."
    bind:value={query}
    class="w-full bg-[var(--bg-tertiary)] text-[var(--text-primary)] px-3 py-2 rounded-lg border border-[var(--border)] text-sm"
  />

  <div class="overflow-y-auto min-h-0 max-h-[60vh] flex flex-col gap-1">
    {#if loading}
      <p class="text-sm text-[var(--text-secondary)] py-4 text-center">Loading...</p>
    {:else if error}
      <div class="py-4 text-center">
        <p class="text-sm text-[var(--text-secondary)]">Couldn't load skipped items.</p>
        <button class="mt-2 text-xs px-3 py-1 rounded bg-[var(--accent)] text-white" onclick={load}>Retry</button>
      </div>
    {:else if items.length === 0}
      <p class="text-sm text-[var(--text-secondary)] py-4 text-center">No skipped items.</p>
    {:else if visible.length === 0}
      <p class="text-sm text-[var(--text-secondary)] py-4 text-center">No matches.</p>
    {:else}
      {#each visible as it (it.url)}
        <div class="flex items-center gap-2 px-2 py-1.5 rounded hover:bg-[var(--bg-tertiary)]">
          <span class="text-sm text-[var(--text-primary)] truncate min-w-0 flex-1">{it.title ?? it.url}</span>
          <span class="text-[11px] text-[var(--text-secondary)] shrink-0">{relativeTime(it.dismissed_at, now)}</span>
          <button
            class="shrink-0 text-xs px-2 py-0.5 rounded bg-[var(--accent)] text-white hover:brightness-110"
            onclick={() => restoreOne(it.url)}
          >Restore</button>
        </div>
      {/each}
    {/if}
  </div>
</div>

{#if confirmingClear}
  <ConfirmDialog
    title="Restore all skipped items?"
    message={`This will un-skip all ${items.length} items so they can appear in scans again.`}
    confirmLabel="Restore all"
    oncancel={() => (confirmingClear = false)}
    onconfirm={doClearAll}
  />
{/if}
