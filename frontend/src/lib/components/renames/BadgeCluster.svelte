<script lang="ts">
  import Badge from '$lib/components/Badge.svelte';
  import { formatStatus, renameStatusVariant, confidenceVariant, dvLayerVariant } from '$lib/constants';
  import type { RenameJob } from '$lib/api/types';

  let { job, compact = false }: { job: RenameJob; compact?: boolean } = $props();

  let confidence = $derived(
    job.match_confidence == null ? null : Math.round(job.match_confidence)
  );
  let mediaRes = $derived(
    [job.media_type ? job.media_type.toUpperCase() : null, job.resolution]
      .filter(Boolean)
      .join(' · ')
  );
</script>

<div class="flex flex-wrap items-center gap-1">
  <Badge variant={renameStatusVariant(job.status)} label={formatStatus(job.status)} />

  {#if confidence != null}
    <Badge variant={confidenceVariant(confidence)} label="{confidence}%" />
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
