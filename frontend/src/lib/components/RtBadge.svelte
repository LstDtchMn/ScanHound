<script lang="ts">
  /** Rotten Tomatoes score with the real Tomatometer theming: a red "fresh"
   *  tomato at >=60%, a green "rotten" splat below. Score text is colored to
   *  match. Renders nothing when there's no score. */
  interface Props {
    score: number | null | undefined;
    /** 'sm' facts line, 'lg' phone tile / sheet, 'xl' single-tile / swipe deck. */
    size?: 'sm' | 'lg' | 'xl';
  }
  let { score, size = 'sm' }: Props = $props();

  let fresh = $derived(score != null && score >= 60);
  // Official-ish Tomatometer colors: fresh red, rotten green.
  let color = $derived(fresh ? '#FA320A' : '#0AC855');
  let icon = $derived(size === 'xl' ? 'w-6 h-6' : size === 'lg' ? 'w-4 h-4' : 'w-3.5 h-3.5');
  let text = $derived(size === 'xl' ? 'text-lg' : size === 'lg' ? 'text-sm' : 'text-[11px]');
</script>

{#if score != null}
  <span class="inline-flex items-center gap-0.5 font-semibold {text}" style="color: {color};" title="Rotten Tomatoes {fresh ? 'Fresh' : 'Rotten'} — {score}%">
    {#if fresh}
      <!-- Fresh: round tomato with a leaf -->
      <svg class={icon} viewBox="0 0 24 24" fill={color} aria-hidden="true">
        <path d="M12 3c.4 1.1 1.4 1.9 2.6 2-.7.9-1 2-.8 3.1C16.6 8.9 19 11.6 19 14.8 19 18.2 15.9 21 12 21s-7-2.8-7-6.2c0-3.4 2.7-6.2 6.3-6.7C10.6 6.4 11 4.4 12 3z"/>
      </svg>
    {:else}
      <!-- Rotten: green splat -->
      <svg class={icon} viewBox="0 0 24 24" fill={color} aria-hidden="true">
        <path d="M12 2l1.5 3.2L17 3.8l-.6 3.6 3.6.4-2.3 2.8 3 2.2-3.4 1.3 1.6 3.3-3.6-.8-.4 3.6-2.9-2.2-2.9 2.2-.4-3.6-3.6.8L6.7 14 3.3 12.7l3-2.2L4 7.7l3.6-.4L7 3.8l3.5 1.4z"/>
      </svg>
    {/if}
    {score}%
  </span>
{/if}
