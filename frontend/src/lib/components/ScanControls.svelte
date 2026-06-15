<script lang="ts">
  import { scanState, scanProgress, scanPhase, scanItemCount, startScan, stopScan } from '$lib/stores/scanner';
  import type { ScanType } from '$lib/stores/scanner';
  import { clearResults } from '$lib/stores/results';

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

  // Category flags — reset to defaults when source changes
  let flags = $state<Record<string, boolean>>(
    Object.fromEntries(sourceCategories['HDEncode'].map((c) => [c.key, c.default]))
  );

  function onSourceChange(src: Source) {
    selectedSource = src;
    flags = Object.fromEntries(sourceCategories[src].map((c) => [c.key, c.default]));
  }

  let categories = $derived(sourceCategories[selectedSource]);
  let hasInteracted = $state(false);

  function handleStart() {
    hasInteracted = true;
    clearResults();
    startScan(selectedType, query, pages, selectedSource, flags);
  }

  // Expose categories and flags for FilterBar
  export function getCategoryState() {
    return { categories, flags, selectedType, scanState: $scanState };
  }
</script>

<div class="flex items-center gap-2 px-3 py-1.5 border-b border-[var(--border)]">
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

<style>
  @keyframes pulse-once {
    0%, 100% { box-shadow: 0 0 0 0 rgba(6, 182, 212, 0); }
    50% { box-shadow: 0 0 0 8px rgba(6, 182, 212, 0.3); }
  }
  :global(.animate-pulse-once) {
    animation: pulse-once 2s ease-in-out 2;
  }
</style>
