<script lang="ts">
  import { onMount } from 'svelte';
  import {
    renameJobs, renameStatus, renameCategory, renameQuery, renameSort,
    viewMode, renameQueue, applyCancelling, applyActive,
    loadRenameJobs, loadRenameStatus, loadDvScans,
    applyJob, undoJob, deleteJob, cancelApply,
    acceptCombinedJob, acceptCorrectionJob,
    dvScanProgress, dvScanResult, dvScans, dvCounts, dvScanRunning,
    dvSyncRunning, dvSyncProgress, dvSyncResult,
    archivedRenameJobs, loadArchivedRenameJobs, clearSelection
  } from '$lib/stores/renames';
  import { categoryOf } from '$lib/renames/category';
  import { matchesQuery } from '$lib/renames/review';
  import { groupJobsBySeason } from '$lib/renames/seasonGroups';
  import {
    tileSize, gridColumns, gridGap, TILE_MIN_PX, GRID_GAP_CLASS
  } from '$lib/stores/results';
  import { settings } from '$lib/stores/settings';
  import { api } from '$lib/api/client';
  import { addToast } from '$lib/stores/notifications';
  import type { RenameJob } from '$lib/api/types';
  import { isPhone } from '$lib/stores/viewport';

  import RenamesHeader from '$lib/components/renames/RenamesHeader.svelte';
  import StatusDashboard from '$lib/components/renames/StatusDashboard.svelte';
  import RenameFilterBar from '$lib/components/renames/RenameFilterBar.svelte';
  import BulkBar from '$lib/components/renames/BulkBar.svelte';
  import RenameRow from '$lib/components/renames/RenameRow.svelte';
  import SeasonGroupRow from '$lib/components/renames/SeasonGroupRow.svelte';
  import RenameCard from '$lib/components/renames/RenameCard.svelte';
  import RematchModal from '$lib/components/renames/RematchModal.svelte';
  import ConflictModal from '$lib/components/renames/ConflictModal.svelte';
  import TrashPanel from '$lib/components/renames/TrashPanel.svelte';
  import MobileRenamesView from '$lib/components/renames/MobileRenamesView.svelte';

  // Status filter is local orchestrator state (surfaced via the stat cards):
  // all | needs_review | matched | applied | failed.
  let statusFilter = $state<string>('all');
  // Controlled RematchModal — rows/cards call onRematch(job) → set this; modal
  // renders only while set.
  let rematchJob = $state<RenameJob | null>(null);
  // Controlled ConflictModal — same pattern as rematchJob: RenameRow calls
  // onCompare(job) → set this; modal renders only while set, at page root
  // (outside the <ul>) so it's never an invalid non-<li> child of it.
  let conflictJob = $state<RenameJob | null>(null);
  // The job whose legacy per-job action menu (Undo / Re-identify / Accept… /
  // Remove) is open. One at a time.
  let actionsOpenId = $state<number | null>(null);
  let busy = $state<number | null>(null);

  onMount(() => {
    loadRenameJobs();
    loadRenameStatus();
    loadDvScans();
  });

  // Archived jobs are excluded from the default (non-archived) load above —
  // fetch them fresh each time the Archived tab is selected. A selection
  // held from whatever tab was previously showing is cleared on every switch
  // too: the selected ids generally aren't even present in the newly-shown
  // list, so leaving them selected would show a stale "N selected" count in
  // BulkBar and — for Archived's Restore button specifically — a confusing
  // "Restored 0 job(s)…" toast if clicked against ids that aren't archived.
  $effect(() => {
    clearSelection();
    if (statusFilter === 'archived') {
      loadArchivedRenameJobs();
    }
  });

  // --- Re-identify all (ported verbatim from the old page) ---
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

  // Dolby Vision header button: open the inline DV scan panel + scroll to it.
  let dvOpen = $state(false);
  function dolbyVision() {
    dvOpen = true;
    // Defer until the panel is in the DOM (it lives in #dv-scan-surface).
    requestAnimationFrame(() =>
      document.getElementById('dv-scan-surface')?.scrollIntoView({ behavior: 'smooth' })
    );
  }

  // --- Dolby Vision FEL/MEL scan (ported verbatim from the old page) ---
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
    if (!folder || $dvScanRunning) return;  // guard: one scan at a time
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

  // --- Dolby Vision Plex label sync ---
  // The POST returns immediately; we stay "running" until dv:sync_done arrives
  // (the store's WS handler flips dvSyncRunning back to false — success or error).
  async function dvSync() {
    if ($dvSyncRunning) return; // guard: one sync at a time
    dvSyncRunning.set(true);
    dvSyncResult.set(null);
    try {
      await api.dvSyncLabels(false);
      addToast('Dolby Vision', 'Syncing Plex labels — matching detected layers to the copy Plex serves.');
    } catch (e) {
      dvSyncRunning.set(false);
      addToast('Error', e instanceof Error ? e.message : 'Failed to start label sync', 'error');
    }
  }

  // --- Per-job legacy actions ---
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

  async function run(id: number, fn: (id: number) => Promise<void>, ok: string) {
    busy = id;
    try {
      await fn(id);
      addToast('Rename', ok);
      actionsOpenId = null;
    } catch {
      addToast('Rename', 'Action failed', 'error');
    } finally {
      busy = null;
    }
  }

  function onRematch(job: RenameJob) {
    actionsOpenId = null;
    rematchJob = job;
  }

  // Close the "More actions" dropdown on any click outside it — previously
  // it only closed via its own toggle or after an action ran, so a click
  // anywhere else on the page (another row, the header, empty space) left
  // it open indefinitely.
  function onWindowClick(e: MouseEvent) {
    if (actionsOpenId === null) return;
    const target = e.target as HTMLElement | null;
    if (target?.closest('[data-actions-menu]')) return;
    actionsOpenId = null;
  }

  function onCompare(job: RenameJob) {
    conflictJob = job;
  }

  // --- Derived visible set: status → category → query → sort ---
  function sortJobs(arr: RenameJob[], mode: typeof $renameSort): RenameJob[] {
    const a = [...arr];
    switch (mode) {
      case 'detected_asc': return a.sort((x, y) => (x.detected_at ?? '').localeCompare(y.detected_at ?? ''));
      case 'confidence_desc': return a.sort((x, y) => (y.match_confidence ?? -1) - (x.match_confidence ?? -1));
      case 'title_asc': return a.sort((x, y) => (x.title ?? '').localeCompare(y.title ?? ''));
      default: return a.sort((x, y) => (y.detected_at ?? '').localeCompare(x.detected_at ?? ''));
    }
  }
  // Archived jobs are excluded from the default (non-archived) load entirely,
  // so the Archived tab renders from the separate $archivedRenameJobs store
  // (already archived-only) instead of filtering $renameJobs by status.
  let shown = $derived(
    statusFilter === 'archived'
      ? sortJobs(
          $archivedRenameJobs
            .filter((j) => $renameCategory === 'all' || categoryOf(j).has($renameCategory))
            .filter((j) => matchesQuery(j, $renameQuery)),
          $renameSort
        )
      : sortJobs(
          $renameJobs
            // Keep an in-flight ('applying') item visible on whatever tab you're on
            // while its move/overwrite runs — otherwise it drops into a limbo that
            // matches no status tab and vanishes mid-apply. It settles into Applied
            // (or Failed) when done and then follows the normal tab rules.
            .filter((j) => statusFilter === 'all' || j.status === statusFilter || j.status === 'applying')
            .filter((j) => $renameCategory === 'all' || categoryOf(j).has($renameCategory))
            .filter((j) => matchesQuery(j, $renameQuery)),
          $renameSort
        )
  );
  let shownIds = $derived(shown.map((j) => j.id));
  let hasFilters = $derived(
    statusFilter !== 'all' || $renameCategory !== 'all' || $renameQuery.trim() !== ''
  );

  // Grid prefs — reuse the Scan page machinery (incl. the tile_columns setting
  // fallback) so columns/gap stay in lockstep with the Scan grid.
  let effectiveColumns = $derived(
    $gridColumns !== 'auto' ? $gridColumns : (($settings.tile_columns as number) || 0)
  );
  let gridStyle = $derived(
    effectiveColumns > 0
      ? `grid-template-columns: repeat(${effectiveColumns}, 1fr)`
      : `grid-template-columns: repeat(auto-fill, minmax(${TILE_MIN_PX[$tileSize]}px, 1fr))`
  );
  let gridGapClass = $derived(GRID_GAP_CLASS[$gridGap]);

  function clearFilters() {
    statusFilter = 'all';
    renameCategory.set('all');
    renameQuery.set('');
  }

