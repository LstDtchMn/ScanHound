<script lang="ts">
  import { api } from '$lib/api/client';
  import type { WatchlistItem, WatchlistStats } from '$lib/api/types';
  import { addToast } from '$lib/stores/notifications';
  import { onMount } from 'svelte';
  import Skeleton from '$lib/components/Skeleton.svelte';
  import ErrorCard from '$lib/components/ErrorCard.svelte';
  import Badge from '$lib/components/Badge.svelte';
  import ConfirmDialog from '$lib/components/ConfirmDialog.svelte';
  import { priorityVariant, PRIORITY_LABELS, WATCHLIST_STATUS_COLORS } from '$lib/constants';

  let items: WatchlistItem[] = $state([]);
  let loading = $state(true);
  let loadError = $state('');
  let filterInput = $state('');
  let filter = $state('');
  let filterTimer: ReturnType<typeof setTimeout> | undefined;
  let searching = $state(false);
  let searchResults: WatchlistItem[] | null = $state(null);

  function onFilterInput() {
    clearTimeout(filterTimer);
    filterTimer = setTimeout(() => { filter = filterInput; }, 200);
  }

  $effect(() => {
    const q = filter;
    if (!q) {
      searchResults = null;
      return;
    }
    searching = true;
    api.watchlistSearch(q).then((results) => {
      // Only apply if filter hasn't changed since we started
      if (filter === q) {
        searchResults = results;
        searching = false;
      }
    }).catch(() => {
      if (filter === q) {
        searchResults = null;
        searching = false;
      }
    });
  });

  let statusFilter = $state('');
  let showAddForm = $state(false);
  let sortBy = $state<'title' | 'added_date' | 'priority' | 'status'>('priority');
  let sortDir = $state<'asc' | 'desc'>('desc');

  // Stats
  let stats: WatchlistStats | null = $state(null);

  // Import dropdown
  let showImportMenu = $state(false);
  let showTraktDialog = $state(false);
  let traktUsername = $state('');
  let traktImporting = $state(false);
  let jsonFileInput: HTMLInputElement | undefined = $state();
  let imdbFileInput: HTMLInputElement | undefined = $state();
  let letterboxdFileInput: HTMLInputElement | undefined = $state();

  // Inline editing
  let confirmDelete = $state<{ id: number; title: string } | null>(null);
  let editingId: number | null = $state(null);
  let editTitle = $state('');
  let editYear = $state('');
  let editImdbId = $state('');
  let editType = $state('movie');
  let editSeason = $state('');
  let editPriority = $state(2);
  let editResolution = $state('');
  let editPreferDovi = $state(false);
  let editNotes = $state('');

  // Add form fields
  let newTitle = $state('');
  let newYear = $state('');
  let newImdbId = $state('');
  let newType = $state('movie');
  let newPriority = $state(2);
  let newSeason = $state('');
  let newResolution = $state('');
  let newNotes = $state('');

  onMount(() => {
    loadItems();
    loadStats();
  });

  async function loadStats() {
    try {
      stats = await api.watchlistStats();
    } catch {
      // non-critical, don't toast
    }
  }

  async function loadItems() {
    loading = true;
    loadError = '';
    try {
      items = await api.watchlistList(statusFilter || undefined);
    } catch (e) {
      loadError = e instanceof Error ? e.message : 'Failed to load watchlist';
    } finally {
      loading = false;
    }
  }

  async function addItem() {
    if (!newTitle.trim()) return;
    if (newType === 'tv_season' && !newSeason) {
      addToast('Error', 'Season number is required for TV Season', 'error');
      return;
    }
    try {
      await api.watchlistAdd({
        title: newTitle.trim(),
        year: newYear ? (Number.isNaN(parseInt(newYear, 10)) ? null : parseInt(newYear, 10)) : null,
        imdb_id: newImdbId || null,
        item_type: newType,
        season: newSeason ? (Number.isNaN(parseInt(newSeason, 10)) ? null : parseInt(newSeason, 10)) : null,
        priority: newPriority,
        min_resolution: newResolution || null,
        notes: newNotes
      });
      addToast('Added', `"${newTitle}" added to watchlist`);
      newTitle = ''; newYear = ''; newImdbId = ''; newNotes = ''; newSeason = '';
      showAddForm = false;
      await loadItems();
      await loadStats();
    } catch (e) {
      addToast('Error', e instanceof Error ? e.message : 'Failed to add item', 'error');
    }
  }

  async function removeItem(id: number, title: string) {
    try {
      await api.watchlistRemove(id);
      addToast('Removed', `"${title}" removed`);
      if (editingId === id) editingId = null;
      await loadItems();
      await loadStats();
    } catch (e) {
      addToast('Error', e instanceof Error ? e.message : 'Failed to remove item', 'error');
    }
  }

  // Inline edit functions
  function startEdit(item: WatchlistItem) {
    if (editingId === item.id) {
      editingId = null;
      return;
    }
    editingId = item.id;
    editTitle = item.title;
    editYear = item.year?.toString() ?? '';
    editImdbId = item.imdb_id ?? '';
    editType = item.item_type;
    editSeason = item.season?.toString() ?? '';
    editPriority = item.priority;
    editResolution = item.min_resolution ?? '';
    editPreferDovi = item.prefer_dovi;
    editNotes = item.notes ?? '';
  }

  async function saveEdit() {
    if (editingId === null) return;
    try {
      await api.watchlistUpdate(editingId, {
        title: editTitle.trim(),
        year: editYear ? (Number.isNaN(parseInt(editYear, 10)) ? null : parseInt(editYear, 10)) : null,
        imdb_id: editImdbId || null,
        item_type: editType,
        season: editSeason ? (Number.isNaN(parseInt(editSeason, 10)) ? null : parseInt(editSeason, 10)) : null,
        priority: editPriority,
        min_resolution: editResolution || null,
        prefer_dovi: editPreferDovi,
        notes: editNotes
      });
      addToast('Updated', `"${editTitle}" updated`);
      editingId = null;
      await loadItems();
      await loadStats();
    } catch (e) {
      addToast('Error', e instanceof Error ? e.message : 'Failed to update item', 'error');
    }
  }

  // Import handlers
  function readFileAsText(file: File): Promise<string> {
    return new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => resolve(reader.result as string);
      reader.onerror = () => reject(reader.error);
      reader.readAsText(file);
    });
  }

  async function handleImportJson(e: Event) {
    const input = e.target as HTMLInputElement;
    const file = input.files?.[0];
    if (!file) return;
    try {
      const content = await readFileAsText(file);
      const result = await api.watchlistImportJson(content);
      addToast('Imported', `${result.imported} items imported from JSON`);
      await loadItems();
      await loadStats();
    } catch (e) {
      addToast('Error', e instanceof Error ? e.message : 'Failed to import JSON', 'error');
    }
    input.value = '';
    showImportMenu = false;
  }

  async function handleImportImdb(e: Event) {
    const input = e.target as HTMLInputElement;
    const file = input.files?.[0];
    if (!file) return;
    try {
      const content = await readFileAsText(file);
      const result = await api.watchlistImportImdb(content);
      addToast('Imported', `${result.imported} items imported from IMDb`);
      await loadItems();
      await loadStats();
    } catch (e) {
      addToast('Error', e instanceof Error ? e.message : 'Failed to import IMDb CSV', 'error');
    }
    input.value = '';
    showImportMenu = false;
  }

  async function handleImportLetterboxd(e: Event) {
    const input = e.target as HTMLInputElement;
    const file = input.files?.[0];
    if (!file) return;
    try {
      const content = await readFileAsText(file);
      const result = await api.watchlistImportLetterboxd(content);
      addToast('Imported', `${result.imported} items imported from Letterboxd`);
      await loadItems();
      await loadStats();
    } catch (e) {
      addToast('Error', e instanceof Error ? e.message : 'Failed to import Letterboxd CSV', 'error');
    }
    input.value = '';
    showImportMenu = false;
  }

  async function handleImportTrakt() {
    if (!traktUsername.trim()) return;
    traktImporting = true;
    try {
      const result = await api.watchlistImportTrakt(traktUsername.trim());
      addToast('Imported', `${result.imported} of ${result.total_in_list} items imported from Trakt`);
      showTraktDialog = false;
      traktUsername = '';
      await loadItems();
      await loadStats();
    } catch (e) {
      addToast('Error', e instanceof Error ? e.message : 'Failed to import from Trakt', 'error');
    } finally {
      traktImporting = false;
    }
  }

  async function handleExportJson() {
    try {
      const data = await api.watchlistExport();
      const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' });
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = `watchlist-export-${new Date().toISOString().slice(0, 10)}.json`;
      a.click();
      URL.revokeObjectURL(url);
      addToast('Exported', `${data.count} items exported`);
    } catch (e) {
      addToast('Error', e instanceof Error ? e.message : 'Failed to export watchlist', 'error');
    }
  }

  let filtered = $derived(
    (searchResults !== null ? searchResults : items)
      .filter((item) => !statusFilter || item.status === statusFilter)
      .sort((a, b) => {
        let cmp = 0;
        switch (sortBy) {
          case 'title': cmp = a.title.localeCompare(b.title); break;
          case 'added_date': cmp = (a.added_date ?? '').localeCompare(b.added_date ?? ''); break;
          case 'priority': cmp = a.priority - b.priority; break;
          case 'status': cmp = a.status.localeCompare(b.status); break;
        }
        return sortDir === 'asc' ? cmp : -cmp;
      })
  );

  const statusColors = WATCHLIST_STATUS_COLORS;
  const priorityLabels = PRIORITY_LABELS;

  let pageContainer: HTMLDivElement | undefined = $state();

  // Scroll to top when sort or filter changes
  $effect(() => {
    // eslint-disable-next-line @typescript-eslint/no-unused-expressions
    sortBy;
    // eslint-disable-next-line @typescript-eslint/no-unused-expressions
    sortDir;
    // eslint-disable-next-line @typescript-eslint/no-unused-expressions
    statusFilter;
    // eslint-disable-next-line @typescript-eslint/no-unused-expressions
    filter;
    pageContainer?.scrollTo({ top: 0, behavior: 'smooth' });
  });

  const inputClass = 'px-3 py-2 rounded bg-[var(--bg-tertiary)] border border-[var(--border)] text-sm text-[var(--text-primary)] placeholder:text-[var(--text-secondary)]';
