<script lang="ts">
  import type { MediaInventoryItem } from '$lib/api/types';
  let { items, selected, ontoggle, oninspect } = $props<{
    items: MediaInventoryItem[];
    selected: Set<string>;
    ontoggle: (ratingKey: string, checked: boolean) => void;
    oninspect: (item: MediaInventoryItem) => void;
  }>();
  const layer = (item: MediaInventoryItem) => item.dv_layer?.toUpperCase() ?? '—';
</script>

<div class="hidden md:block overflow-x-auto">
  <table class="w-full text-sm">
    <thead><tr><th class="w-10"><span class="sr-only">Select</span></th><th>Movie</th><th>Live evidence</th><th>HDR10+</th><th>Seed check</th><th>Scan</th><th></th></tr></thead>
    <tbody>
      {#each items as item (item.path)}
        <tr>
          <td><input type="checkbox" aria-label={`Select ${item.title ?? 'movie'} for pilot`} disabled={!item.rating_key} checked={Boolean(item.rating_key && selected.has(item.rating_key))} onchange={(e) => item.rating_key && ontoggle(item.rating_key, e.currentTarget.checked)} /></td>
          <td><strong>{item.title ?? 'Untitled'}</strong><small>{item.year ?? 'Year unknown'} · {item.library_name ?? 'Library unknown'}</small></td>
          <td><span class="evidence">{layer(item)}</span> <span>{item.dv_profile ?? item.hdr ?? 'No HDR evidence'}</span></td>
          <td><span class:unknown={item.hdr10plus_state === 'unknown'} class="state">{item.hdr10plus_state}</span></td>
          <td>{item.discrepancy.replaceAll('_', ' ')}</td>
          <td><span class="state" class:unknown={item.scan_state !== 'current'}>{item.scan_state}</span></td>
          <td><button onclick={() => oninspect(item)}>Evidence</button></td>
        </tr>
      {/each}
    </tbody>
  </table>
</div>

<div class="grid gap-3 md:hidden">
  {#each items as item (item.path)}
    <article class="card">
      <div class="flex gap-3 items-start"><input type="checkbox" aria-label={`Select ${item.title ?? 'movie'} for pilot`} disabled={!item.rating_key} checked={Boolean(item.rating_key && selected.has(item.rating_key))} onchange={(e) => item.rating_key && ontoggle(item.rating_key, e.currentTarget.checked)} /><div class="min-w-0 flex-1"><strong>{item.title ?? 'Untitled'}</strong><small>{item.year ?? 'Year unknown'} · {item.library_name ?? 'Library unknown'}</small></div><button onclick={() => oninspect(item)}>Evidence</button></div>
      <div class="mt-3 flex flex-wrap gap-2 text-xs"><span class="evidence">DV {layer(item)}</span><span class="state">HDR10+ {item.hdr10plus_state}</span><span class="state">{item.scan_state}</span></div>
    </article>
  {/each}
</div>

<style>
  table { border-collapse: collapse; }
  th { color: var(--text-secondary); font-size: .68rem; letter-spacing: .08em; padding: .75rem; text-align: left; text-transform: uppercase; }
  td { border-top: 1px solid var(--border); padding: .85rem .75rem; vertical-align: middle; }
  small { color: var(--text-secondary); display: block; margin-top: .2rem; }
  button { color: var(--accent); font-weight: 700; }
  button:focus-visible, input:focus-visible { outline: 2px solid var(--accent); outline-offset: 3px; }
  .evidence, .state { border: 1px solid var(--border); border-radius: 999px; display: inline-block; padding: .2rem .5rem; }
  .evidence { border-color: color-mix(in srgb, var(--accent) 48%, var(--border)); color: var(--accent); font-weight: 800; }
  .unknown { color: var(--warning); }
  .card { border: 1px solid var(--border); border-radius: .9rem; background: var(--bg-secondary); padding: 1rem; }
</style>
