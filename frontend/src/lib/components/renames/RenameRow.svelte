<script lang="ts">
  import RenamePoster from './RenamePoster.svelte';
  import BadgeCluster from './BadgeCluster.svelte';
  import { selectedJobIds, toggleSelect, applyJob } from '$lib/stores/renames';
  import type { RenameJob } from '$lib/api/types';

  let { job, onRematch }: { job: RenameJob; onRematch: (job: RenameJob) => void } = $props();

  let selected = $derived($selectedJobIds.has(job.id));
  let busy = $state(false);
  let titleLine = $derived(
    [job.title ?? job.package_name ?? job.original_filename ?? `Job ${job.id}`, job.year ? `(${job.year})` : null]
      .filter(Boolean)
      .join(' ')
  );
  let canApply = $derived(job.status === 'matched' || job.status === 'needs_review');

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
    {:else if job.warning_message}
      <div class="text-xs text-[var(--warning)] truncate" title={job.warning_message}>
        {job.warning_message}
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
