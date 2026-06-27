<script lang="ts">
  let { text, children }: { text: string; children?: any } = $props();
  let visible = $state(false);
  let ref = $state<HTMLElement | null>(null);
</script>

<span
  class="relative inline-flex items-center"
  bind:this={ref}
  onmouseenter={() => (visible = true)}
  onmouseleave={() => (visible = false)}
  onfocusin={() => (visible = true)}
  onfocusout={() => (visible = false)}
>
  {@render children?.()}
  {#if visible && text}
    <span
      role="tooltip"
      class="pointer-events-none absolute bottom-full left-0 z-50 mb-1.5 w-72 rounded-lg border border-[var(--border)] bg-[var(--bg-primary)] px-3 py-2 text-xs leading-relaxed text-[var(--text-secondary)] shadow-lg"
    >
      {text}
    </span>
  {/if}
</span>
