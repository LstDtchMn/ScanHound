<script lang="ts">
  import { changelog, latestVersion } from '$lib/changelog';

  let open = $state(false);

  function fmt(iso: string): string {
    const parts = iso.split('-');
    const months = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
    return `${months[parseInt(parts[1]) - 1]} ${parseInt(parts[2])}`;
  }
</script>

<div class="relative">
  <button
    onclick={() => (open = !open)}
    class="flex items-center gap-1 text-[10px] font-mono text-[var(--text-secondary)] hover:text-[var(--text-primary)] transition-colors"
    title="Changelog"
  >
    v{latestVersion.version}
    <span class="opacity-50 font-sans">·</span>
    <span class="font-sans">{fmt(latestVersion.date)}</span>
  </button>

  {#if open}
    <div class="fixed inset-0 z-40" role="presentation" onclick={() => (open = false)}></div>
    <div class="absolute bottom-7 right-0 z-50 w-80 rounded-xl border border-[var(--border)] bg-[var(--bg-primary)] shadow-2xl text-xs overflow-hidden">
      <div class="flex items-center justify-between px-3 py-2 border-b border-[var(--border)]">
        <span class="font-semibold text-[var(--text-primary)] text-[11px]">Changelog</span>
        <button
          onclick={() => (open = false)}
          class="w-5 h-5 flex items-center justify-center rounded hover:bg-[var(--bg-tertiary)] text-[var(--text-secondary)]"
        >&times;</button>
      </div>
      <div class="max-h-80 overflow-y-auto divide-y divide-[var(--border)]">
        {#each changelog as entry}
          <div class="px-3 py-2.5">
            <div class="flex items-baseline justify-between mb-1">
              <span class="font-mono font-semibold text-[var(--text-primary)]">v{entry.version}</span>
              <span class="text-[10px] text-[var(--text-secondary)]">{fmt(entry.date)}</span>
            </div>
            <p class="text-[var(--text-secondary)] italic mb-1.5 text-[11px]">{entry.summary}</p>
            <ul class="space-y-0.5">
              {#each entry.changes as change}
                <li class="text-[var(--text-secondary)] flex gap-1.5">
                  <span class="text-[var(--accent)] shrink-0">·</span>
                  <span>{change}</span>
                </li>
              {/each}
            </ul>
          </div>
        {/each}
      </div>
    </div>
  {/if}
</div>
