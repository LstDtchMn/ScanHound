<script lang="ts">
  import { fly } from 'svelte/transition';

  interface Props {
    title: string;
    message: string;
    confirmLabel?: string;
    cancelLabel?: string;
    variant?: 'default' | 'danger';
    onconfirm: () => void;
    oncancel: () => void;
  }
  let { title, message, confirmLabel = 'Confirm', cancelLabel = 'Cancel', variant = 'default', onconfirm, oncancel }: Props = $props();

  function handleKeydown(e: KeyboardEvent) {
    if (e.key === 'Escape') oncancel();
  }
</script>

<svelte:window onkeydown={handleKeydown} />

<!-- svelte-ignore a11y_no_static_element_interactions -->
<!-- svelte-ignore a11y_click_events_have_key_events -->
<div class="fixed inset-0 z-50 flex items-center justify-center bg-[var(--bg-overlay)]" onclick={oncancel}>
  <div
    transition:fly={{ y: -20, duration: 200 }}
    class="bg-[var(--bg-secondary)] border border-[var(--border)] rounded-xl shadow-2xl p-6 w-full max-w-sm"
    role="alertdialog"
    aria-label={title}
    tabindex="-1"
    onclick={(e) => e.stopPropagation()}
  >
    <h2 class="text-base font-semibold mb-2">{title}</h2>
    <p class="text-sm text-[var(--text-secondary)] mb-5">{message}</p>
    <div class="flex justify-end gap-2">
      <button
        onclick={oncancel}
        class="px-4 py-2 text-xs rounded-lg bg-[var(--bg-tertiary)] border border-[var(--border)] text-[var(--text-primary)] hover:bg-[var(--border)] transition-colors"
      >
        {cancelLabel}
      </button>
      <button
        onclick={onconfirm}
        class="px-4 py-2 text-xs rounded-lg text-white transition-colors
          {variant === 'danger' ? 'bg-[var(--error)] hover:brightness-110' : 'bg-[var(--accent)] hover:brightness-110'}"
      >
        {confirmLabel}
      </button>
    </div>
  </div>
</div>
