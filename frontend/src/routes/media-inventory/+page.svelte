<script lang="ts">
  import { onMount } from 'svelte';
  import { goto } from '$app/navigation';
  import { page } from '$app/stores';
  import { api } from '$lib/api/client';
  import type { MediaInventoryItem } from '$lib/api/types';
  import InventoryFilters from '$lib/components/media-inventory/InventoryFilters.svelte';
  import InventoryTable from '$lib/components/media-inventory/InventoryTable.svelte';
  import InventoryEvidenceDrawer from '$lib/components/media-inventory/InventoryEvidenceDrawer.svelte';
  import {
    activeMetadataRun, cancelRun, compactInventoryFilters, inventoryError,
    inventoryFacets, inventoryFilters, inventoryItems, inventoryLoading,
    inventoryTotal, loadInventory, pauseRun, refreshRun, resumeRun,
    retryFailures, startPilot, type InventoryFilters as Filters
  } from '$lib/stores/mediaInventory';
  import { addToast } from '$lib/stores/notifications';

  let selected = $state(new Set<string>());
  let inspected = $state<MediaInventoryItem | null>(null);
  let scanBusy = $state(false);
  let debounce: ReturnType<typeof setTimeout> | undefined;

  function filtersFromUrl(): Filters {
    return Object.fromEntries($page.url.searchParams.entries());
  }

  function applyFilters(next: Filters) {
    clearTimeout(debounce);
    debounce = setTimeout(() => {
      const compact = compactInventoryFilters(next);
      void goto(`/media-inventory?${new URLSearchParams(compact)}`, { replaceState: true, noScroll: true });
      void loadInventory(compact);
    }, next.q !== $inventoryFilters.q ? 250 : 0);
  }

  function toggle(ratingKey: string, checked: boolean) {
    const next = new Set(selected);
    if (checked) next.add(ratingKey); else next.delete(ratingKey);
    selected = next;
  }

  async function beginPilot() {
    if (!selected.size) return;
    scanBusy = true;
    try {
      const run = await startPilot([...selected]);
      addToast('4K metadata pilot', `${run.expected_count} generated manifest item(s) queued`);
    } catch (error) {
      addToast('4K metadata pilot', error instanceof Error ? error.message : 'Pilot could not start', 'error');
    } finally { scanBusy = false; }
  }

  async function control(action: 'pause' | 'resume' | 'cancel' | 'retry') {
    const run = $activeMetadataRun;
    if (!run) return;
    scanBusy = true;
    try {
      if (action === 'pause') await pauseRun(run.run_uuid);
      if (action === 'resume') await resumeRun(run.run_uuid);
      if (action === 'cancel') await cancelRun(run.run_uuid);
      if (action === 'retry') await retryFailures(run.run_uuid);
    } catch (error) {
      addToast('Metadata scan', error instanceof Error ? error.message : 'Scan control failed', 'error');
    } finally { scanBusy = false; }
  }

  async function exportCsv() {
    try {
      const blob = await api.downloadMediaInventoryCsv(compactInventoryFilters($inventoryFilters));
      const url = URL.createObjectURL(blob);
      const link = document.createElement('a');
      link.href = url; link.download = 'media-inventory.csv'; link.click();
      URL.revokeObjectURL(url);
    } catch (error) {
      addToast('Inventory export', error instanceof Error ? error.message : 'Export failed', 'error');
    }
  }

  const countFacet = (name: string, value?: string) =>
    ($inventoryFacets[name] ?? []).filter((f) => !value || f.value === value).reduce((n, f) => n + f.count, 0);

  onMount(() => {
    void loadInventory(filtersFromUrl());
    const timer = setInterval(() => {
      const run = $activeMetadataRun;
      if (run && ['queued', 'running', 'paused'].includes(run.status)) void refreshRun(run.run_uuid);
    }, 2000);
    return () => { clearInterval(timer); clearTimeout(debounce); };
  });
</script>

<svelte:head><title>4K Metadata | ScanHound</title></svelte:head>

