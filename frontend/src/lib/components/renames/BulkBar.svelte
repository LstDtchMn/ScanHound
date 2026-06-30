<script lang="ts">
  import {
    selectedJobIds, selectAll, clearSelection, bulkBusy,
    bulkApply, bulkReidentify, bulkDelete, bulkSetDestination, applyConfident
  } from '$lib/stores/renames';
  import { settings } from '$lib/stores/settings';

  let { shownIds }: { shownIds: number[] } = $props();

  // Build root options from configured settings — send the ACTUAL path strings.
  let roots = $derived.by(() => {
    const s = $settings;
    const opts: { label: string; value: string }[] = [];
    if (s.auto_rename_tv_library)        opts.push({ label: 'TV',          value: s.auto_rename_tv_library });
    if (s.auto_rename_movie_library_4k)  opts.push({ label: 'Movies 4K',   value: s.auto_rename_movie_library_4k });
    if (s.auto_rename_movie_library)     opts.push({ label: 'Movies 1080p', value: s.auto_rename_movie_library });
    return opts;
  });

  let selectedCount = $derived($selectedJobIds.size);
  let allShownSelected = $derived(
    shownIds.length > 0 && shownIds.every((id) => $selectedJobIds.has(id))
  );
  let destOpen = $state(false);
  let destRoot = $state('');

  // Keep destRoot pointing at the first available root whenever the list changes.
  $effect(() => {
    if (roots.length > 0 && !roots.some((r) => r.value === destRoot)) {
      destRoot = roots[0].value;
    }
  });

  function toggleAll() {
    if (allShownSelected) clearSelection();
    else selectAll(shownIds);
  }

  function confirmDelete() {
    if (confirm(`Delete ${selectedCount} job(s)? This cannot be undone.`)) bulkDelete();
  }

  function applyDest() {
    if (!destRoot) return;
    bulkSetDestination(destRoot);
    destOpen = false;
  }
</script>

{#if selectedCount > 0}
  <div
    class="sticky top-0 z-20 flex flex-wrap items-center gap-2 px-3 py-2 rounded-lg
      bg-[var(--bg-secondary)] border border-[var(--accent)] shadow"
  >
    <label class="flex items-center gap-1 text-xs">
      <input
        type="checkbox"
        class="accent-[var(--accent)]"
        checked={allShownSelected}
        onchange={toggleAll}
      />
      Select all
    </label>
    <span class="text-xs text-[var(--text-secondary)]">{selectedCount} selected</span>

    <div class="flex flex-wrap items-center gap-1 ml-auto">
      <button
        class="px-2 py-1 rounded text-[11px] font-medium bg-[var(--accent)] text-white disabled:opacity-50"
        disabled={$bulkBusy}
        onclick={bulkApply}
      >Apply</button>

      <button
        class="px-2 py-1 rounded text-[11px] font-medium bg-[var(--bg-tertiary)] disabled:opacity-50"
        disabled={$bulkBusy}
        onclick={bulkReidentify}
      >Re-identify</button>

      <div class="relative">
        <button
          class="px-2 py-1 rounded text-[11px] font-medium bg-[var(--bg-tertiary)] disabled:opacity-50"
          disabled={$bulkBusy}
          onclick={() => (destOpen = !destOpen)}
        >Set destination ▾</button>
        {#if destOpen}
          <div
            class="absolute right-0 mt-1 z-30 flex flex-col gap-1 p-2 rounded-lg
              bg-[var(--bg-secondary)] border border-[var(--border)] shadow"
          >
            {#if roots.length > 0}
              <select
                bind:value={destRoot}
                class="px-2 py-1 rounded text-xs bg-[var(--bg-tertiary)] border border-[var(--border)]"
              >
                {#each roots as r (r.value)}
                  <option value={r.value}>{r.label}</option>
                {/each}
              </select>
              <button
                class="px-2 py-1 rounded text-[11px] font-medium bg-[var(--accent)] text-white"
                onclick={applyDest}
              >Apply destination</button>
            {:else}
              <p class="text-xs text-[var(--text-secondary)] px-1">
                No library roots configured.<br />
                Set them in Settings → Rename.
              </p>
            {/if}
          </div>
        {/if}
      </div>

      <button
        class="px-2 py-1 rounded text-[11px] font-medium bg-[var(--success)]/15 text-[var(--success)] disabled:opacity-50"
        disabled={$bulkBusy}
        title="Applies only matched jobs with confidence ≥ 95%; needs_review / low-confidence are skipped"
        onclick={() => applyConfident([...$selectedJobIds])}
      >Apply confident</button>

      <button
        class="px-2 py-1 rounded text-[11px] font-medium bg-[var(--error)]/15 text-[var(--error)] disabled:opacity-50"
        disabled={$bulkBusy}
        onclick={confirmDelete}
      >Delete</button>
    </div>
  </div>
{/if}
