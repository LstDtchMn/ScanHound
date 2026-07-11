<script lang="ts">
  import RenamePoster from './RenamePoster.svelte';
  import BadgeCluster from './BadgeCluster.svelte';
  import Badge from '$lib/components/Badge.svelte';
  import {
    selectedJobIds, toggleSelect, applyJob, renameProgress, applyActive, progressClock
  } from '$lib/stores/renames';
  import { hasDestinationConflict } from '$lib/renames/review';
  import { formatBytes } from '$lib/renames/conflictView';
  import { addToast } from '$lib/stores/notifications';
  import type { RenameJob } from '$lib/api/types';

  /** No progress update for this long while still 'applying' (and not past
   *  100%, which has its own "Verifying…" state below) reads as possibly
   *  stalled. Set generously above the worst-case healthy chunk-read time
   *  (a single 8 MiB chunk on an extremely slow drive) so a genuinely slow
   *  transfer isn't flagged, while a job frozen for minutes/hours clearly is. */
  const STALL_MS = 30_000;

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
  // pct reaching 100 doesn't mean the job is done — the post-copy cold-cache
  // hash re-verify (backend/rename/fileops.py's _copy_verify_atomic) reads
  // the whole file back with no progress callback at all, so the bar sits at
  // 100% for a real (if usually short) stretch before the job's status
  // actually flips off 'applying'. Label that "Verifying…" rather than
  // freezing on "100%" with no explanation, and never treat it as stalled.
  let verifying = $derived(applying && !!prog && prog.pct >= 100);
  let stalled = $derived(
    applying && !!prog && prog.pct < 100 && $progressClock - prog.updatedAt > STALL_MS
  );

  function gb(bytes: number): string {
    return (bytes / 1024 ** 3).toFixed(1);
  }

  function formatEta(seconds: number): string {
    if (seconds < 60) return `${Math.ceil(seconds)}s`;
    const totalMin = Math.floor(seconds / 60);
    const secs = Math.round(seconds % 60);
    if (totalMin < 60) return secs > 0 ? `${totalMin}m ${secs}s` : `${totalMin}m`;
    const hours = Math.floor(totalMin / 60);
    const min = totalMin % 60;
    return min > 0 ? `${hours}h ${min}m` : `${hours}h`;
  }

  async function apply() {
    busy = true;
    try {
      await applyJob(job.id);
    } catch (e) {
      // The Apply button is disabled while $applyActive is true (see below),
      // so a busy-queue rejection shouldn't normally reach here — but the
      // single-job apply route folds "busy" into a generic 400 "Apply
      // failed" (no distinguishable busy flag like the bulk routes have), so
      // this is a defense-in-depth catch for that case and any other apply
      // failure, not a busy-specific one. Previously uncaught: the click
      // silently did nothing with zero feedback.
      addToast('Apply failed', e instanceof Error ? e.message : 'Could not apply — try again.', 'error');
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
      <div class="flex items-center gap-1.5 text-xs" title={job.warning_message}>
        <Badge variant="warning" label="⚠ Already in library" />
        {#if job.conflict_existing_size != null && job.conflict_incoming_size != null}
          {#if job.conflict_same_size === true}
            <Badge variant="accent" label={`same size · ${formatBytes(job.conflict_existing_size)}`} />
          {:else}
            <Badge variant="default" label={`${formatBytes(job.conflict_existing_size)} → ${formatBytes(job.conflict_incoming_size)}`} />
          {/if}
        {/if}
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
            <div class="h-full transition-[width] duration-200 {stalled ? 'bg-[var(--warning)]' : 'bg-[var(--accent)]'}"
                 style="width: {prog.pct}%"></div>
          {:else}
            <!-- No byte progress yet: instant same-drive move, or just starting -->
            <div class="h-full w-1/3 bg-[var(--accent)]/70 animate-pulse"></div>
          {/if}
        </div>
        <div class="mt-0.5 text-[11px] {stalled ? 'text-[var(--warning)]' : 'text-[var(--text-secondary)]'}">
          {#if prog}
            {#if verifying}
              Verifying… ({gb(prog.bytes_total)} GB)
            {:else}
              Moving… {prog.pct}% ({gb(prog.bytes_done)} / {gb(prog.bytes_total)} GB)
              {#if prog.bytes_per_sec}
                · {formatBytes(prog.bytes_per_sec)}/s
                {#if prog.eta_seconds != null}· {formatEta(prog.eta_seconds)} left{/if}
              {/if}
              {#if stalled}
                · No update in a while — may be stalled
              {/if}
            {/if}
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
        disabled={busy || $applyActive}
        title={$applyActive ? 'A bulk apply is in progress — try again once it finishes.' : undefined}
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
