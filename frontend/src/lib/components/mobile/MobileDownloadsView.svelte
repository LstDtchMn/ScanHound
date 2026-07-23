<script lang="ts">
  import { onMount, onDestroy } from 'svelte';
  import { api } from '$lib/api/client';
  import { addToast } from '$lib/stores/notifications';
  import { groupDownloads, type DownloadGroup } from '$lib/downloads/dupes';
  import type { DownloadResult } from '$lib/api/types';
  import VerificationRetries from '$lib/components/VerificationRetries.svelte';

  let results = $state<DownloadResult[]>([]);
  let loaded = $state(false);
  let busy = $state(false);
  let timer: ReturnType<typeof setTimeout> | null = null;
  let delay = 2500;
  let alive = true;
  let inFlight = false;

  const groups = $derived(groupDownloads(results));
  const active = $derived(results.filter((r) => r.state === 'downloading').length);
  const queued = $derived(results.filter((r) => r.state === 'queued').length);

  function pct(r: DownloadResult): number {
    return r.bytes_total > 0 ? Math.min(100, Math.round((r.bytes_loaded / r.bytes_total) * 100)) : 0;
  }
  function gb(bytes: number): string {
    return (bytes / 1e9).toFixed(1);
  }
  function stateLabel(s: string): string {
    return ({ queued: 'Queued', downloading: 'Downloading', downloaded: 'Downloaded',
      extracting: 'Extracting', extracted: 'Finished', failed: 'Failed' } as Record<string, string>)[s] || s;
  }

  async function poll() {
    if (inFlight) return;
    inFlight = true;
    try {
      results = await api.downloadResults();
      loaded = true;
      delay = 2500;
    } catch {
      delay = Math.min(delay * 2, 10000);   // back off on error, keep last list
    } finally {
      inFlight = false;
      if (alive && document.visibilityState === 'visible') timer = setTimeout(poll, delay);
    }
  }

  function onVisibility() {
    if (document.visibilityState === 'visible') {
      if (!timer && !inFlight) poll();
    } else if (timer) {
      clearTimeout(timer);
      timer = null;
    }
  }

  onMount(() => {
    poll();
    document.addEventListener('visibilitychange', onVisibility);
  });
  onDestroy(() => {
    alive = false;
    if (timer) clearTimeout(timer);
    document.removeEventListener('visibilitychange', onVisibility);
  });

  async function control(action: 'pause' | 'resume' | 'stop') {
    busy = true;
    try {
      const r = await api.jdControl(action);
      if (!r.ok) throw new Error(r.error || 'failed');
      addToast('JDownloader', `Sent ${action}`);
    } catch (e) {
      addToast('Error', e instanceof Error ? e.message : `Could not ${action}`, 'error');
    } finally {
      busy = false;
    }
  }

  async function clearFinished() {
    const done = results.filter((r) => r.state === 'extracted' || r.state === 'failed');
    if (!done.length) return;
    for (const r of done) {
      try { await api.removeDownloadResult(r.id); } catch { /* idempotent; ignore */ }
    }
    await poll();
  }

  async function cancel(r: DownloadResult) {
    try {
      await api.removeDownloadResult(r.id);
      results = results.filter((x) => x.id !== r.id);   // optimistic
      addToast('Removed', r.title || r.name);
    } catch (e) {
      addToast('Error', e instanceof Error ? e.message : 'Could not remove', 'error');
    }
  }

  async function keepBest(g: DownloadGroup) {
    if (!confirm(`Keep "${g.best.name}" and cancel ${g.activeItems.length - 1} other release(s) of ${g.title}?`)) return;
    for (const r of g.activeItems) if (r.id !== g.best.id) await cancel(r);
  }
</script>

<div class="flex flex-col h-full">
  <!-- Summary + global controls -->
  <div class="flex items-center gap-2 px-3 py-2 border-b border-[var(--border)] text-sm">
    <span class="text-[var(--text-secondary)]">{active} downloading · {queued} queued</span>
    <div class="flex-1"></div>
    <button class="px-2 py-1 rounded bg-[var(--bg-tertiary)] text-xs" disabled={busy} onclick={() => control('pause')}>Pause</button>
    <button class="px-2 py-1 rounded bg-[var(--bg-tertiary)] text-xs" disabled={busy} onclick={() => control('resume')}>Resume</button>
    <button class="px-2 py-1 rounded bg-[var(--bg-tertiary)] text-xs" disabled={busy} onclick={() => control('stop')}>Stop</button>
    <button class="px-2 py-1 rounded bg-[var(--bg-tertiary)] text-xs" onclick={clearFinished}>Clear&nbsp;done</button>
  </div>

  <div class="flex-1 overflow-y-auto">
    <VerificationRetries />
    <div class="p-3 space-y-3">
    {#if loaded && results.length === 0}
      <p class="text-center text-[var(--text-secondary)] mt-10">No active downloads.</p>
    {/if}

    {#each groups as g (g.key)}
      <div class="rounded-xl border border-[var(--border)] bg-[var(--bg-secondary)] p-3">
        {#if g.isDuplicate}
          <div class="flex items-center gap-2 mb-2">
            <span class="text-xs font-semibold px-2 py-0.5 rounded bg-amber-500/20 text-amber-400">{g.items.length} duplicates</span>
            <span class="text-sm font-medium truncate">{g.title}</span>
            <div class="flex-1"></div>
            {#if g.canKeepBest}
              <button class="text-xs px-2 py-1 rounded bg-[var(--accent)] text-white" onclick={() => keepBest(g)}>Keep best</button>
            {/if}
          </div>
        {/if}
        {#each g.items as r (r.id ?? r.package_uuid ?? r.name)}
          <div class="py-1.5 {g.isDuplicate ? 'pl-2 border-l-2 border-[var(--border)]' : ''}">
            <div class="flex items-center gap-2">
              {#if !g.isDuplicate}<span class="text-sm font-medium truncate">{r.title || r.name}</span>{/if}
              {#if g.isDuplicate}<span class="text-xs text-[var(--text-secondary)] truncate">{r.name}</span>{/if}
              <div class="flex-1"></div>
              <span class="text-xs {r.state === 'failed' ? 'text-red-400' : 'text-[var(--text-secondary)]'}">{stateLabel(r.state)}</span>
              <button class="text-xs px-2 py-0.5 rounded bg-[var(--bg-tertiary)]" onclick={() => cancel(r)} aria-label="Cancel download">Cancel</button>
            </div>
            <div class="mt-1 h-1.5 rounded bg-[var(--bg-tertiary)] overflow-hidden">
              <div class="h-full bg-[var(--accent)]" style="width: {pct(r)}%"></div>
            </div>
            <div class="mt-0.5 text-[11px] text-[var(--text-secondary)]">
              {gb(r.bytes_loaded)} / {gb(r.bytes_total)} GB · {r.host}{#if r.error} · <span class="text-red-400">{r.error}</span>{/if}
            </div>
          </div>
        {/each}
      </div>
    {/each}
    </div>
  </div>
</div>
