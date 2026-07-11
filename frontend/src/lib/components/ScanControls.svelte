<script lang="ts">
  import { scanState, scanProgress, scanPhase, scanItemCount, startScan, stopScan } from '$lib/stores/scanner';
  import type { ScanType } from '$lib/stores/scanner';
  import { clearResults, categoryFilter, toggleCategoryFilter, sourceCategories, normCat, flagsFor, type ScanSource, mobileChromeCollapsed } from '$lib/stores/results';
  import BottomSheet from './BottomSheet.svelte';

  let scanSheet = $state(false);

  const scanTypes: { value: ScanType; label: string }[] = [
    { value: 'deep', label: 'Deep Scan' },
    { value: 'incremental', label: 'Incremental' },
    { value: 'loaded', label: 'Load Cache' },
    { value: 'search', label: 'Site Search' }
  ];

  type Source = ScanSource;

  let selectedType = $state<ScanType>('deep');
  let selectedSource = $state<Source>('HDEncode');
  let query = $state('');
  let pages = $state(1);

  // Category flags — a READ-ONLY mirror of categoryFilter (mapped onto the
  // current source's per-source keys via flagsFor/normCat, both co-located
  // with categoryFilter in results.ts), not local state. This is what makes
  // categoryFilter a true single source of truth: there's no local copy here
  // for an external write (FilterBar's chips) to go stale against, and
  // nothing here writes categoryFilter except toggleCategoryFilter calls
  // below (no $effect mirroring flags back out — that write path is gone).
  // An explicitly empty categoryFilter (every chip toggled off) correctly
  // yields every flag false here — see flagsFor's doc comment in results.ts
  // for why that must NOT fall back to the source's defaults.
  // Switching source re-maps the SAME normalized preference onto the new
  // source's keys (e.g. a "remux only" choice survives HDEncode -> DDLBase as
  // both 4k_remux AND 1080p_remux turning on together, since categoryFilter
  // only ever tracks the normalized 'remux' category, not per-source
  // sub-keys — a deliberate simplification, see the commit message).
  let flags = $derived(flagsFor(selectedSource, $categoryFilter));

  function onSourceChange(src: Source) {
    selectedSource = src;
  }

  let categories = $derived(sourceCategories[selectedSource]);
  let hasInteracted = $state(false);

  let scanTypeLabel = $derived(scanTypes.find((t) => t.value === selectedType)?.label ?? 'Scan');

  // Auto-hide the mobile bar per scroll direction (set by MobileScanView), but
  // never while a scan is actually running/stopping — progress must stay pinned.
  let hideMobileBar = $derived($mobileChromeCollapsed && $scanState === 'idle');

  // True when no category is selected for a category-driven scan (every chip
  // toggled off) — starting a scan in this state would silently diverge from
  // what the (all-unchecked) checkboxes show, either scraping nothing useful
  // or falling back to some default set server-side. 'search' doesn't use
  // categories at all, so it's never gated by this. See flagsFor (results.ts)
  // for why categoryFilter=[] must project to all-false rather than defaults.
  let noCategorySelected = $derived(selectedType !== 'search' && $categoryFilter.length === 0);

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
              checked={flags[cat.key]}
              onchange={() => toggleCategoryFilter(normCat(cat.key))}
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
      disabled={noCategorySelected}
      title={noCategorySelected ? 'Select at least one category' : undefined}
      class="px-3 py-1 bg-[var(--accent)] hover:bg-[var(--accent-hover)] text-white rounded text-xs font-medium transition-colors disabled:opacity-50 {!hasInteracted ? 'animate-pulse-once' : ''}"
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
      disabled={noCategorySelected}
      title={noCategorySelected ? 'Select at least one category' : undefined}
      class="shrink-0 px-4 py-2 bg-[var(--accent)] text-white rounded-lg text-sm font-semibold disabled:opacity-50"
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
              onclick={() => toggleCategoryFilter(normCat(cat.key))}
              class="px-3 py-1.5 rounded-full text-sm border transition-colors {flags[cat.key] ? 'bg-[var(--accent)]/15 border-[var(--accent)] text-[var(--accent)]' : 'border-[var(--border)] text-[var(--text-secondary)]'}"
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

    <button
      onclick={mobileStart}
      disabled={noCategorySelected}
      title={noCategorySelected ? 'Select at least one category' : undefined}
      class="w-full py-3 bg-[var(--accent)] text-white rounded-lg text-sm font-semibold disabled:opacity-50"
    >Start Scan</button>
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
