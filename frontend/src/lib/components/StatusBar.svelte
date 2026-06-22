<script lang="ts">
  import { stats, selectedKeys } from '$lib/stores/results';
  import { logs, logPanelOpen } from '$lib/stores/logs';

  let errorCount = $derived($logs.filter(l => l.level === 'error' || l.level === 'warning').length);
  let selectedCount = $derived($selectedKeys.size);
</script>

<div class="flex items-center gap-3 sm:gap-4 px-3 sm:px-4 py-2 border-t border-[var(--border)] text-[11px] sm:text-xs text-[var(--text-secondary)] whitespace-nowrap overflow-x-auto">
  <span class="hidden sm:inline">Total: <strong class="text-[var(--text-primary)]">{$stats.total}</strong></span>
  <span>Missing: <strong class="text-[var(--error)]">{$stats.missing}</strong></span>
  <span>Upgrades: <strong class="text-[var(--warning)]">{$stats.upgrade}</strong></span>
  <span class="hidden sm:inline">In Library: <strong class="text-[var(--success)]">{$stats.library}</strong></span>

  {#if selectedCount > 0}
    <span>Selected: <strong class="text-[var(--accent)]">{selectedCount}</strong></span>
  {/if}

  <div class="flex-1"></div>

  <button
    onclick={() => logPanelOpen.update((v) => !v)}
    class="flex items-center gap-1 hover:text-[var(--text-primary)] transition-colors"
    title="Toggle log panel"
  >
    Logs
    {#if errorCount > 0}
      <span class="bg-red-500/20 text-red-400 px-1.5 rounded-full text-[10px]">{errorCount}</span>
    {/if}
  </button>
</div>
