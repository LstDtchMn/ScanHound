<script lang="ts">
  import { onMount } from 'svelte';
  import {
    plexMetadataScanStatus,
    refreshPlexMetadataScanStatus,
    startPlexMetadataScan,
    cancelPlexMetadataScan
  } from '$lib/stores/plex';
  import { addToast } from '$lib/stores/notifications';
  import { formatEta } from './plexMetadataScanFormat';

  let busy = $state(false);
  // Minimal selected-scope entry point (Task 4) -- no Plex-library item
  // picker/checklist component exists elsewhere in this codebase to reuse,
  // so this is a deliberate YAGNI textarea fallback: paste plex_cache keys
  // (one per line) to scan just those titles. The scope: 'selected' route
  // itself already existed server-side since Task 2.
  let selectedIdsRaw = $state('');

  const isRunning = $derived($plexMetadataScanStatus.status === 'running');
  const progressPct = $derived(
    $plexMetadataScanStatus.total > 0
      ? Math.round(($plexMetadataScanStatus.processed / $plexMetadataScanStatus.total) * 100)
      : 0
  );

  onMount(() => {
    refreshPlexMetadataScanStatus();
  });

  async function scanAll() {
    busy = true;
    try {
      await startPlexMetadataScan('all');
    } catch (e) {
      addToast('Metadata Scan', e instanceof Error ? e.message : 'Failed to start scan', 'error');
    } finally {
      busy = false;
    }
  }

  async function cancel() {
    busy = true;
    try {
      await cancelPlexMetadataScan();
    } catch (e) {
      addToast('Metadata Scan', e instanceof Error ? e.message : 'Failed to cancel scan', 'error');
    } finally {
      busy = false;
    }
  }

  async function scanSelected() {
    const ids = selectedIdsRaw
      .split('\n')
      .map((s) => s.trim())
      .filter(Boolean);
    if (ids.length === 0) return;
    busy = true;
    try {
      await startPlexMetadataScan('selected', ids);
    } catch (e) {
      addToast('Metadata Scan', e instanceof Error ? e.message : 'Failed to start scan', 'error');
    } finally {
      busy = false;
    }
  }
</script>

<div class="bg-[var(--bg-secondary)] rounded-lg p-5 border border-[var(--border)] space-y-4">
  <h3 class="text-sm font-semibold text-[var(--text-secondary)] uppercase tracking-wide">Library Metadata Scan</h3>
  <p class="text-xs text-[var(--text-secondary)]">
    Populates resolution, audio, HDR, and Dolby Vision FEL/MEL layer data for every movie already in your Plex
    library. This is a full heavy scan (including the slow Dolby Vision layer check) and can take hours for a
    large 4K/DV library -- cancel and re-run any time; already-scanned files are skipped near-instantly.
  </p>

  {#if isRunning}
    <div>
      <div class="flex justify-between text-[10px] text-[var(--text-secondary)] mb-0.5">
        <span>{$plexMetadataScanStatus.processed} / {$plexMetadataScanStatus.total} ({progressPct}%)</span>
        <span>ETA {formatEta($plexMetadataScanStatus.eta_seconds)}</span>
      </div>
      <div class="h-1 bg-[var(--bg-tertiary)] rounded-full overflow-hidden">
        <div
          class="h-full bg-[var(--accent)] transition-all duration-300 rounded-full"
          style="width: {progressPct}%"
        ></div>
      </div>
    </div>
    {#if $plexMetadataScanStatus.current_files.length > 0}
      <p class="text-xs text-[var(--text-secondary)] truncate">
        Scanning: {$plexMetadataScanStatus.current_files.join(', ')}
      </p>
    {/if}
    <button
      onclick={cancel}
      disabled={busy}
      class="px-4 py-2 text-sm rounded-lg bg-[var(--error)] hover:bg-red-600 text-white font-medium transition-colors disabled:opacity-50"
    >
      {busy ? 'Cancelling...' : 'Cancel Scan'}
    </button>
  {:else}
    <button
      onclick={scanAll}
      disabled={busy}
      class="px-4 py-2 text-sm rounded-lg bg-[var(--bg-tertiary)] hover:bg-[var(--border)] text-[var(--text-primary)] border border-[var(--border)] transition-colors disabled:opacity-50"
    >
      {busy ? 'Starting...' : 'Scan All Movies'}
    </button>

    {#if $plexMetadataScanStatus.status === 'done'}
      <p class="text-xs text-[var(--success)]">
        Last scan complete -- {$plexMetadataScanStatus.processed} file(s) processed.
      </p>
    {:else if $plexMetadataScanStatus.status === 'cancelled'}
      <p class="text-xs text-[var(--text-secondary)]">
        Last scan cancelled at {$plexMetadataScanStatus.processed} / {$plexMetadataScanStatus.total}.
      </p>
    {:else if $plexMetadataScanStatus.status === 'error'}
      <p class="text-xs text-[var(--error)]">Last scan failed: {$plexMetadataScanStatus.error}</p>
    {/if}

    <details class="relative">
      <summary
        class="list-none cursor-pointer px-2 py-1 rounded text-[11px] text-[var(--text-secondary)] bg-[var(--bg-tertiary)] border border-[var(--border)] hover:border-[var(--accent)] select-none w-fit"
      >
        Scan specific titles
      </summary>
      <div class="mt-2 space-y-2">
        <p class="text-xs text-[var(--text-secondary)]">
          Paste one Plex item key per line (found via the library API or an existing conflict/compare view).
        </p>
        <textarea
          bind:value={selectedIdsRaw}
          rows="3"
          placeholder={'key1\nkey2'}
          class="w-full text-xs rounded-lg bg-[var(--bg-tertiary)] border border-[var(--border)] text-[var(--text-primary)] p-2 font-mono"
        ></textarea>
        <button
          onclick={scanSelected}
          disabled={busy || !selectedIdsRaw.trim()}
          class="px-4 py-2 text-sm rounded-lg bg-[var(--bg-tertiary)] hover:bg-[var(--border)] text-[var(--text-primary)] border border-[var(--border)] transition-colors disabled:opacity-50"
        >
          {busy ? 'Starting...' : 'Scan These Titles'}
        </button>
      </div>
    </details>
  {/if}
</div>
