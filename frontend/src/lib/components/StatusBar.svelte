<script lang="ts">
  import { stats, selectedKeys } from '$lib/stores/results';
  import { logs, logPanelOpen } from '$lib/stores/logs';
  import { plexConnected } from '$lib/stores/plex';
  import { jdConnection } from '$lib/stores/jdownloader';
  import { settings, settingsLoaded } from '$lib/stores/settings';
  import ChangelogBadge from './ChangelogBadge.svelte';

  let errorCount = $derived($logs.filter(l => l.level === 'error' || l.level === 'warning').length);
  let selectedCount = $derived($selectedKeys.size);

  let metaOk = $derived($settingsLoaded && (!!$settings.tmdb_api_key || !!$settings.omdb_api_key));
  let jdEnabled = $derived($settingsLoaded && !!$settings.jd_enabled);
</script>

<!-- Desktop status bar; on mobile the filter chips + swipe footer convey this. -->
<div class="hidden md:flex items-center gap-4 px-4 py-2 border-t border-[var(--border)] text-xs text-[var(--text-secondary)]">
  <span>Total: <strong class="text-[var(--text-primary)]">{$stats.total}</strong></span>
  <span>Missing: <strong class="text-[var(--error)]">{$stats.missing}</strong></span>
  <span>Upgrades: <strong class="text-[var(--warning)]">{$stats.upgrade}</strong></span>
  <span>In Library: <strong class="text-[var(--success)]">{$stats.library}</strong></span>

  {#if selectedCount > 0}
    <span>Selected: <strong class="text-[var(--accent)]">{selectedCount}</strong></span>
  {/if}

  <div class="flex-1"></div>

  <!-- Always-visible connection status dots -->
  <div class="flex items-center gap-2.5">
    <span
      class="flex items-center gap-1 cursor-default"
      title={$plexConnected ? 'Plex connected' : 'Plex not connected'}
    >
      <span class="w-1.5 h-1.5 rounded-full {$plexConnected ? 'bg-[var(--success)]' : 'bg-[var(--error)]'}"></span>
      <span class="text-[10px]">Plex</span>
    </span>
    <span
      class="flex items-center gap-1 cursor-default"
      title={metaOk ? 'Metadata API configured' : 'No metadata API key — set TMDB or OMDB in Settings → Sources'}
    >
      <span class="w-1.5 h-1.5 rounded-full {!$settingsLoaded ? 'bg-[var(--text-secondary)] opacity-40 animate-pulse' : metaOk ? 'bg-[var(--success)]' : 'bg-amber-400'}"></span>
      <span class="text-[10px]">Meta</span>
    </span>
    {#if jdEnabled}
      <span
        class="flex items-center gap-1 cursor-default"
        title={$jdConnection.checking ? 'JDownloader checking…' : $jdConnection.connected ? 'JDownloader connected' : ($jdConnection.error ?? 'JDownloader not connected')}
      >
        <span class="w-1.5 h-1.5 rounded-full {$jdConnection.checking ? 'bg-amber-400 animate-pulse' : $jdConnection.connected ? 'bg-[var(--success)]' : 'bg-[var(--error)]'}"></span>
        <span class="text-[10px]">JD</span>
      </span>
    {/if}
  </div>

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

  <span class="text-[var(--border)]">|</span>
  <ChangelogBadge />
</div>
