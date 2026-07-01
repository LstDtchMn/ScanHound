import { writable, derived, get } from 'svelte/store';
import { api } from '$lib/api/client';
import { connection } from './connection';
import type { ScanResult, ScanStats } from '$lib/api/types';

export type StatusFilter = 'all' | 'missing' | 'upgrade' | 'library';
export type ViewMode = 'grid' | 'list' | 'swipe';
export type SortOption =
  | 'title-asc'
  | 'title-desc'
  | 'year-desc'
  | 'year-asc'
  | 'size-desc'
  | 'size-asc'
  | 'rating-desc'
  | 'rating-asc'
  | 'posted-desc'
  | 'posted-asc';

/** A writable store backed by localStorage (SSR-safe), so the user's view
 *  mode and sort choice persist across reloads. */
export function persisted<T>(key: string, fallback: T) {
  let initial = fallback;
  try {
    const raw = typeof localStorage !== 'undefined' ? localStorage.getItem(key) : null;
    if (raw != null) initial = JSON.parse(raw) as T;
  } catch { /* ignore */ }
  const store = writable<T>(initial);
  store.subscribe((v) => {
    try { if (typeof localStorage !== 'undefined') localStorage.setItem(key, JSON.stringify(v)); } catch { /* ignore */ }
  });
  return store;
}

export const results = writable<ScanResult[]>([]);
export const statusFilter = writable<StatusFilter>('all');
export const searchFilter = writable<string>('');
/** Selected genres/languages to show; empty array means "All" (no filter). */
export const genreFilter = writable<string[]>([]);
export const languageFilter = writable<string[]>([]);
export function toggleGenreFilter(genre: string) {
  genreFilter.update((g) => (g.includes(genre) ? g.filter((x) => x !== genre) : [...g, genre]));
}
export function toggleLanguageFilter(lang: string) {
  languageFilter.update((l) => (l.includes(lang) ? l.filter((x) => x !== lang) : [...l, lang]));
}
export const viewMode = persisted<ViewMode>('sh-view-mode', 'grid');
/** Whether the user has explicitly picked a view (vs. the platform default).
 *  Lets phones default to the swipe deck without overriding a deliberate choice. */
export const viewModeExplicit = persisted<boolean>('sh-view-mode-explicit', false);
export function setViewMode(m: ViewMode) {
  viewMode.set(m);
  viewModeExplicit.set(true);
}
export const stats = writable<ScanStats>({
  total: 0,
  missing: 0,
  upgrade: 0,
  library: 0
});
export const sortBy = persisted<SortOption>('sh-sort-by', 'posted-desc');

export type Density = 'comfortable' | 'compact';
export const density = persisted<Density>('sh-density', 'comfortable');

// ── Grid/tile view display options (device-local, instant) ───────────
export type TileSize = 'sm' | 'md' | 'lg';
/** Card min-width for the responsive auto-fill grid (ignored when a fixed
 *  column count is set in Settings). */
export const tileSize = persisted<TileSize>('sh-tile-size', 'md');
/** Min-width in px per tile size — fed into grid-template-columns minmax(). */
export const TILE_MIN_PX: Record<TileSize, number> = { sm: 120, md: 160, lg: 220 };

export type PosterAspect = '2/3' | '16/9' | '1/1';
export const posterAspect = persisted<PosterAspect>('sh-poster-aspect', '2/3');
/** Tailwind aspect class for each poster aspect. */
export const POSTER_ASPECT_CLASS: Record<PosterAspect, string> = {
  '2/3': 'aspect-[2/3]', '16/9': 'aspect-video', '1/1': 'aspect-square'
};

/** Show the title/meta block beneath the poster, or render poster-only cards. */
export const tileShowMeta = persisted<boolean>('sh-tile-show-meta', true);

export type GridGap = 'tight' | 'normal' | 'roomy';
export const gridGap = persisted<GridGap>('sh-grid-gap', 'normal');
export const GRID_GAP_CLASS: Record<GridGap, string> = {
  tight: 'gap-2', normal: 'gap-4', roomy: 'gap-6'
};

/** Column count for the grid. 'auto' = responsive auto-fill sized by tile size;
 *  a number = that many fixed equal columns. Device-local + instant; takes
 *  precedence over the (server-wide) tile_columns setting. */
export type GridColumns = 'auto' | number;
export const gridColumns = persisted<GridColumns>('sh-grid-columns', 'auto');
export const GRID_COLUMN_CHOICES: GridColumns[] = ['auto', 2, 3, 4, 5, 6, 8];


