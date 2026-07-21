<script lang="ts">
  import { onMount } from 'svelte';
  import { api } from '$lib/api/client';
  import { addToast } from '$lib/stores/notifications';
  import { canEnablePrimary, evidenceLabel, reasonLabel } from '$lib/rss/status';

  type FeedState = {
    feed_key: string;
    last_status?: number | null;
    last_checked_at?: string | null;
    last_changed_at?: string | null;
    last_modified?: string | null;
    observed_depth_seconds?: number | null;
    consecutive_failures?: number;
    last_error_code?: string | null;
  };

  type Readiness = {
    ready: boolean;
    required_cycles: number;
    successful_cycles: number;
    required_days: number;
    observed_days: number;
    normal_feeds_healthy: boolean;
    reasons: string[];
  };

  type Candidate = {
    canonical_url: string;
    title: string;
    pub_date: string;
    media_type: string;
    resolution?: string | null;
    size_text?: string | null;
    dv_evidence: string;
    hdr_evidence: string;
    hevc_evidence: string;
    hdr_formats: string[];
    title_year?: number | null;
    description_year?: number | null;
    identity_state: string;
    relevance_state: string;
    detail_reason?: string | null;
    hydration_state: string;
    evidence_incomplete: boolean;
    year_conflict: boolean;
    discovery_source?: string | null;
  };

  type RssStatus = {
    mode: 'listing' | 'rss_shadow' | 'rss_primary';
    enabled: boolean;
    feeds: FeedState[];
    last_cycle?: Record<string, unknown> | null;
    readiness: Readiness;
    candidate_counts: Record<string, number>;
    hydration_counts: Record<string, number>;
    unknown_counts: Record<string, number>;
    shadow: {
      successful_cycles: number; relevant_misses: number;
      request_reduction_pct: number; recovery_cycles: number;
      latest?: Record<string, unknown> | null;
    };
    coordinator: Record<string, unknown>;
    safe_defaults: {
      listing_fallback: boolean;
      rss_auto_grab: boolean;
      hydration_limit: number;
    };
  };

  let status = $state<RssStatus | null>(null);
  let candidates = $state<Candidate[]>([]);
  let loading = $state(true);
  let modeSaving = $state(false);
  let actionUrl = $state<string | null>(null);
  let stateFilter = $state('');

  async function refresh() {
    loading = true;
    try {
      const [nextStatus, candidateResponse] = await Promise.all([
        api.rssStatus(),
        api.rssCandidates(stateFilter || undefined)
      ]);
      status = nextStatus;
      candidates = candidateResponse.items;
    } catch (error) {
      addToast(
        'RSS',
        error instanceof Error ? error.message : 'Could not load RSS status',
        'error'
      );
    } finally {
      loading = false;
    }
  }

  async function setMode(mode: RssStatus['mode']) {
    if (mode === 'rss_primary' && !canEnablePrimary(status?.readiness)) {
      addToast(
        'RSS discovery',
        'RSS primary is locked until shadow validation is complete',
        'warning'
      );
      return;
    }
    modeSaving = true;
    try {
      await api.rssSetMode(mode);
      addToast(
        'RSS discovery',
        mode === 'listing'
          ? 'Rolled back to listing discovery'
          : `Mode changed to ${mode.replace('_', ' ')}`
      );
      await refresh();
    } catch (error) {
      addToast(
        'RSS discovery',
        error instanceof Error ? error.message : 'Mode change failed',
        'error'
      );
    } finally {
      modeSaving = false;
    }
  }

  async function hydrate(url: string, retry = false) {
    actionUrl = url;
    try {
      if (retry) await api.rssRetry(url);
      else await api.rssHydrate(url);
      addToast(
        'RSS candidate',
        retry ? 'Retry queued' : 'Detail hydration started'
      );
      window.setTimeout(() => void refresh(), 1000);
    } catch (error) {
      addToast(
        'RSS candidate',
        error instanceof Error ? error.message : 'Hydration could not start',
        'error'
      );
    } finally {
      actionUrl = null;
    }
  }

  function duration(seconds?: number | null) {
    if (seconds == null) return 'unknown';
    if (seconds >= 86400) return `${(seconds / 86400).toFixed(1)} d`;
    if (seconds >= 3600) return `${(seconds / 3600).toFixed(1)} h`;
    return `${Math.round(seconds / 60)} min`;
  }

  onMount(() => {
    void refresh();
  });
</script>

<svelte:head><title>RSS Operations | ScanHound</title></svelte:head>

