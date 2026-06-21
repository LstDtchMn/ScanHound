<script lang="ts">
  import { toasts, dismissToast } from '$lib/stores/notifications';
  import { fly } from 'svelte/transition';
</script>

<div class="fixed bottom-4 right-4 flex flex-col gap-2 z-50" role="status" aria-live="polite">
  {#each $toasts as toast (toast.id)}
    <div
      transition:fly={{ y: 20, duration: 200 }}
      class="px-4 py-3 rounded-lg shadow-lg max-w-sm border
        {toast.priority === 'high' ? 'bg-red-900/90 border-red-500/40' : toast.priority === 'warning' ? 'bg-amber-900/80 border-amber-500/40' : 'bg-[var(--bg-tertiary)] border-[var(--border)]'}"
    >
      <div class="flex justify-between items-start gap-3">
        <div>
          <p class="font-medium text-sm">{toast.title}</p>
          <p class="text-xs text-[var(--text-secondary)] mt-0.5">{toast.body}</p>
        </div>
        <button
          class="text-[var(--text-secondary)] hover:text-[var(--text-primary)] text-sm leading-none"
          onclick={() => dismissToast(toast.id)}
        >&times;</button>
      </div>
    </div>
  {/each}
</div>
