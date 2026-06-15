<script lang="ts">
  interface Props {
    message: string;
    onretry?: () => void;
  }
  let { message, onretry }: Props = $props();
</script>

<div class="flex flex-col items-center justify-center gap-3 py-12 text-center">
  <div class="w-12 h-12 rounded-full bg-[var(--error)]/10 flex items-center justify-center text-[var(--error)] text-xl">
    !
  </div>
  <p class="text-sm text-[var(--text-secondary)] max-w-sm">{message}</p>
  {#if onretry}
    <button
      onclick={onretry}
      class="px-4 py-2 text-xs rounded-lg bg-[var(--bg-tertiary)] border border-[var(--border)] text-[var(--text-primary)] hover:bg-[var(--border)] transition-colors"
    >
      Retry
    </button>
  {/if}
</div>