<div class="h-full overflow-y-auto p-4 md:p-6 space-y-5">
  <header class="flex flex-wrap items-center gap-3">
    <div>
      <h1 class="text-xl font-bold">HDEncode RSS Operations</h1>
      <p class="text-sm text-[var(--text-secondary)]">
        Feed evidence, hydration, health, and rollback controls.
      </p>
    </div>
    <div class="flex-1"></div>
    <button
      class="px-3 py-2 rounded-lg bg-[var(--bg-tertiary)] text-sm"
      onclick={() => void refresh()}
      disabled={loading}
    >{loading ? 'Refreshing…' : 'Refresh'}</button>
  </header>

  {#if status}
    <section class="grid md:grid-cols-4 gap-3">
      <div class="bg-[var(--bg-secondary)] border border-[var(--border)] rounded-xl p-4">
        <div class="text-xs uppercase text-[var(--text-secondary)]">Discovery mode</div>
        <select
          class="mt-2 w-full bg-[var(--bg-tertiary)] border border-[var(--border)] rounded-lg p-2"
          value={status.mode}
          disabled={modeSaving}
          onchange={(event) => {
            const target = event.currentTarget as HTMLSelectElement;
            void setMode(target.value as RssStatus['mode']);
          }}
        >
          <option value="listing">Listing rollback</option>
          <option value="rss_shadow">RSS shadow</option>
          <option value="rss_primary" disabled={!status.readiness.ready}>
            RSS primary
          </option>
        </select>
        <p class="mt-2 text-xs text-[var(--text-secondary)]">
          Primary mode skips routine HDEncode listing requests.
        </p>
      </div>

      <div class="bg-[var(--bg-secondary)] border border-[var(--border)] rounded-xl p-4">
        <div class="text-xs uppercase text-[var(--text-secondary)]">Primary readiness</div>
        <p class="mt-2 text-sm font-medium">
          {status.readiness.ready ? 'Ready' : 'Shadow validation incomplete'}
        </p>
        <p class="text-sm">
          Cycles: {status.readiness.successful_cycles}/{status.readiness.required_cycles}
        </p>
        <p class="text-sm">
          Days: {status.readiness.observed_days.toFixed(1)}/{status.readiness.required_days}
        </p>
        {#if status.readiness.reasons.length}
          <p class="mt-1 text-xs text-[var(--text-secondary)]">
            {status.readiness.reasons.map(reasonLabel).join(', ')}
          </p>
        {/if}
      </div>

      <div class="bg-[var(--bg-secondary)] border border-[var(--border)] rounded-xl p-4">
        <div class="text-xs uppercase text-[var(--text-secondary)]">Safety</div>
        <p class="mt-2 text-sm">
          Listing fallback: {status.safe_defaults.listing_fallback ? 'Enabled' : 'Off'}
        </p>
        <p class="text-sm">
          RSS auto-grab: {status.safe_defaults.rss_auto_grab ? 'Enabled' : 'Off'}
        </p>
        <p class="text-sm">Hydration cap: {status.safe_defaults.hydration_limit}/cycle</p>
      </div>

      <div class="bg-[var(--bg-secondary)] border border-[var(--border)] rounded-xl p-4">
        <div class="text-xs uppercase text-[var(--text-secondary)]">Unknown evidence</div>
        <p class="mt-2 text-sm">DV: {status.unknown_counts.dv ?? 0}</p>
        <p class="text-sm">HDR: {status.unknown_counts.hdr ?? 0}</p>
        <p class="text-sm">Identity: {status.unknown_counts.identity ?? 0}</p>
        <p class="text-sm">Year conflicts: {status.unknown_counts.year_conflict ?? 0}</p>
      </div>
    </section>

    <section class="grid md:grid-cols-4 gap-3">
      <div class="bg-[var(--bg-secondary)] border border-[var(--border)] rounded-xl p-4">
        <div class="text-xs uppercase text-[var(--text-secondary)]">Shadow comparisons</div>
        <p class="mt-2 text-sm">Cycles: {status.shadow.successful_cycles}</p>
        <p class="text-sm">Relevant misses: {status.shadow.relevant_misses}</p>
      </div>
      <div class="bg-[var(--bg-secondary)] border border-[var(--border)] rounded-xl p-4">
        <div class="text-xs uppercase text-[var(--text-secondary)]">Request reduction</div>
        <p class="mt-2 text-lg font-semibold">{status.shadow.request_reduction_pct.toFixed(1)}%</p>
        <p class="text-xs text-[var(--text-secondary)]">Measured against constrained listing requests</p>
      </div>
      <div class="bg-[var(--bg-secondary)] border border-[var(--border)] rounded-xl p-4">
        <div class="text-xs uppercase text-[var(--text-secondary)]">Recovery evidence</div>
        <p class="mt-2 text-sm">Cycles: {status.shadow.recovery_cycles}</p>
        <p class="text-xs text-[var(--text-secondary)]">Restart or adaptive catch-up</p>
      </div>
      <div class="bg-[var(--bg-secondary)] border border-[var(--border)] rounded-xl p-4">
        <div class="text-xs uppercase text-[var(--text-secondary)]">Coordinator</div>
        <p class="mt-2 text-sm">{String(status.coordinator.state ?? 'unknown')}</p>
        <p class="text-xs text-[var(--text-secondary)]">{String(status.coordinator.reason_code ?? 'No active block')}</p>
      </div>
    </section>

    <section class="bg-[var(--bg-secondary)] border border-[var(--border)] rounded-xl overflow-hidden">
      <div class="p-4 border-b border-[var(--border)]">
        <h2 class="font-semibold">Feed health</h2>
      </div>
      <div class="overflow-x-auto">
        <table class="w-full text-sm">
          <thead class="text-left text-[var(--text-secondary)]">
            <tr>
              <th class="p-3">Feed</th><th>Status</th><th>Checked</th>
              <th>Changed</th><th>Depth</th><th>Validator</th>
            </tr>
          </thead>
          <tbody>
            {#each status.feeds as feed}
              <tr class="border-t border-[var(--border)]">
                <td class="p-3 font-medium">{feed.feed_key}</td>
                <td>
                  {feed.last_status ?? '—'}
                  {feed.last_error_code ? ` · ${feed.last_error_code}` : ''}
                </td>
                <td>
                  {feed.last_checked_at
                    ? new Date(feed.last_checked_at).toLocaleString()
                    : 'Never'}
                </td>
                <td>
                  {feed.last_changed_at
                    ? new Date(feed.last_changed_at).toLocaleString()
                    : 'Never'}
                </td>
                <td>{duration(feed.observed_depth_seconds)}</td>
                <td>{feed.last_modified ? 'Last-Modified' : 'None'}</td>
              </tr>
            {/each}
          </tbody>
        </table>
      </div>
    </section>
  {/if}

  <section class="bg-[var(--bg-secondary)] border border-[var(--border)] rounded-xl">
    <div class="p-4 border-b border-[var(--border)] flex items-center gap-3">
      <h2 class="font-semibold">Candidates</h2>
      <div class="flex-1"></div>
      <select
        class="bg-[var(--bg-tertiary)] border border-[var(--border)] rounded-lg p-2 text-sm"
        bind:value={stateFilter}
        onchange={() => void refresh()}
      >
        <option value="">All states</option>
        <option value="detail_required">Detail required</option>
        <option value="relevant_missing">Missing</option>
        <option value="relevant_upgrade">Upgrade</option>
        <option value="irrelevant_conclusive">Conclusive skip</option>
      </select>
    </div>
    <div class="divide-y divide-[var(--border)]">
      {#each candidates as item}
        <article class="p-4 flex flex-wrap gap-3 items-start">
          <div class="min-w-0 flex-1">
            <div class="font-medium break-words">{item.title}</div>
            <div class="mt-1 flex flex-wrap gap-1.5 text-xs">
              <span class="rounded px-2 py-0.5 bg-[var(--bg-tertiary)]">
                {item.resolution ?? 'Resolution unknown'}
              </span>
              <span class="rounded px-2 py-0.5 bg-[var(--bg-tertiary)]">
                DV: {evidenceLabel(item.dv_evidence)}
              </span>
              <span class="rounded px-2 py-0.5 bg-[var(--bg-tertiary)]">
                HDR: {evidenceLabel(item.hdr_evidence)}
              </span>
              <span class="rounded px-2 py-0.5 bg-[var(--bg-tertiary)]">
                {item.relevance_state}
              </span>
              <span class="rounded px-2 py-0.5 bg-[var(--bg-tertiary)]">
                Source: {item.discovery_source ?? 'rss'}
              </span>
              {#if item.year_conflict}
                <span class="rounded px-2 py-0.5 bg-[var(--bg-tertiary)]">
                  Year conflict
                </span>
              {/if}
              {#if item.evidence_incomplete}
                <span class="rounded px-2 py-0.5 bg-[var(--bg-tertiary)]">
                  Incomplete evidence
                </span>
              {/if}
            </div>
            {#if item.detail_reason}
              <p class="mt-2 text-xs text-[var(--text-secondary)]">
                Detail reason: {reasonLabel(item.detail_reason)}
              </p>
            {/if}
          </div>
          {#if item.hydration_state !== 'completed'}
            <button
              class="px-3 py-1.5 rounded-lg text-sm bg-[var(--accent)] text-white disabled:opacity-50"
              disabled={
                actionUrl === item.canonical_url
                || item.hydration_state === 'running'
                || !status?.enabled
              }
              onclick={() =>
                void hydrate(
                  item.canonical_url,
                  item.hydration_state === 'failed'
                )}
            >
              {item.hydration_state === 'failed'
                ? 'Retry'
                : item.hydration_state === 'running'
                  ? 'Hydrating…'
                  : 'Hydrate'}
            </button>
          {/if}
        </article>
      {:else}
        <p class="p-6 text-sm text-[var(--text-secondary)]">
          No candidates in this view.
        </p>
      {/each}
    </div>
  </section>
</div>
