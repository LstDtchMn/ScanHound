<script lang="ts">
  import ModalOverlay from '$lib/components/ModalOverlay.svelte';
  import RenameReviewCard from './RenameReviewCard.svelte';
  import { api } from '$lib/api/client';
  import {
    applyJob, deleteJob, acceptCombinedJob, acceptCorrectionJob, refreshRenames
  } from '$lib/stores/renames';
  import type { RenameJob } from '$lib/api/types';

  let { job, onClose }: { job: RenameJob; onClose: () => void } = $props();

  let busy = $state(false);

  // Run a resolve action, then close so the (refreshed) list reflects the
  // outcome. applyJob/acceptCombinedJob/acceptCorrectionJob already refresh
  // the store internally; deleteJob updates the store directly. Either way,
  // by the time `fn()` resolves the list behind this modal is current.
  async function act(fn: () => Promise<unknown> | unknown) {
    busy = true;
    try {
      await fn();
      onClose();
    } finally {
      busy = false;
    }
  }
</script>

<ModalOverlay onclose={onClose}>
  <div class="w-full max-w-lg bg-[var(--bg-secondary)] border border-[var(--border)] rounded-xl shadow-2xl"
       role="dialog" aria-modal="true" tabindex="-1">
    <RenameReviewCard
      {job}
      {busy}
      onApply={() => act(() => applyJob(job.id))}
      onOverwrite={() => act(() => applyJob(job.id, 'overwrite'))}
      onKeepBoth={() => act(() => applyJob(job.id, 'keep_both'))}
      onSkip={onClose}
      onRematch={onClose}
      onReidentify={() => act(async () => { await api.reidentifyRename(job.id); await refreshRenames(); })}
      onAcceptCombined={() => act(() => acceptCombinedJob(job.id))}
      onAcceptCorrection={() => act(() => acceptCorrectionJob(job.id))}
      onRemove={() => act(() => deleteJob(job.id))}
    />
  </div>
</ModalOverlay>