/** Active quick-filter chips: any of '4k' | 'hdrdv' | 'inplex'. */
export const quickFilters = persisted<string[]>('sh-quick-filters', []);
export function toggleQuickFilter(key: string) {
  quickFilters.update((q) => (q.includes(key) ? q.filter((k) => k !== key) : [...q, key]));
}

/** Source categories ('4k' | 'remux' | 'tv') currently shown in the list — driven
 *  by the ScanControls 4K/Remux/TV toggles so they filter the (pre-cached) results
 *  instantly. Items with an unknown/empty category always show. */
export const categoryFilter = persisted<string[]>('sh-category-filter', ['4k']);

export const selectedKeys = writable<Set<string>>(new Set());

/** Release URLs the user swiped away ("skip"); persisted server-side so the
 *  deck only surfaces fresh items across scans. Hydrated on app load. */
export const dismissedUrls = writable<Set<string>>(new Set());

/** Whether the shown results came from the background pre-cache (no live scan
 *  this session). Drives a subtle "showing cached results" banner; cleared the
 *  moment a live scan produces results. */
export const fromCache = writable<boolean>(false);
export const cacheUpdatedAt = writable<string | null>(null);
let fromCacheActive = false;

// ── Server-side pagination (paged mode) ───────────────────────────────
/** When true, `results` is loaded page-by-page from the server (which has
 *  already applied filters/sort/dismissal) and `filteredResults` becomes a
 *  passthrough. When false, the legacy client-side filter+sort pipeline runs
 *  over the full (pre-fetched) result set. */
export const pagedMode = writable<boolean>(true);
export const hasMore = writable<boolean>(false);
export const loadingMore = writable<boolean>(false);
export const loadError = writable<boolean>(false);
/** Total matching items on the server for the current filter set (paged mode). */
export const filteredTotal = writable<number>(0);
/** Per-title counts over the filtered server set (paged mode; for dup-badges etc). */
export const titleCounts = writable<Record<string, number>>({});

let activeScanResultCount = 0;

/** Parse a human-readable size string like "4.5 GB" into bytes for comparison */
function parseSizeToBytes(size: string): number {
  if (!size) return 0;
  const match = size.match(/([\d.]+)\s*(TB|GB|MB|KB|B)/i);
  if (!match) return 0;
  const val = parseFloat(match[1]);
  const unit = match[2].toUpperCase();
  const multipliers: Record<string, number> = {
    B: 1,
    KB: 1024,
    MB: 1024 ** 2,
    GB: 1024 ** 3,
    TB: 1024 ** 4
  };
  return val * (multipliers[unit] || 0);
}

/** Parse a posted_date string like "June 8, 2026 at 12:56 AM" into a timestamp. */
function parsePostedDate(s: string | null | undefined): number {
  if (!s) return 0;
  const t = Date.parse(s.replace(' at ', ' '));
  return Number.isNaN(t) ? 0 : t;
}

connection.on('scan:result', (data) => {
  const item = data as unknown as ScanResult;
  // A live scan supersedes any pre-cached results: clear the cache rows on the
  // first streamed item so live and cached never mix.
  if (fromCacheActive) {
    results.set([]);
    fromCacheActive = false;
    fromCache.set(false);
  }
  activeScanResultCount += 1;
  results.update((items) => [...items, item]);
  // Incrementally update stats as items stream in
  stats.update((s) => {
    const next = { ...s, total: s.total + 1 };
    const status = item.status?.toLowerCase() || '';
    if (status.includes('missing')) next.missing += 1;
    else if (status.includes('upgrade')) next.upgrade += 1;
    else if (status.includes('library') || status.includes('in_library')) next.library += 1;
    return next;
  });
});

connection.on('scan:complete', (data) => {
  const s = data.stats as ScanStats;
  if (s) stats.set(s);
  // A completed live scan always supersedes the cache banner.
  fromCacheActive = false;
  fromCache.set(false);

  // If a completed scan produced no streamed items, ensure stale results
  // from an earlier run are cleared out of the UI.
  if (!s || s.total === 0 || activeScanResultCount === 0) {
    results.set([]);
    selectedKeys.set(new Set());
    selectedDetail.set(null);
    focusedIndex.set(-1);
  }

  activeScanResultCount = 0;
});

