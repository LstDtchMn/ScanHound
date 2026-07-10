<script lang="ts">
  import { onMount } from 'svelte';
  import { api } from '$lib/api/client';
  import { addToast } from '$lib/stores/notifications';
  import type { PipelineItem, PipelineCounts } from '$lib/api/types';
  import SourceSearchModal from '$lib/components/pipeline/SourceSearchModal.svelte';
  import ErrorCard from '$lib/components/ErrorCard.svelte';

  const CATEGORY_LABELS: Record<string, string> = {
    never_started: 'Never started',
    download_failed: 'Download failed',
    in_progress: 'In progress',
    pending_rename: 'Pending rename',
    rename_failed: 'Rename failed',
    not_in_plex: 'Not in Plex',
    verified: 'Verified',
    unknown: 'Unknown',
  };
  const ACTIONABLE = ['never_started', 'download_failed', 'rename_failed', 'not_in_plex', 'unknown'];

  // category is null while a verdict is pending re-evaluation (regrab /
  // grab-alternative call clear_pipeline_verdict, and it stays null until the
  // next reconcile pass judges it — see backend/pipeline_service.py).
  function categoryLabel(cat: string | null): string {
    if (!cat) return 'Pending re-evaluation';
    return CATEGORY_LABELS[cat] ?? cat;
  }

  let items = $state<PipelineItem[]>([]);
  let counts = $state<PipelineCounts>({});
  let activeCategory = $state<string | null>(null);
  let searchModalUrl = $state<string | null>(null);
  let busy = $state<string | null>(null);
  let loading = $state(true);
  let loadError = $state('');

  async function load() {
    loading = true;
    loadError = '';
    try {
      counts = await api.getPipelineCounts();
      items = await api.getPipelineItems(activeCategory ?? undefined);
    } catch (e) {
      loadError = e instanceof Error ? e.message : 'Failed to load pipeline items';
    } finally {
      loading = false;
    }
  }

  onMount(load);

  function selectCategory(cat: string | null) {
    activeCategory = cat;
    load();
  }

  async function dismiss(item: PipelineItem) {
    busy = item.url;
    try {
      await api.dismissPipelineItem(item.url);
      items = items.filter((i) => i.url !== item.url);
      addToast('Dismissed', item.title || item.url);
      // Keep the chip counts in sync with the item that just disappeared.
      counts = await api.getPipelineCounts();
    } catch (e) {
      addToast('Error', e instanceof Error ? e.message : 'Could not dismiss', 'error');
    } finally {
      busy = null;
    }
  }

  async function regrab(item: PipelineItem) {
    busy = item.url;
    try {
      await api.regrabPipelineItem(item.url);
      addToast('Re-grab', `Retrying ${item.title || item.url}…`);
      // The backend clears this item's verdict (category -> null) as soon as
      // the regrab is accepted, so refresh counts AND the item list to reflect
      // that shift (otherwise the row keeps showing its stale pre-regrab
      // category with action buttons still enabled).
      counts = await api.getPipelineCounts();
      items = await api.getPipelineItems(activeCategory ?? undefined);
    } catch (e) {
      addToast('Error', e instanceof Error ? e.message : 'Could not regrab', 'error');
    } finally {
      busy = null;
    }
  }
</script>

<div class="flex-1 min-h-0 overflow-auto p-4 space-y-4">
  <h1 class="text-lg font-semibold">Pipeline</h1>

  <div class="flex flex-wrap gap-2">
    <button
      class="px-3 py-1.5 rounded-lg text-sm {activeCategory === null ? 'bg-[var(--accent)] text-white' : 'bg-[var(--bg-tertiary)]'}"
      onclick={() => selectCategory(null)}
    >All ({Object.values(counts).reduce((a, b) => a + b, 0)})</button>
    {#each Object.entries(CATEGORY_LABELS) as [cat, label]}
      {#if counts[cat]}
        <button
          class="px-3 py-1.5 rounded-lg text-sm {activeCategory === cat ? 'bg-[var(--accent)] text-white' : 'bg-[var(--bg-tertiary)]'}"
          onclick={() => selectCategory(cat)}
        >{label} ({counts[cat]})</button>
      {/if}
    {/each}
  </div>

  {#if loading}
    <p class="text-center text-[var(--text-secondary)] py-12 text-sm">Loading…</p>
  {:else if loadError}
    <ErrorCard message={loadError} onretry={load} />
  {:else if items.length === 0}
    <p class="text-center text-[var(--text-secondary)] py-12">Nothing to review.</p>
  {:else}
    <ul class="divide-y divide-[var(--border)] rounded-lg border border-[var(--border)] overflow-hidden">
      {#each items as item (item.url)}
        <li class="p-3 flex items-center gap-3">
          <div class="flex-1 min-w-0">
            <div class="font-medium truncate">{item.title || item.package_name || item.url}</div>
            <div class="text-xs text-[var(--text-secondary)]">
              {categoryLabel(item.category)}
              {#if item.detail}<span class="text-[var(--error)]"> — {item.detail}</span>{/if}
            </div>
          </div>
          {#if item.category && ACTIONABLE.includes(item.category)}
            <button class="px-2 py-1 text-xs rounded bg-[var(--accent)] text-white disabled:opacity-50"
              disabled={busy === item.url} onclick={() => regrab(item)}>Re-grab</button>
            <button class="px-2 py-1 text-xs rounded bg-[var(--bg-tertiary)] disabled:opacity-50"
              disabled={busy === item.url} onclick={() => (searchModalUrl = item.url)}>Search sources</button>
          {/if}
          <button class="px-2 py-1 text-xs rounded bg-[var(--bg-tertiary)] disabled:opacity-50"
            disabled={busy === item.url} onclick={() => dismiss(item)}>Dismiss</button>
        </li>
      {/each}
    </ul>
  {/if}
</div>

{#if searchModalUrl}
  <SourceSearchModal url={searchModalUrl} onClose={() => (searchModalUrl = null)} />
{/if}
