<script lang="ts">
  interface Props {
    width?: string;
    height?: string;
    rounded?: string;
    count?: number;
  }
  let { width = '100%', height = '1rem', rounded = 'rounded-lg', count = 1 }: Props = $props();
</script>

{#each Array(count) as _, i}
  <div
    class="animate-pulse bg-[var(--bg-tertiary)] {rounded}"
    style="width: {width}; height: {height};"
  ></div>
{/each}