</script>

<!--
  Scroll container. MUST be a plain block, not `flex flex-col`: as a flex
  column, the jobs <ul> becomes a flex item with the default flex-shrink:1,
  so it shrinks to the leftover height and its `overflow-hidden` (rounded
  corners) clips every row past the fold — the container then thinks its
  content fits and never scrolls. A block lets each section keep its natural
  height so the container overflows and scrolls (same pattern as the Scan
  page). `space-y-4` replaces the old `gap-4`.
-->
<svelte:window onclick={onWindowClick} />

<div class="flex-1 min-h-0 overflow-auto p-4 space-y-4">
  <RenamesHeader
    onDolbyVision={dolbyVision}
    onReidentifyAll={reidentifyAll}
    {reidentifyingAll}
  />

  {#if $renameQueue}
    <div class="rounded-lg border border-[var(--accent)]/40 bg-[var(--accent)]/5 px-3 py-2">
      <div class="flex items-center justify-between text-xs">
        <span class="font-medium text-[var(--text-primary)]">
          Applying {Math.min($renameQueue.done + 1, $renameQueue.total)} of {$renameQueue.total}
          {#if $renameQueue.current_title}<span class="text-[var(--text-secondary)]"> — {$renameQueue.current_title}</span>{/if}
        </span>
        <div class="flex items-center gap-2">
          <span class="text-[var(--text-secondary)]">{Math.round(($renameQueue.done / Math.max($renameQueue.total, 1)) * 100)}%</span>
          <button
            type="button"
            class="px-2 py-1 rounded text-[11px] font-medium bg-[var(--bg-tertiary)] disabled:opacity-50"
            disabled={$applyCancelling}
            onclick={() => cancelApply()}
          >
            {$applyCancelling ? 'Stopping…' : 'Stop'}
          </button>
        </div>
      </div>
      <div class="mt-1 h-1.5 rounded-full bg-[var(--bg-tertiary)] overflow-hidden">
        <div class="h-full bg-[var(--accent)] transition-[width] duration-300"
             style="width: {($renameQueue.done / Math.max($renameQueue.total, 1)) * 100}%"></div>
      </div>
    </div>
  {/if}

  {#if $renameStatus && !$renameStatus.enabled}
    <p class="text-xs text-[var(--text-secondary)]">
      Auto-rename is off. Enable it in
      <a href="/settings" class="text-[var(--accent)] hover:underline">Settings → Renaming</a>
      to track and organise extracted downloads.
    </p>
  {/if}

  {#if $isPhone}
  <MobileRenamesView />
  {:else}
  <StatusDashboard {statusFilter} onFilter={(s) => (statusFilter = s)} />

  <RenameFilterBar />

  <BulkBar {shownIds} viewingArchived={statusFilter === 'archived'} />

  {#if shown.length === 0}
    <div class="text-center py-12 text-[var(--text-secondary)]">
      {#if statusFilter === 'archived'}
        <p>Nothing archived yet — jobs land here automatically when applied, or when you archive them from the queue.</p>
      {:else if $renameJobs.length === 0}
        <p>No rename jobs yet. Use <strong>Process ▾</strong> to scan a folder.</p>
      {:else if hasFilters}
        <p>No jobs match these filters.</p>
        <button
          class="mt-2 px-3 py-1.5 rounded text-xs bg-[var(--bg-tertiary)]"
          onclick={clearFilters}
        >Clear filters</button>
      {:else}
        <p>No rename jobs to show.</p>
      {/if}
    </div>
  {:else if $viewMode === 'grid'}
    <div class="grid {gridGapClass}" style={gridStyle}>
      {#each shown as job (job.id)}
        <div class="relative min-w-0" data-actions-menu>
          <RenameCard {job} {onRematch} />
          <button
            class="absolute top-1.5 right-1.5 z-10 px-1.5 py-0.5 rounded text-[11px] font-medium bg-[var(--bg-secondary)]/90 border border-[var(--border)] text-[var(--text-secondary)] hover:text-[var(--text-primary)]"
            aria-label="More actions for {job.original_filename ?? job.title ?? `job ${job.id}`}"
            aria-expanded={actionsOpenId === job.id}
            onclick={(e) => { e.stopPropagation(); actionsOpenId = actionsOpenId === job.id ? null : job.id; }}
          >⋯</button>
          {#if actionsOpenId === job.id}
            <div class="absolute top-7 right-1.5 z-20 flex flex-col gap-1 p-2 rounded-lg bg-[var(--bg-secondary)] border border-[var(--border)] shadow">
              {#if job.status === 'matched' || job.status === 'needs_review'}
                <button onclick={() => run(job.id, applyJob, 'Applied')} disabled={busy === job.id || $applyActive}
                  title={$applyActive ? 'A bulk apply is in progress — try again once it finishes.' : undefined}
                  class="px-2.5 py-1 text-xs rounded-lg bg-[var(--accent)] text-white hover:opacity-90 disabled:opacity-50 text-left">Apply</button>
              {/if}
              {#if job.status === 'needs_review' || job.status === 'failed'}
                <button onclick={() => reidentify(job.id)} disabled={busy === job.id}
                  class="px-2.5 py-1 text-xs rounded-lg border border-[var(--border)] text-[var(--text-secondary)] hover:bg-[var(--bg-tertiary)] disabled:opacity-50 text-left"
                  title="Re-run automatic identification with the current matcher">Re-identify</button>
              {/if}
              {#if job.status === 'needs_review' && job.combined_episode}
                <button onclick={() => run(job.id, acceptCombinedJob, 'Accepted as combined')} disabled={busy === job.id}
                  class="px-2.5 py-1 text-xs rounded-lg bg-amber-500/20 text-amber-700 dark:text-amber-300 hover:bg-amber-500/30 disabled:opacity-50 text-left"
                  title="Confirm this is a combined double-episode file">Accept {job.combined_episode.proposed_code}</button>
              {/if}
              {#if job.status === 'needs_review' && job.suggested_correction}
                <button onclick={() => run(job.id, acceptCorrectionJob, 'Correction applied')} disabled={busy === job.id}
                  class="px-2.5 py-1 text-xs rounded-lg bg-amber-500/20 text-amber-700 dark:text-amber-300 hover:bg-amber-500/30 disabled:opacity-50 text-left"
                  title="Use the proposed episode correction">Accept S{String(job.suggested_correction.proposed.season).padStart(2,'0')}E{String(job.suggested_correction.proposed.episode).padStart(2,'0')}</button>
              {/if}
              {#if job.status === 'applied'}
                <button onclick={() => run(job.id, undoJob, 'Reverted')} disabled={busy === job.id}
                  class="px-2.5 py-1 text-xs rounded-lg border border-[var(--border)] text-[var(--text-secondary)] hover:bg-[var(--bg-tertiary)] disabled:opacity-50 text-left">Undo</button>
              {/if}
              <button onclick={() => run(job.id, deleteJob, 'Removed')} disabled={busy === job.id}
                class="px-2.5 py-1 text-xs rounded-lg text-[var(--error)] hover:bg-[var(--bg-tertiary)] disabled:opacity-50 text-left">Remove</button>
            </div>
          {/if}
        </div>
      {/each}
    </div>
  {:else}
    <ul class="divide-y divide-[var(--border)] rounded-lg border border-[var(--border)] overflow-hidden">
      {#each groupJobsBySeason(shown) as entry (entry.type === 'season' ? entry.key : entry.job.id)}
        {#if entry.type === 'season'}
          <SeasonGroupRow jobs={entry.jobs} show={entry.show} season={entry.season} {onRematch} {onCompare} />
        {:else}
          {@const job = entry.job}
          <RenameRow {job} {onRematch} {onCompare} />
          <li class="px-3 py-1 bg-[var(--bg-tertiary)]/30" data-actions-menu>
              <button
                class="text-[11px] text-[var(--text-secondary)] hover:text-[var(--text-primary)]"
                aria-label="More actions for {job.original_filename ?? job.title ?? `job ${job.id}`}"
                aria-expanded={actionsOpenId === job.id}
                onclick={() => (actionsOpenId = actionsOpenId === job.id ? null : job.id)}
              >{actionsOpenId === job.id ? 'Hide actions ▴' : 'More actions ▾'}</button>
              {#if actionsOpenId === job.id}
                <div class="mt-1 flex flex-wrap items-center gap-1">
                  {#if job.status === 'matched' || job.status === 'needs_review'}
                    <button onclick={() => run(job.id, applyJob, 'Applied')} disabled={busy === job.id || $applyActive}
                      title={$applyActive ? 'A bulk apply is in progress — try again once it finishes.' : undefined}
                      class="px-2.5 py-1 text-xs rounded-lg bg-[var(--accent)] text-white hover:opacity-90 disabled:opacity-50">Apply</button>
                  {/if}
                  {#if job.status === 'needs_review' || job.status === 'failed'}
                    <button onclick={() => reidentify(job.id)} disabled={busy === job.id}
                      class="px-2.5 py-1 text-xs rounded-lg border border-[var(--border)] text-[var(--text-secondary)] hover:bg-[var(--bg-tertiary)] disabled:opacity-50"
                      title="Re-run automatic identification with the current matcher">Re-identify</button>
                  {/if}
                  {#if job.status === 'needs_review' && job.combined_episode}
                    <button onclick={() => run(job.id, acceptCombinedJob, 'Accepted as combined')} disabled={busy === job.id}
                      class="px-2.5 py-1 text-xs rounded-lg bg-amber-500/20 text-amber-700 dark:text-amber-300 hover:bg-amber-500/30 disabled:opacity-50"
                      title="Confirm this is a combined double-episode file">Accept {job.combined_episode.proposed_code}</button>
                  {/if}
                  {#if job.status === 'needs_review' && job.suggested_correction}
                    <button onclick={() => run(job.id, acceptCorrectionJob, 'Correction applied')} disabled={busy === job.id}
                      class="px-2.5 py-1 text-xs rounded-lg bg-amber-500/20 text-amber-700 dark:text-amber-300 hover:bg-amber-500/30 disabled:opacity-50"
                      title="Use the proposed episode correction">Accept S{String(job.suggested_correction.proposed.season).padStart(2,'0')}E{String(job.suggested_correction.proposed.episode).padStart(2,'0')}</button>
                  {/if}
                  {#if job.status === 'applied'}
                    <button onclick={() => run(job.id, undoJob, 'Reverted')} disabled={busy === job.id}
                      class="px-2.5 py-1 text-xs rounded-lg border border-[var(--border)] text-[var(--text-secondary)] hover:bg-[var(--bg-tertiary)] disabled:opacity-50">Undo</button>
                  {/if}
                  <button onclick={() => run(job.id, deleteJob, 'Removed')} disabled={busy === job.id}
                    class="px-2.5 py-1 text-xs rounded-lg text-[var(--error)] hover:bg-[var(--bg-tertiary)] disabled:opacity-50">Remove</button>
                </div>
              {/if}
            </li>
        {/if}
      {/each}
    </ul>
  {/if}
  {/if}

  <!-- Dolby Vision scan surface — the StatusDashboard DV card scrolls here. -->
  <div id="dv-scan-surface" class="rounded-lg border border-[var(--border)]">
    <button
      onclick={() => (dvOpen = !dvOpen)}
      aria-expanded={dvOpen}
      class="w-full flex items-center justify-between px-3 py-2 text-sm font-medium text-left hover:bg-[var(--bg-tertiary)]/40"
    >
      <span>Dolby Vision FEL/MEL scan</span>
      <span class="text-[var(--text-secondary)] text-xs">{dvOpen ? '▴' : '▾'}</span>
    </button>
    {#if dvOpen}
      <div class="px-3 pb-3">
        <div class="flex items-center gap-2 flex-wrap">
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

        <div class="mt-3 pt-3 border-t border-[var(--border)]">
          <div class="flex items-center gap-2 flex-wrap">
            <button
              onclick={dvSync}
              disabled={$dvSyncRunning}
              class="px-3 py-1.5 text-sm rounded-lg bg-[var(--accent)] hover:opacity-90 text-white font-medium transition disabled:opacity-50"
            >{$dvSyncRunning ? 'Syncing…' : 'Sync Plex labels'}</button>
            <span class="text-xs text-[var(--text-secondary)]">
              Applies <code>DV FEL/MEL/P8/P5</code> to the exact copy Plex serves. Only these four labels are managed — your own labels are never touched.
            </span>
          </div>
          {#if $dvSyncProgress}
            <div class="mt-2 text-xs text-[var(--text-secondary)]">
              Matching {$dvSyncProgress.done}/{$dvSyncProgress.total}…
            </div>
          {/if}
          {#if $dvSyncResult && !$dvSyncProgress}
            <div class="mt-2 text-xs">
              {#if $dvSyncResult.error}
                <span class="text-[var(--error)]">{$dvSyncResult.error}</span>
              {:else}
                Labeled: <strong>{$dvSyncResult.added}</strong> added, {$dvSyncResult.removed} removed, {$dvSyncResult.matched} matched.
              {/if}
            </div>
          {/if}
        </div>
      </div>
    {/if}
  </div>

  <TrashPanel />
</div>

{#if rematchJob}
  <RematchModal job={rematchJob} onClose={() => (rematchJob = null)} />
{/if}
{#if conflictJob}
  <ConflictModal job={conflictJob} onClose={() => (conflictJob = null)} />
{/if}
