<script lang="ts">
  import RenamePoster from './RenamePoster.svelte';
  import BadgeCluster from './BadgeCluster.svelte';
  import { tileShowMeta } from '$lib/stores/results';
  import { selectedJobIds, toggleSelect } from '$lib/stores/renames';
  import type { RenameJob } from '$lib/api/types';

  let { job, onRematch }: { job: RenameJob; onRematch: (job: RenameJob) => void } = $props();

  let selected = $derived($selectedJobIds.has(job.id));
  let titleLine = $derived(
    [job.title ?? job.package_name ?? job.original_filename ?? `Job ${job.id}`, job.year ? `(${job.year})` : null]
      .filter(Boolean)
      .join(' ')
  );
</script>

<div
  class="group min-w-0 rounded-lg overflow-hidden border transition-colors cursor-pointer
    {selected ? 'border-[var(--accent)]' : 'border-[var(--border)] hover:border-[var(--accent)]/60'}"
  onclick={() => onRematch(job)}
  role="button"
  tabindex="0"
  onkeydown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); onRematch(job); } }}
>
  <div class="relative">
    <RenamePoster posterUrl={job.poster_url} alt={job.title ?? ''} />
    <input
      type="checkbox"
      class="absolute top-1.5 left-1.5 accent-[var(--accent)] z-10"
      checked={selected}
      onclick={(e) => e.stopPropagation()}
      onchange={() => toggleSelect(job.id)}
      aria-label="Select {titleLine}"
    />
    <div class="absolute bottom-1.5 left-1.5 right-1.5">
      <BadgeCluster {job} compact />
    </div>
  </div>

  {#if $tileShowMeta}
    <div class="p-2 min-w-0">
      <div class="text-xs font-medium truncate" title={titleLine}>{titleLine}</div>
      {#if job.new_filename}
        <div class="text-[10px] text-[var(--text-secondary)] truncate" title={job.new_filename}>
          {job.new_filename}
        </div>
      {/if}
    </div>
  {/if}
</div>