</script>

{#if showImportMenu}
  <div class="fixed inset-0 z-40" role="button" tabindex="-1" onclick={() => (showImportMenu = false)} onkeydown={(e: KeyboardEvent) => { if (e.key === 'Escape') showImportMenu = false; }}></div>
{/if}

<div class="flex flex-col h-full overflow-auto p-6 gap-4" bind:this={pageContainer}>
  <div class="flex items-center justify-between">
    <h1 class="text-xl font-bold">Watchlist</h1>
    <div class="flex items-center gap-2">
      <!-- Export JSON -->
      <button
        class="px-3 py-1.5 text-xs rounded-lg bg-[var(--bg-secondary)] border border-[var(--border)] text-[var(--text-primary)] hover:bg-[var(--bg-tertiary)] transition-colors"
        onclick={handleExportJson}
      >
        Export JSON
      </button>

      <!-- Import dropdown -->
      <div class="relative">
        <button
          class="px-3 py-1.5 text-xs rounded-lg bg-[var(--bg-secondary)] border border-[var(--border)] text-[var(--text-primary)] hover:bg-[var(--bg-tertiary)] transition-colors"
          onclick={() => (showImportMenu = !showImportMenu)}
        >
          Import &#9662;
        </button>
        {#if showImportMenu}
          <div class="absolute right-0 mt-1 w-48 rounded-lg bg-[var(--bg-secondary)] border border-[var(--border)] shadow-lg z-50 py-1">
            <button
              class="w-full text-left px-4 py-2 text-xs text-[var(--text-primary)] hover:bg-[var(--bg-tertiary)] transition-colors"
              onclick={() => jsonFileInput?.click()}
            >
              Import JSON
            </button>
            <button
              class="w-full text-left px-4 py-2 text-xs text-[var(--text-primary)] hover:bg-[var(--bg-tertiary)] transition-colors"
              onclick={() => imdbFileInput?.click()}
            >
              Import IMDb CSV
            </button>
            <button
              class="w-full text-left px-4 py-2 text-xs text-[var(--text-primary)] hover:bg-[var(--bg-tertiary)] transition-colors"
              onclick={() => letterboxdFileInput?.click()}
            >
              Import Letterboxd CSV
            </button>
            <div class="border-t border-[var(--border)] my-1"></div>
            <button
              class="w-full text-left px-4 py-2 text-xs text-[var(--text-primary)] hover:bg-[var(--bg-tertiary)] transition-colors"
              onclick={() => { showImportMenu = false; showTraktDialog = true; }}
            >
              Import from Trakt
            </button>
          </div>
        {/if}
      </div>

      <!-- Hidden file inputs -->
      <input bind:this={jsonFileInput} type="file" accept=".json" class="hidden" onchange={handleImportJson} />
      <input bind:this={imdbFileInput} type="file" accept=".csv" class="hidden" onchange={handleImportImdb} />
      <input bind:this={letterboxdFileInput} type="file" accept=".csv" class="hidden" onchange={handleImportLetterboxd} />

      <!-- Add button -->
      <button
        class="px-3 py-1.5 text-xs rounded-lg bg-[var(--accent)] text-white hover:opacity-90 transition-opacity"
        onclick={() => (showAddForm = !showAddForm)}
      >
        {showAddForm ? 'Cancel' : '+ Add'}
      </button>
    </div>
  </div>

  <!-- Stats bar -->
  {#if stats}
    <div class="flex items-center gap-3 flex-wrap">
      <span class="text-xs px-2 py-1 rounded-full bg-[var(--bg-secondary)] border border-[var(--border)] text-[var(--warning)] font-medium">
        {stats.by_status?.wanted ?? 0} wanted
      </span>
      <span class="text-xs px-2 py-1 rounded-full bg-[var(--bg-secondary)] border border-[var(--border)] text-[var(--success)] font-medium">
        {stats.by_status?.found ?? 0} found
      </span>
      <span class="text-xs px-2 py-1 rounded-full bg-[var(--bg-secondary)] border border-[var(--border)] text-[var(--accent)] font-medium">
        {stats.by_status?.downloaded ?? 0} downloaded
      </span>
      <span class="text-xs px-2 py-1 rounded-full bg-[var(--bg-secondary)] border border-[var(--border)] text-[var(--success)] font-medium">
        {stats.by_status?.in_library ?? 0} in library
      </span>
    </div>
  {/if}

  <!-- Add form -->
  {#if showAddForm}
    <!-- svelte-ignore a11y_no_static_element_interactions -->
    <div class="bg-[var(--bg-secondary)] rounded-lg p-4 border border-[var(--border)] space-y-3" onkeydown={(e: KeyboardEvent) => { if (e.key === 'Escape') { e.stopPropagation(); showAddForm = false; } }}>
      <div class="grid grid-cols-2 gap-3">
        <input bind:value={newTitle} placeholder="Title *" class="col-span-2 {inputClass}" />
        <input bind:value={newYear} placeholder="Year" type="number" class={inputClass} />
        <input bind:value={newImdbId} placeholder="IMDb ID (tt...)" class={inputClass} />
        <select bind:value={newType} class={inputClass}>
          <option value="movie">Movie</option>
          <option value="tv_show">TV Show</option>
          <option value="tv_season">TV Season</option>
        </select>
        <select bind:value={newPriority} class={inputClass}>
          <option value={1}>Low Priority</option>
          <option value={2}>Normal Priority</option>
          <option value={3}>High Priority</option>
        </select>
        {#if newType === 'tv_season'}
          <input bind:value={newSeason} placeholder="Season # *" type="number" min="1" class={inputClass} />
        {/if}
        <select bind:value={newResolution} class={inputClass}>
          <option value="">Any Resolution</option>
          <option value="720p">720p+</option>
          <option value="1080p">1080p+</option>
          <option value="4K">4K</option>
        </select>
        <input bind:value={newNotes} placeholder="Notes" class={inputClass} />
      </div>
      <button onclick={addItem} class="px-4 py-2 text-xs rounded-lg bg-[var(--accent)] text-white hover:opacity-90">
        Add to Watchlist
      </button>
    </div>
  {/if}

  <!-- Filters -->
  <div class="flex gap-2">
    <input
      bind:value={filterInput}
      oninput={onFilterInput}
      placeholder="Search watchlist..."
      class="flex-1 px-3 py-2 rounded bg-[var(--bg-secondary)] border border-[var(--border)] text-sm text-[var(--text-primary)] placeholder:text-[var(--text-secondary)]"
    />
    <select
      bind:value={statusFilter}
      onchange={() => loadItems()}
      class="px-3 py-2 rounded bg-[var(--bg-secondary)] border border-[var(--border)] text-sm text-[var(--text-primary)]"
    >
      <option value="">All</option>
      <option value="wanted">Wanted</option>
      <option value="found">Found</option>
      <option value="downloaded">Downloaded</option>
      <option value="in_library">In Library</option>
    </select>

    <select
      bind:value={sortBy}
      class="px-3 py-2 rounded bg-[var(--bg-secondary)] border border-[var(--border)] text-sm text-[var(--text-primary)]"
    >
      <option value="priority">Sort: Priority</option>
      <option value="title">Sort: Title</option>
      <option value="added_date">Sort: Date Added</option>
      <option value="status">Sort: Status</option>
    </select>

    <button
      onclick={() => sortDir = sortDir === 'asc' ? 'desc' : 'asc'}
      class="px-2 py-2 rounded bg-[var(--bg-secondary)] border border-[var(--border)] text-sm text-[var(--text-primary)] hover:bg-[var(--bg-tertiary)]"
      title={sortDir === 'asc' ? 'Ascending' : 'Descending'}
    >
      {sortDir === 'asc' ? '\u2191' : '\u2193'}
    </button>
  </div>

  <!-- Items list -->
  <div class="transition-opacity duration-150 {searching ? 'opacity-50' : 'opacity-100'}" class:pointer-events-none={searching}>
  {#if searching && filtered.length === 0}
    <div class="flex items-center justify-center py-12 gap-2 text-[var(--text-secondary)]">
      <svg class="w-4 h-4 animate-spin" viewBox="0 0 24 24" fill="none">
        <circle cx="12" cy="12" r="10" stroke="currentColor" stroke-width="2" opacity="0.25"/>
        <path d="M12 2a10 10 0 019.95 9" stroke="currentColor" stroke-width="2" stroke-linecap="round"/>
      </svg>
      <span class="text-xs">Searching...</span>
    </div>
  {:else if loadError}
    <ErrorCard message={loadError} onretry={loadItems} />
  {:else if loading}
    <div class="space-y-1">
      {#each Array(4) as _}
        <div class="flex items-center gap-3 px-4 py-3 rounded-lg bg-[var(--bg-secondary)] border border-[var(--border)]">
          <Skeleton width="0.25rem" height="2rem" rounded="rounded-full" />
          <div class="flex-1 space-y-1.5">
            <Skeleton width="40%" height="0.875rem" />
            <Skeleton width="25%" height="0.625rem" />
          </div>
          <Skeleton width="3.5rem" height="0.875rem" rounded="rounded-full" />
          <Skeleton width="3rem" height="0.75rem" />
        </div>
      {/each}
    </div>
  {:else if filtered.length === 0}
    <div class="flex flex-col items-center justify-center h-64 gap-4">
      <svg class="w-12 h-12 text-[var(--text-secondary)] opacity-30" viewBox="0 0 48 48" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
        <path d="M12 6h24a2 2 0 012 2v32l-14-8-14 8V8a2 2 0 012-2z"/>
      </svg>
      {#if items.length === 0}
        <p class="text-sm text-[var(--text-secondary)]">Your watchlist is empty</p>
        <p class="text-xs text-[var(--text-secondary)] opacity-60">Add movies and TV shows to track. You can import from IMDb or Letterboxd.</p>
        <div class="flex gap-2">
          <button
            onclick={() => (showAddForm = true)}
            class="px-3 py-1.5 text-xs rounded-lg bg-[var(--accent)] text-white hover:opacity-90 transition-opacity"
          >
            Add Item
          </button>
          <button
            onclick={() => (showImportMenu = true)}
            class="px-3 py-1.5 text-xs rounded-lg bg-[var(--bg-tertiary)] border border-[var(--border)] text-[var(--text-primary)] hover:bg-[var(--border)] transition-colors"
          >
            Import
          </button>
        </div>
      {:else}
        <p class="text-sm text-[var(--text-secondary)]">No matching items</p>
        <p class="text-xs text-[var(--text-secondary)] opacity-60">Try adjusting your search or status filter.</p>
      {/if}
    </div>
  {:else}
    <div class="space-y-1">
      {#each filtered as item (item.id)}
        <!-- Item row -->
        <div>
          <div
            role="button"
            tabindex="0"
            class="flex items-center gap-3 px-4 py-2 rounded-lg bg-[var(--bg-secondary)] border border-[var(--border)] hover:border-[var(--text-secondary)] transition-colors group cursor-pointer {editingId === item.id ? 'rounded-b-none border-b-0' : ''}"
            onclick={() => startEdit(item)}
            onkeydown={(e: KeyboardEvent) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); startEdit(item); } }}
          >
            <!-- Poster thumbnail (hidden when no image to save space) -->
            {#if item.poster_url}
              <img
                src={item.poster_url}
                alt={item.title}
                class="w-8 h-12 object-cover rounded flex-shrink-0"
              />
            {/if}

            <!-- Priority indicator -->
            <div class="w-1 h-6 rounded-full {item.priority === 3 ? 'bg-[var(--error)]' : item.priority === 2 ? 'bg-[var(--warning)]' : 'bg-[var(--border)]'}"></div>

            <!-- Title + metadata -->
            <div class="flex-1 min-w-0">
              <div class="flex items-center gap-2">
                <span class="font-medium text-sm truncate">{item.title}</span>
                {#if item.year}<span class="text-xs text-[var(--text-secondary)]">({item.year})</span>{/if}
              </div>
              <div class="flex items-center gap-2 text-xs text-[var(--text-secondary)] mt-0.5">
                <span class="capitalize">{item.item_type.replace('_', ' ')}</span>
                {#if item.season}<span>S{item.season}</span>{/if}
                {#if item.min_resolution}<span>min {item.min_resolution}</span>{/if}
                {#if item.notes}<span class="truncate max-w-[200px]">{item.notes}</span>{/if}
              </div>
            </div>

            <!-- IMDb link -->
            {#if item.imdb_id}
              <a
                href="https://www.imdb.com/title/{item.imdb_id}"
                target="_blank"
                rel="noopener noreferrer"
                class="px-2 py-0.5 text-[10px] font-bold rounded bg-amber-900/40 text-amber-400 hover:bg-amber-900/60 transition-colors"
                onclick={(e: MouseEvent) => e.stopPropagation()}
              >
                IMDb
              </a>
            {/if}

            <!-- Status -->
            <span class="text-xs font-medium capitalize {statusColors[item.status] || 'text-[var(--text-secondary)]'}">
              {item.status.replace(/_/g, ' ')}
            </span>

            <!-- Priority badge -->
            <Badge label={priorityLabels[item.priority] || ''} variant={priorityVariant(item.priority)} />

            <!-- Hover action buttons -->
            <div class="flex items-center gap-1 opacity-50 group-hover:opacity-100 transition-opacity">
              <!-- Edit (pencil) -->
              <button
                class="p-1.5 rounded hover:bg-[var(--bg-tertiary)] text-[var(--text-secondary)] hover:text-[var(--text-primary)] transition-colors"
                title="Edit"
                onclick={(e: MouseEvent) => { e.stopPropagation(); startEdit(item); }}
              >
                <svg class="w-3.5 h-3.5" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5">
                  <path d="M11.5 1.5l3 3L5 14H2v-3L11.5 1.5z"/>
                </svg>
              </button>
              <!-- Delete (trash) -->
              <button
                class="p-1.5 rounded hover:bg-[var(--bg-tertiary)] text-[var(--text-secondary)] hover:text-[var(--error)] transition-colors"
                title="Delete"
                onclick={(e: MouseEvent) => { e.stopPropagation(); confirmDelete = { id: item.id, title: item.title }; }}
              >
                <svg class="w-3.5 h-3.5" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5">
                  <path d="M2 4h12M5 4V2h6v2M6 7v5M10 7v5M3 4l1 10h8l1-10"/>
                </svg>
              </button>
            </div>
          </div>

          <!-- Inline edit form -->
          {#if editingId === item.id}
            <!-- svelte-ignore a11y_no_static_element_interactions -->
            <div class="bg-[var(--bg-secondary)] rounded-b-lg border border-[var(--border)] border-t border-t-[var(--border)] p-4 space-y-3" onkeydown={(e: KeyboardEvent) => { if (e.key === 'Escape') { e.stopPropagation(); editingId = null; } }}>
              <div class="grid grid-cols-2 md:grid-cols-3 gap-3">
                <div class="flex flex-col gap-1">
                  <label for="edit-title-{item.id}" class="text-xs text-[var(--text-secondary)]">Title</label>
                  <input id="edit-title-{item.id}" bind:value={editTitle} class={inputClass} />
                </div>
                <div class="flex flex-col gap-1">
                  <label for="edit-year-{item.id}" class="text-xs text-[var(--text-secondary)]">Year</label>
                  <input id="edit-year-{item.id}" bind:value={editYear} type="number" class={inputClass} />
                </div>
                <div class="flex flex-col gap-1">
                  <label for="edit-imdb-{item.id}" class="text-xs text-[var(--text-secondary)]">IMDb ID</label>
                  <input id="edit-imdb-{item.id}" bind:value={editImdbId} placeholder="tt..." class={inputClass} />
                </div>
                <div class="flex flex-col gap-1">
                  <label for="edit-type-{item.id}" class="text-xs text-[var(--text-secondary)]">Type</label>
                  <select id="edit-type-{item.id}" bind:value={editType} class={inputClass}>
                    <option value="movie">Movie</option>
                    <option value="tv_show">TV Show</option>
                    <option value="tv_season">TV Season</option>
                  </select>
                </div>
                {#if editType === 'tv_season' || editType === 'tv_show'}
                  <div class="flex flex-col gap-1">
                    <label for="edit-season-{item.id}" class="text-xs text-[var(--text-secondary)]">Season</label>
                    <input id="edit-season-{item.id}" bind:value={editSeason} type="number" min="1" class={inputClass} />
                  </div>
                {/if}
                <div class="flex flex-col gap-1">
                  <label for="edit-priority-{item.id}" class="text-xs text-[var(--text-secondary)]">Priority</label>
                  <select id="edit-priority-{item.id}" bind:value={editPriority} class={inputClass}>
                    <option value={1}>Low</option>
                    <option value={2}>Normal</option>
                    <option value={3}>High</option>
                  </select>
                </div>
                <div class="flex flex-col gap-1">
                  <label for="edit-res-{item.id}" class="text-xs text-[var(--text-secondary)]">Min Resolution</label>
                  <select id="edit-res-{item.id}" bind:value={editResolution} class={inputClass}>
                    <option value="">Any</option>
                    <option value="720p">720p+</option>
                    <option value="1080p">1080p+</option>
                    <option value="4K">4K</option>
                  </select>
                </div>
                <div class="flex items-center gap-2 self-end pb-2">
                  <input type="checkbox" id="edit-dovi-{item.id}" bind:checked={editPreferDovi} class="rounded" />
                  <label for="edit-dovi-{item.id}" class="text-xs text-[var(--text-secondary)]">Prefer Dolby Vision</label>
                </div>
              </div>
              <div class="flex flex-col gap-1">
                <label for="edit-notes-{item.id}" class="text-xs text-[var(--text-secondary)]">Notes</label>
                <textarea id="edit-notes-{item.id}" bind:value={editNotes} rows="2" class="{inputClass} w-full resize-none"></textarea>
              </div>
              <div class="flex items-center gap-2">
                <button
                  onclick={saveEdit}
                  class="px-4 py-1.5 text-xs rounded-lg bg-[var(--accent)] text-white hover:opacity-90 transition-opacity"
                >
                  Save
                </button>
                <button
                  onclick={() => (editingId = null)}
                  class="px-4 py-1.5 text-xs rounded-lg bg-[var(--bg-tertiary)] border border-[var(--border)] text-[var(--text-primary)] hover:bg-[var(--border)] transition-colors"
                >
                  Cancel
                </button>
              </div>
            </div>
          {/if}
        </div>
      {/each}
    </div>
  {/if}
  </div>

  <!-- Footer count -->
  <div class="text-xs text-[var(--text-secondary)] text-right">
    {filtered.length}{searchResults !== null ? '' : ` of ${items.length}`} items{searching ? ' (searching...)' : ''}
  </div>
</div>

{#if confirmDelete}
  <ConfirmDialog
    title="Remove Item"
    message="Are you sure you want to remove &quot;{confirmDelete.title}&quot; from your watchlist?"
    confirmLabel="Remove"
    variant="danger"
    onconfirm={() => { if (confirmDelete) { removeItem(confirmDelete.id, confirmDelete.title); confirmDelete = null; } }}
    oncancel={() => confirmDelete = null}
  />
{/if}

{#if showTraktDialog}
  <!-- svelte-ignore a11y_no_static_element_interactions -->
  <div class="fixed inset-0 bg-black/50 z-50 flex items-center justify-center" onclick={() => showTraktDialog = false} onkeydown={(e) => { if (e.key === 'Escape') showTraktDialog = false; }}>
    <!-- svelte-ignore a11y_no_static_element_interactions -->
    <div class="bg-[var(--bg-secondary)] rounded-lg p-5 w-80 border border-[var(--border)] shadow-xl" onclick={(e) => e.stopPropagation()} onkeydown={() => {}}>
      <h3 class="text-sm font-semibold mb-3">Import from Trakt</h3>
      <p class="text-xs text-[var(--text-secondary)] mb-3">Enter a Trakt username to import their public watchlist.</p>
      <input
        type="text"
        placeholder="Trakt username"
        bind:value={traktUsername}
        class="w-full px-3 py-2 text-sm rounded-lg bg-[var(--bg-primary)] border border-[var(--border)] text-[var(--text-primary)] placeholder:text-[var(--text-secondary)] focus:outline-none focus:border-[var(--accent)] mb-3"
        onkeydown={(e) => { if (e.key === 'Enter') handleImportTrakt(); }}
      />
      <div class="flex gap-2 justify-end">
        <button
          onclick={() => showTraktDialog = false}
          class="px-3 py-1.5 text-xs rounded-lg bg-[var(--bg-tertiary)] hover:bg-[var(--border)] text-[var(--text-primary)] transition-colors"
        >Cancel</button>
        <button
          onclick={handleImportTrakt}
          disabled={traktImporting || !traktUsername.trim()}
          class="px-3 py-1.5 text-xs rounded-lg bg-[var(--accent)] text-white hover:opacity-90 transition-opacity disabled:opacity-50"
        >{traktImporting ? 'Importing...' : 'Import'}</button>
      </div>
    </div>
  </div>
{/if}
