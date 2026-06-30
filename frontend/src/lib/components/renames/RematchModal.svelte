<script lang="ts">
  import { fly } from 'svelte/transition';
  import ModalOverlay from '$lib/components/ModalOverlay.svelte';
  import { api } from '$lib/api/client';
  import { refreshRenames } from '$lib/stores/renames';
  import { addToast } from '$lib/stores/notifications';
  import type { RenameJob, TmdbSearchResult, RematchPreviewResponse } from '$lib/api/types';

  let { job, onClose }: { job: RenameJob; onClose: () => void } = $props();

  let mediaType = $state<'movie' | 'tv'>(
    job.media_type === 'tv' || job.media_type === 'show' ? 'tv' : 'movie'
  );
  let query = $state(job.title ?? '');
  let results = $state<TmdbSearchResult[]>([]);
  let searchBusy = $state(false);
  let selected = $state<TmdbSearchResult | null>(null);
  let season = $state<number | null>(job.season);
  let episode = $state<number | null>(job.episode);
  let preview = $state<RematchPreviewResponse | null>(null);
  let confirmBusy = $state(false);
  let errorMsg = $state<string | null>(null);

  let debounceTimer: ReturnType<typeof setTimeout> | null = null;

  function parsePastedId(q: string): { tmdb_id?: number } {
    const t = q.trim();
    if (/^\d+$/.test(t)) return { tmdb_id: parseInt(t, 10) };
    // IMDB ids (tt\d+) can't be used directly with rematch — treat as text search
    return {};
  }

  async function runSearch() {
    const q = query.trim();
    if (!q) { results = []; return; }

    // Pasted numeric TMDB id → direct pick, skip the search list.
    const pasted = parsePastedId(q);
    if (pasted.tmdb_id != null) {
      const synth: TmdbSearchResult = {
        tmdb_id: pasted.tmdb_id,
        title: `TMDB ${pasted.tmdb_id}`,
        year: null,
        media_type: mediaType,
        poster_url: null
      };
      selected = synth;
      results = [];
      await loadPreview();
      return;
    }

    searchBusy = true;
    errorMsg = null;
    try {
      const r = await api.searchTmdb(q, mediaType);
      results = r.results;
    } catch (e) {
      errorMsg = `Search failed: ${e instanceof Error ? e.message : String(e)}`;
      results = [];
    } finally {
      searchBusy = false;
    }
  }

  function onQueryInput(v: string) {
    query = v;
    selected = null;
    preview = null;
    if (debounceTimer) clearTimeout(debounceTimer);
    debounceTimer = setTimeout(runSearch, 350);
  }

  function pick(r: TmdbSearchResult) {
    selected = r;
    errorMsg = null;
    loadPreview();
  }

  async function loadPreview() {
    if (!selected) return;
    errorMsg = null;
    try {
      preview = await api.rematchPreview(job.id, {
        tmdb_id: selected.tmdb_id,
        media_type: mediaType,
        season: mediaType === 'tv' ? (season ?? undefined) : undefined,
        episode: mediaType === 'tv' ? (episode ?? undefined) : undefined
      });
    } catch (e) {
      errorMsg = `Preview failed: ${e instanceof Error ? e.message : String(e)}`;
      preview = null;
    }
  }

  function setMediaType(mt: 'movie' | 'tv') {
    if (mt === mediaType) return;
    mediaType = mt;
    selected = null;
    preview = null;
    results = [];
    if (query.trim()) runSearch();
  }

  async function confirm() {
    if (!selected || confirmBusy) return;
    confirmBusy = true;
    errorMsg = null;
    try {
      const r = await api.rematchRename(
        job.id,
        selected.tmdb_id,
        mediaType,
        mediaType === 'tv' ? (season ?? undefined) : undefined,
        mediaType === 'tv' ? (episode ?? undefined) : undefined
      );
      addToast(
        r.status === 'matched' ? 'Rematched' : `Rematched (${r.status})`,
        r.status === 'matched'
          ? `Ready to apply: ${r.new_filename}`
          : r.warning ?? 'Queued as needs_review',
        r.status === 'matched' ? 'success' : 'warning'
      );
      await refreshRenames();
      onClose();
    } catch (e) {
      errorMsg = `Rematch failed: ${e instanceof Error ? e.message : String(e)}`;
    } finally {
      confirmBusy = false;
    }
  }

  let dialogLabel = $derived(
    `Rematch: ${job.title ?? job.original_filename ?? `Job ${job.id}`}`
  );

  // Disable Confirm when library not configured
  let confirmDisabled = $derived(
    !selected || confirmBusy || (preview?.library_configured === false)
  );
</script>