/** All unique genres from current scan results. */
export const availableGenres = derived(results, ($results) => {
  const set = new Set<string>();
  for (const r of $results) {
    for (const g of r.genres || []) set.add(g);
  }
  return [...set].sort();
});

/** All unique languages from current scan results. */
export const availableLanguages = derived(results, ($results) => {
  const set = new Set<string>();
  for (const r of $results) {
    if (r.language) set.add(r.language);
  }
  return [...set].sort();
});

/** True if the result has at least one matching copy already in Plex. */
function hasPlexCopy(i: ScanResult): boolean {
  try { return JSON.parse(i.plex_versions || '[]').length > 0; } catch { return false; }
}

const SORT_PARAM: Record<SortOption, { sort: string; order: string }> = {
  'title-asc': { sort: 'title', order: 'asc' },
  'title-desc': { sort: 'title', order: 'desc' },
  'year-desc': { sort: 'year', order: 'desc' },
  'year-asc': { sort: 'year', order: 'asc' },
  'size-desc': { sort: 'size', order: 'desc' },
  'size-asc': { sort: 'size', order: 'asc' },
  'rating-desc': { sort: 'rating', order: 'desc' },
  'rating-asc': { sort: 'rating', order: 'asc' },
  'posted-desc': { sort: 'posted_date', order: 'desc' },
  'posted-asc': { sort: 'posted_date', order: 'asc' }
};

let currentPage = 0;
let currentQueryKey = '';

/** Snapshot of every filter/sort input that changes the server query, so a
 *  page load can detect it's been superseded by a filter change mid-flight. */
function filterQueryKey(): string {
  return JSON.stringify([
    get(statusFilter), get(searchFilter), get(genreFilter), get(languageFilter),
    get(quickFilters), get(categoryFilter), get(sortBy)
  ]);
}

function buildResultParams(page: number): Record<string, string> {
  const p: Record<string, string> = { page: String(page), per_page: '100' };
  const s = get(statusFilter); if (s !== 'all') p.filter = s;
  const q = get(searchFilter); if (q) p.search = q;
  const cats = get(categoryFilter); if (cats.length) p.category = cats.join(',');
  const g = get(genreFilter); if (g.length) p.genre = g.join(',');
  const l = get(languageFilter); if (l.length) p.language = l.join(',');
  const qf = get(quickFilters); if (qf.length) p.quick = qf.join(',');
  const so = SORT_PARAM[get(sortBy)]; p.sort = so.sort; p.order = so.order;
  return p;
}

/** Load a page of server-filtered/sorted results (paged mode). `reset` starts
 *  over from page 1 and replaces `results`; otherwise the next page is
 *  fetched and appended. No-ops outside paged mode or while already loading.
 *  Discards the response if the filters changed while the request was in
 *  flight (a fresh load for the new filters will already be underway). */
export async function loadResults(reset: boolean): Promise<void> {
  if (!get(pagedMode)) return;
  if (get(loadingMore)) return;
  const key = filterQueryKey();
  if (!reset && key !== currentQueryKey) return; // stale append
  const page = reset ? 1 : currentPage + 1;
  loadingMore.set(true);
  loadError.set(false);
  try {
    const data = await api.getCachedResults(buildResultParams(page));
    if (filterQueryKey() !== key) return; // superseded while awaiting — discard
    const items = (data.items ?? []) as ScanResult[];
    if (reset) { results.set(items); currentPage = 1; currentQueryKey = key; }
    else { results.update((r) => [...r, ...items]); currentPage = page; }
    filteredTotal.set(data.total ?? items.length);
    titleCounts.set((data as { title_counts?: Record<string, number> }).title_counts ?? {});
    if (data.stats) stats.set(data.stats);
    hasMore.set(get(results).length < (data.total ?? 0));
    if ((data as { source?: string }).source === 'cache') {
      cacheUpdatedAt.set((data as { last_updated?: string }).last_updated ?? null);
      fromCache.set(true);
    }
  } catch {
    loadError.set(true);
  } finally {
    loadingMore.set(false);
  }
}

