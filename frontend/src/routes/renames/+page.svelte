<script lang="ts">
  import { onMount } from 'svelte';
  import {
    renameJobs, renameStatus, loadRenameJobs, loadRenameStatus,
    applyJob, undoJob, deleteJob, rematchJob,
    acceptCombinedJob, acceptCorrectionJob, folderPreview,
    dvScanProgress, dvScanResult, dvScans, dvCounts, loadDvScans, dvScanRunning
  } from '$lib/stores/renames';
  import { get } from 'svelte/store';
  import { addToast } from '$lib/stores/notifications';
  import { api } from '$lib/api/client';
  import type { RenameJob } from '$lib/api/types';

  type Filter = 'all' | 'needs_review' | 'matched' | 'applied' | 'failed';
  let filter = $state<Filter>('all');
  let busy = $state<number | null>(null);

  // Manual "process a folder" — rename an existing backlog with no JDownloader.
  // Remember the last folder the user processed instead of hardcoding a
  // host-specific path; empty on first use (the input shows an example).
  let folderOpen = $state(false);
  let folderPath = $state(
    (typeof localStorage !== 'undefined' && localStorage.getItem('sh-process-folder')) || ''
  );
  let folderBusy = $state(false);
  async function processFolder() {
    const folder = folderPath.trim();
    if (!folder || folderBusy) return;
    try { localStorage.setItem('sh-process-folder', folder); } catch {}
    folderBusy = true;
    try {
      await api.renameProcessFolder(folder);
      addToast('Process folder', 'Scanning — rename jobs will appear here as they are identified.');
      folderOpen = false;
      // Jobs are created in the background; poll a few times so they show up.
      for (const d of [2000, 5000, 10000]) setTimeout(loadRenameJobs, d);
    } catch (e) {
      addToast('Error', e instanceof Error ? e.message : 'Failed to start folder processing', 'error');
    } finally {
      folderBusy = false;
    }
  }

  // Dry run: identify + propose targets without creating any jobs or moving
  // files. Result arrives over the WebSocket into the folderPreview store.
  let previewBusy = $state(false);
  async function previewFolder() {
    const folder = folderPath.trim();
    if (!folder || previewBusy) return;
    try { localStorage.setItem('sh-process-folder', folder); } catch {}
    previewBusy = true;
    folderPreview.set(null);
    try {
      await api.renameProcessFolder(folder, true);
      addToast('Preview', 'Identifying — a preview of what would happen will appear below.');
    } catch (e) {
      addToast('Error', e instanceof Error ? e.message : 'Failed to start preview', 'error');
    } finally {
      // The preview itself runs in the background; re-enable shortly after.
      setTimeout(() => (previewBusy = false), 1500);
    }
  }

  // Dolby Vision FEL/MEL scan of a folder — populates the DV inventory.
  let dvOpen = $state(false);
  let dvPath = $state(
    (typeof localStorage !== 'undefined' && localStorage.getItem('sh-dv-folder')) || ''
  );
  let dvForce = $state(false);
  const dvFallbackBadge = 'bg-[var(--bg-tertiary)] text-[var(--text-secondary)]';
  const dvBadge: Record<string, string> = {
    fel: 'bg-amber-500/20 text-amber-600 dark:text-amber-400',
    mel: 'bg-sky-500/20 text-sky-600 dark:text-sky-400',
    profile5: 'bg-[var(--bg-tertiary)] text-[var(--text-secondary)]',
    profile8: 'bg-[var(--bg-tertiary)] text-[var(--text-secondary)]',
    none: 'bg-[var(--bg-tertiary)] text-[var(--text-secondary)]',
    unknown: 'bg-[var(--error)]/15 text-[var(--error)]'
  };
  const dvLabel: Record<string, string> = {
    fel: 'FEL', mel: 'MEL', profile5: 'P5', profile8: 'P8', none: 'No DV', unknown: '?'
  };
  async function dvScan() {
    const folder = dvPath.trim();
    if (!folder || get(dvScanRunning)) return;  // guard: one scan at a time
    try { localStorage.setItem('sh-dv-folder', folder); } catch {}
    // Stay "running" until the backend broadcasts dv:scan_done (the POST itself
    // returns immediately). dv:scan_done always fires — success, error, or busy —
    // so the button re-enables exactly when the scan actually finishes.
    dvScanRunning.set(true);
    dvScanResult.set(null);
    try {
      await api.dvScanFolder(folder, dvForce);
      addToast('Dolby Vision', 'Scanning — this reads each file, so it can take a while.');
    } catch (e) {
      dvScanRunning.set(false);
      addToast('Error', e instanceof Error ? e.message : 'Failed to start DV scan', 'error');
    }
  }

  // Re-run identification on existing jobs (no need to remove stale ones first).
  async function reidentify(id: number) {
    if (busy === id) return;
    busy = id;
    try {
      const r = await api.reidentifyRename(id);
      if (r.ok) addToast('Re-identify', 'Re-matched with the current matcher.');
      else addToast('Re-identify', r.error || 'Could not re-identify', 'warning');
      await loadRenameJobs();
      await loadRenameStatus();
    } catch (e) {
      addToast('Error', e instanceof Error ? e.message : 'Re-identify failed', 'error');
    } finally {
      busy = null;
    }
  }

  let reidentifyingAll = $state(false);
  async function reidentifyAll() {
    if (reidentifyingAll) return;
    reidentifyingAll = true;
    try {
      await api.reidentifyAllRenames();
      addToast('Re-identify all', 'Re-running the matcher on all reviewable jobs…');
      for (const d of [2000, 5000, 10000]) setTimeout(() => { loadRenameJobs(); loadRenameStatus(); }, d);
    } catch (e) {
      addToast('Error', e instanceof Error ? e.message : 'Failed to start re-identify', 'error');
    } finally {
      reidentifyingAll = false;
    }
  }

  const filters: { value: Filter; label: string }[] = [
    { value: 'all', label: 'All' },
    { value: 'needs_review', label: 'Needs review' },
    { value: 'matched', label: 'Matched' },
    { value: 'applied', label: 'Applied' },
    { value: 'failed', label: 'Failed' }
  ];

  onMount(() => {
    loadRenameJobs();
    loadRenameStatus();
    loadDvScans();
  });

  let shown = $derived(
    filter === 'all' ? $renameJobs : $renameJobs.filter((j) => j.status === filter)
  );

  function statusClass(job: RenameJob): string {
    if (job.status === 'failed') return 'bg-[var(--error)]/15 text-[var(--error)]';
    if (job.status === 'needs_review') return 'bg-amber-500/15 text-amber-600 dark:text-amber-400';
    if (job.status === 'applied') return 'bg-[var(--success)]/15 text-[var(--success)]';
    if (job.status === 'reverted') return 'bg-[var(--bg-tertiary)] text-[var(--text-secondary)] line-through';
    return 'bg-[var(--bg-tertiary)] text-[var(--text-secondary)]';
  }

  async function run(id: number, fn: (id: number) => Promise<void>, ok: string) {
    busy = id;
    try {
      await fn(id);
      addToast('Rename', ok);
    } catch {
      addToast('Rename', 'Action failed', 'error');
    } finally {
      busy = null;
    }
  }

  // Rematch state — one job can have its form open at a time
  let rematchOpenId = $state<number | null>(null);
  let rematchTmdbInput = $state('');
  let rematchMediaType = $state<'movie' | 'tv'>('movie');
  let rematchBusy = $state(false);

  function openRematch(job: RenameJob) {
    rematchOpenId = job.id;
    rematchTmdbInput = '';
    rematchMediaType = (job.media_type === 'tv' || job.media_type === 'show') ? 'tv' : 'movie';
  }

  async function submitRematch(jobId: number) {
    const id = parseInt(rematchTmdbInput.trim(), 10);
    if (!id || isNaN(id)) { addToast('Rematch', 'Enter a valid TMDB ID', 'error'); return; }
    rematchBusy = true;
    try {
      await rematchJob(jobId, id, rematchMediaType);
      addToast('Rematch', 'Re-matched successfully');
      rematchOpenId = null;
    } catch {
      addToast('Rematch', 'Rematch failed', 'error');
    } finally {
      rematchBusy = false;
    }
  }

  function tmdbSearchUrl(job: RenameJob): string {
    // Strip extension + quality tags for a cleaner search query
    const raw = job.original_filename ?? '';
    const base = raw.replace(/\.[^.]+$/, '').replace(/[._]?(1080p|2160p|4K|BluRay|WEB[-.]DL|HDTV|x264|x265|HEVC|AAC|AC3|DTS|H\.?264|H\.?265).*/i, '').replace(/[._]/g, ' ').trim();
    return `https://www.themoviedb.org/search?query=${encodeURIComponent(base || raw)}`;
  }

  function relTime(s: string | null): string {
    if (!s) return '';
    const iso = s.includes('T') ? s : s.replace(' ', 'T') + 'Z';
    const t = Date.parse(iso);
    if (Number.isNaN(t)) return '';
    const secs = Math.max(0, Math.round((Date.now() - t) / 1000));
    if (secs < 60) return 'just now';
    const m = Math.round(secs / 60);
    if (m < 60) return `${m}m ago`;
    const h = Math.round(m / 60);
    if (h < 24) return `${h}h ago`;
    return `${Math.round(h / 24)}d ago`;
  }
