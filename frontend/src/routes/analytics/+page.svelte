<script lang="ts">
  import { api } from '$lib/api/client';
  import type { AnalyticsSummary, LibraryStats, TrendData } from '$lib/api/types';
  import { addToast } from '$lib/stores/notifications';
  import { onMount } from 'svelte';
  import { goto } from '$app/navigation';
  import Skeleton from '$lib/components/Skeleton.svelte';
  import ErrorCard from '$lib/components/ErrorCard.svelte';
  import Tooltip from '$lib/components/Tooltip.svelte';
  import { resolutionLabel } from '$lib/constants';

  const TREND_OPTIONS = [7, 14, 30, 90] as const;

  let data: AnalyticsSummary | null = $state(null);
  let trendDays = $state(30);
  let trendData: TrendData | null = $state(null);
  let trendLoading = $state(false);
  let loading = $state(true);
  let error = $state('');

  let activeTrends = $derived(trendData ?? (data as AnalyticsSummary | null)?.trends ?? null);

  let scanSums = $derived.by(() => {
    const t = activeTrends;
    const d = data;
    if (!d) return { scans: 0, items: 0, missing: 0, upgrades: 0 };
    if (!t || !trendData) {
      // Using default 30d data from summary
      return {
        scans: d.scans.total_scans,
        items: d.scans.total_items_scanned,
        missing: d.scans.total_missing_found,
        upgrades: d.scans.total_upgrades_found
      };
    }
    const sum = (a: number[]) => a.reduce((x, y) => x + y, 0);
    return {
      scans: sum(t.scan_count),
      items: sum(t.items_scanned),
      missing: sum(t.missing_found),
      upgrades: sum(t.upgrades_found)
    };
  });

  function exportJson() {
    if (!data) return;
    const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `scanhound-analytics-${new Date().toISOString().slice(0, 10)}.json`;
    a.click();
    URL.revokeObjectURL(url);
  }

  async function loadData() {
    loading = true;
    error = '';
    try {
      data = await api.analyticsSummary();
    } catch (e) {
      error = e instanceof Error ? e.message : 'Failed to load analytics';
    } finally {
      loading = false;
    }
  }

  async function loadTrends(days: number) {
    trendDays = days;
    if (days === 30 && data) {
      // 30d is already in the summary response, reset override
      trendData = null;
      return;
    }
    trendLoading = true;
    try {
      trendData = await api.analyticsTrends(days);
    } catch (e) {
      // Keep previous trends visible but notify user
      addToast('Warning', e instanceof Error ? e.message : 'Failed to load trend data', 'error');
    } finally {
      trendLoading = false;
    }
  }

  // Download stats aggregated from history
  interface DownloadStats {
    total: number;
    completed: number;
    clipboard: number;
    browser: number;
    failed: number;
  }
  let downloadStats = $state<DownloadStats | null>(null);

  async function loadDownloadStats() {
    try {
      const hist = await api.downloadHistory(2000);
      const s: DownloadStats = { total: hist.length, completed: 0, clipboard: 0, browser: 0, failed: 0 };
      for (const h of hist) {
        const st = (h.status || 'completed').toLowerCase();
        if (st === 'completed') s.completed++;
        else if (st === 'clipboard') s.clipboard++;
        else if (st === 'browser') s.browser++;
        else if (st === 'failed') s.failed++;
      }
      downloadStats = s;
    } catch { /* ignore */ }
  }

  onMount(() => { loadData(); loadScanHistory(); loadDownloadStats(); });

  function formatSize(gb: number): string {
    if (gb >= 1024) return `${(gb / 1024).toFixed(1)} TB`;
    return `${gb.toFixed(1)} GB`;
  }

  function qualityBorderColor(score: number): string {
    if (score >= 70) return 'var(--success)';
    if (score >= 50) return 'var(--warning)';
    return 'var(--error)';
  }

  function buildSections(d: AnalyticsSummary | null): Array<{ label: string; stats: LibraryStats }> {
    if (!d) return [];
    return [
      { label: 'Movies', stats: d.library.movies },
      { label: 'TV Shows', stats: d.library.tv_shows }
    ];
  }
  let sections = $derived(buildSections(data));

  // Individual scan records
  let scanRecords = $state<{ id: number; timestamp: string; scan_type: string; items_scanned: number; missing_count: number; upgrade_count: number; duration_seconds: number; sources_scanned: string }[]>([]);

  async function loadScanHistory() {
    try {
      scanRecords = await api.scanHistory(15);
    } catch { /* silent */ }
  }
</script>