export const filteredResults = derived(
  [results, statusFilter, searchFilter, genreFilter, languageFilter, sortBy, quickFilters, dismissedUrls, categoryFilter, pagedMode],
  ([$results, $filter, $search, $genre, $language, $sort, $quick, $dismissed, $category, $paged]) => {
    if ($paged) return $results; // server already filtered + sorted
    let items = $results;
    // Hide swiped-away ("skip") items everywhere they'd otherwise appear.
    if ($dismissed.size > 0) {
      items = items.filter((i) => !i.url || !$dismissed.has(i.url));
    }
    // Source-category toggles (4K/Remux/TV). The backend tags each item with its
    // crawl category; for items predating that (legacy cache, site search) we
    // infer one — TV packs carry a season, everything else is treated as 4K
    // (the old cache only held 4K movies). An item shows only when its category
    // is enabled.
    {
      const known = new Set(['4k', 'remux', 'tv']);
      const enabled = new Set($category);
      const effCategory = (i: ScanResult) => i.category || (i.season != null ? 'tv' : '4k');
      // Known categories obey the toggles; anything else (e.g. explicit Site
      // Search results tagged 'search') always shows.
      items = items.filter((i) => {
        const c = effCategory(i);
        return !known.has(c) || enabled.has(c);
      });
    }
    if ($filter !== 'all') {
      items = items.filter(
        (i) => i.status === $filter || (i.status && i.status.includes($filter))
      );
    }
    if ($search) {
      const q = $search.toLowerCase();
      items = items.filter((i) => i.title.toLowerCase().includes(q));
    }
    if ($genre.length > 0) {
      items = items.filter((i) => i.genres?.some((g) => $genre.includes(g)));
    }
    if ($language.length > 0) {
      items = items.filter((i) => $language.includes(i.language));
    }
    // Quick-filter chips (AND-combined with the above)
    if ($quick.includes('4k')) items = items.filter((i) => i.resolution === '4K');
    if ($quick.includes('hdrdv')) items = items.filter((i) => i.dovi || (!!i.hdr && i.hdr !== 'SDR'));
    if ($quick.includes('inplex')) items = items.filter(hasPlexCopy);
    // Sort
    items = [...items].sort((a, b) => {
      switch ($sort) {
        case 'title-asc':
          return a.title.localeCompare(b.title);
        case 'title-desc':
          return b.title.localeCompare(a.title);
        case 'year-desc':
          return (b.year ?? 0) - (a.year ?? 0);
        case 'year-asc':
          return (a.year ?? 0) - (b.year ?? 0);
        case 'size-desc':
          return parseSizeToBytes(b.size) - parseSizeToBytes(a.size);
        case 'size-asc':
          return parseSizeToBytes(a.size) - parseSizeToBytes(b.size);
        case 'rating-desc':
          return (b.rating ?? 0) - (a.rating ?? 0);
        case 'rating-asc':
          return (a.rating ?? 0) - (b.rating ?? 0);
        case 'posted-desc':
          return parsePostedDate(b.posted_date) - parsePostedDate(a.posted_date);
        case 'posted-asc':
          return parsePostedDate(a.posted_date) - parsePostedDate(b.posted_date);
        default:
          return 0;
      }
    });
    return items;
  }
);

/** True for items the swipe deck should present — actionable (missing/upgrade)
 *  releases that have a source URL and aren't already selected. Selected items
 *  drop out of the deck so a right-swipe doesn't resurface the same card. */
function isActionable(status: string | null | undefined): boolean {
  const s = (status || '').toLowerCase();
  return s.includes('missing') || s.includes('upgrade');
}

export const deckResults = derived(
  [filteredResults, selectedKeys],
  ([$filtered, $selected]) =>
    $filtered.filter((i) => !!i.url && isActionable(i.status) && !$selected.has(i.url))
);

/** Seed the store from pre-cached background-scan results when there are no
 *  live results yet (fresh session / server restart), so the app opens with
 *  something to show. Sets fromCache so the UI can flag it. */
export async function hydrateCache() {
  try {
    const data = await api.getCachedResults({ per_page: '500' });
    if (data.items && data.items.length > 0) {
      results.set(data.items as ScanResult[]);
      if (data.stats) stats.set(data.stats);
      cacheUpdatedAt.set(data.last_updated ?? null);
      fromCache.set(true);
      fromCacheActive = true;
    }
  } catch {
    /* no cache / offline — leave empty */
  }
}

/** Load the persisted dismissal set from the server (call once on app start). */
export async function hydrateDismissed() {
  try {
    const { items } = await api.dismissedList();
    dismissedUrls.set(new Set(items.map((d) => d.url)));
  } catch {
    /* offline / no server — leave empty */
  }
}

/** Swipe-left: dismiss an item (optimistic), persisting it server-side.
 *  Resolves false if the server call failed and the optimistic update was
 *  reverted — callers can use this to drop a now-stale undo entry. */
