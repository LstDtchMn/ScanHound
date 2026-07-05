<script lang="ts">
  import { scanState, scanProgress, scanPhase, scanItemCount, startScan, stopScan } from '$lib/stores/scanner';
  import type { ScanType } from '$lib/stores/scanner';
  import { clearResults, categoryFilter, scanBarCollapsed } from '$lib/stores/results';
  import { get } from 'svelte/store';
  import BottomSheet from './BottomSheet.svelte';

  let scanSheet = $state(false);

  const scanTypes: { value: ScanType; label: string }[] = [
    { value: 'deep', label: 'Deep Scan' },
    { value: 'incremental', label: 'Incremental' },
    { value: 'loaded', label: 'Load Cache' },
    { value: 'search', label: 'Site Search' }
  ];

  type Source = 'HDEncode' | 'DDLBase' | 'Adit-HD';

  export const sourceCategories: Record<Source, { key: string; label: string; default: boolean }[]> = {
    'HDEncode': [
      { key: '4k', label: '4K', default: true },
      { key: 'remux', label: 'Remux', default: false },
      { key: 'tv', label: 'TV', default: false }
    ],
    'DDLBase': [
      { key: '4k_webdl', label: '4K', default: true },
      { key: '4k_remux', label: '4K Remux', default: true },
      { key: '1080p_remux', label: '1080p Remux', default: true }
    ],
    'Adit-HD': [
      { key: '4k', label: '4K', default: true },
      { key: 'remux', label: 'Remux', default: false },
      { key: 'tv', label: 'TV', default: false }
    ]
  };

  let selectedType = $state<ScanType>('deep');
  let selectedSource = $state<Source>('HDEncode');
  let query = $state('');
  let pages = $state(1);

  // Map a per-source category key to its normalized display category.
  function normCat(key: string): string {
    if (key === 'tv') return 'tv';
    return key.includes('remux') ? 'remux' : '4k';
  }

  // Derive the toggle flags for a source from the persisted display filter, so
  // the saved 4K/Remux/TV choice survives a reload. Falls back to source
  // defaults when nothing is persisted (an empty filter would hide everything).
  function flagsFor(src: Source, filter: string[]): Record<string, boolean> {
    const cats = sourceCategories[src];
    if (!filter || filter.length === 0) {
      return Object.fromEntries(cats.map((c) => [c.key, c.default]));
    }
    return Object.fromEntries(cats.map((c) => [c.key, filter.includes(normCat(c.key))]));
  }

  // Category flags — seeded from the persisted filter (not blind defaults) so
  // the $effect below writes back the same value on mount instead of clobbering
  // the user's saved choice. Reset to defaults when the source changes.
  let flags = $state<Record<string, boolean>>(flagsFor('HDEncode', get(categoryFilter)));

  function onSourceChange(src: Source) {
    selectedSource = src;
    flags = Object.fromEntries(sourceCategories[src].map((c) => [c.key, c.default]));
  }

  let categories = $derived(sourceCategories[selectedSource]);
  let hasInteracted = $state(false);

  // Drive the instant display filter from the 4K/Remux/TV toggles. The cache is
  // pre-scanned with every category, so toggling here filters the loaded list
  // immediately (keeping the others) instead of needing a re-scan.
  $effect(() => {
    const cats: string[] = [];
    if (flags['4k'] || flags['4k_webdl']) cats.push('4k');
    if (flags['remux'] || flags['4k_remux'] || flags['1080p_remux']) cats.push('remux');
    if (flags['tv']) cats.push('tv');
    categoryFilter.set(cats);
  });
  let scanTypeLabel = $derived(scanTypes.find((t) => t.value === selectedType)?.label ?? 'Scan');

  // Auto-hide the mobile bar per scroll direction (set by MobileScanView), but
  // never while a scan is actually running/stopping — progress must stay pinned.
  let hideMobileBar = $derived($scanBarCollapsed && $scanState === 'idle');

  function handleStart() {
    hasInteracted = true;
    clearResults();
    startScan(selectedType, query, pages, selectedSource, flags);
  }

  function mobileStart() {
    scanSheet = false;
    handleStart();
  }

  // Expose categories and flags for FilterBar
  export function getCategoryState() {
    return { categories, flags, selectedType, scanState: $scanState };
  }
</script>

