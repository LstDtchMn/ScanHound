<script lang="ts">
  import { renameJobs, renameCategory, renameQuery, renameSort } from '$lib/stores/renames';
  import { categoryOf, RENAME_CATEGORIES, type RenameCategory } from '$lib/renames/category';

  const LABELS: Record<RenameCategory, string> = {
    all: 'All',
    movies: 'Movies',
    tv: 'TV',
    '4k': '4K',
    '1080p': '1080p',
    remux: 'Remux'
  };

  // Per-category live counts derived from the full job list.
  let counts = $derived.by(() => {
    const c: Record<RenameCategory, number> = {
      all: $renameJobs.length, movies: 0, tv: 0, '4k': 0, '1080p': 0, remux: 0
    };
    for (const job of $renameJobs) {
      for (const cat of categoryOf(job)) c[cat] += 1;
    }
    return c;
  });

  const SORTS: { value: typeof $renameSort; label: string }[] = [
    { value: 'detected_desc', label: 'Newest' },
    { value: 'detected_asc', label: 'Oldest' },
    { value: 'confidence_desc', label: 'Confidence' },
    { value: 'title_asc', label: 'Title A–Z' }
  ];
</script>

<div class="flex flex-wrap items-center gap-2">
  <div class="flex flex-wrap items-center gap-1">
    {#each RENAME_CATEGORIES as cat (cat)}
      <button
        onclick={() => renameCategory.set(cat)}
        class="px-2 py-1 rounded text-[11px] font-medium transition-colors border
          {$renameCategory === cat
            ? 'bg-[var(--accent)]/15 border-[var(--accent)] text-[var(--accent)]'
            : 'border-transparent text-[var(--text-secondary)] hover:bg-[var(--bg-tertiary)]'}"
      >
        {LABELS[cat]} <span class="opacity-70">({counts[cat]})</span>
      </button>
    {/each}
  </div>

  <input
    type="search"
    placeholder="Search title / filename…"
    value={$renameQuery}
    oninput={(e) => renameQuery.set((e.target as HTMLInputElement).value)}
    class="flex-1 min-w-[160px] px-2 py-1 rounded text-xs bg-[var(--bg-tertiary)] border border-[var(--border)] focus:border-[var(--accent)] outline-none"
  />

  <select
    value={$renameSort}
    onchange={(e) => renameSort.set((e.target as HTMLSelectElement).value as typeof $renameSort)}
    class="px-2 py-1 rounded text-xs bg-[var(--bg-tertiary)] border border-[var(--border)]"
  >
    {#each SORTS as s (s.value)}
      <option value={s.value}>{s.label}</option>
    {/each}
  </select>
</div>
