<script lang="ts">
  import { filteredLogs, logs, logLevelFilter, logPanelOpen, clearLogs } from '$lib/stores/logs';
  import { fly } from 'svelte/transition';

  let scrollEl: HTMLDivElement | undefined = $state();
  let prevLogCount = $state(0);

  const levelColors: Record<string, string> = {
    error: 'text-red-400',
    warning: 'text-amber-400',
    info: 'text-[var(--text-secondary)]',
    debug: 'text-[var(--text-secondary)] opacity-60'
  };

  // Auto-scroll only when new log entries arrive (not on filter change)
  $effect(() => {
    const currentCount = $logs.length;
    if (currentCount > prevLogCount && scrollEl) {
      scrollEl.scrollTop = scrollEl.scrollHeight;
    }
    prevLogCount = currentCount;
  });
</script>

{#if $logPanelOpen}
  <div
    transition:fly={{ y: 200, duration: 200 }}
    class="h-48 border-t border-[var(--border)] bg-[var(--bg-secondary)] flex flex-col"
  >
    <div class="flex items-center gap-2 px-3 py-1.5 border-b border-[var(--border)] text-xs">
      <span class="font-medium text-[var(--text-primary)]">Logs</span>

      <select
        bind:value={$logLevelFilter}
        class="bg-[var(--bg-tertiary)] text-[var(--text-primary)] px-2 py-0.5 rounded border border-[var(--border)] text-xs"
      >
        <option value="all">All</option>
        <option value="info">Info</option>
        <option value="warning">Warning</option>
        <option value="error">Error</option>
        <option value="debug">Debug</option>
      </select>

      <div class="flex-1"></div>

      <button
        onclick={() => clearLogs()}
        class="text-[var(--text-secondary)] hover:text-[var(--text-primary)] transition-colors"
      >
        Clear
      </button>
      <button
        onclick={() => logPanelOpen.set(false)}
        class="text-[var(--text-secondary)] hover:text-[var(--text-primary)] transition-colors ml-1"
      >
        &times;
      </button>
    </div>

    <div bind:this={scrollEl} class="flex-1 overflow-auto px-3 py-1 font-mono text-[11px] leading-relaxed">
      {#each $filteredLogs as entry}
        <div class="{levelColors[entry.level] || 'text-[var(--text-secondary)]'}">
          <span class="opacity-50">{entry.timestamp}</span>
          <span class="uppercase font-medium w-12 inline-block">{entry.level}</span>
          {entry.message}
        </div>
      {/each}
      {#if $filteredLogs.length === 0}
        <div class="text-[var(--text-secondary)] opacity-50 py-4 text-center">No log entries</div>
      {/if}
    </div>
  </div>
{/if}
