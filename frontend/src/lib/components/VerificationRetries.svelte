<script lang="ts">
  import { onDestroy, onMount } from 'svelte';
  import { api } from '$lib/api/client';
  import { connection } from '$lib/stores/connection';
  import { addToast } from '$lib/stores/notifications';
  import type { BrowserStatus, DownloadQueueItem, VerificationHold } from '$lib/api/types';
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
  let holds = $state<VerificationHold[]>([]);
  /** Which source cards are expanded. A single global flag hid the ENTIRE
   *  retry list whenever any hold existed -- unrelated ready retries and failed
   *  items vanished by default, and expanding one card revealed all of them
   *  rather than that card's rows. Keyed by source so two held sources behave
   *  independently. */
  let expanded = $state<Record<string, boolean>>({});
  let releasing = $state('');
  let browser = $state<BrowserStatus | null>(null);
  let loading = $state(false);
  let busy = $state('');
  let intervalMinutes = $state(10);
  let timer: ReturnType<typeof setTimeout> | null = null;
  let alive = true;

  /** Rows the backend classified as held, per source. Never re-derived from
   *  state/source here: one classification, computed by the layer that owns the
   *  policy. */
  const heldFor = $derived((source: string) =>
    items.filter((i) => i.verification_held && i.verification_hold_source === source));
  /** Everything NOT behind a hold stays visible, always. */
  const unheld = $derived(items.filter((i) => !i.verification_held));

  /** The release itself, as the listing described it. Every field here
   *  already arrives with each row -- list_retries() selects the whole
   *  queue row -- it was simply never rendered, so two cards for the same
   *  title were indistinguishable. */
  function releaseLine(item: DownloadQueueItem): string {
    const parts: string[] = [];
    if (item.year) parts.push(String(item.year));
    if (item.season != null) parts.push(`S${String(item.season).padStart(2, '0')}`);
    if (item.resolution) parts.push(item.resolution);
    // DV supersedes the hdr string rather than joining it: a Dolby Vision
    // release is often tagged HDR too, and showing both reads as two formats.
    if (item.dovi) parts.push('DV');
    else if (item.hdr) parts.push(item.hdr);
    if (item.size_text) parts.push(item.size_text);
    return parts.join(' \u00b7 ');
  }

  /** The machine-readable pair behind the prose message. The prose explains
   *  what happened to a person; these are what you search the logs for, and
   *  what distinguishes two rows whose sentences read identically. */
  function reasonCodes(item: DownloadQueueItem): string {
    const reason = (item.last_reason_code ?? '').trim();
    const cause = (item.last_cause_code ?? '').trim();
    if (!reason && !cause) return '';
    if (!cause || cause === reason) return reason || cause;
    return `${reason} / ${cause}`;
  }

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
      holds = retryResponse.holds ?? [];
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

  /** Release a hold, using the source the BACKEND reported.
   *
   *  Not a hardcoded 'hdencode': the hold marker names its own source, and
   *  hardcoding one is why the earlier attempt at this could only ever clear a
   *  single source. */
  async function releaseHold(hold: VerificationHold) {
    releasing = hold.source;
    try {
      const r = await api.clearVerificationHold(hold.source);
      // Show what the backend says to do next rather than inventing our own
      // wording -- it knows whether a trigger item is left to probe.
      addToast(
        'Hold released',
        `${hold.affected} request(s) for ${hold.source} can be tried again. ${r.next_action ?? ''}`.trim()
      );
      await load();
    } catch (e) {
      addToast('Could not release', e instanceof Error ? e.message : 'Please try again.', 'error');
    } finally {
      releasing = '';
    }
  }

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
      <button class="px-2 py-1.5 rounded bg-[var(--bg-tertiary)] text-xs" onclick={load} disabled={loading}>
        {loading ? 'Loading…' : 'Refresh'}
      </button>
    </div>
  </div>

  {#each holds as hold (hold.source)}
    <!-- ONE condition, not N stuck downloads. Held item cards deliberately
         suppress their own "Retry after <time>" (see the item card below): the
         timestamp is real but has no authority, because decide() returns
         VERIFICATION_HOLD before it looks at any cooldown. This card states the
         condition once instead of leaving forty rows to imply it will heal. -->
    <div class="mx-4 mb-3 rounded-lg border border-amber-500/40 bg-amber-500/10 p-3">
      <div class="flex items-start gap-2 flex-wrap">
        <span class="text-sm font-semibold text-amber-300">
          {hold.source} — waiting for verification
        </span>
        <span class="text-[11px] px-2 py-0.5 rounded bg-amber-500/20 text-amber-200">
          {#if hold.affected > 0}
            {hold.affected} request{hold.affected === 1 ? '' : 's'} paused
          {:else}
            nothing waiting on it
          {/if}
        </span>
      </div>
      <p class="mt-2 text-xs text-[var(--text-secondary)]">
        {#if hold.affected > 0}
          ScanHound met a verification challenge it cannot complete on its own, so
          it stopped sending requests to {hold.source}.
        {:else}
          No current retry is blocked by this hold, but the marker on
          {hold.source} is still armed. Releasing it stops the stale hold
          blocking later recovery attempts.
        {/if}
        <strong class="text-amber-300">This will not clear on its own</strong> —
        not when the retry times below run out. It clears when
        {hold.clears_when}.
      </p>
      <div class="mt-3 flex gap-2 flex-wrap">
        <button
          class="px-2.5 py-1 rounded bg-[var(--accent)] text-white text-xs disabled:opacity-40"
          disabled={releasing !== ''}
          title="Stop holding these back and let them try {hold.source} again"
          onclick={() => releaseHold(hold)}
        >
          {releasing === hold.source ? 'Releasing…' : 'Try again anyway'}
        </button>
        {#if hold.affected > 0}
          <button class="px-2.5 py-1 rounded bg-[var(--bg-tertiary)] text-xs"
                  onclick={() => (expanded = { ...expanded, [hold.source]: !expanded[hold.source] })}>
            <!-- Promise only what can be rendered. `affected` counts every held
                 row; the retries list is capped, so above the cap the two differ
                 and the button must say so rather than expand to fewer. -->
            {expanded[hold.source] ? 'Hide' : 'Show'}
            {#if (hold.shown ?? hold.affected) < hold.affected}
              {hold.shown} of the {hold.affected} paused
            {:else}
              the {hold.affected} paused
            {/if}
          </button>
        {/if}
      </div>
    </div>
  {/each}

  <!-- Held rows expand UNDER their own source card, above. Unrelated retry work
       is never hidden by a hold: the previous version's single flag made ready
       retries and failed items disappear the moment any hold existed. -->
  {#each holds as hold (hold.source + '-rows')}
    {#if expanded[hold.source]}
      <div class="px-4 pb-3 grid gap-2 md:grid-cols-2 xl:grid-cols-3">
        {#each heldFor(hold.source) as item (item.item_uuid)}
          {@render itemCard(item)}
        {/each}
      </div>
    {/if}
  {/each}

  {#if unheld.length > 0}
    <div class="px-4 pb-3 grid gap-2 md:grid-cols-2 xl:grid-cols-3">
      {#each unheld as item (item.item_uuid)}
        {@render itemCard(item)}
      {/each}
    </div>
  {:else if holds.length === 0}
    <p class="px-4 pb-3 text-xs text-[var(--text-secondary)]">No verification retries or scheduled link grabs.</p>
  {/if}
</section>

{#snippet itemCard(item: DownloadQueueItem)}
        <article class="rounded-lg border border-[var(--border)] bg-[var(--bg-secondary)] p-3">
          <div class="flex items-start gap-2">
            <div class="min-w-0 flex-1">
              <div class="font-medium text-sm truncate" title={item.title}>
                {#if item.canonical_url}
                  <!-- noopener/noreferrer: this is an untrusted source page and it
                       must not get a handle on the ScanHound window. -->
                  <a
                    href={item.canonical_url}
                    target="_blank"
                    rel="noopener noreferrer"
                    class="hover:underline text-[var(--accent)]"
                    title={item.canonical_url}
                  >{item.title}</a>
                {:else}
                  {item.title}
                {/if}
              </div>
              {#if releaseLine(item)}
                <div
                  class="text-[11px] text-[var(--text-secondary)] truncate"
                  title={releaseLine(item)}
                >{releaseLine(item)}</div>
              {/if}
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
          {#if reasonCodes(item)}
            <p class="mt-1 text-[10px] font-mono text-[var(--text-secondary)] break-all">
              {reasonCodes(item)}
            </p>
          {/if}
          {#if item.verification_held}
            <!-- Do NOT show a retry time on a held row. That timestamp is real
                 but irrelevant: the hold outranks every clock in decide(), and
                 showing it is what made 40 rows look like they would fix
                 themselves at 8:57 PM. -->
            <p class="mt-1 text-[11px] text-amber-300">Waiting on verification — no retry time applies</p>
          {:else if item.source_cooldown_until || item.cooldown_until}
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
{/snippet}
