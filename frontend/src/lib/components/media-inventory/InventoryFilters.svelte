<script lang="ts">
  import type { InventoryFilters } from '$lib/stores/mediaInventory';
  import type { MediaInventoryFacets } from '$lib/api/types';

  let { filters, facets, onchange } = $props<{
    filters: InventoryFilters;
    facets: MediaInventoryFacets;
    onchange: (next: InventoryFilters) => void;
  }>();

  function update(key: keyof InventoryFilters, value: string) {
    onchange({ ...filters, [key]: value, page: '1' });
  }
</script>

<section class="grid gap-3 lg:grid-cols-[minmax(16rem,2fr)_repeat(5,minmax(8rem,1fr))]" aria-label="Inventory filters">
  <label class="filter-field">
    <span>Find a movie or path</span>
    <input value={filters.q ?? ''} oninput={(e) => update('q', e.currentTarget.value)} placeholder="Title, year, or file" />
  </label>
  <label class="filter-field">
    <span>DV layer</span>
    <select value={filters.dv_layer ?? ''} onchange={(e) => update('dv_layer', e.currentTarget.value)}>
      <option value="">All layers</option>
      {#each facets.dv_layer ?? [] as facet}<option value={facet.value}>{facet.value.toUpperCase()} ({facet.count})</option>{/each}
    </select>
  </label>
  <label class="filter-field">
    <span>HDR10+</span>
    <select value={filters.hdr10plus_state ?? ''} onchange={(e) => update('hdr10plus_state', e.currentTarget.value)}>
      <option value="">Any evidence</option>
      {#each facets.hdr10plus_state ?? [] as facet}<option value={facet.value}>{facet.value} ({facet.count})</option>{/each}
    </select>
  </label>
  <label class="filter-field">
    <span>HDR format</span>
    <select value={filters.hdr ?? ''} onchange={(e) => update('hdr', e.currentTarget.value)}>
      <option value="">All HDR</option>
      {#each facets.hdr ?? [] as facet}<option value={facet.value}>{facet.value} ({facet.count})</option>{/each}
    </select>
  </label>
  <label class="filter-field">
    <span>Scan state</span>
    <select value={filters.scan_state ?? ''} onchange={(e) => update('scan_state', e.currentTarget.value)}>
      <option value="">All states</option>
      {#each facets.scan_state ?? [] as facet}<option value={facet.value}>{facet.value} ({facet.count})</option>{/each}
    </select>
  </label>
  <label class="filter-field">
    <span>Seed check</span>
    <select value={filters.discrepancy ?? ''} onchange={(e) => update('discrepancy', e.currentTarget.value)}>
      <option value="">All comparisons</option>
      {#each facets.discrepancy ?? [] as facet}<option value={facet.value}>{facet.value.replaceAll('_', ' ')} ({facet.count})</option>{/each}
    </select>
  </label>
</section>

<style>
  .filter-field { display: grid; gap: .35rem; min-width: 0; }
  .filter-field span { color: var(--text-secondary); font-size: .68rem; font-weight: 700; letter-spacing: .08em; text-transform: uppercase; }
  .filter-field input, .filter-field select { width: 100%; border: 1px solid var(--border); border-radius: .65rem; background: var(--bg-tertiary); color: var(--text-primary); padding: .65rem .75rem; font-size: .875rem; }
  .filter-field input:focus-visible, .filter-field select:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
</style>
