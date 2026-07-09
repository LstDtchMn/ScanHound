<script lang="ts">
  import { onMount } from 'svelte';
  import { api } from '$lib/api/client';
  import MobileDownloadsView from '$lib/components/mobile/MobileDownloadsView.svelte';
  import { mobile } from '$lib/stores/media';
  import Badge from '$lib/components/Badge.svelte';
  import Skeleton from '$lib/components/Skeleton.svelte';
  import ErrorCard from '$lib/components/ErrorCard.svelte';
  import { selectedKeys, results } from '$lib/stores/results';
  import { addToast } from '$lib/stores/notifications';
  import { downloadQueue, batchProgress, downloadHost, type QueueItem } from '$lib/stores/downloads';
  import { connection } from '$lib/stores/connection';
  import { historyStatusVariant as _historyStatusVariant, historyStatusLabel as _historyStatusLabel, historyBorderColor } from '$lib/constants';
  import type { JdPackage, JdRunState, DownloadResult, DownloadHistoryEntry } from '$lib/api/types';

  // JDownloader live status — links grouped into collapsible packages.
  let jdPackages = $state<JdPackage[]>([]);
  let jdInfo = $state<{ connected: boolean; online: number; offline: number; total: number; packageCount: number; truncated: boolean; error?: string } | null>(null);
  let jdLoading = $state(false);
  // Packages render collapsed by default (JDownloader-style); this set holds
  // the UUIDs the user has expanded to reveal their parts.
  let jdExpanded = $state(new Set<string>());
  function toggleJdPackage(uuid: string) {
    const next = new Set(jdExpanded);
    if (next.has(uuid)) next.delete(uuid); else next.add(uuid);
    jdExpanded = next;
  }
  async function loadJdLinks() {
    jdLoading = true;
    try {
      const r = await api.jdStatus();
      jdInfo = {
        connected: r.connected, online: r.online, offline: r.offline, total: r.total,
        packageCount: r.package_count ?? (r.packages?.length ?? 0),
        truncated: r.truncated ?? false, error: r.error,
      };
      jdPackages = r.packages ?? [];
      jdState = r.state ?? 'unknown';
    } catch (e) {
      jdInfo = { connected: false, online: 0, offline: 0, total: 0, packageCount: 0, truncated: false, error: e instanceof Error ? e.message : 'Failed to load JDownloader status' };
      jdPackages = [];
    } finally {
      jdLoading = false;
    }
  }

  // JDownloader global queue controls
  let jdState = $state<JdRunState>('unknown');
  let jdControlBusy = $state(false);
  async function loadJdState() {
    try {
      const r = await api.jdState();
      jdState = r.state;
      if (jdInfo) {
        jdInfo.connected = r.connected;
        jdInfo.error = r.error;
      }
    } catch {
      // transient — keep the last known state
    }
  }
  async function jdControl(action: 'start' | 'stop' | 'pause' | 'resume') {
    jdControlBusy = true;
    try {
      const r = await api.jdControl(action);
      if (r.ok) {
        jdState = r.state ?? jdState;
        const verb = { start: 'started', stop: 'stopped', pause: 'paused', resume: 'resumed' }[action];
        addToast('JDownloader', `Downloads ${verb}`);
      } else {
        addToast('Error', r.error ?? 'Control failed', 'error');
      }
    } catch (e) {
      addToast('Error', e instanceof Error ? e.message : 'JDownloader control failed', 'error');
    } finally {
      jdControlBusy = false;
      loadJdLinks();
    }
  }
  function jdStateLabel(s: JdRunState): string {
    return { running: 'Running', paused: 'Paused', stopped: 'Stopped', unknown: 'Unknown' }[s] ?? s;
  }
  function jdStateColor(s: JdRunState): string {
    if (s === 'running') return 'var(--success)';
    if (s === 'paused') return 'var(--warning)';
    if (s === 'stopped') return 'var(--error)';
    return 'var(--text-secondary)';
  }

  // Download + extraction tracking (polled from JDownloader, persisted server-side)
  let dlResults = $state<DownloadResult[]>([]);
  async function loadResults() {
    try {
      dlResults = await api.downloadResults();
    } catch {
      // transient — keep the last known list
    }
  }
  async function clearResults() {
    try {
      await api.clearDownloadResults();
      dlResults = [];
    } catch (e) {
      addToast('Error', e instanceof Error ? e.message : 'Failed to clear results', 'error');
    }
  }
  function resultPct(r: DownloadResult): number {
    if (r.bytes_total > 0) return Math.min(100, Math.round((r.bytes_loaded / r.bytes_total) * 100));
    return r.downloaded ? 100 : 0;
  }
  function stateLabel(s: string): string {
    switch (s) {
      case 'queued': return 'Queued';
      case 'downloading': return 'Downloading';
      case 'downloaded': return 'Downloaded';
      case 'extracting': return 'Extracting';
      case 'extracted': return 'Extracted';
      case 'failed': return 'Failed';
      default: return s;
    }
  }
  function extractionVariant(e: string): 'success' | 'error' | 'warning' | 'default' {
    if (e === 'success') return 'success';
    if (e === 'error') return 'error';
    if (e === 'running') return 'warning';
    return 'default';
  }
  function extractionLabel(e: string): string {
    if (e === 'success') return 'Extracted ✓';
    if (e === 'error') return 'Extract ✗';
    if (e === 'running') return 'Extracting…';
    return 'No extract';
  }
  function formatBytes(b: number): string {
    if (!b) return '';
    if (b >= 1e9) return (b / 1e9).toFixed(1) + ' GB';
    if (b >= 1e6) return (b / 1e6).toFixed(0) + ' MB';
    return (b / 1e3).toFixed(0) + ' KB';
  }
  function trackerDownloadBadge(r: DownloadResult): { label: string; variant: 'success' | 'warning' | 'default' | 'error' } {
    if (r.state === 'queued') return { label: 'Queued', variant: 'default' };
    if (r.state === 'failed' && !r.downloaded) return { label: 'Failed', variant: 'error' };
    if (r.downloaded) return { label: 'Downloaded ✓', variant: 'success' };
    return { label: 'Downloading', variant: 'warning' };
  }
  function jdAvailVariant(a: string): 'success' | 'error' | 'warning' | 'default' {
    if (a === 'ONLINE') return 'success';
    if (a === 'OFFLINE') return 'error';
    return 'warning';
  }
  let jdBrokenOnly = $state(false);
  let jdVisiblePackages = $derived(jdBrokenOnly ? jdPackages.filter((p) => p.offline > 0) : jdPackages);
  function availLabel(a: string): string {
    return a === 'ONLINE' ? 'Online' : a === 'OFFLINE' ? 'Broken' : 'Checking';
  }

  // Drag-to-resize a scroll pane (native `resize: vertical`), persisting the
  // chosen height to localStorage so the JD-links / tracker split stays put.
  function persistResize(node: HTMLElement, key: string) {
    try {
      const saved = localStorage.getItem(key);
      if (saved) node.style.height = `${saved}px`;
    } catch { /* ignore */ }
    const ro = new ResizeObserver(() => {
      try { localStorage.setItem(key, String(node.offsetHeight)); } catch { /* ignore */ }
    });
    ro.observe(node);
    return { destroy() { ro.disconnect(); } };
  }

  // Accordion state for the two live-status sections (JD Links / Tracker).
  // Expanding one auto-collapses the other to maximise visible area.
  let activeSection = $state<'jd' | 'tracker' | null>('jd');
  function toggleSection(s: 'jd' | 'tracker') {
    activeSection = activeSection === s ? null : s;
  }

  let history = $state<DownloadHistoryEntry[]>([]);
  let loading = $state(false);
  let error = $state('');
  let searchInput = $state('');
  let statusFilter = $state('all');
  let refreshing = $state(false);
  let collapsedGroups = $state(new Set<string>());

  async function loadHistory() {
    loading = true;
    error = '';
    try {
      history = await api.downloadHistory();
    } catch (e) {
      error = e instanceof Error ? e.message : 'Failed to load download history';
    } finally {
      loading = false;
    }
  }

  async function refreshHistory() {
    refreshing = true;
    try {
      history = await api.downloadHistory();
      error = '';
    } catch (e) {
      error = e instanceof Error ? e.message : 'Failed to load download history';
    } finally {
      refreshing = false;
    }
  }

  async function downloadSelected() {
    const keys = [...$selectedKeys];
    if (keys.length === 0) {
      addToast('No Selection', 'Select items from the scan results first.', 'warning');
      return;
    }
    const items = keys
      .map((key) => {
        const result = $results.find((r) => r.url === key);
        return result ? { url: result.url ?? '', title: result.title ?? key, year: result.year ?? null, season: result.season ?? null,
          resolution: result.resolution ?? '', size: result.size ?? '', hdr: result.hdr ?? '', dovi: result.dovi ?? false } : null;
      })
      .filter((item): item is { url: string; title: string; year: number | null; season: number | null; resolution: string; size: string; hdr: string; dovi: boolean } => item !== null);
    if (items.length === 0) {
      addToast('Error', 'Could not resolve selected items.', 'error');
      return;
    }

    // Add items to queue
    const queueIds = items.map((item) => ({
      id: downloadQueue.add(item.title, item.url),
      title: item.title
    }));

    try {
      await api.downloadBatch(items, $downloadHost);
      queueIds.forEach((q) => downloadQueue.markSent(q.id));
      addToast('Downloads Started', `Queued ${items.length} item(s) for download.`);
    } catch (e) {
      queueIds.forEach((q) => downloadQueue.markFailed(q.id));
      addToast('Error', e instanceof Error ? e.message : 'Failed to start downloads.', 'error');
    }
  }

  function statusColor(status: QueueItem['status']): string {
    switch (status) {
      case 'sending': return 'var(--accent)';
      case 'sent': return 'var(--accent)';
      case 'done': return '#22c55e';
      case 'failed': return '#ef4444';
    }
  }

  function statusLabel(status: QueueItem['status']): string {
    switch (status) {
      case 'sending': return 'Sending...';
      case 'sent': return 'Sent to JDownloader';
      case 'done': return 'Complete';
      case 'failed': return 'Failed';
    }
  }

  const historyStatusVariant = _historyStatusVariant;
  const historyStatusLabel = _historyStatusLabel;
  const entryBorderColor = historyBorderColor;

  let historyContainer: HTMLDivElement | undefined = $state();

  // Scroll to top when search or status filter changes
  $effect(() => {
    // eslint-disable-next-line @typescript-eslint/no-unused-expressions
    searchInput; statusFilter;
    historyContainer?.scrollTo({ top: 0, behavior: 'smooth' });
  });

  let filteredHistory = $derived.by(() => {
    let result = history;
    const q = searchInput.trim().toLowerCase();
    if (q) {
      result = result.filter(e =>
        (e.title ?? '').toLowerCase().includes(q) ||
        (e.url ?? '').toLowerCase().includes(q)
      );
    }
    if (statusFilter !== 'all') {
      result = result.filter(e => (e.status || 'completed').toLowerCase() === statusFilter);
    }
    return result;
  });

  // Group entries by title
  interface DownloadGroup {
    title: string;
    entries: DownloadHistoryEntry[];
  }

  let groupedHistory = $derived.by(() => {
    const groups = new Map<string, DownloadHistoryEntry[]>();
    for (const entry of filteredHistory) {
      const key = entry.title;
      if (!groups.has(key)) groups.set(key, []);
      groups.get(key)!.push(entry);
    }
    const result: DownloadGroup[] = [];
    for (const [title, entries] of groups) {
      result.push({ title, entries });
    }
    return result;
  });

  function toggleGroup(title: string) {
    const next = new Set(collapsedGroups);
    if (next.has(title)) {
      next.delete(title);
    } else {
      next.add(title);
    }
    collapsedGroups = next;
  }

  // Desktop data-loading + live subscriptions. Set up lazily the first time we
  // are NOT on a phone, and only once. This runs on the desktop route only; the
  // phone view (MobileDownloadsView) does its own polling, so we skip this on
  // mobile to avoid double-polling.
  let desktopTeardown: (() => void) | null = null;
  function initDesktop() {
    if (desktopTeardown) return;  // already set up
    loadHistory();
    loadJdLinks();
    loadResults();
    // Live-sync the queue run-state pushed by /jd-control (e.g. start/stop from
    // another tab) instead of waiting for the next 5s poll.
    const offState = connection.on('download:state', (d) => {
      const s = d.state as JdRunState | undefined;
      if (s) jdState = s;
    });
    // Live-update the tracker from the poller's push (instead of only the 5s
    // poll) — the backend broadcasts download:results whenever a package's
    // state/bytes/extraction changes.
    const offResults = connection.on('download:results', (d) => {
      if (Array.isArray(d.results)) dlResults = d.results as DownloadResult[];
    });
    // Periodic fallback refresh (covers any missed push / reconnect) — lightweight
    // /jd-state, not the full link list (which can be megabytes).
    const id = setInterval(() => { loadResults(); loadJdState(); }, 5000);
    desktopTeardown = () => { clearInterval(id); offState(); offResults(); };
  }

  onMount(() => {
    // `mobile` is a live matchMedia store — subscribing (fires immediately with
    // the current value) means a window resized ACROSS the md breakpoint (a
    // desktop browser narrowed then widened) still wires up the desktop view
    // rather than leaving it uninitialized/empty. On an actual phone `mobile`
    // stays true, so initDesktop never runs and we don't double-poll.
    const unsub = mobile.subscribe((isMobile) => { if (!isMobile) initDesktop(); });
    return () => { unsub(); desktopTeardown?.(); };
  });
