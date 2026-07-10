<script lang="ts">
  import RenamePoster from './RenamePoster.svelte';
  import Badge from '$lib/components/Badge.svelte';
  import { confidenceVariant, dvLayerVariant, formatStatus, renameStatusVariant } from '$lib/constants';
  import { hasDestinationConflict } from '$lib/renames/review';
  import { specRows, needsDvScan } from '$lib/renames/conflictView';
  import { api } from '$lib/api/client';
  import { dvScanTick } from '$lib/stores/renames';
  import type { RenameJob, ConflictComparison } from '$lib/api/types';

  let {
    job,
    busy = false,
    onApply,
    onOverwrite,
    onKeepBoth,
    onSkip,
    onRematch,
    onReidentify,
    onAcceptCombined,
    onAcceptCorrection,
    onRemove
  }: {
    job: RenameJob;
    busy?: boolean;
    onApply: () => void;
    onOverwrite: () => void;
    onKeepBoth: () => void;
    onSkip: () => void;
    onRematch: () => void;
    onReidentify: () => void;
    onAcceptCombined: () => void;
    onAcceptCorrection: () => void;
    onRemove: () => void;
  } = $props();

  let titleLine = $derived(
    [job.title ?? job.package_name ?? job.original_filename ?? `Job ${job.id}`, job.year ? `(${job.year})` : null]
      .filter(Boolean)
      .join(' ')
  );
  let confidence = $derived(job.match_confidence == null ? null : Math.round(job.match_confidence));
  let reasons = $derived(job.match_reasons ?? []);
  let reasonsOpen = $state(false);

  let toPath = $derived(
    job.destination_path && job.new_filename
      ? `${job.destination_path.replace(/[\\/]+$/, '')}/${job.new_filename}`
      : (job.new_filename ?? '—')
  );

  let conflict = $derived(hasDestinationConflict(job));

  // --- Two-file compare (conflict path) ---
  let preview = $state<ConflictComparison | null>(null);
  let previewSeq = 0;
  let previewError = $state<string | null>(null);

  async function loadPreview() {
    const seq = ++previewSeq;
    previewError = null;
    try {
      const p = await api.conflictPreview(job.id);
      if (seq !== previewSeq) return;
      preview = p;
    } catch (e) {
      if (seq !== previewSeq) return;
      previewError = e instanceof Error ? e.message : String(e);
    }
  }

  // Fetch once per job (not on every re-render) — mirrors RematchModal's
  // previewSeq-guarded loadPreview, but the trigger here is "this job just
  // became the conflict card", not user input.
  let loadedForJobId: number | null = null;
  $effect(() => {
    if (conflict && job.id !== loadedForJobId) {
      loadedForJobId = job.id;
      loadPreview();
    }
  });

  // --- On-demand DV layer scan ---
  let dvScanning = $state(false);
  async function scanDv() {
    dvScanning = true;
    try {
      await api.scanConflictDv(job.id);
    } catch {
      dvScanning = false;
    }
  }
  // dv:conflict_scan_done bumps the shared dvScanTick; re-fetch this card's
  // preview so a newly-detected FEL/MEL layer shows up.
  $effect(() => {
    if ($dvScanTick && conflict) loadPreview();
    dvScanning = false;
  });

  let showDvScanButton = $derived(
    !!preview && (needsDvScan(preview.existing) || needsDvScan(preview.incoming))
  );
</script>

