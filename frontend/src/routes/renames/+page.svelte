<script lang="ts">
  import { onMount } from 'svelte';
  import {
    renameJobs, renameStatus, loadRenameJobs, loadRenameStatus,
    applyJob, undoJob, deleteJob, rematchJob,
    acceptCombinedJob, acceptCorrectionJob
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

  // Rematch state — one job can have its form open at a time
  let rematchOpenId = $state<number | null>(null);
  let rematchTmdbInput = $state('');
  let rematchMediaType = $state<'movie' | 'tv'>('movie');
  let rematchBusy = $state(false);

  function openRematch(job: RenameJob) {
    rematchOpenId = job.id;
    rematchTmdbInput = '';
    rematchMediaType = (job.media_type === 'tv' || job.media_type === 'show') ? 'tv' : 'movie';
  }

  async function submitRematch(jobId: number) {
    const id = parseInt(rematchTmdbInput.trim(), 10);
    if (!id || isNaN(id)) { addToast('Rematch', 'Enter a valid TMDB ID', 'error'); return; }
    rematchBusy = true;
    try {
      await rematchJob(jobId, id, rematchMediaType);
      addToast('Rematch', 'Re-matched successfully');
      rematchOpenId = null;
    } catch {
      addToast('Rematch', 'Rematch failed', 'error');
    } finally {
      rematchBusy = false;
    }
  }

  function tmdbSearchUrl(job: RenameJob): string {
    // Strip extension + quality tags for a cleaner search query
    const raw = job.original_filename ?? '';
    const base = raw.replace(/\.[^.]+$/, '').replace(/[._]?(1080p|2160p|4K|BluRay|WEB[-.]DL|HDTV|x264|x265|HEVC|AAC|AC3|DTS|H\.?264|H\.?265).*/i, '').replace(/[._]/g, ' ').trim();
    return `https://www.themoviedb.org/search?query=${encodeURIComponent(base || raw)}`;
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
              {#if job.status === 'needs_review' || job.status === 'failed'}
                <button
                  onclick={() => rematchOpenId === job.id ? (rematchOpenId = null) : openRematch(job)}
                  class="px-2.5 py-1 text-xs rounded-lg border border-[var(--border)] text-[var(--text-secondary)] hover:bg-[var(--bg-tertiary)]"
                  title="Re-match to a different TMDB title"
                >Rematch</button>
              {/if}
              {#if job.status === 'needs_review' && job.combined_episode}
                <button
                  onclick={() => run(job.id, acceptCombinedJob, 'Accepted as combined')}
                  disabled={busy === job.id}
                  class="px-2.5 py-1 text-xs rounded-lg bg-amber-500/20 text-amber-700 dark:text-amber-300 hover:bg-amber-500/30 disabled:opacity-50"
                  title="Confirm this is a combined double-episode file"
                >Accept {job.combined_episode.proposed_code}</button>
              {/if}
              {#if job.status === 'needs_review' && job.suggested_correction}
                <button
                  onclick={() => run(job.id, acceptCorrectionJob, 'Correction applied')}
                  disabled={busy === job.id}
                  class="px-2.5 py-1 text-xs rounded-lg bg-amber-500/20 text-amber-700 dark:text-amber-300 hover:bg-amber-500/30 disabled:opacity-50"
                  title="Use the proposed episode correction"
                >Accept S{String(job.suggested_correction.proposed.season).padStart(2,'0')}E{String(job.suggested_correction.proposed.episode).padStart(2,'0')}</button>
              {/if}
              {#if job.status === 'applied'}
                <button onclick={() => run(job.id, undoJob, 'Reverted')} disabled={busy === job.id}
                  class="px-2.5 py-1 text-xs rounded-lg border border-[var(--border)] text-[var(--text-secondary)] hover:bg-[var(--bg-tertiary)] disabled:opacity-50">Undo</button>
              {/if}
              <button onclick={() => run(job.id, deleteJob, 'Removed')} disabled={busy === job.id}
                class="px-2.5 py-1 text-xs rounded-lg text-[var(--text-secondary)] hover:bg-[var(--bg-tertiary)] disabled:opacity-50">Remove</button>
            </div>
          </div>
          {#if rematchOpenId === job.id}
            <div class="mt-2 p-3 rounded-lg bg-[var(--bg-tertiary)] border border-[var(--border)] text-xs space-y-2">
              <p class="text-[var(--text-secondary)]">Find the correct entry on TMDB, then paste its numeric ID here.</p>
              <a href={tmdbSearchUrl(job)} target="_blank" rel="noopener" class="inline-flex items-center gap-1 text-[var(--accent)] hover:underline">
                Search TMDB ↗
              </a>
              <div class="flex items-center gap-2 flex-wrap">
                <select bind:value={rematchMediaType} class="px-2 py-1 rounded bg-[var(--bg-secondary)] border border-[var(--border)] text-[var(--text-primary)] focus:outline-none focus:border-[var(--accent)]">
                  <option value="movie">Movie</option>
                  <option value="tv">TV Show</option>
                </select>
                <input
                  type="number"
                  bind:value={rematchTmdbInput}
                  placeholder="TMDB ID (e.g. 550)"
                  class="flex-1 min-w-0 px-2 py-1 rounded bg-[var(--bg-secondary)] border border-[var(--border)] text-[var(--text-primary)] placeholder:text-[var(--text-secondary)] focus:outline-none focus:border-[var(--accent)]"
                  onkeydown={(e) => e.key === 'Enter' && submitRematch(job.id)}
                />
                <button onclick={() => submitRematch(job.id)} disabled={rematchBusy}
                  class="px-2.5 py-1 rounded bg-[var(--accent)] text-white hover:opacity-90 disabled:opacity-50">
                  {rematchBusy ? '…' : 'Submit'}
                </button>
                <button onclick={() => (rematchOpenId = null)} class="px-2.5 py-1 rounded text-[var(--text-secondary)] hover:bg-[var(--border)]">Cancel</button>
              </div>
            </div>
          {/if}
        </li>
      {/each}
    </ul>
  {/if}
</div>
