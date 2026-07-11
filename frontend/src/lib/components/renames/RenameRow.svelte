<script lang="ts">
  import RenamePoster from './RenamePoster.svelte';
  import BadgeCluster from './BadgeCluster.svelte';
  import Badge from '$lib/components/Badge.svelte';
  import { selectedJobIds, toggleSelect, applyJob, renameProgress } from '$lib/stores/renames';
  import { hasDestinationConflict } from '$lib/renames/review';
  import type { RenameJob } from '$lib/api/types';

  let {
    job,
    onRematch,
    onCompare
  }: { job: RenameJob; onRematch: (job: RenameJob) => void; onCompare: (job: RenameJob) => void } = $props();

  let selected = $derived($selectedJobIds.has(job.id));
  let busy = $state(false);
  let isConflict = $derived(hasDestinationConflict(job));
  let titleLine = $derived(
    [job.title ?? job.package_name ?? job.original_filename ?? `Job ${job.id}`, job.year ? `(${job.year})` : null]
      .filter(Boolean)
      .join(' ')
  );
  let canApply = $derived(job.status === 'matched' || job.status === 'needs_review');
  let applying = $derived(job.status === 'applying');
  let prog = $derived($renameProgress.get(job.id));

  function gb(bytes: number): string {
    return (bytes / 1024 ** 3).toFixed(1);
  }

  async function apply() {
    busy = true;
    try {
      await applyJob(job.id);
    } finally {
      busy = false;
    }
  }
</script>

<li class="flex items-center gap-3 px-3 py-2 hover:bg-[var(--bg-tertiary)]/40 transition-colors min-w-0">
  <input
    type="checkbox"
    class="shrink-0 accent-[var(--accent)]"
    checked={selected}
    onchange={(e) => { e.stopPropagation(); toggleSelect(job.id); }}
    aria-label="Select {titleLine}"
  />

  <div class="w-10 shrink-0">
    <RenamePoster posterUrl={job.poster_url} alt={job.title ?? ''} />
  </div>

  <div class="flex-1 min-w-0">
    <div class="font-medium text-sm truncate">{titleLine}</div>
    <div class="text-xs text-[var(--text-secondary)] truncate" title={job.original_filename ?? ''}>
      {job.original_filename ?? '—'}
    </div>
    {#if job.new_filename}
      <div class="text-xs text-[var(--accent)] truncate" title={job.new_filename}>
        → {job.new_filename}
      </div>
    {/if}
    {#if job.error_message}
      <div class="text-xs text-[var(--error)] truncate" title={job.error_message}>
        {job.error_message}
      </div>
    {:else if isConflict}
      <div class="flex items-center gap-1.5 text-xs">
        <Badge variant="warning" label="⚠ Conflict" />
        {#if job.conflict_same_size}
          <Badge variant="default" label="likely duplicate" />
        {/if}
        <span class="text-[var(--text-secondary)] truncate" title={job.warning_message}>{job.warning_message}</span>
        <button
          type="button"
          class="ml-auto shrink-0 px-2 py-0.5 rounded text-[11px] font-medium bg-[var(--accent)] text-white hover:brightness-110"
          onclick={() => onCompare(job)}
        >
          Compare
        </button>
      </div>
    {:else if job.warning_message}
      <div class="text-xs text-[var(--warning)] truncate" title={job.warning_message}>
        {job.warning_message}
      </div>
    {/if}
    {#if applying}
      <div class="mt-1">
        <div class="h-1.5 rounded-full bg-[var(--bg-tertiary)] overflow-hidden">
          {#if prog}
            <div class="h-full bg-[var(--accent)] transition-[width] duration-200"
                 style="width: {prog.pct}%"></div>
          {:else}
            <!-- No byte progress yet: instant same-drive move, or just starting -->
            <div class="h-full w-1/3 bg-[var(--accent)]/70 animate-pulse"></div>
          {/if}
        </div>
        <div class="mt-0.5 text-[11px] text-[var(--text-secondary)]">
          {#if prog}
            Moving… {prog.pct}% ({gb(prog.bytes_done)} / {gb(prog.bytes_total)} GB)
          {:else}
            Applying…
          {/if}
        </div>
      </div>
    {/if}
  </div>

  <div class="shrink-0 hidden sm:block">
    <BadgeCluster {job} />
  </div>

  <div class="shrink-0 flex items-center gap-1">
    {#if canApply}
      <button
        class="px-2 py-1 rounded text-[11px] font-medium bg-[var(--accent)] text-white disabled:opacity-50"
        disabled={busy}
        onclick={apply}
      >
        Apply
      </button>
    {/if}
    <button
      class="px-2 py-1 rounded text-[11px] font-medium text-[var(--text-secondary)] hover:bg-[var(--bg-tertiary)]"
      onclick={() => onRematch(job)}
    >
      Rematch
    </button>
  </div>
</li>
