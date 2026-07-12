<script lang="ts">
  import StatCard from './StatCard.svelte';
  import { renameStatus, dvCounts, applyConfident, loadDvScans, applyActive } from '$lib/stores/renames';
  import { dvLayerColor } from '$lib/constants';
  import { browser } from '$app/environment';
  import { onMount } from 'svelte';

  let { statusFilter, onFilter }: { statusFilter: string; onFilter: (status: string) => void } =
    $props();

  let counts = $derived($renameStatus?.counts ?? {});
  function n(key: string): number {
    const v = (counts as Record<string, number>)[key];
    return typeof v === 'number' ? v : 0;
  }

  // Archiving is orthogonal to status — sourced from renameStatus.archived,
  // not the status→count map above.
  let archivedCount = $derived($renameStatus?.archived ?? 0);

  // dvCounts is Record<string, number> keyed by layer (fel, mel, p8, p5, etc.)
  let fel = $derived(($dvCounts as Record<string, number>)?.fel ?? 0);
  let mel = $derived(($dvCounts as Record<string, number>)?.mel ?? 0);

  function toggle(status: string) {
    onFilter(statusFilter === status ? 'all' : status);
  }

  onMount(() => {
    if (fel === 0 && mel === 0) {
      loadDvScans();
    }
  });
</script>

<div class="flex flex-wrap gap-3">
  <StatCard
    label="Needs review"
    count={n('needs_review')}
    variant="warning"
    borderStatus="needs_review"
    active={statusFilter === 'needs_review'}
    onclick={() => toggle('needs_review')}
  />

  <div class="flex-1 min-w-0 flex flex-col gap-1">
    <StatCard
      label="Matched"
      count={n('matched')}
      variant="accent"
      borderStatus="matched"
      active={statusFilter === 'matched'}
      onclick={() => toggle('matched')}
    />
    <button
      class="text-[11px] font-medium text-[var(--accent)] hover:underline px-1 text-left
        disabled:opacity-50 disabled:no-underline disabled:cursor-not-allowed"
      disabled={$applyActive}
      onclick={() => applyConfident()}
      title="Apply every matched job with confidence ≥ 95% across the page"
    >
      Apply all confident
    </button>
  </div>

  <StatCard
    label="Applied"
    count={n('applied')}
    variant="success"
    borderStatus="applied"
    active={statusFilter === 'applied'}
    onclick={() => toggle('applied')}
  />
  <StatCard
    label="Failed"
    count={n('failed')}
    variant="error"
    borderStatus="failed"
    active={statusFilter === 'failed'}
    onclick={() => toggle('failed')}
  />
  <StatCard
    label="Archived"
    count={archivedCount}
    variant="default"
    borderStatus="archived"
    active={statusFilter === 'archived'}
    onclick={() => toggle('archived')}
  />

  <button
    class="flex-1 min-w-0 text-left rounded-lg border-2 border-[var(--border)] px-3 py-2 hover:bg-[var(--bg-tertiary)]/40"
    onclick={() => browser && document.getElementById('dv-scan-surface')?.scrollIntoView({ behavior: 'smooth' })}
    title="Dolby Vision inventory (read-only)"
  >
    <div class="text-sm font-bold flex gap-2">
      <span style="color: {dvLayerColor('fel')}">FEL {fel}</span>
      <span style="color: {dvLayerColor('mel')}">MEL {mel}</span>
    </div>
    <div class="text-xs text-[var(--text-secondary)]">Dolby Vision</div>
  </button>
</div>
