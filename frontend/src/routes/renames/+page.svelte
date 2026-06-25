<script lang="ts">
  import { onMount } from 'svelte';
  import {
    renameJobs, renameStatus, loadRenameJobs, loadRenameStatus,
    applyJob, undoJob, deleteJob
  } from '$lib/stores/renames';
  import { addToast } from '$lib/stores/notifications';
  import type { RenameJob } from '$lib/api/types';

  type Filter = 'all' | 'needs_review' | 'matched' | 'applied' | 'failed';
  let filter = $state<Filter>('all');
  let busy = $state<number | null>(null);

  const filters: { value: Filter; label: string }[] = [
    { value: 'all', label: 'All' },
    { value: 'needs_review', label: 'Needs review' },
    { value: 'matched', label: 'Matched' },
    { value: 'applied', label: 'Applied' },
    { value: 'failed', label: 'Failed' }
  ];

  onMount(() => {
    loadRenameJobs();
    loadRenameStatus();
  });

  let shown = $derived(
    filter === 'all' ? $renameJobs : $renameJobs.filter((j) => j.status === filter)
  );

  function statusClass(job: RenameJob): string {
    if (job.status === 'failed') return 'bg-[var(--error)]/15 text-[var(--error)]';
    if (job.status === 'needs_review') return 'bg-amber-500/15 text-amber-600 dark:text-amber-400';
    if (job.status === 'applied') return 'bg-[var(--success)]/15 text-[var(--success)]';
    if (job.status === 'reverted') return 'bg-[var(--bg-tertiary)] text-[var(--text-secondary)] line-through';
    return 'bg-[var(--bg-tertiary)] text-[var(--text-secondary)]';
  }

  async function run(id: number, fn: (id: number) => Promise<void>, ok: string) {
    busy = id;
    try {
      await fn(id);
      addToast('Rename', ok);
    } catch {
      addToast('Rename', 'Action failed', 'error');
    } finally {
      busy = null;
    }
  }

  function relTime(s: string | null): string {
    if (!s) return '';
    const iso = s.includes('T') ? s : s.replace(' ', 'T') + 'Z';
    const t = Date.parse(iso);
    if (Number.isNaN(t)) return '';
    const secs = Math.max(0, Math.round((Date.now() - t) / 1000));
    if (secs < 60) return 'just now';
    const m = Math.round(secs / 60);
    if (m < 60) return `${m}m ago`;
    const h = Math.round(m / 60);
    if (h < 24) return `${h}h ago`;
    return `${Math.round(h / 24)}d ago`;
  }
</script>

<div class="flex-1 overflow-auto">
  <div class="px-4 py-3 border-b border-[var(--border)]">
    <div class="flex items-center justify-between">
      <h1 class="text-lg font-semibold">Renames</h1>
      <button
        onclick={() => { loadRenameJobs(); loadRenameStatus(); }}
        class="text-xs text-[var(--text-secondary)] hover:text-[var(--text-primary)]"
      >Refresh</button>
    </div>
    {#if $renameStatus && !$renameStatus.enabled}
      <p class="mt-1 text-xs text-[var(--text-secondary)]">
        Auto-rename is off. Enable it in
        <a href="/settings" class="text-[var(--accent)] hover:underline">Settings → Renaming</a>
        to track and organise extracted downloads.
      </p>
    {:else if $renameStatus && $renameStatus.needs_review > 0}
      <p class="mt-1 text-xs text-amber-600 dark:text-amber-400">
        {$renameStatus.needs_review} item{$renameStatus.needs_review === 1 ? '' : 's'} need review.
      </p>
    {/if}
    <div class="mt-3 flex gap-1 overflow-x-auto">
      {#each filters as f}
        <button
          onclick={() => (filter = f.value)}
          class="px-3 py-1.5 text-xs rounded-lg whitespace-nowrap transition-colors {filter === f.value ? 'bg-[var(--accent)] text-white' : 'text-[var(--text-secondary)] hover:bg-[var(--bg-tertiary)]'}"
        >
          {f.label}{#if $renameStatus?.counts[f.value]} ({$renameStatus.counts[f.value]}){/if}
        </button>
      {/each}
    </div>
  </div>

  {#if shown.length === 0}
    <div class="p-8 text-center text-sm text-[var(--text-secondary)]">
      No rename jobs{filter !== 'all' ? ` (${filter.replace('_', ' ')})` : ''} yet.
    </div>
  {:else}
    <ul class="divide-y divide-[var(--border)]">
      {#each shown as job (job.id)}
        <li class="px-4 py-3">
          <div class="flex items-start gap-3">
            <div class="flex-1 min-w-0">
              <div class="flex items-center gap-2 flex-wrap">
                <span class="text-[10px] px-1.5 py-0.5 rounded uppercase font-medium tracking-wide {statusClass(job)}">{job.status.replace('_', ' ')}</span>
                {#if job.match_source === 'llm'}
                  <span class="text-[10px] px-1.5 py-0.5 rounded bg-[var(--accent)]/15 text-[var(--accent)] font-medium">LLM</span>
                {/if}
                {#if job.match_confidence != null}
                  <span class="text-[10px] text-[var(--text-secondary)]">{Math.round(job.match_confidence)}%</span>
                {/if}
                {#if job.media_type}
                  <span class="text-[10px] text-[var(--text-secondary)] uppercase">{job.media_type}</span>
                {/if}
                <span class="text-[10px] text-[var(--text-secondary)] ml-auto">{relTime(job.detected_at)}</span>
              </div>
              <div class="mt-1 text-sm truncate" title={job.original_filename ?? ''}>{job.original_filename}</div>
              {#if job.new_filename}
                <div class="text-xs text-[var(--text-secondary)] truncate" title={job.new_filename}>→ {job.new_filename}</div>
              {/if}
              {#if job.warning_message}
                <div class="text-[11px] text-amber-600 dark:text-amber-400 mt-0.5">{job.warning_message}</div>
              {/if}
              {#if job.error_message}
                <div class="text-[11px] text-[var(--error)] mt-0.5">{job.error_message}</div>
              {/if}
            </div>
            <div class="flex flex-col gap-1 shrink-0">
              {#if job.status === 'matched' || job.status === 'needs_review'}
                <button onclick={() => run(job.id, applyJob, 'Applied')} disabled={busy === job.id}
                  class="px-2.5 py-1 text-xs rounded-lg bg-[var(--accent)] text-white hover:opacity-90 disabled:opacity-50">Apply</button>
              {/if}
              {#if job.status === 'applied'}
                <button onclick={() => run(job.id, undoJob, 'Reverted')} disabled={busy === job.id}
                  class="px-2.5 py-1 text-xs rounded-lg border border-[var(--border)] text-[var(--text-secondary)] hover:bg-[var(--bg-tertiary)] disabled:opacity-50">Undo</button>
              {/if}
              <button onclick={() => run(job.id, deleteJob, 'Removed')} disabled={busy === job.id}
                class="px-2.5 py-1 text-xs rounded-lg text-[var(--text-secondary)] hover:bg-[var(--bg-tertiary)] disabled:opacity-50">Remove</button>
            </div>
          </div>
        </li>
      {/each}
    </ul>
  {/if}
</div>
