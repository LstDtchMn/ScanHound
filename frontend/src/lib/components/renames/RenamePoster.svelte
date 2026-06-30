<script lang="ts">
  import { posterAspect, POSTER_ASPECT_CLASS } from '$lib/stores/results';

  let {
    posterUrl = null,
    alt = '',
    class: klass = ''
  }: { posterUrl?: string | null; alt?: string; class?: string } = $props();
</script>

<div
  class="{POSTER_ASPECT_CLASS[$posterAspect]} bg-[var(--bg-tertiary)] relative overflow-hidden {klass}"
>
  {#if posterUrl}
    <img
      src={posterUrl}
      {alt}
      class="w-full h-full object-cover"
      loading="lazy"
    />
  {:else}
    <div class="flex items-center justify-center h-full text-[var(--text-secondary)] text-xs">
      No poster
    </div>
  {/if}
</div>
