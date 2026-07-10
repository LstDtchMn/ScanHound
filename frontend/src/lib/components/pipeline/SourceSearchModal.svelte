<script lang="ts">
  import { onMount } from 'svelte';
  import { fly } from 'svelte/transition';
  import ModalOverlay from '$lib/components/ModalOverlay.svelte';
  import { api } from '$lib/api/client';
  import { addToast } from '$lib/stores/notifications';
  import type { AlternativeRelease } from '$lib/api/types';

  let { url, onClose }: { url: string; onClose: () => void } = $props();

  let loading = $state(true);
  let releases = $state<AlternativeRelease[]>([]);
  let errors = $state<string[]>([]);
  let grabbing = $state<string | null>(null);

  onMount(async () => {
    try {
      const res = await api.searchPipelineSources(url);
      releases = res.releases;
      errors = res.errors;
    } catch (e) {
      errors = [e instanceof Error ? e.message : 'Search failed'];
    } finally {
      loading = false;
    }
  });

  async function grab(rel: AlternativeRelease) {
    if (grabbing) return;
    grabbing = rel.url;
    try {
      await api.grabAlternative(rel);
      addToast('Grabbing', rel.display_title);
      onClose();
    } catch (e) {
      addToast('Error', e instanceof Error ? e.message : 'Could not grab', 'error');
    } finally {
      grabbing = null;
    }
  }
</script>

<ModalOverlay onclose={onClose}>
  <div
    transition:fly={{ y: -20, duration: 200 }}
    role="dialog"
    aria-modal="true"
    aria-label="Alternative sources"
    tabindex="-1"
    class="bg-[var(--bg-secondary)] border border-[var(--border)] rounded-xl shadow-2xl
           w-full max-w-lg max-h-[85vh] overflow-auto flex flex-col gap-3 p-4"
  >
    <!-- Header -->
    <div class="flex items-center justify-between gap-2">
      <h2 class="font-semibold text-sm truncate">Alternative sources</h2>
      <button
        onclick={onClose}
        aria-label="Close"
        class="shrink-0 text-[var(--text-secondary)] hover:text-[var(--text-primary)] transition-colors text-base leading-none"
      >✕</button>
    </div>

    {#if loading}
      <p class="text-xs text-[var(--text-secondary)]">Searching…</p>
    {:else}
      {#if errors.length > 0}
        <p class="text-xs text-[var(--error)]">
          {errors.join('; ')}{#if releases.length === 0} — adithd requires the desktop scraper and is not searched here.{/if}
        </p>
      {/if}
      {#if releases.length === 0 && errors.length === 0}
        <p class="text-xs text-[var(--text-secondary)]">No results found.</p>
      {/if}
      {#if releases.length > 0}
        <ul class="flex flex-col divide-y divide-[var(--border)] overflow-auto">
          {#each releases as rel (rel.url)}
            <li class="py-2 flex items-center gap-2">
              <div class="flex-1 min-w-0">
                <div class="text-sm font-medium truncate" title={rel.display_title}>{rel.display_title}</div>
                <div class="text-xs text-[var(--text-secondary)]">
                  {rel.source} · {rel.res || '?'} · {rel.size || '?'}
                  {#if rel.dovi}<span class="text-amber-500"> · DV</span>{/if}
                  {#if rel.hdr}<span class="text-amber-500"> · {rel.hdr}</span>{/if}
                </div>
              </div>
              <button
                onclick={() => grab(rel)}
                disabled={grabbing === rel.url}
                aria-busy={grabbing === rel.url}
                class="shrink-0 px-3 py-1.5 rounded text-xs font-medium bg-[var(--accent)] text-white
                       disabled:opacity-50 hover:brightness-110 transition-all"
              >{grabbing === rel.url ? 'Grabbing…' : 'Grab'}</button>
            </li>
          {/each}
        </ul>
      {/if}
    {/if}
  </div>
</ModalOverlay>