</script>

{#if $mobile}
  <MobileDownloadsView />
{:else}
<div class="p-4 border-b border-[var(--border)]">
  <div class="flex items-center gap-3 flex-wrap">
    <h1 class="text-lg font-semibold">Downloads</h1>
    <button
      onclick={downloadSelected}
      class="px-4 py-2 bg-[var(--accent)] hover:bg-[var(--accent-hover)] text-white rounded-lg text-sm font-medium transition-colors"
    >
      Download Selected ({$selectedKeys.size})
    </button>
    <input
      type="text"
      bind:value={searchInput}
      placeholder="Search downloads..."
      class="ml-auto w-48 px-3 py-2 rounded-lg bg-[var(--bg-tertiary)] border border-[var(--border)] text-sm text-[var(--text-primary)] placeholder:text-[var(--text-secondary)] focus:outline-none focus:border-[var(--accent)]"
    />
    <select
      bind:value={statusFilter}
      class="px-2 py-2 rounded-lg bg-[var(--bg-tertiary)] border border-[var(--border)] text-sm text-[var(--text-primary)] focus:outline-none focus:border-[var(--accent)] cursor-pointer"
    >
      <option value="all">All Status</option>
      <option value="completed">Completed</option>
      <option value="clipboard">Clipboard</option>
      <option value="browser">Browser</option>
      <option value="failed">Failed</option>
    </select>
    <button
      onclick={() => refreshHistory()}
      disabled={refreshing}
      class="px-3 py-2 bg-[var(--bg-tertiary)] hover:bg-[var(--border)] text-[var(--text-primary)] rounded-lg text-sm transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
    >
      {refreshing ? 'Refreshing...' : 'Refresh'}
    </button>
  </div>
</div>

{#if jdInfo}
  <div class="border-b border-[var(--border)]">
    <div
      role="button"
      tabindex="0"
      onclick={() => toggleSection('jd')}
      onkeydown={(e) => (e.key === 'Enter' || e.key === ' ') && (e.preventDefault(), toggleSection('jd'))}
      class="w-full flex items-center gap-3 px-4 py-3 text-left hover:bg-[var(--bg-tertiary)]/40 transition-colors cursor-pointer select-none"
    >
      <svg class="w-3.5 h-3.5 flex-shrink-0 text-[var(--text-secondary)] transition-transform {activeSection === 'jd' ? 'rotate-90' : ''}" viewBox="0 0 16 16" fill="currentColor"><path d="M6 4l4 4-4 4V4z"/></svg>
      <h2 class="text-sm font-semibold">JDownloader Links</h2>
      {#if jdInfo.connected}
        <span class="text-xs text-[var(--text-secondary)]">
          {jdInfo.packageCount} package(s) · {jdInfo.total} link(s) · <span class="text-[var(--success)]">{jdInfo.online} online</span>{#if jdInfo.offline > 0} · <span class="text-[var(--error)]">{jdInfo.offline} broken</span>{/if}
        </span>
      {:else}
        <span class="text-xs text-[var(--warning)]">Not connected</span>
      {/if}
      <div class="ml-auto flex items-center gap-2" onclick={(e) => e.stopPropagation()} onkeydown={(e) => e.stopPropagation()}>
        {#if jdInfo.connected && jdInfo.offline > 0}
          <label class="flex items-center gap-1.5 text-xs text-[var(--text-secondary)] cursor-pointer">
            <input type="checkbox" bind:checked={jdBrokenOnly} class="accent-[var(--accent)]" />
            Broken only
          </label>
        {/if}
        <button onclick={loadJdLinks} disabled={jdLoading} class="px-2.5 py-1 rounded text-xs bg-[var(--bg-tertiary)] hover:bg-[var(--border)] disabled:opacity-50">
          {jdLoading ? 'Loading…' : 'Refresh'}
        </button>
      </div>
    </div>
    {#if activeSection === 'jd'}
    <div class="px-4 pb-3">
    {#if jdInfo.connected}
      <div class="flex items-center gap-2 mb-2">
        <span class="text-xs text-[var(--text-secondary)]">Download queue:</span>
        <span class="text-xs font-medium px-2 py-0.5 rounded-full" style="background: color-mix(in srgb, {jdStateColor(jdState)} 15%, transparent); color: {jdStateColor(jdState)};">{jdStateLabel(jdState)}</span>
        <button onclick={() => jdControl('start')} disabled={jdControlBusy} class="px-2.5 py-1 rounded text-xs bg-[var(--bg-tertiary)] hover:bg-[var(--border)] disabled:opacity-50" title="Start downloads">▶ Start</button>
        {#if jdState === 'paused'}
          <button onclick={() => jdControl('resume')} disabled={jdControlBusy} class="px-2.5 py-1 rounded text-xs bg-[var(--bg-tertiary)] hover:bg-[var(--border)] disabled:opacity-50" title="Resume downloads">⏵ Resume</button>
        {:else}
          <button onclick={() => jdControl('pause')} disabled={jdControlBusy} class="px-2.5 py-1 rounded text-xs bg-[var(--bg-tertiary)] hover:bg-[var(--border)] disabled:opacity-50" title="Pause downloads">⏸ Pause</button>
        {/if}
        <button onclick={() => jdControl('stop')} disabled={jdControlBusy} class="px-2.5 py-1 rounded text-xs bg-[var(--bg-tertiary)] hover:bg-[var(--border)] disabled:opacity-50" title="Stop downloads">⏹ Stop</button>
      </div>
    {/if}
    {#if jdInfo.connected && jdVisiblePackages.length > 0}
      <div
        class="space-y-1 overflow-auto resize-y min-h-24 [max-height:80vh] pb-1"
        style="height: 24rem"
        use:persistResize={'sh-jdlinks-h'}
      >
        {#each jdVisiblePackages as pkg (pkg.uuid)}
          <div class="rounded border {pkg.offline > 0 ? 'border-[var(--error)]/50 bg-[var(--error)]/5' : 'border-[var(--border)]'}">
            <button
              type="button"
              class="flex items-center gap-3 w-full px-3 py-1.5 text-left text-xs hover:bg-[var(--bg-tertiary)]/60 transition-colors"
              onclick={() => toggleJdPackage(pkg.uuid)}
            >
              <svg class="w-3 h-3 flex-shrink-0 text-[var(--text-secondary)] transition-transform {jdExpanded.has(pkg.uuid) ? 'rotate-90' : ''}" viewBox="0 0 16 16" fill="currentColor">
                <path d="M6 4l4 4-4 4V4z" />
              </svg>
              <div class="flex-1 min-w-0">
                <div class="font-medium truncate" title={pkg.title || pkg.name}>{pkg.title || pkg.name || '(unknown title)'}</div>
                {#if pkg.title && pkg.name && pkg.title !== pkg.name}
                  <div class="text-[10px] text-[var(--text-secondary)] truncate" title={pkg.name}>{pkg.name}</div>
                {/if}
              </div>
              <span class="text-[10px] whitespace-nowrap">
                {#if pkg.offline > 0}<span class="text-[var(--error)]">{pkg.offline} broken</span> · {/if}<span class="text-[var(--success)]">{pkg.online}</span><span class="text-[var(--text-secondary)]">/{pkg.total}</span>
              </span>
              {#if pkg.bytes_total > 0}<span class="text-[10px] text-[var(--text-secondary)] whitespace-nowrap">{formatBytes(pkg.bytes_total)}</span>{/if}
              {#if pkg.host}<span class="text-[var(--text-secondary)] whitespace-nowrap hidden sm:inline">{pkg.host}</span>{/if}
              <span class="text-[10px] text-[var(--text-secondary)] uppercase whitespace-nowrap w-20 text-right">{pkg.stage}</span>
            </button>
            {#if jdExpanded.has(pkg.uuid)}
              <div class="px-3 pb-2 pt-0.5 space-y-1 border-t border-[var(--border)]/60">
                {#each pkg.links as link, i (link.name + '-' + i)}
                  <div class="flex items-center gap-3 pl-6 pr-1 py-1 text-xs">
                    <Badge label={availLabel(link.availability)} variant={jdAvailVariant(link.availability)} />
                    <div class="flex-1 min-w-0">
                      <div class="truncate text-[var(--text-secondary)]" title={link.name}>{link.name || '(unnamed file)'}</div>
                    </div>
                    {#if link.host}<span class="text-[var(--text-secondary)] whitespace-nowrap">{link.host}</span>{/if}
                    <span class="text-[10px] text-[var(--text-secondary)] uppercase whitespace-nowrap w-20 text-right">{link.stage}</span>
                  </div>
                {/each}
              </div>
            {/if}
          </div>
        {/each}
        {#if jdInfo.truncated}
          <p class="text-[10px] text-[var(--text-secondary)] text-center py-1">Showing first {jdPackages.length} of {jdInfo.packageCount} packages (broken-first)</p>
        {/if}
      </div>
    {:else if jdInfo.connected && jdBrokenOnly}
      <p class="text-xs text-[var(--success)]">No broken links 🎉</p>
    {:else if jdInfo.connected}
      <p class="text-xs text-[var(--text-secondary)]">No links in JDownloader yet.</p>
    {/if}
    </div>
    {/if}
  </div>
{/if}

{#if dlResults.length > 0}
  <div class="border-b border-[var(--border)]">
    <div
      role="button"
      tabindex="0"
      onclick={() => toggleSection('tracker')}
      onkeydown={(e) => (e.key === 'Enter' || e.key === ' ') && (e.preventDefault(), toggleSection('tracker'))}
      class="w-full flex items-center gap-3 px-4 py-3 text-left hover:bg-[var(--bg-tertiary)]/40 transition-colors cursor-pointer select-none"
    >
      <svg class="w-3.5 h-3.5 flex-shrink-0 text-[var(--text-secondary)] transition-transform {activeSection === 'tracker' ? 'rotate-90' : ''}" viewBox="0 0 16 16" fill="currentColor"><path d="M6 4l4 4-4 4V4z"/></svg>
      <h2 class="text-sm font-semibold">Download &amp; Extraction Status</h2>
      <span class="text-xs text-[var(--text-secondary)]">
        {dlResults.length} item(s) ·
        <span class="text-[var(--success)]">{dlResults.filter((r) => r.state === 'extracted').length} done</span> ·
        <span class="text-[var(--error)]">{dlResults.filter((r) => r.state === 'failed').length} failed</span>
      </span>
      <div class="ml-auto" onclick={(e) => e.stopPropagation()} onkeydown={(e) => e.stopPropagation()}>
        <button onclick={clearResults} class="px-2.5 py-1 rounded text-xs bg-[var(--bg-tertiary)] hover:bg-[var(--border)]">Clear</button>
      </div>
    </div>
    {#if activeSection === 'tracker'}
    <div class="px-4 pb-3">
    <div
      class="space-y-1 overflow-auto resize-y min-h-24 [max-height:80vh] pb-1"
      style="height: 20rem"
      use:persistResize={'sh-tracker-h'}
    >
      {#each dlResults as r (r.name)}
        {@const badge = trackerDownloadBadge(r)}
        <div class="flex items-center gap-3 px-3 py-2 rounded border {r.state === 'failed' ? 'border-[var(--error)]/50 bg-[var(--error)]/5' : 'border-[var(--border)]'} text-xs">
          <Badge label={badge.label} variant={badge.variant} />
          {#if !['queued', 'downloading'].includes(r.state) && r.extraction !== 'na'}
            <Badge label={extractionLabel(r.extraction)} variant={extractionVariant(r.extraction)} />
          {/if}
          <div class="flex-1 min-w-0">
            <div class="font-medium truncate" title={r.title}>{r.title || r.name}</div>
            {#if r.error}
              <div class="text-[10px] text-[var(--error)] truncate" title={r.error}>{r.error}</div>
            {:else}
              <div class="w-full h-1 bg-[var(--bg-tertiary)] rounded-full overflow-hidden mt-1">
                <div class="h-full rounded-full" style="width: {resultPct(r)}%; background: {r.state === 'failed' ? 'var(--error)' : r.downloaded ? 'var(--success)' : 'var(--accent)'};"></div>
              </div>
              {#if r.bytes_total > 0 || r.bytes_loaded > 0}
                <div class="text-[10px] text-[var(--text-secondary)] mt-0.5">{formatBytes(r.bytes_loaded)}{r.bytes_total ? ' / ' + formatBytes(r.bytes_total) : ''}</div>
              {/if}
            {/if}
          </div>
          {#if r.host}<span class="text-[var(--text-secondary)] whitespace-nowrap">{r.host}</span>{/if}
          <span class="text-[10px] text-[var(--text-secondary)] uppercase whitespace-nowrap w-20 text-right">{r.state === 'extracted' && r.extraction === 'na' ? 'Complete' : stateLabel(r.state)}</span>
        </div>
      {/each}
    </div>
    </div>
    {/if}
  </div>
{/if}

{#if $batchProgress}
  <div class="px-4 py-3 border-b border-[var(--border)] bg-[color-mix(in_srgb,var(--accent)_6%,var(--bg-primary))]">
    <div class="flex items-center justify-between mb-1.5">
      <span class="text-sm font-medium">
        Downloading {$batchProgress.completed} of {$batchProgress.total}
        {#if $batchProgress.currentTitle}
          &mdash; {$batchProgress.currentTitle}
        {/if}
      </span>
      {#if $batchProgress.completed >= $batchProgress.total}
        <span class="text-xs text-[var(--success)]">Complete</span>
      {/if}
    </div>
    <div class="w-full h-1.5 bg-[var(--bg-tertiary)] rounded-full overflow-hidden">
      <div
        class="h-full rounded-full transition-all duration-300"
        style="width: {$batchProgress.total > 0 ? ($batchProgress.completed / $batchProgress.total) * 100 : 0}%; background: {$batchProgress.completed >= $batchProgress.total ? 'var(--success)' : 'var(--accent)'};"
      ></div>
    </div>
  </div>
{/if}

{#if $downloadQueue.length > 0}
  <div class="p-4 border-b border-[var(--border)]">
    <div class="flex items-center justify-between mb-2">
      <h2 class="text-sm font-semibold text-[var(--text-secondary)]">Active Queue</h2>
      <button
        onclick={() => downloadQueue.clearCompleted()}
        class="text-xs text-[var(--text-secondary)] hover:text-[var(--text-primary)] transition-colors"
      >
        Clear completed
      </button>
    </div>
    <div class="space-y-2">
      {#each $downloadQueue as item (item.id)}
        <div class="flex items-center gap-3 p-3 rounded-lg border border-[var(--border)]"
          style="background: color-mix(in srgb, {statusColor(item.status)} 8%, var(--bg-secondary)); border-left: 3px solid {statusColor(item.status)};"
        >
          <div class="flex-1 min-w-0">
            <p class="text-sm font-medium truncate">{item.title}</p>
          </div>
          <div class="flex items-center gap-2">
            {#if item.status === 'sending' || item.status === 'sent'}
              <div class="relative w-24 h-1.5 bg-[var(--bg-tertiary)] rounded-full overflow-hidden">
                <div
                  class="absolute inset-y-0 left-0 rounded-full"
                  style="background: {statusColor(item.status)}; width: {item.status === 'sending' ? '40%' : '80%'}; transition: width 0.5s ease;"
                ></div>
                {#if item.status === 'sending'}
                  <div class="absolute inset-0 bg-gradient-to-r from-transparent via-white/20 to-transparent animate-shimmer"></div>
                {/if}
              </div>
            {/if}
            <span class="text-xs whitespace-nowrap" style="color: {statusColor(item.status)};">
              {statusLabel(item.status)}
            </span>
            <button
              onclick={() => downloadQueue.remove(item.id)}
              class="text-xs text-[var(--text-secondary)] hover:text-[var(--text-primary)] ml-1"
              title="Dismiss"
            >x</button>
          </div>
        </div>
      {/each}
    </div>
  </div>
{/if}

<div class="flex-1 overflow-auto p-4" bind:this={historyContainer}>
  {#if error}
    <ErrorCard message={error} onretry={loadHistory} />
  {:else if loading}
    <div class="space-y-2">
      {#each Array(6) as _}
        <div class="flex items-center gap-3 p-3 bg-[var(--bg-secondary)] rounded-lg border border-[var(--border)]">
          <div class="flex-1"><Skeleton width="60%" height="1rem" /></div>
          <Skeleton width="3rem" height="1.25rem" rounded="rounded-full" />
          <Skeleton width="6rem" height="0.75rem" />
          <Skeleton width="8rem" height="0.75rem" />
        </div>
      {/each}
    </div>
  {:else if filteredHistory.length === 0}
    <div class="flex flex-col items-center justify-center h-64 gap-4">
      <svg class="w-12 h-12 text-[var(--text-secondary)] opacity-30" viewBox="0 0 48 48" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
        <path d="M14 6h20l6 8v24a4 4 0 01-4 4H12a4 4 0 01-4-4V14l6-8z"/>
        <path d="M8 14h32"/>
        <path d="M20 24l4 4 4-4"/>
        <line x1="24" y1="18" x2="24" y2="28"/>
      </svg>
      {#if history.length === 0}
        <p class="text-sm text-[var(--text-secondary)]">No download history</p>
        <p class="text-xs text-[var(--text-secondary)] opacity-60">Downloads will appear here after you grab links from scan results.</p>
      {:else}
        <p class="text-sm text-[var(--text-secondary)]">No matching downloads</p>
        <p class="text-xs text-[var(--text-secondary)] opacity-60">Try adjusting your search filter.</p>
      {/if}
    </div>
  {:else}
    <div class="space-y-1">
      {#each groupedHistory as group}
        {#if group.entries.length > 1}
          <!-- Grouped entries -->
          <div>
            <button
              class="flex items-center gap-2 w-full px-3 py-2 rounded-lg hover:bg-[var(--bg-secondary)] transition-colors text-left"
              onclick={() => toggleGroup(group.title)}
            >
              <svg class="w-3 h-3 text-[var(--text-secondary)] transition-transform {collapsedGroups.has(group.title) ? '' : 'rotate-90'}" viewBox="0 0 16 16" fill="currentColor">
                <path d="M6 4l4 4-4 4V4z"/>
              </svg>
              <span class="text-sm font-medium">{group.title}</span>
              <span class="text-xs text-[var(--text-secondary)]">({group.entries.length})</span>
            </button>
            {#if !collapsedGroups.has(group.title)}
              <div class="ml-5 space-y-1 mt-1">
                {#each group.entries as entry, i}
                  <div
                    class="flex items-center gap-3 p-3 rounded-lg border border-[var(--border)] {i % 2 === 1 ? 'bg-[var(--bg-secondary)]' : ''}"
                    style="border-left: 3px solid {entryBorderColor(entry.status)};"
                  >
                    <!-- File icon -->
                    <svg class="w-4 h-4 flex-shrink-0 text-[var(--text-secondary)]" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5">
                      <path d="M3 2h6l4 4v8a1 1 0 01-1 1H3a1 1 0 01-1-1V3a1 1 0 011-1z"/>
                      <path d="M9 2v4h4"/>
                      <path d="M5 10l2 2 2-2" stroke-linecap="round" stroke-linejoin="round"/>
                    </svg>
                    <div class="flex-1 min-w-0">
                      <p class="text-sm font-medium truncate">{entry.title}</p>
                      {#if entry.path}
                        <p class="text-xs text-[var(--text-secondary)] truncate">{entry.path}</p>
                      {/if}
                    </div>
                    {#if entry.resolution}
                      <Badge label={entry.resolution} />
                    {/if}
                    {#if entry.size}
                      <span class="text-xs text-[var(--text-secondary)] whitespace-nowrap">{entry.size}</span>
                    {/if}
                    <span class="text-xs text-[var(--text-secondary)] whitespace-nowrap">{entry.downloaded_at ?? entry.timestamp ?? ''}</span>
                    <Badge
                      label={historyStatusLabel(entry.status)}
                      variant={historyStatusVariant(entry.status)}
                    />
                  </div>
                {/each}
              </div>
            {/if}
          </div>
        {:else}
          <!-- Single entry (no group) -->
          {@const entry = group.entries[0]}
          <div
            class="flex items-center gap-3 p-3 rounded-lg border border-[var(--border)]"
            style="border-left: 3px solid {entryBorderColor(entry.status)};"
          >
            <!-- File icon -->
            <svg class="w-4 h-4 flex-shrink-0 text-[var(--text-secondary)]" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5">
              <path d="M3 2h6l4 4v8a1 1 0 01-1 1H3a1 1 0 01-1-1V3a1 1 0 011-1z"/>
              <path d="M9 2v4h4"/>
              <path d="M5 10l2 2 2-2" stroke-linecap="round" stroke-linejoin="round"/>
            </svg>
            <div class="flex-1 min-w-0">
              <p class="text-sm font-medium truncate">{entry.title}</p>
              {#if entry.path}
                <p class="text-xs text-[var(--text-secondary)] truncate">{entry.path}</p>
              {/if}
            </div>
            {#if entry.resolution}
              <Badge label={entry.resolution} />
            {/if}
            {#if entry.size}
              <span class="text-xs text-[var(--text-secondary)] whitespace-nowrap">{entry.size}</span>
            {/if}
            <span class="text-xs text-[var(--text-secondary)] whitespace-nowrap">{entry.downloaded_at ?? entry.timestamp ?? ''}</span>
            <Badge
              label={historyStatusLabel(entry.status)}
              variant={historyStatusVariant(entry.status)}
            />
          </div>
        {/if}
      {/each}
    </div>
  {/if}
</div>

<style>
  @keyframes shimmer {
    0% { transform: translateX(-100%); }
    100% { transform: translateX(200%); }
  }
  :global(.animate-shimmer) {
    animation: shimmer 1.5s infinite;
  }
</style>
{/if}
