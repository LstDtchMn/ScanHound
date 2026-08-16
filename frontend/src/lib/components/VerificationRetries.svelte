<script lang="ts">
  import { onDestroy, onMount } from 'svelte';
  import { api } from '$lib/api/client';
  import { connection } from '$lib/stores/connection';
  import { addToast } from '$lib/stores/notifications';
  import type { BrowserStatus, DownloadQueueItem } from '$lib/api/types';
  import { checkedAgo } from '$lib/components/pipeline/pipelineDisplay';

  /** How long this item has been queued, e.g. "3d". '' when the timestamp is
   *  missing or unparsable, so the row simply omits it rather than rendering
   *  "NaNd". checkedAgo handles both timestamp shapes the API emits — sqlite's
   *  naive UTC and Python's offset-carrying isoformat — which is why it is
   *  reused here instead of a local Date.parse. */
  function waitingFor(item: DownloadQueueItem): string {
    return checkedAgo(item.created_at ?? '').replace(/ ago$/, '');
  }

  let items = $state<DownloadQueueItem[]>([]);
  let browser = $state<BrowserStatus | null>(null);
  let loading = $state(false);
  let busy = $state('');
  let intervalMinutes = $state(10);
  let timer: ReturnType<typeof setTimeout> | null = null;
  let alive = true;

  function localTime(value?: string | null): string {
    if (!value) return '';
    const parsed = Date.parse(value);
    return Number.isNaN(parsed) ? value : new Date(parsed).toLocaleString();
  }

  function stateLabel(state: string): string {
    return ({
      // Not "verification required" — that read as something the user could go
      // and complete. The challenge runs inside ScanHound's own automated
      // browser, which this UI has no way to interact with; retrying only
      // probes whether the source still presents it.
      verification_required: 'Manual attention required',
      waiting_source: 'Waiting for HDEncode',
      ready: 'Ready to retry',
      scheduled: 'Scheduled',
      claimed: 'Retrying',
      failed: 'Retry failed'
    } as Record<string, string>)[state] || state;
  }

  function stateClass(state: string): string {
    if (state === 'verification_required' || state === 'failed') return 'text-red-400 bg-red-500/10';
    if (state === 'waiting_source') return 'text-amber-400 bg-amber-500/10';
    if (state === 'ready' || state === 'scheduled') return 'text-blue-300 bg-blue-500/10';
    return 'text-[var(--text-secondary)] bg-[var(--bg-tertiary)]';
  }

  async function load() {
    if (loading) return;
    loading = true;
    try {
      const [retryResponse, browserResponse] = await Promise.all([
        api.downloadRetries(),
        api.browserStatus()
      ]);
      items = retryResponse.items;
      browser = browserResponse;
    } catch {
      // Retain the last useful snapshot.
    } finally {
      loading = false;
    }
  }

  function schedulePoll() {
    if (!alive) return;
    if (timer) clearTimeout(timer);
    timer = setTimeout(async () => {
      await load();
      schedulePoll();
    }, 10000);
  }

  onMount(() => {
    load();
    schedulePoll();
    const offQueue = connection.on('download:queue_updated', () => load());
    const offRetry = connection.on('download:retry_required', () => load());
    const offBatch = connection.on('download:batch_paused', () => load());
    return () => {
      offQueue();
      offRetry();
      offBatch();
    };
  });

  onDestroy(() => {
    alive = false;
    if (timer) clearTimeout(timer);
  });

  async function retry(item: DownloadQueueItem) {
    busy = item.item_uuid;
    try {
      await api.retryDownloadItem(item.item_uuid);
      addToast('Retry scheduled', item.title);
      await load();
    } catch (e) {
      addToast('Retry unavailable', e instanceof Error ? e.message : 'The source is still paused.', 'warning');
    } finally {
      busy = '';
    }
  }

  async function retryReady() {
    busy = 'all';
    try {
      const result = await api.retryReadyDownloads(intervalMinutes);
      const held = (result as { held?: number }).held ?? 0;
      const base = `${result.scheduled} item(s), ${intervalMinutes}-minute spacing`;
      // Verification-held items are skipped by the backend on purpose: a bulk
      // retry cannot probe a challenge for you. They need 'Retry now' one at a time.
      addToast(
        'Retries scheduled',
        held > 0
          ? `${base}. ${held} held for manual attention — use 'Retry now' one at a time.`
          : base
      );
      await load();
    } catch (e) {
      addToast('Retry unavailable', e instanceof Error ? e.message : 'The source is still paused.', 'warning');
    } finally {
      busy = '';
    }
  }

  /** Whether anything is currently held for manual attention.
   *
   * Drives the visibility of the clear action so the button only exists when
   * it can do something — an always-present "clear the hold" invites clicking
   * it as a general unstick, which is the opposite of what a hold means. */
  let heldCount = $derived(
    items.filter((i) => i.state === 'verification_required').length
  );

  async function clearHold() {
    busy = 'hold';
    try {
      const r = await api.clearVerificationHold('hdencode');
      // The backend's own wording is better than anything invented here: it
      // says plainly that a still-active challenge will re-arm the hold within
      // a cycle, and that this is fail-safe rather than a failure.
      addToast(
        r.cleared > 0 ? 'Verification hold released' : 'No hold was open',
        r.message,
        r.cleared > 0 ? 'success' : 'info'
      );
      await load();
    } catch (e) {
      addToast(
        'Could not release the hold',
        e instanceof Error ? e.message : 'The request was rejected.',
        'warning'
      );
    } finally {
      busy = '';
    }
  }

  async function remove(item: DownloadQueueItem) {
    busy = item.item_uuid;
    try {
      await api.removeDownloadRetry(item.item_uuid);
      items = items.filter((candidate) => candidate.item_uuid !== item.item_uuid);
    } catch (e) {
      addToast('Error', e instanceof Error ? e.message : 'Could not remove retry.', 'error');
    } finally {
      busy = '';
    }
  }
