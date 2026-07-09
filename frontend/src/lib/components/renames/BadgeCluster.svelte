<script lang="ts">
  import Badge from '$lib/components/Badge.svelte';
  import { formatStatus, renameStatusVariant, confidenceVariant, dvLayerVariant } from '$lib/constants';
  import type { RenameJob } from '$lib/api/types';

  let { job, compact = false }: { job: RenameJob; compact?: boolean } = $props();

  let confidence = $derived(
    job.match_confidence == null ? null : Math.round(job.match_confidence)
  );
  let reasons = $derived(job.match_reasons ?? []);
  let hasReasons = $derived(confidence != null && confidence < 100 && reasons.length > 0);
  let showReasons = $state(false);
  let mediaRes = $derived(
    [job.media_type ? job.media_type.toUpperCase() : null, job.resolution]
      .filter(Boolean)
      .join(' · ')
  );
</script>

<div class="flex flex-wrap items-center gap-1">
  <Badge variant={renameStatusVariant(job.status)} label={formatStatus(job.status)} />

  {#if confidence != null}
    {#if hasReasons}
      <div class="relative inline-flex">
        <button
          type="button"
          onclick={(e) => { e.stopPropagation(); showReasons = !showReasons; }}
          aria-label="Why is this only {confidence}% certain?"
          aria-expanded={showReasons}
          class="focus:outline-none focus-visible:ring-2 focus-visible:ring-[var(--accent)] rounded"
        >
          <Badge variant={confidenceVariant(confidence)} label="{confidence}% ⓘ" />
        </button>
        {#if showReasons}
          <!-- backdrop closes on outside click/tap -->
          <button type="button" aria-label="Close" tabindex="-1"
            class="fixed inset-0 z-20 cursor-default"
            onclick={(e) => { e.stopPropagation(); showReasons = false; }}></button>
          <div
            class="absolute z-30 top-full mt-1 left-0 w-64 max-w-[80vw] p-2.5 rounded-lg
              bg-[var(--bg-secondary)] border border-[var(--border)] shadow-xl text-left"
            role="dialog"
          >
            <div class="text-[11px] font-semibold text-[var(--text-secondary)] mb-1.5">
              Why this isn't a 100% match
            </div>
            <ul class="space-y-1">
              {#each reasons as r}
                <li class="text-xs text-[var(--text-primary)] flex gap-1.5 leading-snug">
                  <span class="text-[var(--warning)] shrink-0">•</span><span>{r}</span>
                </li>
              {/each}
            </ul>
          </div>
        {/if}
      </div>
    {:else}
      <Badge variant={confidenceVariant(confidence)} label="{confidence}%" />
    {/if}
  {/if}

  {#if mediaRes && !compact}
    <Badge variant="info" label={mediaRes} />
  {/if}

  {#if job.dv_layer}
    <Badge variant={dvLayerVariant(job.dv_layer)} label={job.dv_layer.toUpperCase()} />
  {/if}

  {#if job.keep_recommended}
    <Badge variant="success" label="★ Keep" />
  {/if}

  {#if job.destination_conflict}
    <Badge variant="orange" label="⚠ Duplicate" />
  {/if}
</div>
