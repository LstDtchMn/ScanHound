<script lang="ts">
  import ProcessMenu from './ProcessMenu.svelte';
  import { viewMode, loadRenameJobs, loadRenameStatus } from '$lib/stores/renames';

  let {
    onDolbyVision,
    onReidentifyAll,
    reidentifyingAll = false
  }: {
    onDolbyVision: () => void;
    onReidentifyAll: () => void;
    reidentifyingAll?: boolean;
  } = $props();

  function refresh() {
    loadRenameJobs();
    loadRenameStatus();
  }
</script>

<div class="flex items-center justify-between gap-3 flex-wrap">
  <h1 class="text-lg font-semibold">Renames</h1>

  <div class="flex items-center gap-2 flex-wrap">
    <ProcessMenu />

    <button
      onclick={onDolbyVision}
      title="Scan a folder for Dolby Vision FEL/MEL"
      class="text-xs px-2.5 py-1 rounded bg-[var(--bg-tertiary)] border border-[var(--border)] text-[var(--text-secondary)] hover:border-[var(--accent)] hover:text-[var(--text-primary)]"
    >Dolby Vision</button>

    <button
      onclick={onReidentifyAll}
      disabled={reidentifyingAll}
      title="Re-run identification on all reviewable jobs with the current matcher"
      class="text-xs px-2.5 py-1 rounded bg-[var(--bg-tertiary)] border border-[var(--border)] text-[var(--text-secondary)] hover:border-[var(--accent)] hover:text-[var(--text-primary)] disabled:opacity-50"
    >{reidentifyingAll ? 'Re-identifying…' : 'Re-identify all'}</button>

    <div class="flex rounded overflow-hidden border border-[var(--border)]">
      <button
        onclick={() => viewMode.set('list')}
        class="px-2 py-1 text-xs {$viewMode === 'list' ? 'bg-[var(--accent)] text-white' : 'text-[var(--text-secondary)] hover:bg-[var(--bg-tertiary)]'}"
        aria-label="List view"
        aria-pressed={$viewMode === 'list'}
      >☰</button>
      <button
        onclick={() => viewMode.set('grid')}
        class="px-2 py-1 text-xs {$viewMode === 'grid' ? 'bg-[var(--accent)] text-white' : 'text-[var(--text-secondary)] hover:bg-[var(--bg-tertiary)]'}"
        aria-label="Grid view"
        aria-pressed={$viewMode === 'grid'}
      >▦</button>
    </div>

    <button
      onclick={refresh}
      class="text-xs px-2.5 py-1 rounded text-[var(--text-secondary)] hover:text-[var(--text-primary)]"
    >Refresh</button>
  </div>
</div>