<div class="flex flex-col h-full overflow-auto p-4 md:p-6 gap-4 md:gap-6">
  <div class="flex items-center justify-between">
    <h1 class="text-xl font-bold">Analytics</h1>
    {#if data}
      <div class="flex items-center gap-2">
        <div class="flex items-center rounded-lg border border-[var(--border)] overflow-hidden">
          {#each TREND_OPTIONS as days}
            <button
              onclick={() => loadTrends(days)}
              disabled={trendLoading}
              class="px-3 py-1.5 text-xs transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
              class:bg-[var(--accent)]={trendDays === days}
              class:text-white={trendDays === days}
              class:bg-[var(--bg-tertiary)]={trendDays !== days}
              class:text-[var(--text-secondary)]={trendDays !== days}
              class:hover:bg-[var(--border)]={trendDays !== days}
            >
              {days}d
            </button>
          {/each}
        </div>
        <button
          onclick={exportJson}
          class="px-3 py-1.5 text-xs rounded-lg bg-[var(--bg-tertiary)] hover:bg-[var(--border)] text-[var(--text-primary)] border border-[var(--border)] transition-colors"
        >
          Export JSON
        </button>
      </div>
    {/if}
  </div>

  {#if loading}
    <div class="grid grid-cols-2 md:grid-cols-4 gap-4">
      {#each Array(4) as _}
        <div class="bg-[var(--bg-secondary)] rounded-lg p-4 border border-[var(--border)]">
          <Skeleton width="60%" height="2rem" />
          <div class="mt-2"><Skeleton width="40%" height="0.75rem" /></div>
        </div>
      {/each}
    </div>
    <div class="grid grid-cols-1 md:grid-cols-2 gap-4">
      <div class="bg-[var(--bg-secondary)] rounded-lg p-4 border border-[var(--border)] space-y-3">
        <Skeleton width="5rem" height="1rem" />
        <Skeleton count={5} height="0.875rem" />
      </div>
      <div class="bg-[var(--bg-secondary)] rounded-lg p-4 border border-[var(--border)] space-y-3">
        <Skeleton width="5rem" height="1rem" />
        <Skeleton count={5} height="0.875rem" />
      </div>
    </div>
  {:else if error}
    <ErrorCard message={error} onretry={loadData} />
  {:else if data}
    {#if data.library.total_items === 0 && data.scans.total_scans === 0}
      <div class="flex flex-col items-center justify-center h-64 gap-4">
        <svg class="w-12 h-12 text-[var(--text-secondary)] opacity-30" viewBox="0 0 48 48" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
          <rect x="6" y="22" width="8" height="20" rx="1"/>
          <rect x="20" y="14" width="8" height="28" rx="1"/>
          <rect x="34" y="6" width="8" height="36" rx="1"/>
        </svg>
        <p class="text-sm text-[var(--text-secondary)]">No analytics data yet</p>
        <p class="text-xs text-[var(--text-secondary)] opacity-60">Run your first scan to start collecting library statistics.</p>
        <button
          onclick={() => goto('/')}
          class="px-4 py-2 text-xs rounded-lg bg-[var(--accent)] text-white hover:opacity-90 transition-opacity"
        >
          Start Scan
        </button>
      </div>
    {:else}
    <!-- Summary cards -->
    <div class="grid grid-cols-2 md:grid-cols-4 gap-4">
      <div class="bg-[var(--bg-secondary)] rounded-lg p-4 border border-[var(--border)] border-l-4" style="border-left-color: var(--accent);">
        <div class="text-2xl font-bold text-[var(--accent)]">{data.library.total_items}</div>
        <Tooltip text="Total movies + TV episodes detected in your Plex library across all configured library sections.">
          <div class="text-xs text-[var(--text-secondary)] mt-1 cursor-help underline decoration-dotted">Total Items ⓘ</div>
        </Tooltip>
      </div>
      <div class="bg-[var(--bg-secondary)] rounded-lg p-4 border border-[var(--border)] border-l-4" style="border-left-color: var(--accent);">
        <div class="text-2xl font-bold text-[var(--accent)]">{formatSize(data.library.total_size_gb)}</div>
        <Tooltip text="Combined disk footprint of all library files reported by Plex. Excludes items Plex hasn't indexed yet.">
          <div class="text-xs text-[var(--text-secondary)] mt-1 cursor-help underline decoration-dotted">Total Size ⓘ</div>
        </Tooltip>
      </div>
      <div class="bg-[var(--bg-secondary)] rounded-lg p-4 border border-[var(--border)] border-l-4" style="border-left-color: {qualityBorderColor(data.library.overall_quality_score)};">
        <div class="text-2xl font-bold" style="color: {qualityBorderColor(data.library.overall_quality_score)};">{data.library.overall_quality_score.toFixed(1)}</div>
        <Tooltip text="0–100 score weighting resolution (4K=100, 1080p=75, 720p=50), Dolby Vision (+15), HDR (+10), and upgrade potential (−points for items with available upgrades). Higher is better.">
          <div class="text-xs text-[var(--text-secondary)] mt-1 cursor-help underline decoration-dotted">Quality Score ⓘ</div>
        </Tooltip>
      </div>
      <div class="bg-[var(--bg-secondary)] rounded-lg p-4 border border-[var(--border)] border-l-4" style="border-left-color: var(--accent);">
        <div class="text-2xl font-bold text-[var(--accent)]">{scanSums.scans}</div>
        <Tooltip text="Number of full or partial scans run during this period. Each scan compares scraped results against your Plex library.">
          <div class="text-xs text-[var(--text-secondary)] mt-1 cursor-help underline decoration-dotted">Scans ({trendDays}d) ⓘ</div>
        </Tooltip>
      </div>
    </div>

    <!-- Download stats -->
    {#if downloadStats}
    <div class="bg-[var(--bg-secondary)] rounded-lg p-4 border border-[var(--border)]">
      <h2 class="text-sm font-semibold mb-3">Download History</h2>
      <div class="grid grid-cols-2 sm:grid-cols-4 gap-4 text-xs">
        <div>
          <div class="text-lg font-bold text-[var(--accent)]">{downloadStats.total}</div>
          <Tooltip text="Total entries in the download history, across all methods (JDownloader, clipboard, browser).">
            <div class="text-[var(--text-secondary)] cursor-help underline decoration-dotted">Total Grabbed ⓘ</div>
          </Tooltip>
        </div>
        <div>
          <div class="text-lg font-bold text-[var(--success)]">{downloadStats.completed}</div>
          <Tooltip text="Successfully sent to JDownloader for download.">
            <div class="text-[var(--text-secondary)] cursor-help underline decoration-dotted">Via JDownloader ⓘ</div>
          </Tooltip>
        </div>
        <div>
          <div class="text-lg font-bold">{downloadStats.clipboard + downloadStats.browser}</div>
          <Tooltip text="Links copied to clipboard or opened in browser instead of JDownloader (manual download fallback).">
            <div class="text-[var(--text-secondary)] cursor-help underline decoration-dotted">Manual ⓘ</div>
          </Tooltip>
        </div>
        <div>
          <div class="text-lg font-bold text-[var(--error)]">{downloadStats.failed}</div>
          <Tooltip text="Attempts where ScanHound couldn't extract or deliver download links.">
            <div class="text-[var(--text-secondary)] cursor-help underline decoration-dotted">Failed ⓘ</div>
          </Tooltip>
        </div>
      </div>
    </div>
    {/if}

    <!-- Library breakdown -->
    <div class="grid grid-cols-1 md:grid-cols-2 gap-4">
      {#each sections as { label, stats }}
        {@const resTotal = Object.values(stats.resolution_counts).reduce((a, b) => a + b, 0)}
        {@const hdrTotal = stats.dovi_count + stats.hdr_count + stats.sdr_count}
        <div class="bg-[var(--bg-secondary)] rounded-lg p-4 border border-[var(--border)]">
          <h2 class="text-sm font-semibold mb-3">{label}</h2>
          <div class="space-y-2 text-xs">
            <div class="flex justify-between">
              <span class="text-[var(--text-secondary)]">Items</span>
              <span>{stats.total_items}</span>
            </div>
            <div class="flex justify-between">
              <span class="text-[var(--text-secondary)]">Size</span>
              <span>{formatSize(stats.total_size_gb)}</span>
            </div>
            <div class="flex justify-between">
              <span class="text-[var(--text-secondary)]">Quality</span>
              <span>{stats.quality_score.toFixed(1)}</span>
            </div>

            <!-- Resolution breakdown as horizontal bars -->
            {#if Object.keys(stats.resolution_counts).length > 0}
              <div class="pt-2 border-t border-[var(--border)]">
                <div class="text-[var(--text-secondary)] mb-2">Resolution</div>
                <div class="space-y-1.5">
                  {#each Object.entries(stats.resolution_counts) as [res, count], i}
                    {@const pct = resTotal > 0 ? (count / resTotal) * 100 : 0}
                    <div class="flex items-center gap-2">
                      <span class="w-12 text-right flex-shrink-0">{resolutionLabel(res)}</span>
                      <div class="flex-1 h-4 bg-[var(--bg-tertiary)] rounded overflow-hidden">
                        <div
                          class="h-full rounded transition-all"
                          style="width: {pct}%; background: var(--accent); opacity: {0.5 + (i * 0.15)};"
                        ></div>
                      </div>
                      <span class="w-8 text-right flex-shrink-0 text-[var(--text-secondary)]">{count}</span>
                    </div>
                  {/each}
                </div>
              </div>
            {/if}

            <!-- HDR stacked bar -->
            <div class="pt-2 border-t border-[var(--border)]">
              <div class="text-[var(--text-secondary)] mb-2">HDR</div>
              {#if stats.dovi_count > 0 || stats.hdr_count > 0}
                <div class="w-full h-5 bg-[var(--bg-tertiary)] rounded overflow-hidden flex">
                  {#if stats.dovi_count > 0}
                    <div
                      class="h-full"
                      style="width: {(stats.dovi_count / hdrTotal) * 100}%; background: var(--accent);"
                      title="DV: {stats.dovi_count}"
                    ></div>
                  {/if}
                  {#if stats.hdr_count > 0}
                    <div
                      class="h-full"
                      style="width: {(stats.hdr_count / hdrTotal) * 100}%; background: var(--warning);"
                      title="HDR: {stats.hdr_count}"
                    ></div>
                  {/if}
                  {#if stats.sdr_count > 0}
                    <div
                      class="h-full"
                      style="width: {(stats.sdr_count / hdrTotal) * 100}%; background: var(--bg-tertiary);"
                      title="SDR: {stats.sdr_count}"
                    ></div>
                  {/if}
                </div>
                <div class="flex gap-4 mt-1.5 text-[10px] text-[var(--text-secondary)]">
                  <span class="flex items-center gap-1">
                    <span class="inline-block w-2 h-2 rounded-sm" style="background: var(--accent);"></span>
                    DV: {stats.dovi_count}
                  </span>
                  <span class="flex items-center gap-1">
                    <span class="inline-block w-2 h-2 rounded-sm" style="background: var(--warning);"></span>
                    HDR: {stats.hdr_count}
                  </span>
                  <span class="flex items-center gap-1">
                    <span class="inline-block w-2 h-2 rounded-sm" style="background: var(--bg-tertiary); border: 1px solid var(--border);"></span>
                    SDR: {stats.sdr_count}
                  </span>
                </div>
              {:else if stats.sdr_count > 0}
                <div class="text-[var(--text-secondary)]">All SDR ({stats.sdr_count} items)</div>
              {:else}
                <div class="text-[var(--text-secondary)]">No data</div>
              {/if}
            </div>

            <!-- Upgrade Potential bar -->
            <div class="pt-2 border-t border-[var(--border)]">
              <div class="flex justify-between mb-1">
                <Tooltip text="Percentage of items in this section where ScanHound found a higher-quality version available (e.g. you have 1080p but 4K is indexed). 0% means your library is at peak quality for what's available.">
                  <span class="text-[var(--text-secondary)] cursor-help underline decoration-dotted">Upgrade Potential ⓘ</span>
                </Tooltip>
                <span>{stats.upgrade_potential.toFixed(0)}%</span>
              </div>
              <div class="w-full h-1.5 bg-[var(--bg-tertiary)] rounded-full overflow-hidden">
                <div
                  class="h-full rounded-full transition-all"
                  style="width: {Math.min(stats.upgrade_potential, 100)}%; background: var(--accent);"
                ></div>
              </div>
            </div>
          </div>
        </div>
      {/each}
    </div>

    <!-- Scan history -->
    <div class="bg-[var(--bg-secondary)] rounded-lg p-4 border border-[var(--border)]">
      <h2 class="text-sm font-semibold mb-3">Scan History ({trendDays} days){#if trendLoading}<span class="ml-2 text-xs text-[var(--text-secondary)] font-normal">Loading...</span>{/if}</h2>
      <div class="grid grid-cols-2 md:grid-cols-4 gap-4 text-xs">
        <div>
          <div class="text-lg font-bold">{scanSums.items}</div>
          <div class="text-[var(--text-secondary)]">Items Scanned</div>
        </div>
        <div>
          <div class="text-lg font-bold text-[var(--error)]">{scanSums.missing}</div>
          <div class="text-[var(--text-secondary)]">Missing Found</div>
        </div>
        <div>
          <div class="text-lg font-bold text-[var(--warning)]">{scanSums.upgrades}</div>
          <div class="text-[var(--text-secondary)]">Upgrades Found</div>
        </div>
        <div>
          <div class="text-lg font-bold">{(data.scans.avg_duration ?? 0).toFixed(0)}s</div>
          <div class="text-[var(--text-secondary)]">Avg Duration</div>
        </div>
      </div>

      <!-- Trend sparkline (stacked bar chart) -->
      {#if activeTrends && activeTrends.dates.length > 0}
        <div class="mt-4 pt-3 border-t border-[var(--border)]">
          <div class="flex items-center justify-between mb-2">
            <div class="text-xs text-[var(--text-secondary)]">Daily Scan Activity</div>
            <div class="flex items-center gap-3 text-[10px] text-[var(--text-secondary)]">
              <span class="flex items-center gap-1"><span class="inline-block w-2 h-2 rounded-sm bg-[var(--accent)]"></span> Scans</span>
              <span class="flex items-center gap-1"><span class="inline-block w-2 h-2 rounded-sm bg-[var(--error)]"></span> Missing</span>
              <span class="flex items-center gap-1"><span class="inline-block w-2 h-2 rounded-sm bg-[var(--warning)]"></span> Upgrades</span>
            </div>
          </div>
          <div class="flex items-end gap-0.5 h-20">
            {#each activeTrends.scan_count as count, i}
              {@const maxItems = Math.max(...activeTrends.items_scanned, 1)}
              {@const itemsPct = (activeTrends.items_scanned[i] / maxItems) * 100}
              {@const missingPct = activeTrends.items_scanned[i] > 0 ? (activeTrends.missing_found[i] / activeTrends.items_scanned[i]) * 100 : 0}
              {@const upgradePct = activeTrends.items_scanned[i] > 0 ? (activeTrends.upgrades_found[i] / activeTrends.items_scanned[i]) * 100 : 0}
              <div
                class="flex-1 rounded-t overflow-hidden flex flex-col justify-end hover:opacity-80 transition-opacity"
                style="height: {itemsPct}%"
                title="{activeTrends.dates[i]}: {count} scan(s), {activeTrends.items_scanned[i]} items, {activeTrends.missing_found[i]} missing, {activeTrends.upgrades_found[i]} upgrades"
              >
                <div class="w-full" style="height: {upgradePct}%; background: var(--warning); min-height: {upgradePct > 0 ? '1px' : '0'};"></div>
                <div class="w-full" style="height: {missingPct}%; background: var(--error); min-height: {missingPct > 0 ? '1px' : '0'};"></div>
                <div class="w-full flex-1" style="background: var(--accent);"></div>
              </div>
            {/each}
          </div>
          <div class="flex justify-between mt-1 text-[8px] text-[var(--text-secondary)] opacity-50">
            <span>{activeTrends.dates[0]}</span>
            <span>{activeTrends.dates[activeTrends.dates.length - 1]}</span>
          </div>
        </div>
      {/if}
    </div>
    <!-- Scan History Table -->
    {#if scanRecords.length > 0}
      <div class="bg-[var(--bg-secondary)] rounded-lg p-4 border border-[var(--border)]">
        <h2 class="text-sm font-semibold mb-3">Recent Scans</h2>
        <div class="overflow-x-auto">
          <table class="w-full text-xs">
            <thead>
              <tr class="text-[var(--text-secondary)] border-b border-[var(--border)]">
                <th class="text-left py-1.5 px-2">Date</th>
                <th class="text-left py-1.5 px-2">Type</th>
                <th class="text-right py-1.5 px-2">Items</th>
                <th class="text-right py-1.5 px-2">Missing</th>
                <th class="text-right py-1.5 px-2">Upgrades</th>
                <th class="text-right py-1.5 px-2">Duration</th>
              </tr>
            </thead>
            <tbody>
              {#each scanRecords as scan}
                <tr class="border-b border-[var(--border)] hover:bg-[var(--bg-tertiary)]">
                  <td class="py-1.5 px-2 text-[var(--text-secondary)]">{new Date(scan.timestamp).toLocaleString()}</td>
                  <td class="py-1.5 px-2">{scan.scan_type}</td>
                  <td class="py-1.5 px-2 text-right">{scan.items_scanned}</td>
                  <td class="py-1.5 px-2 text-right text-[var(--error)]">{scan.missing_count}</td>
                  <td class="py-1.5 px-2 text-right text-[var(--warning)]">{scan.upgrade_count}</td>
                  <td class="py-1.5 px-2 text-right text-[var(--text-secondary)]">{scan.duration_seconds?.toFixed(0)}s</td>
                </tr>
              {/each}
            </tbody>
          </table>
        </div>
      </div>
    {/if}

    {/if}
  {/if}
</div>