<ModalOverlay onclose={onClose}>
  <!-- svelte-ignore a11y_no_noninteractive_element_interactions -->
  <div
    transition:fly={{ y: -20, duration: 200 }}
    role="dialog"
    aria-modal="true"
    aria-label={dialogLabel}
    tabindex="-1"
    class="bg-[var(--bg-secondary)] border border-[var(--border)] rounded-xl shadow-2xl
           w-full max-w-lg max-h-[85vh] overflow-auto flex flex-col gap-3 p-4"
  >
    <!-- Header -->
    <div class="flex items-center justify-between gap-2">
      <h2 class="font-semibold text-sm truncate" title={dialogLabel}>{dialogLabel}</h2>
      <button
        onclick={onClose}
        aria-label="Close"
        class="shrink-0 text-[var(--text-secondary)] hover:text-[var(--text)] transition-colors text-base leading-none"
      >✕</button>
    </div>

    <!-- Movie / TV toggle -->
    <div class="flex gap-1">
      {#each (['movie', 'tv'] as const) as mt (mt)}
        <button
          onclick={() => setMediaType(mt)}
          class="px-3 py-1 rounded text-[11px] font-medium transition-colors
            {mediaType === mt
              ? 'bg-[var(--accent)] text-white'
              : 'text-[var(--text-secondary)] hover:bg-[var(--bg-tertiary)]'}"
          aria-pressed={mediaType === mt}
        >
          {mt === 'movie' ? 'Movie' : 'TV'}
        </button>
      {/each}
    </div>

    <!-- Search input -->
    <input
      type="search"
      placeholder="Search TMDB, or paste a numeric TMDB id…"
      value={query}
      oninput={(e) => onQueryInput((e.target as HTMLInputElement).value)}
      class="w-full px-2 py-1.5 rounded text-sm
             bg-[var(--bg-tertiary)] border border-[var(--border)]
             focus:border-[var(--accent)] outline-none"
      aria-label="TMDB search"
    />

    <!-- TV season / episode overrides -->
    {#if mediaType === 'tv'}
      <div class="flex gap-3">
        <label class="flex items-center gap-1.5 text-xs text-[var(--text-secondary)]">
          Season
          <input
            type="number"
            min="0"
            value={season ?? ''}
            oninput={(e) => {
              const v = (e.target as HTMLInputElement).value;
              season = v === '' ? null : +v;
              if (selected) loadPreview();
            }}
            class="w-16 px-1.5 py-0.5 rounded bg-[var(--bg-tertiary)] border border-[var(--border)]
                   text-[var(--text)] outline-none focus:border-[var(--accent)]"
            aria-label="Season number"
          />
        </label>
        <label class="flex items-center gap-1.5 text-xs text-[var(--text-secondary)]">
          Episode
          <input
            type="number"
            min="0"
            value={episode ?? ''}
            oninput={(e) => {
              const v = (e.target as HTMLInputElement).value;
              episode = v === '' ? null : +v;
              if (selected) loadPreview();
            }}
            class="w-16 px-1.5 py-0.5 rounded bg-[var(--bg-tertiary)] border border-[var(--border)]
                   text-[var(--text)] outline-none focus:border-[var(--accent)]"
            aria-label="Episode number"
          />
        </label>
      </div>
    {/if}

    <!-- Search state -->
    {#if searchBusy}
      <div class="text-xs text-[var(--text-secondary)]">Searching…</div>
    {:else if results.length > 0}
      <ul class="flex flex-col gap-1 max-h-52 overflow-auto" role="listbox" aria-label="Search results">
        {#each results as r (r.tmdb_id)}
          <li role="option" aria-selected={selected?.tmdb_id === r.tmdb_id}>
            <button
              onclick={() => pick(r)}
              class="w-full flex items-center gap-2 p-1.5 rounded text-left transition-colors
                {selected?.tmdb_id === r.tmdb_id
                  ? 'bg-[var(--accent)]/15 ring-1 ring-[var(--accent)]/40'
                  : 'hover:bg-[var(--bg-tertiary)]'}"
            >
              <div class="w-8 shrink-0 aspect-[2/3] bg-[var(--bg-tertiary)] rounded overflow-hidden">
                {#if r.poster_url}
                  <img src={r.poster_url} alt="" class="w-full h-full object-cover" loading="lazy" />
                {/if}
              </div>
              <span class="text-xs truncate flex-1">
                {r.title}{r.year ? ` (${r.year})` : ''}
              </span>
              <span class="shrink-0 text-[10px] uppercase text-[var(--text-secondary)]">
                {r.media_type}
              </span>
            </button>
          </li>
        {/each}
      </ul>
    {/if}

    <!-- Error message -->
    {#if errorMsg}
      <div class="text-xs p-2 rounded bg-[var(--error)]/15 text-[var(--error)]">
        {errorMsg}
      </div>
    {/if}

    <!-- Preview -->
    {#if preview}
      {#if preview.library_configured === false}
        <div class="text-xs p-2 rounded bg-[var(--warning)]/15 text-[var(--warning)]" role="alert">
          Warning: Library not configured — {preview.warning ?? 'this will be queued as needs_review, not placed.'} Confirm is disabled until a library is set up.
        </div>
      {/if}
      <div class="text-xs p-2 rounded bg-[var(--bg-tertiary)] flex flex-col gap-1">
        <div class="flex gap-1 min-w-0">
          <span class="shrink-0 text-[var(--text-secondary)]">New name:</span>
          <span class="truncate" title={preview.new_filename}>{preview.new_filename}</span>
        </div>
        <div class="flex gap-1 min-w-0">
          <span class="shrink-0 text-[var(--text-secondary)]">Destination:</span>
          <span class="truncate" title={preview.destination_path ?? ''}>{preview.destination_path ?? '—'}</span>
        </div>
      </div>
    {/if}

    <!-- Actions -->
    <div class="flex justify-end gap-2 pt-1">
      <button
        onclick={onClose}
        class="px-3 py-1.5 rounded text-xs font-medium
               text-[var(--text-secondary)] hover:bg-[var(--bg-tertiary)] transition-colors"
      >
        Cancel
      </button>
      <button
        onclick={confirm}
        disabled={confirmDisabled}
        aria-busy={confirmBusy}
        class="px-3 py-1.5 rounded text-xs font-medium bg-[var(--accent)] text-white
               disabled:opacity-50 hover:brightness-110 transition-all"
        title={preview?.library_configured === false ? 'Library not configured — set up a destination library first' : undefined}
      >
        {confirmBusy ? 'Confirming…' : 'Confirm'}
      </button>
    </div>
  </div>
</ModalOverlay>