<div class="rounded-xl border border-[var(--border)] bg-[var(--bg-secondary)] p-3 flex flex-col gap-3">
  <!-- Header -->
  <div class="flex gap-3">
    <div class="w-16 shrink-0">
      <RenamePoster posterUrl={job.poster_url} alt={job.title ?? ''} />
    </div>
    <div class="flex-1 min-w-0">
      <h3 class="text-sm font-semibold leading-snug truncate" title={titleLine}>{titleLine}</h3>
      <div class="mt-1.5 flex flex-wrap items-center gap-1.5">
        {#if confidence != null}
          <div class="relative inline-flex">
            <button
              type="button"
              onclick={() => (reasonsOpen = !reasonsOpen)}
              aria-label="Match confidence {confidence}%"
              aria-expanded={reasonsOpen}
              class="focus:outline-none focus-visible:ring-2 focus-visible:ring-[var(--accent)] rounded"
            >
              <Badge variant={confidenceVariant(confidence)} label="{confidence}% ⓘ" size="xl" />
            </button>
            {#if reasonsOpen}
              <!-- backdrop closes on outside click/tap -->
              <button
                type="button"
                aria-label="Close"
                tabindex="-1"
                class="fixed inset-0 z-20 cursor-default"
                onclick={() => (reasonsOpen = false)}
              ></button>
              <div
                class="absolute z-30 top-full mt-1 left-0 w-64 max-w-[80vw] p-2.5 rounded-lg
                  bg-[var(--bg-secondary)] border border-[var(--border)] shadow-xl text-left"
                role="dialog"
              >
                <div class="text-[11px] font-semibold text-[var(--text-secondary)] mb-1.5">
                  Why this isn't a 100% match
                </div>
                {#if reasons.length}
                  <ul class="space-y-1">
                    {#each reasons as r}
                      <li class="text-xs text-[var(--text-primary)] flex gap-1.5 leading-snug">
                        <span class="text-[var(--warning)] shrink-0">•</span><span>{r}</span>
                      </li>
                    {/each}
                  </ul>
                {:else}
                  <p class="text-xs text-[var(--text-secondary)]">No details recorded.</p>
                {/if}
              </div>
            {/if}
          </div>
        {/if}
        {#if job.match_source}
          <span class="text-[10px] text-[var(--text-secondary)] uppercase tracking-wide">{job.match_source}</span>
        {/if}
        {#if job.dv_layer}
          <Badge variant={dvLayerVariant(job.dv_layer)} label={job.dv_layer.toUpperCase()} />
        {/if}
        <Badge variant={renameStatusVariant(job.status)} label={formatStatus(job.status)} size="xs" />
      </div>
    </div>
  </div>

  <!-- From -> To -->
  <div class="flex flex-col gap-1 text-[11px] font-mono">
    <div class="break-all text-[var(--text-secondary)]">{job.original_path ?? job.original_filename ?? '—'}</div>
    <div class="break-all text-[var(--text-primary)]">→ {toPath}</div>
  </div>

  {#if !conflict}
    <!-- No conflict: plain Apply -->
    <button
      class="w-full py-2 rounded-lg bg-[var(--accent)] text-white text-sm font-semibold disabled:opacity-50 hover:brightness-110 transition-all"
      disabled={busy}
      onclick={onApply}
    >
      Apply
    </button>
  {:else}
    <!-- Conflict: two-file compare -->
    <div class="rounded-lg border border-[var(--border)] p-2.5 flex flex-col gap-2">
      {#if previewError}
        <div class="text-xs text-[var(--error)]">Comparison failed: {previewError}</div>
      {:else if !preview}
        <div class="text-xs text-[var(--text-secondary)]">Loading comparison…</div>
      {:else if !preview.incoming}
        <div class="text-xs text-[var(--error)]">{preview.reason ?? 'Could not load a comparison for this job.'}</div>
      {:else if preview.existing?.present === false}
        <div class="text-xs text-[var(--success)]">Destination is free — no conflicting file on disk.</div>
        <button
          class="w-full py-2 rounded-lg bg-[var(--accent)] text-white text-sm font-semibold disabled:opacity-50 hover:brightness-110 transition-all"
          disabled={busy}
          onclick={onApply}
        >
          Apply
        </button>
      {:else}
        <div class="overflow-x-auto">
          <table class="w-full text-xs border-collapse">
            <thead>
              <tr>
                <th class="text-left font-medium pb-1"></th>
                <th
                  class="text-left font-medium pb-1 px-1.5 {preview.recommended === 'existing' ? 'text-[var(--success)]' : 'text-[var(--text-primary)]'}"
                >
                  Existing{#if preview.recommended === 'existing'}&nbsp;<span title="Recommended keep">★</span>{/if}
                </th>
                <th
                  class="text-left font-medium pb-1 px-1.5 {preview.recommended === 'incoming' ? 'text-[var(--success)]' : 'text-[var(--text-primary)]'}"
                >
                  Incoming{#if preview.recommended === 'incoming'}&nbsp;<span title="Recommended keep">★</span>{/if}
                </th>
              </tr>
            </thead>
            <tbody>
              {#each specRows(preview.existing, preview.incoming) as row (row.label)}
                <tr class="border-t border-[var(--border)]/60">
                  <td class="py-1 pr-2 text-[var(--text-secondary)] whitespace-nowrap">{row.label}</td>
                  <td class="py-1 px-1.5 {preview.recommended === 'existing' ? 'font-semibold text-[var(--success)]' : ''}">{row.existing}</td>
                  <td class="py-1 px-1.5 {preview.recommended === 'incoming' ? 'font-semibold text-[var(--success)]' : ''}">{row.incoming}</td>
                </tr>
              {/each}
            </tbody>
          </table>
        </div>

        {#if preview.reason}
          <div class="text-[11px] text-[var(--text-secondary)]">
            <span class="text-[var(--success)] font-medium">★ Recommended keep:</span> {preview.reason}
          </div>
        {/if}

        {#if showDvScanButton}
          <button
            class="self-start px-2.5 py-1 rounded-lg text-[11px] font-medium border border-[var(--border)] text-[var(--text-secondary)] hover:bg-[var(--bg-tertiary)] disabled:opacity-50"
            disabled={dvScanning || busy}
            onclick={scanDv}
          >
            {dvScanning ? 'Scanning DV layers…' : 'Scan DV layers'}
          </button>
        {/if}

        <div class="flex flex-wrap gap-2 pt-1">
          <button
            class="flex-1 py-2 rounded-lg bg-[var(--error)] text-white text-xs font-semibold disabled:opacity-50 hover:brightness-110 transition-all"
            disabled={busy}
            onclick={onOverwrite}
          >
            Overwrite
          </button>
          <button
            class="flex-1 py-2 rounded-lg bg-[var(--accent)] text-white text-xs font-semibold disabled:opacity-50 hover:brightness-110 transition-all"
            disabled={busy}
            onclick={onKeepBoth}
          >
            Keep both
          </button>
          <button
            class="flex-1 py-2 rounded-lg border border-[var(--border)] text-[var(--text-secondary)] text-xs font-semibold hover:bg-[var(--bg-tertiary)] disabled:opacity-50"
            disabled={busy}
            onclick={onSkip}
          >
            Skip
          </button>
        </div>
      {/if}
    </div>
  {/if}

  <!-- Secondary actions -->
  <div class="flex flex-wrap items-center gap-1.5 pt-1 border-t border-[var(--border)]/60">
    <button
      class="px-2.5 py-1 rounded-lg text-[11px] font-medium border border-[var(--border)] text-[var(--text-secondary)] hover:bg-[var(--bg-tertiary)] disabled:opacity-50"
      disabled={busy}
      onclick={onRematch}
    >
      Rematch
    </button>
    {#if job.status === 'needs_review' || job.status === 'failed'}
      <button
        class="px-2.5 py-1 rounded-lg text-[11px] font-medium border border-[var(--border)] text-[var(--text-secondary)] hover:bg-[var(--bg-tertiary)] disabled:opacity-50"
        disabled={busy}
        onclick={onReidentify}
        title="Re-run automatic identification with the current matcher"
      >
        Re-identify
      </button>
    {/if}
    {#if job.status === 'needs_review' && job.combined_episode}
      <button
        class="px-2.5 py-1 rounded-lg text-[11px] font-medium bg-amber-500/20 text-amber-700 dark:text-amber-300 hover:bg-amber-500/30 disabled:opacity-50"
        disabled={busy}
        onclick={onAcceptCombined}
        title="Confirm this is a combined double-episode file"
      >
        Accept {job.combined_episode.proposed_code}
      </button>
    {/if}
    {#if job.status === 'needs_review' && job.suggested_correction}
      <button
        class="px-2.5 py-1 rounded-lg text-[11px] font-medium bg-amber-500/20 text-amber-700 dark:text-amber-300 hover:bg-amber-500/30 disabled:opacity-50"
        disabled={busy}
        onclick={onAcceptCorrection}
        title="Use the proposed episode correction"
      >
        Accept S{String(job.suggested_correction.proposed.season).padStart(2, '0')}E{String(job.suggested_correction.proposed.episode).padStart(2, '0')}
      </button>
    {/if}
    <button
      class="ml-auto px-2.5 py-1 rounded-lg text-[11px] font-medium text-[var(--error)] hover:bg-[var(--bg-tertiary)] disabled:opacity-50"
      disabled={busy}
      onclick={onRemove}
    >
      Remove
    </button>
  </div>
</div>