export function dismissItem(url: string, title?: string): Promise<boolean> {
  if (!url) return Promise.resolve(false);
  dismissedUrls.update((s) => {
    const next = new Set(s);
    next.add(url);
    return next;
  });
  return api.dismissItems([url], title ? { [url]: title } : undefined, true).then(
    () => true,
    () => {
      // Revert on failure so the UI reflects the server's truth.
      dismissedUrls.update((s) => {
        const next = new Set(s);
        next.delete(url);
        return next;
      });
      return false;
    }
  );
}

/** Undo a dismissal so the item can reappear. */
export function restoreItem(url: string): Promise<boolean> {
  if (!url) return Promise.resolve(false);
  dismissedUrls.update((s) => {
    const next = new Set(s);
    next.delete(url);
    return next;
  });
  return api.dismissItems([url], undefined, false).then(
    () => true,
    () => {
      dismissedUrls.update((s) => {
        const next = new Set(s);
        next.add(url);
        return next;
      });
      return false;
    }
  );
}

export function clearResults() {
  results.set([]);
  stats.set({ total: 0, missing: 0, upgrade: 0, library: 0 });
  selectedKeys.set(new Set());
  selectedDetail.set(null);
  focusedIndex.set(-1);
  activeScanResultCount = 0;
  fromCacheActive = false;
  fromCache.set(false);
}

/** Mark result rows (by url) as Downloaded and adjust the status counters. */
export function markDownloaded(urls: Array<string | undefined | null>) {
  const urlSet = new Set(urls.filter((u): u is string => !!u));
  if (urlSet.size === 0) return;
  let missingDelta = 0;
  let upgradeDelta = 0;
  let libraryDelta = 0;
  results.update((items) =>
    items.map((it) => {
      if (it.url && urlSet.has(it.url) && it.status !== 'downloaded') {
        const s = (it.status || '').toLowerCase();
        if (s.includes('missing')) missingDelta--;
        else if (s.includes('upgrade')) upgradeDelta--;
        else if (s.includes('library')) libraryDelta--;
        return { ...it, status: 'downloaded' };
      }
      return it;
    })
  );
  if (missingDelta || upgradeDelta || libraryDelta) {
    stats.update((st) => ({
      ...st,
      missing: Math.max(0, st.missing + missingDelta),
      upgrade: Math.max(0, st.upgrade + upgradeDelta),
      library: Math.max(0, st.library + libraryDelta)
    }));
  }
}

/** After a grab, optimistically tag the OTHER releases in the same group with a
 *  "grabbed similar" note showing the grabbed specs — so siblings update the
 *  instant the grab lands, before the backend cache re-match persists it. The
 *  exact release stays Downloaded (markDownloaded handles that). */
export function markGrabbedSiblings(grabbedUrl: string | undefined | null) {
  if (!grabbedUrl) return;
  results.update((items) => {
    const grabbed = items.find((i) => i.url === grabbedUrl);
    if (!grabbed || !grabbed.group_key) return items;
    const note = {
      resolution: grabbed.resolution || '',
      size: grabbed.size || '',
      downloaded_at: new Date().toISOString(),
      hdr: grabbed.hdr || '',
      dovi: grabbed.dovi ?? false
    };
    return items.map((it) =>
      it.group_key === grabbed.group_key &&
      it.url !== grabbedUrl &&
      (it.status || '').toLowerCase().includes('missing')
        ? { ...it, prior_grab: note }
        : it
    );
  });
}

export function toggleSelect(groupKey: string) {
  selectedKeys.update((s) => {
    const next = new Set(s);
    if (next.has(groupKey)) next.delete(groupKey);
    else next.add(groupKey);
    return next;
  });
}

export async function selectAll(filteredKeys?: string[]) {
  const applySelection = () => {
    if (filteredKeys) {
      selectedKeys.update((s) => {
        const next = new Set(s);
        for (const k of filteredKeys) next.add(k);
        return next;
      });
    } else {
      results.update((items) => {
        selectedKeys.set(new Set(items.map((i) => i.url)));
        return items;
      });
    }
  };
  try {
    await api.selectAll();
  } catch {
    // API call failed — select locally anyway
  }
  applySelection();
}

export async function deselectAll() {
  try {
    await api.deselectAll();
  } catch {
    // API call failed — deselect locally anyway
  }
  selectedKeys.set(new Set());
}

export const selectedDetail = writable<ScanResult | null>(null);
export const focusedIndex = writable<number>(-1);
