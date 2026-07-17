<script lang="ts">
  import { onMount } from 'svelte';
  import { api } from '$lib/api/client';
  import { addToast } from '$lib/stores/notifications';
  import type { PipelineItem, PipelineCounts } from '$lib/api/types';
  import SourceSearchModal from '$lib/components/pipeline/SourceSearchModal.svelte';
  import ErrorCard from '$lib/components/ErrorCard.svelte';
  import StatCard from '$lib/components/renames/StatCard.svelte';
  import RenamePoster from '$lib/components/renames/RenamePoster.svelte';
  import { CATEGORY_VARIANT, POSTER_CATEGORIES, checkedAgo, categoryColor } from './pipelineDisplay';

  // CATEGORY_LABELS / EMPTY_STATES: Fable-drafted copy (Task 4 Step 1).
  const CATEGORY_LABELS: Record<string, string> = {
    never_started: 'Never Started',
    download_failed: 'Download Failed',
    downloading: 'Downloading',
    pending_rename: 'Pending Rename',
    rename_failed: 'Rename Failed',
    awaiting_plex_refresh: 'Awaiting Plex',
    not_in_plex: 'Not in Plex',
    verified: 'Verified',
    unknown: 'Unknown',
  };
  const EMPTY_STATES: Record<string, string> = {
    never_started: 'No grabs are stuck before download.',
    download_failed: 'No downloads have failed.',
    downloading: 'Nothing is downloading right now.',
    pending_rename: 'No files are waiting to be renamed.',
    rename_failed: 'No renames have failed.',
    awaiting_plex_refresh: 'Nothing is waiting on a Plex refresh.',
    not_in_plex: 'Nothing is missing from Plex.',
    verified: 'Nothing has been verified yet.',
    unknown: 'No items need a closer look.',
    all: 'The pipeline is clear.',
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

  <!-- Stat cards: every category always renders (stable layout), plus All.
       Grid on phones (3 col) / small tablets (5 col) so labels never clip;
       reverts to the desktop flex-wrap at lg where StatCard's own flex-1 sizing
       takes over. -->
  <div class="grid grid-cols-3 sm:grid-cols-5 lg:flex lg:flex-wrap gap-2 sm:gap-3">
    <StatCard label="All" count={Object.values(counts).reduce((a, b) => a + b, 0)}
      variant="default" active={activeCategory === null} onclick={() => selectCategory(null)} />
    {#each Object.entries(CATEGORY_LABELS) as [cat, label]}
      <StatCard {label} count={counts[cat] ?? 0} variant={CATEGORY_VARIANT[cat] ?? 'default'}
        active={activeCategory === cat} onclick={() => selectCategory(cat)} />
    {/each}
  </div>

  {#if loading}
    <p class="text-center text-[var(--text-secondary)] py-12 text-sm">Loading…</p>
  {:else if loadError}
    <ErrorCard message={loadError} onretry={load} />
  {:else if items.length === 0}
    <p class="text-center text-[var(--text-secondary)] py-12">
      {EMPTY_STATES[activeCategory ?? 'all'] ?? EMPTY_STATES.all}
    </p>
  {:else}
    <ul class="divide-y divide-[var(--border)] rounded-lg border border-[var(--border)] overflow-hidden">
      {#each items as item (item.url)}
        {@const ago = checkedAgo(item.checked_at)}
        {@const grabbedAgo = checkedAgo(item.grabbed_at ?? '')}
        {@const renamedAgo = checkedAgo(item.renamed_at ?? '')}
        <!-- Phone: stack content over a full-width button row so the text
             column keeps full width (no one-word-per-line wrapping). sm+:
             the original side-by-side row. -->
        <li class="p-3 flex flex-col gap-2 sm:flex-row sm:items-center sm:gap-3">
          <div class="flex items-center gap-3 min-w-0 sm:flex-1">
            {#if item.category && POSTER_CATEGORIES.has(item.category)}
              <RenamePoster posterUrl={item.poster_url} alt={item.title ?? ''} class="w-10 shrink-0 rounded" />
            {/if}
            <div class="flex-1 min-w-0">
              <div class="font-medium truncate">
                <a href={item.url} target="_blank" rel="noopener noreferrer"
                  class="hover:underline">
                  {item.title || item.package_name || item.url}
                  {#if item.season != null}<span> S{String(item.season).padStart(2, '0')}</span>{/if}
                </a>
                {#if item.year}<span class="text-[var(--text-secondary)] font-normal"> ({item.year})</span>{/if}
              </div>
              <div class="text-xs text-[var(--text-secondary)] flex flex-wrap gap-x-2">
                <span style="color: {categoryColor(item.category)}">{categoryLabel(item.category)}</span>
                {#if item.resolution}<span>{item.resolution}</span>{/if}
                {#if grabbedAgo}<span>grabbed {grabbedAgo}</span>{/if}
                {#if renamedAgo}<span>renamed {renamedAgo}</span>{/if}
                {#if ago}<span>checked {ago}</span>{/if}
              </div>
              {#if item.detail}
                <div class="text-xs text-[var(--error)] truncate" title={item.detail}>{item.detail}</div>
              {/if}
            </div>
          </div>
          <div class="flex gap-2 shrink-0">
            {#if item.category && ACTIONABLE.includes(item.category)}
              <button class="px-2 py-1 text-xs rounded bg-[var(--accent)] text-white disabled:opacity-50"
                disabled={busy === item.url} onclick={() => regrab(item)}>Re-grab</button>
              <button class="px-2 py-1 text-xs rounded bg-[var(--bg-tertiary)] disabled:opacity-50"
                disabled={busy === item.url} onclick={() => (searchModalUrl = item.url)}>Search sources</button>
            {/if}
            <button class="px-2 py-1 text-xs rounded bg-[var(--bg-tertiary)] disabled:opacity-50"
              disabled={busy === item.url} onclick={() => dismiss(item)}>Dismiss</button>
          </div>
        </li>
      {/each}
    </ul>
  {/if}
</div>

{#if searchModalUrl}
  <SourceSearchModal url={searchModalUrl} onClose={() => (searchModalUrl = null)} />
{/if}