<div class="h-full overflow-y-auto p-4 md:p-6 space-y-5">
  <header class="flex flex-wrap items-start gap-4">
    <div class="min-w-0 flex-1">
      <p class="text-xs uppercase tracking-[.16em] font-bold text-[var(--accent)]">Authoritative local-file inventory</p>
      <h1 class="text-2xl font-bold mt-1">4K signal evidence</h1>
      <p class="text-sm text-[var(--text-secondary)] mt-1 max-w-3xl">Search Dolby Vision FEL/MEL profiles, HDR10+, failures, and historic scan disagreements. Analysis is read-only; Plex and Kometa writes stay outside this screen.</p>
    </div>
    <div class="flex gap-2">
      <button class="secondary" onclick={() => void exportCsv()}>Export CSV</button>
      <button class="primary" disabled={!selected.size || scanBusy} onclick={() => void beginPilot()}>Start selected pilot ({selected.size})</button>
    </div>
  </header>

  <section class="coverage" aria-label="Inventory coverage">
    <div><span>Indexed</span><strong>{$inventoryTotal}</strong><small>4K files in current search</small></div>
    <div><span>FEL</span><strong>{countFacet('dv_layer', 'fel')}</strong><small>Live local-file evidence</small></div>
    <div><span>HDR10+</span><strong>{countFacet('hdr10plus_state', 'present')}</strong><small>Authoritative positives</small></div>
    <div><span>Needs attention</span><strong>{countFacet('scan_state', 'failed') + countFacet('scan_state', 'source_changed')}</strong><small>Retryable or changed files</small></div>
  </section>

  {#if $activeMetadataRun}
    <section class="runbar" aria-live="polite">
      <div><span class="pulse" class:active={$activeMetadataRun.status === 'running'}></span><strong>{$activeMetadataRun.scope} scan</strong><small>{$activeMetadataRun.status} · {$activeMetadataRun.expected_count} manifest items</small></div>
      <div class="flex flex-wrap gap-2">
        {#if $activeMetadataRun.status === 'running'}<button class="secondary" disabled={scanBusy} onclick={() => void control('pause')}>Pause after current file</button><button class="secondary" disabled={scanBusy} onclick={() => void control('cancel')}>Cancel</button>{/if}
        {#if ['paused', 'cancelled', 'interrupted'].includes($activeMetadataRun.status)}<button class="primary" disabled={scanBusy} onclick={() => void control('resume')}>Resume</button>{/if}
        {#if ['completed', 'failed'].includes($activeMetadataRun.status)}<button class="secondary" disabled={scanBusy} onclick={() => void control('retry')}>Retry failures</button>{/if}
      </div>
    </section>
  {/if}

  <InventoryFilters filters={$inventoryFilters} facets={$inventoryFacets} onchange={applyFilters} />

  <section class="panel">
    <div class="panel-head"><div><h2 class="font-semibold">Evidence inventory</h2><p>{$inventoryTotal} matching file(s)</p></div>{#if $inventoryLoading}<span class="text-sm text-[var(--text-secondary)]">Reading inventory…</span>{/if}</div>
    {#if $inventoryError}<div class="message error"><strong>Inventory could not load.</strong><p>{$inventoryError}</p></div>
    {:else if !$inventoryLoading && !$inventoryItems.length}<div class="message"><strong>No matching evidence yet.</strong><p>Adjust the filters or select cached Plex movies for a controlled pilot scan.</p></div>
    {:else}<InventoryTable items={$inventoryItems} {selected} ontoggle={toggle} oninspect={(item) => inspected = item} />{/if}
  </section>

  {#if inspected}<InventoryEvidenceDrawer item={inspected} onclose={() => inspected = null} />{/if}
</div>

<style>
  button.primary, button.secondary { border-radius: .7rem; font-size: .85rem; font-weight: 750; padding: .65rem .85rem; }
  button.primary { background: var(--accent); color: white; }
  button.secondary { background: var(--bg-tertiary); border: 1px solid var(--border); }
  button:disabled { opacity: .45; }
  button:focus-visible { outline: 2px solid var(--accent); outline-offset: 3px; }
  .coverage { display: grid; gap: .75rem; grid-template-columns: repeat(4, minmax(0, 1fr)); }
  .coverage > div { background: var(--bg-secondary); border: 1px solid var(--border); border-radius: .9rem; display: grid; gap: .15rem; padding: 1rem; }
  .coverage span { color: var(--text-secondary); font-size: .68rem; font-weight: 750; letter-spacing: .09em; text-transform: uppercase; }
  .coverage strong { font: 750 1.65rem/1.15 ui-monospace, SFMono-Regular, Consolas, monospace; }
  .coverage small, .runbar small, .panel-head p { color: var(--text-secondary); font-size: .75rem; }
  .runbar { align-items: center; background: color-mix(in srgb, var(--accent) 6%, var(--bg-secondary)); border: 1px solid color-mix(in srgb, var(--accent) 35%, var(--border)); border-radius: .9rem; display: flex; gap: 1rem; justify-content: space-between; padding: .85rem 1rem; }
  .runbar > div:first-child { align-items: center; display: grid; grid-template-columns: auto 1fr; column-gap: .6rem; }
  .runbar small { grid-column: 2; }
  .pulse { background: var(--text-secondary); border-radius: 999px; height: .55rem; width: .55rem; }
  .pulse.active { background: var(--success); box-shadow: 0 0 0 .3rem color-mix(in srgb, var(--success) 15%, transparent); }
  .panel { background: var(--bg-secondary); border: 1px solid var(--border); border-radius: 1rem; overflow: hidden; }
  .panel-head { align-items: center; border-bottom: 1px solid var(--border); display: flex; justify-content: space-between; padding: 1rem; }
  .message { color: var(--text-secondary); padding: 2.5rem 1rem; text-align: center; }
  .message strong { color: var(--text-primary); }
  .message.error strong { color: var(--error); }
  @media (max-width: 767px) { .coverage { grid-template-columns: repeat(2, minmax(0, 1fr)); } .runbar { align-items: flex-start; flex-direction: column; } }
</style>