<!-- Desktop scan toolbar -->
<div class="hidden md:flex items-center gap-2 px-3 py-1.5 border-b border-[var(--border)]">
  <select
    bind:value={selectedType}
    disabled={$scanState !== 'idle'}
    class="bg-[var(--bg-tertiary)] text-[var(--text-primary)] px-2 py-1 rounded border border-[var(--border)] text-xs focus:outline-none focus:border-[var(--accent)]"
  >
    {#each scanTypes as t}
      <option value={t.value}>{t.label}</option>
    {/each}
  </select>

  {#if selectedType !== 'search'}
    <select
      value={selectedSource}
      onchange={(e) => onSourceChange(e.currentTarget.value as Source)}
      disabled={$scanState !== 'idle'}
      class="bg-[var(--bg-tertiary)] text-[var(--text-primary)] px-2 py-1 rounded border border-[var(--border)] text-xs focus:outline-none focus:border-[var(--accent)]"
    >
      <option value="HDEncode">HDEncode</option>
      <option value="DDLBase">DDLBase</option>
      <option value="Adit-HD">Adit-HD</option>
    </select>

    <div class="flex items-center gap-0.5">
      <button
        disabled={$scanState !== 'idle' || pages <= 1}
        onclick={() => pages = Math.max(1, pages - 1)}
        aria-label="Decrease pages"
        class="w-6 h-6 rounded bg-[var(--bg-tertiary)] border border-[var(--border)] text-[var(--text-primary)] text-xs hover:border-[var(--accent)] disabled:opacity-40"
      >-</button>
      <span class="w-6 text-center text-xs text-[var(--text-primary)]">{pages}</span>
      <button
        disabled={$scanState !== 'idle' || pages >= 99}
        onclick={() => pages = Math.min(99, pages + 1)}
        aria-label="Increase pages"
        class="w-6 h-6 rounded bg-[var(--bg-tertiary)] border border-[var(--border)] text-[var(--text-primary)] text-xs hover:border-[var(--accent)] disabled:opacity-40"
      >+</button>
      <span class="text-[10px] text-[var(--text-secondary)] ml-0.5">pg</span>
    </div>

    <!-- Category checkboxes inline -->
    {#if $scanState === 'idle'}
      <div class="flex items-center gap-1.5 ml-1 border-l border-[var(--border)] pl-2">
        {#each categories as cat (cat.key)}
          <label class="flex items-center gap-1 cursor-pointer px-1.5 py-0.5 rounded bg-[var(--bg-tertiary)] border border-[var(--border)] hover:border-[var(--accent)] transition-colors">
            <input
              type="checkbox"
              checked={flags[cat.key] ?? cat.default}
              onchange={(e) => { flags[cat.key] = e.currentTarget.checked; }}
              class="accent-[var(--accent)] w-3 h-3"
            />
            <span class="text-[10px] text-[var(--text-secondary)]">{cat.label}</span>
          </label>
        {/each}
      </div>
    {/if}
  {:else}
    <input
      type="text"
      bind:value={query}
      placeholder="Search title..."
      class="bg-[var(--bg-tertiary)] text-[var(--text-primary)] px-2 py-1 rounded border border-[var(--border)] text-xs flex-1 max-w-xs focus:outline-none focus:border-[var(--accent)]"
    />
  {/if}

  {#if $scanState === 'idle'}
    <button
      onclick={handleStart}
      class="px-3 py-1 bg-[var(--accent)] hover:bg-[var(--accent-hover)] text-white rounded text-xs font-medium transition-colors {!hasInteracted ? 'animate-pulse-once' : ''}"
    >
      Start Scan
    </button>
  {:else}
    <button
      onclick={() => { hasInteracted = true; stopScan(); }}
      disabled={$scanState === 'stopping'}
      class="px-3 py-1 bg-[var(--error)] hover:bg-red-600 text-white rounded text-xs font-medium transition-colors disabled:opacity-50"
    >
      {$scanState === 'stopping' ? 'Stopping...' : 'Stop'}
    </button>
  {/if}

  {#if $scanState === 'running'}
    <div class="flex-1 max-w-xs ml-1">
      <div class="flex justify-between text-[10px] text-[var(--text-secondary)] mb-0.5">
        <span>{$scanPhase}{#if $scanItemCount > 0} · {$scanItemCount} items{/if}</span>
        <span>{Math.round($scanProgress * 100)}%</span>
      </div>
      <div class="h-1 bg-[var(--bg-tertiary)] rounded-full overflow-hidden">
        <div
          class="h-full bg-[var(--accent)] transition-all duration-300 rounded-full"
          style="width: {$scanProgress * 100}%"
        ></div>
      </div>
    </div>
  {/if}
</div>

<!-- Mobile scan bar: collapses via a CSS-grid height animation (no fixed
     max-height guess needed) when hideMobileBar is true. Grid row itself
     carries the transition so both open and close animate smoothly. -->
<div
  class="grid md:hidden transition-[grid-template-rows] duration-200 ease-out"
  style="grid-template-rows: {hideMobileBar ? '0fr' : '1fr'};"
>
<div class="overflow-hidden flex items-center gap-2 px-3 py-2 border-b border-[var(--border)]">
  {#if $scanState === 'idle'}
    <button
      onclick={() => (scanSheet = true)}
      aria-label="Scan options"
      class="flex-1 min-w-0 flex items-center gap-2 px-3 py-2 rounded-lg bg-[var(--bg-tertiary)] border border-[var(--border)] text-sm text-[var(--text-primary)]"
    >
      <span class="truncate">{scanTypeLabel}{#if selectedType !== 'search'} · {selectedSource}{/if}</span>
      <svg class="w-4 h-4 ml-auto shrink-0 text-[var(--text-secondary)]" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" d="M19 9l-7 7-7-7" /></svg>
    </button>
    <button
      onclick={handleStart}
      class="shrink-0 px-4 py-2 bg-[var(--accent)] text-white rounded-lg text-sm font-semibold"
    >Scan</button>
  {:else}
    <div class="flex-1 min-w-0">
      <div class="flex justify-between text-[10px] text-[var(--text-secondary)] mb-0.5">
        <span class="truncate">{$scanPhase}{#if $scanItemCount > 0} · {$scanItemCount}{/if}</span>
        <span>{Math.round($scanProgress * 100)}%</span>
      </div>
      <div class="h-1 bg-[var(--bg-tertiary)] rounded-full overflow-hidden">
        <div class="h-full bg-[var(--accent)] transition-all duration-300 rounded-full" style="width: {$scanProgress * 100}%"></div>
      </div>
    </div>
    <button
      onclick={() => { hasInteracted = true; stopScan(); }}
      disabled={$scanState === 'stopping'}
      class="shrink-0 px-4 py-2 bg-[var(--error)] text-white rounded-lg text-sm font-semibold disabled:opacity-50"
    >{$scanState === 'stopping' ? '…' : 'Stop'}</button>
  {/if}
</div>
</div>

<BottomSheet open={scanSheet} title="Scan options" onclose={() => (scanSheet = false)}>
  <div class="space-y-4">
    <div>
      <label for="sc-type" class="text-xs font-medium text-[var(--text-secondary)] mb-1.5 block">Scan type</label>
      <select id="sc-type" bind:value={selectedType} class="w-full bg-[var(--bg-tertiary)] text-[var(--text-primary)] px-3 py-2.5 rounded-lg border border-[var(--border)] text-sm">
        {#each scanTypes as t}<option value={t.value}>{t.label}</option>{/each}
      </select>
    </div>

    {#if selectedType !== 'search'}
      <div>
        <label for="sc-source" class="text-xs font-medium text-[var(--text-secondary)] mb-1.5 block">Source</label>
        <select id="sc-source" value={selectedSource} onchange={(e) => onSourceChange(e.currentTarget.value as Source)} class="w-full bg-[var(--bg-tertiary)] text-[var(--text-primary)] px-3 py-2.5 rounded-lg border border-[var(--border)] text-sm">
          <option value="HDEncode">HDEncode</option>
          <option value="DDLBase">DDLBase</option>
          <option value="Adit-HD">Adit-HD</option>
        </select>
      </div>

      <div class="flex items-center justify-between">
        <span class="text-xs font-medium text-[var(--text-secondary)]">Pages</span>
        <div class="flex items-center gap-2">
          <button onclick={() => pages = Math.max(1, pages - 1)} disabled={pages <= 1} aria-label="Decrease pages" class="w-9 h-9 rounded-lg bg-[var(--bg-tertiary)] border border-[var(--border)] text-lg disabled:opacity-40">-</button>
          <span class="w-8 text-center text-sm">{pages}</span>
          <button onclick={() => pages = Math.min(99, pages + 1)} disabled={pages >= 99} aria-label="Increase pages" class="w-9 h-9 rounded-lg bg-[var(--bg-tertiary)] border border-[var(--border)] text-lg disabled:opacity-40">+</button>
        </div>
      </div>

      <div>
        <p class="text-xs font-medium text-[var(--text-secondary)] mb-1.5">Categories</p>
        <div class="flex flex-wrap gap-2">
          {#each categories as cat (cat.key)}
            <button
              onclick={() => { flags[cat.key] = !(flags[cat.key] ?? cat.default); }}
              class="px-3 py-1.5 rounded-full text-sm border transition-colors {(flags[cat.key] ?? cat.default) ? 'bg-[var(--accent)]/15 border-[var(--accent)] text-[var(--accent)]' : 'border-[var(--border)] text-[var(--text-secondary)]'}"
            >{cat.label}</button>
          {/each}
        </div>
      </div>
    {:else}
      <div>
        <label for="sc-query" class="text-xs font-medium text-[var(--text-secondary)] mb-1.5 block">Search title</label>
        <input id="sc-query" type="text" bind:value={query} placeholder="e.g. Dune" class="w-full bg-[var(--bg-tertiary)] text-[var(--text-primary)] px-3 py-2.5 rounded-lg border border-[var(--border)] text-sm" />
      </div>
    {/if}

    <button onclick={mobileStart} class="w-full py-3 bg-[var(--accent)] text-white rounded-lg text-sm font-semibold">Start Scan</button>
  </div>
</BottomSheet>

<style>
  @keyframes pulse-once {
    0%, 100% { box-shadow: 0 0 0 0 rgba(6, 182, 212, 0); }
    50% { box-shadow: 0 0 0 8px rgba(6, 182, 212, 0.3); }
  }
  :global(.animate-pulse-once) {
    animation: pulse-once 2s ease-in-out 2;
  }
</style>
