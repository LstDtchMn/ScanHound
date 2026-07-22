<script lang="ts">
  import type { MediaInventoryItem } from '$lib/api/types';
  let { item, onclose } = $props<{ item: MediaInventoryItem; onclose: () => void }>();
</script>

<div class="fixed inset-0 z-40 bg-[var(--bg-overlay)]" role="presentation" onclick={onclose}></div>
<aside class="fixed z-50 inset-y-0 right-0 w-full max-w-lg bg-[var(--bg-secondary)] border-l border-[var(--border)] p-5 overflow-y-auto" aria-label="Metadata evidence" tabindex="-1">
  <header class="flex gap-3 items-start"><div class="min-w-0 flex-1"><p class="eyebrow">Local-file evidence</p><h2 class="text-xl font-bold">{item.title ?? 'Untitled movie'}</h2><p class="text-sm text-[var(--text-secondary)]">{item.year ?? 'Year unknown'} · {item.resolution ?? 'Resolution unknown'}</p></div><button class="close" onclick={onclose} aria-label="Close evidence">×</button></header>

  <ol class="rail" aria-label="Metadata evidence flow">
    <li><span>1</span><div><strong>Seed</strong><p>{item.seed_layer?.toUpperCase() ?? 'No historic FEL/MEL claim'}</p></div></li>
    <li><span>2</span><div><strong>Live file</strong><p>DV {item.scan_layer?.toUpperCase() ?? 'unknown'} · profile {item.dv_profile ?? 'unknown'} · HDR10+ {item.hdr10plus_state}</p></div></li>
    <li><span>3</span><div><strong>Plex</strong><p>Rating key {item.rating_key ?? 'not matched'}; no labels changed by this scan.</p></div></li>
    <li><span>4</span><div><strong>Kometa</strong><p>Export is review-only until the pilot and label dry run are accepted.</p></div></li>
  </ol>

  <dl class="facts"><div><dt>Reconciliation</dt><dd>{item.discrepancy.replaceAll('_', ' ')}</dd></div><div><dt>Scan state</dt><dd>{item.scan_state}</dd></div><div><dt>Last scanned</dt><dd>{item.last_scanned_at ? new Date(item.last_scanned_at).toLocaleString() : 'Never'}</dd></div><div><dt>File</dt><dd class="break-all">{item.path}</dd></div></dl>
</aside>

<style>
  .eyebrow { color: var(--accent); font-size: .7rem; font-weight: 800; letter-spacing: .12em; text-transform: uppercase; }
  .close { border: 1px solid var(--border); border-radius: 999px; font-size: 1.4rem; height: 2.25rem; width: 2.25rem; }
  .close:focus-visible { outline: 2px solid var(--accent); outline-offset: 3px; }
  .rail { display: grid; margin: 2rem 0; }
  .rail li { display: grid; grid-template-columns: 2rem 1fr; gap: .8rem; min-height: 5rem; position: relative; }
  .rail li:not(:last-child)::after { background: var(--border); content: ''; left: .95rem; position: absolute; top: 2rem; bottom: 0; width: 2px; }
  .rail span { align-items: center; background: var(--bg-tertiary); border: 1px solid var(--accent); border-radius: 999px; color: var(--accent); display: flex; font: 700 .75rem ui-monospace, monospace; height: 2rem; justify-content: center; z-index: 1; }
  .rail p { color: var(--text-secondary); font-size: .85rem; margin-top: .25rem; }
  .facts { border-top: 1px solid var(--border); }
  .facts div { border-bottom: 1px solid var(--border); display: grid; gap: .25rem; padding: .8rem 0; }
  dt { color: var(--text-secondary); font-size: .7rem; letter-spacing: .08em; text-transform: uppercase; }
  dd { font-size: .9rem; }
</style>