</script>

<div class="flex-1 overflow-auto">
  <div class="px-4 py-3 border-b border-[var(--border)]">
    <div class="flex items-center justify-between">
      <h1 class="text-lg font-semibold">Renames</h1>
      <div class="flex items-center gap-3">
        <button
          onclick={() => (folderOpen = !folderOpen)}
          class="text-xs px-2.5 py-1 rounded bg-[var(--bg-tertiary)] border border-[var(--border)] text-[var(--text-secondary)] hover:border-[var(--accent)] hover:text-[var(--text-primary)]"
        >Process folder…</button>
        <button
          onclick={() => (dvOpen = !dvOpen)}
          title="Scan a folder for Dolby Vision FEL/MEL"
          class="text-xs px-2.5 py-1 rounded bg-[var(--bg-tertiary)] border border-[var(--border)] text-[var(--text-secondary)] hover:border-[var(--accent)] hover:text-[var(--text-primary)]"
        >Dolby Vision…</button>
        <button
          onclick={reidentifyAll}
          disabled={reidentifyingAll}
          title="Re-run identification on all reviewable jobs with the current matcher"
          class="text-xs px-2.5 py-1 rounded bg-[var(--bg-tertiary)] border border-[var(--border)] text-[var(--text-secondary)] hover:border-[var(--accent)] hover:text-[var(--text-primary)] disabled:opacity-50"
        >{reidentifyingAll ? 'Re-identifying…' : 'Re-identify all'}</button>
        <button
          onclick={() => { loadRenameJobs(); loadRenameStatus(); }}
          class="text-xs text-[var(--text-secondary)] hover:text-[var(--text-primary)]"
        >Refresh</button>
      </div>
    </div>
    {#if folderOpen}
      <div class="mt-2 flex items-center gap-2 flex-wrap">
        <input
          type="text"
          bind:value={folderPath}
          placeholder="F:\Downloads"
          onkeydown={(e) => e.key === 'Enter' && processFolder()}
          class="flex-1 min-w-[12rem] bg-[var(--bg-tertiary)] text-[var(--text-primary)] px-3 py-1.5 rounded-lg border border-[var(--border)] text-sm font-mono focus:outline-none focus:border-[var(--accent)]"
        />
        <button
          onclick={previewFolder}
          disabled={previewBusy || !folderPath.trim()}
          title="Identify without creating jobs or moving files"
          class="px-3 py-1.5 text-sm rounded-lg bg-[var(--bg-tertiary)] border border-[var(--border)] text-[var(--text-secondary)] hover:border-[var(--accent)] hover:text-[var(--text-primary)] transition disabled:opacity-50"
        >{previewBusy ? 'Previewing…' : 'Preview'}</button>
        <button
          onclick={processFolder}
          disabled={folderBusy || !folderPath.trim()}
          class="px-3 py-1.5 text-sm rounded-lg bg-[var(--accent)] hover:opacity-90 text-white font-medium transition disabled:opacity-50"
        >{folderBusy ? 'Starting…' : 'Process'}</button>
      </div>
      <p class="mt-1.5 text-xs text-[var(--text-secondary)]">
        Scans a folder for video files and creates rename jobs for each — for renaming an existing backlog without JDownloader.
        <strong>Preview</strong> shows what would happen without creating jobs or moving anything.
        Host paths (e.g. <code>F:\Downloads</code>) are translated to the container's mounted view; matches still go through review before moving.
      </p>
      {#if $folderPreview}
        <div class="mt-2 rounded-lg border border-[var(--border)] overflow-hidden">
          <div class="px-3 py-1.5 bg-[var(--bg-tertiary)] text-xs flex items-center justify-between">
            <span>
              {#if $folderPreview.error}
                <span class="text-[var(--error)]">{$folderPreview.error}</span>
              {:else}
                Preview: <strong>{$folderPreview.would_match ?? 0}</strong> of {$folderPreview.found} file(s) would match
              {/if}
            </span>
            <button onclick={() => folderPreview.set(null)} class="text-[var(--text-secondary)] hover:text-[var(--text-primary)]">Dismiss</button>
          </div>
          {#if $folderPreview.previews?.length}
            <div class="max-h-72 overflow-auto divide-y divide-[var(--border)]">
              {#each $folderPreview.previews as p}
                <div class="px-3 py-1.5 flex items-center gap-2 text-xs">
                  <span class="shrink-0 px-1.5 py-0.5 rounded {statusClass({ status: p.tracked ? 'matched' : p.status } as RenameJob)}">
                    {p.tracked ? 'tracked' : p.status === 'matched' ? `${p.confidence}` : 'review'}
                  </span>
                  <span class="font-mono truncate text-[var(--text-secondary)] flex-1" title={p.filename}>{p.filename}</span>
                  <span class="opacity-60 shrink-0">→</span>
                  <span class="truncate flex-1 {p.new_filename ? '' : 'text-[var(--text-secondary)] italic'}" title={p.new_filename ?? ''}>
                    {p.new_filename ?? (p.title ? `${p.title}${p.year ? ` (${p.year})` : ''}` : 'no match')}
                  </span>
                </div>
              {/each}
            </div>
          {/if}
          {#if $folderPreview.note}
            <p class="px-3 py-1.5 text-[11px] text-[var(--text-secondary)] border-t border-[var(--border)]">{$folderPreview.note}</p>
          {/if}
        </div>
      {/if}
    {/if}
    {#if dvOpen}
      <div class="mt-2 flex items-center gap-2 flex-wrap">
        <input
          type="text"
          bind:value={dvPath}
          placeholder="G:\Downloads"
          onkeydown={(e) => e.key === 'Enter' && dvScan()}
          class="flex-1 min-w-[12rem] bg-[var(--bg-tertiary)] text-[var(--text-primary)] px-3 py-1.5 rounded-lg border border-[var(--border)] text-sm font-mono focus:outline-none focus:border-[var(--accent)]"
        />
        <label class="text-xs text-[var(--text-secondary)] flex items-center gap-1 select-none">
          <input type="checkbox" bind:checked={dvForce} /> Re-scan all
        </label>
        <button
          onclick={dvScan}
          disabled={$dvScanRunning || !dvPath.trim()}
          class="px-3 py-1.5 text-sm rounded-lg bg-[var(--accent)] hover:opacity-90 text-white font-medium transition disabled:opacity-50"
        >{$dvScanRunning ? 'Scanning…' : 'Scan'}</button>
      </div>
      <p class="mt-1.5 text-xs text-[var(--text-secondary)]">
        Reads each video with <code>dovi_tool</code> to detect Dolby Vision <strong>FEL</strong> vs <strong>MEL</strong>, recording results below.
        It's a full read per file, so it's slow; unchanged files are skipped unless <em>Re-scan all</em> is set. Detection only — no files are moved or modified.
      </p>
      {#if $dvScanProgress}
        <div class="mt-2 text-xs text-[var(--text-secondary)]">
          Scanning {$dvScanProgress.done}/{$dvScanProgress.total}:
          <span class="font-mono">{$dvScanProgress.file}</span>
          {#if $dvScanProgress.layer}<span class="px-1.5 py-0.5 rounded {dvBadge[$dvScanProgress.layer] ?? dvFallbackBadge}">{dvLabel[$dvScanProgress.layer] ?? $dvScanProgress.layer}</span>{/if}
        </div>
      {/if}
      {#if $dvScanResult && !$dvScanProgress}
        <div class="mt-2 text-xs">
          {#if $dvScanResult.error}
            <span class="text-[var(--error)]">{$dvScanResult.error}</span>
          {:else}
            Scanned <strong>{$dvScanResult.scanned}</strong> of {$dvScanResult.found} file(s){#if $dvScanResult.skipped}, {$dvScanResult.skipped} unchanged{/if}.
          {/if}
        </div>
      {/if}
      {#if Object.keys($dvCounts).length}
        <div class="mt-2 flex items-center gap-1.5 flex-wrap text-[11px]">
          <span class="text-[var(--text-secondary)]">Inventory:</span>
          {#each Object.entries($dvCounts) as [layer, n]}
            <span class="px-1.5 py-0.5 rounded {dvBadge[layer] ?? dvFallbackBadge}">{dvLabel[layer] ?? layer} {n}</span>
          {/each}
        </div>
        <div class="mt-2 rounded-lg border border-[var(--border)] max-h-72 overflow-auto divide-y divide-[var(--border)]">
          {#each $dvScans as s}
            <div class="px-3 py-1.5 flex items-center gap-2 text-xs">
              <span class="shrink-0 px-1.5 py-0.5 rounded {dvBadge[s.dv_layer] ?? dvFallbackBadge}">{dvLabel[s.dv_layer] ?? s.dv_layer}</span>
              <span class="truncate flex-1" title={s.path}>{s.title ?? s.path}</span>
            </div>
          {/each}
        </div>
      {/if}
    {/if}
    {#if $renameStatus && !$renameStatus.enabled}
      <p class="mt-1 text-xs text-[var(--text-secondary)]">
        Auto-rename is off. Enable it in
        <a href="/settings" class="text-[var(--accent)] hover:underline">Settings → Renaming</a>
        to track and organise extracted downloads.
      </p>
    {:else if $renameStatus && $renameStatus.needs_review > 0}
      <p class="mt-1 text-xs text-amber-600 dark:text-amber-400">
        {$renameStatus.needs_review} item{$renameStatus.needs_review === 1 ? '' : 's'} need review.
      </p>
    {/if}
    <div class="mt-3 flex gap-1 overflow-x-auto">
      {#each filters as f}
        <button
          onclick={() => (filter = f.value)}
          class="px-3 py-1.5 text-xs rounded-lg whitespace-nowrap transition-colors {filter === f.value ? 'bg-[var(--accent)] text-white' : 'text-[var(--text-secondary)] hover:bg-[var(--bg-tertiary)]'}"
        >
          {f.label}{#if $renameStatus?.counts[f.value]} ({$renameStatus.counts[f.value]}){/if}
        </button>
      {/each}
    </div>
  </div>

  {#if shown.length === 0}
    <div class="p-8 text-center text-sm text-[var(--text-secondary)]">
      No rename jobs{filter !== 'all' ? ` (${filter.replace('_', ' ')})` : ''} yet.
    </div>
  {:else}
    <ul class="divide-y divide-[var(--border)]">
      {#each shown as job (job.id)}
        <li class="px-4 py-3">
          <div class="flex items-start gap-3">
            <div class="flex-1 min-w-0">
              <div class="flex items-center gap-2 flex-wrap">
                <span class="text-[10px] px-1.5 py-0.5 rounded uppercase font-medium tracking-wide {statusClass(job)}">{job.status.replace('_', ' ')}</span>
                {#if job.match_source === 'llm'}
                  <span class="text-[10px] px-1.5 py-0.5 rounded bg-[var(--accent)]/15 text-[var(--accent)] font-medium">LLM</span>
                {/if}
                {#if job.destination_conflict}
                  <span class="text-[10px] px-1.5 py-0.5 rounded bg-orange-500/15 text-orange-600 dark:text-orange-400 font-medium"
                        title="Another job targets the same destination file — apply only one">⚠ Duplicate</span>
                {/if}
                {#if job.keep_recommended}
                  <span class="text-[10px] px-1.5 py-0.5 rounded bg-[var(--success)]/15 text-[var(--success)] font-medium"
                        title={job.keep_reason ? `Best of the duplicates: ${job.keep_reason}` : 'Recommended copy to keep'}>★ Keep{job.keep_reason ? ` · ${job.keep_reason}` : ''}</span>
                {/if}
                {#if job.match_confidence != null}
                  <span class="text-[10px] text-[var(--text-secondary)]">{Math.round(job.match_confidence)}%</span>
                {/if}
                {#if job.media_type}
                  <span class="text-[10px] text-[var(--text-secondary)] uppercase">{job.media_type}</span>
                {/if}
                <span class="text-[10px] text-[var(--text-secondary)] ml-auto">{relTime(job.detected_at)}</span>
              </div>
              <div class="mt-1 text-sm truncate" title={job.original_filename ?? ''}>{job.original_filename}</div>
              {#if job.new_filename}
                <div class="text-xs text-[var(--text-secondary)] truncate" title={job.new_filename}>→ {job.new_filename}</div>
              {/if}
              {#if job.warning_message}
                <div class="text-[11px] text-amber-600 dark:text-amber-400 mt-0.5">{job.warning_message}</div>
              {/if}
              {#if job.error_message}
                <div class="text-[11px] text-[var(--error)] mt-0.5">{job.error_message}</div>
              {/if}
            </div>
            <div class="flex flex-col gap-1 shrink-0">
              {#if job.status === 'matched' || job.status === 'needs_review'}
                <button onclick={() => run(job.id, applyJob, 'Applied')} disabled={busy === job.id}
                  class="px-2.5 py-1 text-xs rounded-lg bg-[var(--accent)] text-white hover:opacity-90 disabled:opacity-50">Apply</button>
              {/if}
              {#if job.status === 'needs_review' || job.status === 'failed'}
                <button
                  onclick={() => reidentify(job.id)}
                  disabled={busy === job.id}
                  class="px-2.5 py-1 text-xs rounded-lg border border-[var(--border)] text-[var(--text-secondary)] hover:bg-[var(--bg-tertiary)] disabled:opacity-50"
                  title="Re-run automatic identification with the current matcher"
                >Re-identify</button>
                <button
                  onclick={() => rematchOpenId === job.id ? (rematchOpenId = null) : openRematch(job)}
                  class="px-2.5 py-1 text-xs rounded-lg border border-[var(--border)] text-[var(--text-secondary)] hover:bg-[var(--bg-tertiary)]"
                  title="Re-match to a different TMDB title"
                >Rematch</button>
              {/if}
              {#if job.status === 'needs_review' && job.combined_episode}
                <button
                  onclick={() => run(job.id, acceptCombinedJob, 'Accepted as combined')}
                  disabled={busy === job.id}
                  class="px-2.5 py-1 text-xs rounded-lg bg-amber-500/20 text-amber-700 dark:text-amber-300 hover:bg-amber-500/30 disabled:opacity-50"
                  title="Confirm this is a combined double-episode file"
                >Accept {job.combined_episode.proposed_code}</button>
              {/if}
              {#if job.status === 'needs_review' && job.suggested_correction}
                <button
                  onclick={() => run(job.id, acceptCorrectionJob, 'Correction applied')}
                  disabled={busy === job.id}
                  class="px-2.5 py-1 text-xs rounded-lg bg-amber-500/20 text-amber-700 dark:text-amber-300 hover:bg-amber-500/30 disabled:opacity-50"
                  title="Use the proposed episode correction"
                >Accept S{String(job.suggested_correction.proposed.season).padStart(2,'0')}E{String(job.suggested_correction.proposed.episode).padStart(2,'0')}</button>
              {/if}
              {#if job.status === 'applied'}
                <button onclick={() => run(job.id, undoJob, 'Reverted')} disabled={busy === job.id}
                  class="px-2.5 py-1 text-xs rounded-lg border border-[var(--border)] text-[var(--text-secondary)] hover:bg-[var(--bg-tertiary)] disabled:opacity-50">Undo</button>
              {/if}
              <button onclick={() => run(job.id, deleteJob, 'Removed')} disabled={busy === job.id}
                class="px-2.5 py-1 text-xs rounded-lg text-[var(--text-secondary)] hover:bg-[var(--bg-tertiary)] disabled:opacity-50">Remove</button>
            </div>
          </div>
          {#if rematchOpenId === job.id}
            <div class="mt-2 p-3 rounded-lg bg-[var(--bg-tertiary)] border border-[var(--border)] text-xs space-y-2">
              <p class="text-[var(--text-secondary)]">Find the correct entry on TMDB, then paste its numeric ID here.</p>
              <a href={tmdbSearchUrl(job)} target="_blank" rel="noopener" class="inline-flex items-center gap-1 text-[var(--accent)] hover:underline">
                Search TMDB ↗
              </a>
              <div class="flex items-center gap-2 flex-wrap">
                <select bind:value={rematchMediaType} class="px-2 py-1 rounded bg-[var(--bg-secondary)] border border-[var(--border)] text-[var(--text-primary)] focus:outline-none focus:border-[var(--accent)]">
                  <option value="movie">Movie</option>
                  <option value="tv">TV Show</option>
                </select>
                <input
                  type="number"
                  bind:value={rematchTmdbInput}
                  placeholder="TMDB ID (e.g. 550)"
                  class="flex-1 min-w-0 px-2 py-1 rounded bg-[var(--bg-secondary)] border border-[var(--border)] text-[var(--text-primary)] placeholder:text-[var(--text-secondary)] focus:outline-none focus:border-[var(--accent)]"
                  onkeydown={(e) => e.key === 'Enter' && submitRematch(job.id)}
                />
                <button onclick={() => submitRematch(job.id)} disabled={rematchBusy}
                  class="px-2.5 py-1 rounded bg-[var(--accent)] text-white hover:opacity-90 disabled:opacity-50">
                  {rematchBusy ? '…' : 'Submit'}
                </button>
                <button onclick={() => (rematchOpenId = null)} class="px-2.5 py-1 rounded text-[var(--text-secondary)] hover:bg-[var(--border)]">Cancel</button>
              </div>
            </div>
          {/if}
        </li>
      {/each}
    </ul>
  {/if}
</div>