</script>

<section class="border-b border-[var(--border)] bg-[var(--bg-secondary)]/40">
  <div class="px-4 py-3 flex items-center gap-3 flex-wrap">
    <div>
      <h2 class="text-sm font-semibold">Verification Retries</h2>
      <p class="text-xs text-[var(--text-secondary)]">
        Challenged and source-deferred link grabs are retained across restarts.
        When automated verification did not complete, retrying sends a single
        probe — the verification itself cannot be completed inside ScanHound.
      </p>
    </div>
    {#if browser}
      <span class="text-[11px] px-2 py-1 rounded bg-[var(--bg-tertiary)] text-[var(--text-secondary)]">
        Browser: {browser.adapter} · {browser.profile_mode} profile
        {#if browser.browser_version} · {browser.browser_version}{/if}
      </span>
    {/if}
    <div class="ml-auto flex items-center gap-2">
      <label class="text-xs text-[var(--text-secondary)]">
        Spacing
        <select bind:value={intervalMinutes} class="ml-1 px-2 py-1 rounded bg-[var(--bg-tertiary)] border border-[var(--border)]">
          <option value={0}>Immediate</option>
          <option value={5}>5 min</option>
          <option value={10}>10 min</option>
          <option value={15}>15 min</option>
          <option value={30}>30 min</option>
          <option value={60}>60 min</option>
        </select>
      </label>
      <button
        class="px-3 py-1.5 rounded bg-[var(--accent)] text-white text-xs disabled:opacity-50"
        disabled={busy !== '' || items.length === 0}
        onclick={retryReady}
      >
        {busy === 'all' ? 'Scheduling…' : 'Retry all ready'}
      </button>
      {#if heldCount > 0}
        <!-- Only shown while something is actually held. This is an operator
             assertion — "I have dealt with the challenge" — not a general
             unstick, so it must not be available as ambient furniture. -->
        <button
          class="px-3 py-1.5 rounded bg-[var(--bg-tertiary)] border border-[var(--accent)] text-xs disabled:opacity-50"
          disabled={busy !== ''}
          title="Releases the hold so ScanHound retries this source. This says you have dealt with the challenge — it does not pass it. If the challenge is still live the hold re-arms within a cycle."
          onclick={clearHold}
        >
          {busy === 'hold' ? 'Releasing…' : 'Release verification hold'}
        </button>
      {/if}
      <button class="px-2 py-1.5 rounded bg-[var(--bg-tertiary)] text-xs" onclick={load} disabled={loading}>
        {loading ? 'Loading…' : 'Refresh'}
      </button>
    </div>
  </div>

  {#if items.length > 0}
    <div class="px-4 pb-3 grid gap-2 md:grid-cols-2 xl:grid-cols-3">
      {#each items as item (item.item_uuid)}
        <article class="rounded-lg border border-[var(--border)] bg-[var(--bg-secondary)] p-3">
          <div class="flex items-start gap-2">
            <div class="min-w-0 flex-1">
              <div class="font-medium text-sm truncate" title={item.title}>{item.title}</div>
              <div class="text-[11px] text-[var(--text-secondary)]">
                {item.service_type} · attempt {item.attempt_count}
                {#if item.transport_attempted === 0} · no page opened{/if}
                {#if waitingFor(item)} · <span title={localTime(item.created_at)}>waiting {waitingFor(item)}</span>{/if}
              </div>
            </div>
            {#if item.manual_recovery_required}
              <span class="text-[10px] px-2 py-0.5 rounded text-red-300 bg-red-500/15 whitespace-nowrap"
                    title="Automatic retry is switched off for this batch and its cooldown has passed, so nothing will promote it on its own.">Needs you</span>
            {/if}
            <span class="text-[10px] px-2 py-0.5 rounded {stateClass(item.state)}">{stateLabel(item.state)}</span>
          </div>

          {#if item.last_message}
            <p class="mt-2 text-xs text-[var(--text-secondary)]">{item.last_message}</p>
          {/if}
          {#if item.source_cooldown_until || item.cooldown_until}
            <p class="mt-1 text-[11px] text-amber-300">
              Retry after {localTime(item.source_cooldown_until || item.cooldown_until)}
            </p>
          {:else if item.scheduled_for}
            <p class="mt-1 text-[11px] text-blue-300">Scheduled {localTime(item.scheduled_for)}</p>
          {/if}

          <div class="mt-3 flex gap-2">
            <button
              class="px-2.5 py-1 rounded bg-[var(--accent)] text-white text-xs disabled:opacity-40"
              disabled={!item.retry_available || busy !== ''}
              title={item.retry_available
                ? 'Send one probe retry for this item'
                : 'HDEncode is still paused'}
              onclick={() => retry(item)}
            >
              {busy === item.item_uuid ? 'Working…' : 'Retry now'}
            </button>
            <button
              class="px-2.5 py-1 rounded bg-[var(--bg-tertiary)] text-xs disabled:opacity-40"
              disabled={busy !== '' || item.state === 'claimed'}
              title={item.state === 'claimed' ? 'Wait for the active retry to finish' : 'Remove this retry'}
              onclick={() => remove(item)}
            >
              Remove
            </button>
          </div>
        </article>
      {/each}
    </div>
  {:else}
    <p class="px-4 pb-3 text-xs text-[var(--text-secondary)]">No verification retries or scheduled link grabs.</p>
  {/if}
</section>
