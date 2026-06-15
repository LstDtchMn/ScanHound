<script lang="ts">
  import { page } from '$app/stores';
  import { goto } from '$app/navigation';
</script>

<div class="flex flex-col items-center justify-center min-h-screen gap-6 p-8 bg-[var(--bg-primary)]">
  <svg class="w-16 h-16 text-[var(--error)]" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="1.5">
    <path stroke-linecap="round" stroke-linejoin="round"
      d="M12 9v3.75m9-.75a9 9 0 11-18 0 9 9 0 0118 0zm-9 3.75h.008v.008H12v-.008z" />
  </svg>

  <div class="text-center">
    <h1 class="text-5xl font-bold text-[var(--text-primary)]">{$page.status}</h1>
    <p class="mt-2 text-lg text-[var(--text-secondary)]">{$page.error?.message ?? 'Something went wrong'}</p>
  </div>

  <div class="flex gap-3">
    <button
      onclick={() => goto('/')}
      class="px-4 py-2 rounded-lg bg-[var(--accent)] text-white text-sm font-medium hover:opacity-90 transition-opacity"
    >Go Home</button>
    <button
      onclick={() => location.reload()}
      class="px-4 py-2 rounded-lg border border-[var(--border)] bg-[var(--bg-secondary)] text-[var(--text-primary)] text-sm font-medium hover:bg-[var(--bg-primary)] transition-colors"
    >Retry</button>
  </div>
</div>
