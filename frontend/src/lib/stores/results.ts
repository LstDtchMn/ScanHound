import { writable, derived, get } from 'svelte/store';
import { api } from '$lib/api/client';
import { connection } from './connection';
import { resolutionRank, sizeToGB } from '$lib/constants';
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
/** Resolution/type facet: any of '4K' | '1080p' | 'TV'. OR-combined (like
 *  genres) — an item shows if it matches ANY selected key, so selecting both
 *  4K and 1080p shows the union rather than the (empty) intersection. Persisted
 *  per-device. 'TV' keys off the effective crawl category, not resolution. */
export const RESOLUTION_KEYS = ['4K', '1080p', 'TV'] as const;
export const resolutionFilter = persisted<string[]>('sh-resolution-filter', []);
export function toggleResolutionFilter(key: string) {
  resolutionFilter.update((r) => (r.includes(key) ? r.filter((x) => x !== key) : [...r, key]));
}
/** The filter keys an item satisfies. A TV show keys ONLY as 'TV' (never by
 *  resolution) so the 4K/1080p filters are movies-only; a movie keys by its
 *  resolution. Matches backend _resolution_keys. */
export function resolutionKeysFor(i: ScanResult): string[] {
  if (i.category === 'tv' || i.season != null) return ['TV'];
  return i.resolution ? [i.resolution] : [];
}
/** Date-range filter bounds, "YYYY-MM-DD" strings; '' means off. Session-only
 *  (not persisted), same as genre/language filters. Inclusive on both ends —
 *  postedBefore covers through the END of that day. */
export const postedAfter = writable<string>('');
export const postedBefore = writable<string>('');
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

/** Phone poster-wall column count: 1 = single large poster per row, 2 = the
 *  multi-poster wall. Device-local + persisted; independent of the desktop
 *  gridColumns above (a phone screen only sensibly holds 1 or 2 posters). */
export const phoneColumns = persisted<1 | 2>('sh-phone-columns', 1);

/** True when the phone's top chrome (the layout's "ScanHound" title bar, the
 *  scan-controls bar, and the FilterBar status-chip row) should auto-hide, in
 *  sync, as one unit — set by MobileScanView from scroll direction (collapse
 *  on scroll-down into the wall, reveal on scroll-up). Session-only (not
 *  persisted) — a transient scroll affordance. Each consumer additionally
 *  ignores this while a scan is running, so progress/status stay visible and
 *  the three regions never end up half-collapsed relative to each other. */
export const mobileChromeCollapsed = writable<boolean>(false);


/** Active quick-filter chips: any of '4k' | 'hdrdv' | 'inplex'. */
export const quickFilters = persisted<string[]>('sh-quick-filters', []);
export function toggleQuickFilter(key: string) {
  quickFilters.update((q) => (q.includes(key) ? q.filter((k) => k !== key) : [...q, key]));
}

/** Source categories ('4k' | 'remux' | 'tv') currently shown in the list — driven
 *  by the ScanControls 4K/Remux/TV toggles so they filter the (pre-cached) results
 *  instantly. Items with an unknown/empty category always show. */
export const categoryFilter = persisted<string[]>('sh-category-filter', ['4k', 'remux', 'tv']);

export const selectedKeys = writable<Set<string>>(new Set());

/** Release URLs the user swiped away ("skip"); persisted server-side so the
 *  deck only surfaces fresh items across scans. Hydrated on app load. */
export const dismissedUrls = writable<Set<string>>(new Set());

/** Whether the shown results came from the background pre-cache (no live scan
 *  this session). Drives a subtle "showing cached results" banner; cleared the
 *  moment a live scan produces results. */
export const fromCache = writable<boolean>(false);
export const cacheUpdatedAt = writable<string | null>(null);

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

/** True if an item's posted_date falls within [after, before] (both
 *  inclusive; before covers through the end of that day). Mirrors the
 *  backend's _filter_and_sort semantics: when either bound is set, items
 *  with a missing/unparseable posted_date (parses to 0) are excluded. */
function inPostedRange(postedDate: string | null | undefined, after: string, before: string): boolean {
  if (!after && !before) return true;
  const ts = parsePostedDate(postedDate);
  if (ts === 0) return false;
  if (after) {
    const afterTs = Date.parse(after + 'T00:00:00');
    if (!Number.isNaN(afterTs) && ts < afterTs) return false;
  }
  if (before) {
    const beforeDayStart = Date.parse(before + 'T00:00:00');
    if (!Number.isNaN(beforeDayStart) && ts >= beforeDayStart + 24 * 60 * 60 * 1000) return false;
  }
  return true;
}

/** Handler for the `scan:result` WS event — exported (rather than kept as an
 *  inline connection.on callback) so tests can invoke it directly without a
 *  real WebSocket. A live stream always supersedes paged/cache-loaded rows:
 *  if we're still in paged mode when the first item streams in (whether the
 *  scan was started via the local Start button, which also flips pagedMode
 *  in clearResults(), or a scheduled scan streaming into an already-open
 *  session), clear the cached rows and flip to live mode so the debounced
 *  filter refetch can never fight the incoming stream. Subsequent items just
 *  append. */
export function handleScanResult(data: Record<string, unknown>) {
  const item = data as unknown as ScanResult;
  if (get(pagedMode)) {
    results.set([]);
    pagedMode.set(false);
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
}

/** Handler for the `scan:complete` WS event — exported for direct testing;
 *  see handleScanResult. */
export function handleScanComplete(data: Record<string, unknown>) {
  const s = data.stats as ScanStats;
  if (s) stats.set(s);
  // A completed live scan always supersedes the cache banner.
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
}

connection.on('scan:result', handleScanResult);
connection.on('scan:complete', handleScanComplete);

/** Max rows kept in `results` while in paged/browse mode (server-backed —
 *  infinite scroll re-fetches whatever falls off, so this just bounds memory).
 *  Live-scan streaming (handleScanResult while pagedMode is false) is never
 *  capped: every streamed result must survive so a completed scan's full
 *  stats/rows stay intact. */
export const PAGED_RESULTS_CAP = 2000;
/** Page size for paged/browse-mode `/results/cached` fetches. `hasMore` is
 *  derived from page*PAGED_PER_PAGE vs the server total (NOT the in-memory
 *  `results.length`, which the cap above can pin below the total and would
 *  otherwise leave `hasMore` stuck true → infinite fetch loop). */
export const PAGED_PER_PAGE = 100;

/** Re-fetch a snapshot of results after the WebSocket reconnects, so any
 *  scan:result/scan:complete events that streamed in while disconnected
 *  aren't lost forever. In paged mode this just reloads page 1 of the
 *  server-filtered set; otherwise (a live/browse snapshot loaded via
 *  api.getResults on app start) it re-pulls that same snapshot. Never runs
 *  mid-live-scan-stream — pagedMode is false during a stream, but so is the
 *  "no scan running, showing a prior snapshot" case, so we distinguish by
 *  whether a scan is actively producing rows (activeScanResultCount > 0).
 *  Exported for direct testing. */
export async function handleReconnectSnapshot(): Promise<void> {
  if (get(pagedMode)) {
    await loadResults(true);
    return;
  }
  if (activeScanResultCount > 0) return; // mid-stream — don't clobber it
  try {
    const data = await api.getResults({ per_page: '500' });
    if (data.items && data.items.length > 0) {
      results.set(data.items as ScanResult[]);
      if (data.stats) stats.set(data.stats);
    }
  } catch {
    /* offline — leave whatever is currently shown */
  }
}

connection.onReconnect(() => {
  handleReconnectSnapshot();
});

/** Server-computed facets (D3) — populated from `/results/cached`'s
 *  `available_genres`/`available_languages`, which are computed over the
 *  *entire* filtered-set basis server-side (see backend `_compute_facets`),
 *  not just whatever page(s) happen to be loaded client-side. Only
 *  meaningful in paged mode; left empty otherwise. */
export const serverGenres = writable<string[]>([]);
export const serverLanguages = writable<string[]>([]);

/** Available genre options for the filter UI. In paged mode, the server
 *  already computed these over the whole matching set (not just loaded
 *  pages) — see B2/D3 — so use that; otherwise (live mode) derive from the
 *  in-memory results, same as before. */
export const availableGenres = derived(
  [results, pagedMode, serverGenres],
  ([$results, $paged, $serverGenres]) => {
    if ($paged) return $serverGenres;
    const set = new Set<string>();
    for (const r of $results) {
      for (const g of r.genres || []) set.add(g);
    }
    return [...set].sort();
  }
);

/** Available language options for the filter UI — see availableGenres. */
export const availableLanguages = derived(
  [results, pagedMode, serverLanguages],
  ([$results, $paged, $serverLanguages]) => {
    if ($paged) return $serverLanguages;
    const set = new Set<string>();
    for (const r of $results) {
      if (r.language) set.add(r.language);
    }
    return [...set].sort();
  }
);

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
/** Bumped on every load call; an in-flight request captures its own value and
 *  checks it against the live one after awaiting the response, so a reset
 *  load that starts *after* an append (but resolves first, or even after) is
 *  never mistaken for the "current" request by that older append. */
let loadGeneration = 0;

/** Snapshot of every filter/sort input that changes the server query, so a
 *  page load can detect it's been superseded by a filter change mid-flight. */
function filterQueryKey(): string {
  return JSON.stringify([
    get(statusFilter), get(searchFilter), get(genreFilter), get(languageFilter),
    get(quickFilters), get(categoryFilter), get(resolutionFilter), get(sortBy), get(postedAfter), get(postedBefore)
  ]);
}

function buildResultParams(page: number): Record<string, string> {
  const p: Record<string, string> = { page: String(page), per_page: String(PAGED_PER_PAGE) };
  const s = get(statusFilter); if (s !== 'all') p.filter = s;
  const q = get(searchFilter); if (q) p.search = q;
  const cats = get(categoryFilter); if (cats.length) p.category = cats.join(',');
  const g = get(genreFilter); if (g.length) p.genre = g.join(',');
  const l = get(languageFilter); if (l.length) p.language = l.join(',');
  const qf = get(quickFilters); if (qf.length) p.quick = qf.join(',');
  const rf = get(resolutionFilter); if (rf.length) p.resolution = rf.join(',');
  const pa = get(postedAfter); if (pa) p.posted_after = pa;
  const pb = get(postedBefore); if (pb) p.posted_before = pb;
  const so = SORT_PARAM[get(sortBy)]; p.sort = so.sort; p.order = so.order;
  return p;
}

/** Load a page of server-filtered/sorted results (paged mode). `reset` starts
 *  over from page 1 and replaces `results`; otherwise the next page is
 *  fetched and appended. No-ops outside paged mode.
 *
 *  A `reset` load always proceeds even if an append is currently in flight —
 *  a filter/sort change must never be silently swallowed just because the
 *  user happened to be mid-infinite-scroll. An in-flight *append*, on the
 *  other hand, still bails if another load (of either kind) is already
 *  running, and — like before — discards its own response if superseded
 *  while awaiting (now detected via a generation counter rather than
 *  `loadingMore`, since `loadingMore` may already reflect a newer request). */
export async function loadResults(reset: boolean): Promise<void> {
  if (!get(pagedMode)) return;
  if (!reset) {
    if (get(loadingMore)) return; // an append never preempts anything in flight
    const key = filterQueryKey();
    if (key !== currentQueryKey) return; // stale append — filters already moved on
  }
  const key = filterQueryKey();
  const generation = ++loadGeneration;
  const page = reset ? 1 : currentPage + 1;
  loadingMore.set(true);
  loadError.set(false);
  try {
    const data = await api.getCachedResults(buildResultParams(page));
    if (generation !== loadGeneration) return; // superseded by a later load — discard
    const items = (data.items ?? []) as ScanResult[];
    if (reset) { results.set(items); currentPage = 1; currentQueryKey = key; }
    else {
      // Cap accumulated rows so an extended infinite-scroll session doesn't
      // grow `results` unbounded. Evict from the front (oldest-loaded pages)
      // since those are what a user actively scrolling forward cares least
      // about — the server can always re-supply them on a fresh page-1 load.
      // NEVER evict a currently-selected row: that would desync `selectedKeys`
      // (url-keyed) and silently shrink a later "Select loaded"/bulk action's
      // target. Length may thus slightly exceed the cap when many rows are
      // selected — bounded by the selection size, which is acceptable.
      results.update((r) => {
        const next = [...r, ...items];
        let toDrop = next.length - PAGED_RESULTS_CAP;
        if (toDrop <= 0) return next;
        const sel = get(selectedKeys);
        const kept: ScanResult[] = [];
        for (const item of next) {
          if (toDrop > 0 && !sel.has(item.url)) { toDrop--; continue; }
          kept.push(item);
        }
        return kept;
      });
      currentPage = page;
    }
    filteredTotal.set(data.total ?? items.length);
    titleCounts.set((data as { title_counts?: Record<string, number> }).title_counts ?? {});
    if (data.stats) stats.set(data.stats);
    // Derive from page*per_page vs total, NOT results.length (the cap-eviction
    // above pins results.length at PAGED_RESULTS_CAP while total keeps growing,
    // which would leave hasMore permanently true → infinite fetch loop).
    hasMore.set(page * PAGED_PER_PAGE < (data.total ?? 0));
    // Server facets (B2/D3) — computed over the whole matching set, not just
    // loaded pages. Always present on /results/cached; default to [] so a
    // response shape mismatch doesn't leave a stale prior value behind.
    const facets = data as { available_genres?: string[]; available_languages?: string[] };
    serverGenres.set(facets.available_genres ?? []);
    serverLanguages.set(facets.available_languages ?? []);
    if ((data as { source?: string }).source === 'cache') {
      cacheUpdatedAt.set((data as { last_updated?: string }).last_updated ?? null);
      fromCache.set(true);
    }
  } catch {
    if (generation === loadGeneration) loadError.set(true);
  } finally {
    if (generation === loadGeneration) loadingMore.set(false);
  }
}

export const filteredResults = derived(
  [results, statusFilter, searchFilter, genreFilter, languageFilter, sortBy, quickFilters, dismissedUrls, categoryFilter, resolutionFilter, pagedMode, postedAfter, postedBefore],
  ([$results, $filter, $search, $genre, $language, $sort, $quick, $dismissed, $category, $resolution, $paged, $postedAfter, $postedBefore]) => {
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
    if ($postedAfter || $postedBefore) {
      items = items.filter((i) => inPostedRange(i.posted_date, $postedAfter, $postedBefore));
    }
    // Resolution/type facet (4K / 1080p / TV) — OR within the set: an item
    // shows if it matches ANY selected key (mirrors backend _resolution_keys).
    if ($resolution.length > 0) {
      const rset = new Set($resolution);
      items = items.filter((i) => resolutionKeysFor(i).some((k) => rset.has(k)));
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

/** A swipe-deck group: one entry per title (group_key), holding every
 *  still-actionable release of that title, best-first. Lets the deck show a
 *  single card per title with a quality picker instead of one card per
 *  release, so you never swipe through duplicates. */
export interface DeckGroup {
  key: string;
  title: string;
  best: ScanResult;
  releases: ScanResult[];
}

/** Rank a release for "best": resolution, then DV, then HDR, then size. */
function releaseRank(i: ScanResult): number {
  return resolutionRank(i.resolution) * 1000
    + (i.dovi ? 300 : 0)
    + (i.hdr && i.hdr !== 'SDR' ? 100 : 0)
    + Math.min(sizeToGB(i.size) ?? 0, 99);
}

export const deckGroups = derived(deckResults, ($deck) => {
  const map = new Map<string, DeckGroup>();
  for (const item of $deck) {
    const key = item.group_key || item.title;
    let g = map.get(key);
    if (!g) { g = { key, title: item.title, best: item, releases: [] }; map.set(key, g); }
    g.releases.push(item);
  }
  const groups = [...map.values()];
  for (const g of groups) {
    g.releases.sort((a, b) => releaseRank(b) - releaseRank(a));
    g.best = g.releases[0];
  }
  return groups;
});

/** True when the swipe deck's card pool is running low and another server
 *  page should be fetched to top it up (paged mode only). Counts GROUPS now,
 *  since the deck shows one card per title. */
export function deckNeedsMore(remainingGroups: number): boolean {
  return get(pagedMode) && get(hasMore) && !get(loadingMore) && remainingGroups < 6;
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
export interface DismissMeta { group_key?: string; resolution?: string; dovi?: boolean }

export function dismissItem(url: string, title?: string, meta?: DismissMeta): Promise<boolean> {
  if (!url) return Promise.resolve(false);
  dismissedUrls.update((s) => {
    const next = new Set(s);
    next.add(url);
    return next;
  });
  let removedRow: ScanResult | undefined;
  if (get(pagedMode)) {
    results.update((items) => {
      const next = items.filter((i) => i.url !== url);
      if (next.length !== items.length) removedRow = items.find((i) => i.url === url);
      return next;
    });
    if (removedRow) filteredTotal.update((n) => Math.max(0, n - 1));
  }
  return api.dismissItems([url], title ? { [url]: title } : undefined, true, meta ? { [url]: meta } : undefined).then(
    () => true,
    () => {
      // Revert on failure so the UI reflects the server's truth.
      dismissedUrls.update((s) => {
        const next = new Set(s);
        next.delete(url);
        return next;
      });
      // Paged mode physically dropped the row — put it back, or the failed
      // dismiss silently vanishes it from the list until the next refresh.
      if (removedRow) {
        let reinserted = false;
        results.update((items) => {
          if (items.some((i) => i.url === url)) return items;  // a concurrent load already re-added it
          reinserted = true;
          return [removedRow!, ...items];
        });
        // Only bump the count if WE re-added the row — otherwise a concurrent
        // loadResults(true) already reset filteredTotal to the server total, and
        // an unconditional +1 would over-count (mirrors restoreItem's guard).
        if (reinserted) filteredTotal.update((n) => n + 1);
      }
      return false;
    }
  );
}

/** Undo a dismissal so the item can reappear.
 *
 *  Pass the original row as `item` so paged mode can bring it back: paged
 *  dismiss physically drops the row from `results` (see dismissItem), so
 *  clearing the dismissed flag alone can't resurrect it — there's nothing left
 *  to un-hide. We re-insert at the FRONT so an undone swipe returns to the top
 *  of the deck. Without `item` (or outside paged mode) this is just the flag
 *  clear, which is all the reactive-filter views need. */
export function restoreItem(url: string, item?: ScanResult): Promise<boolean> {
  if (!url) return Promise.resolve(false);
  dismissedUrls.update((s) => {
    const next = new Set(s);
    next.delete(url);
    return next;
  });
  let reinserted = false;
  if (item && get(pagedMode)) {
    results.update((items) => {
      if (items.some((i) => i.url === url)) return items;  // already present
      reinserted = true;
      return [item, ...items];
    });
    if (reinserted) filteredTotal.update((n) => n + 1);
  }
  return api.dismissItems([url], undefined, false).then(
    () => true,
    () => {
      // Revert the optimistic un-dismiss: re-hide, and drop the row we restored.
      dismissedUrls.update((s) => {
        const next = new Set(s);
        next.add(url);
        return next;
      });
      if (reinserted) {
        results.update((items) => items.filter((i) => i.url !== url));
        filteredTotal.update((n) => Math.max(0, n - 1));
      }
      return false;
    }
  );
}

/** Clears the current result set. Its only caller today is ScanControls'
 *  handleStart (the local Start-Scan button), so this also flips out of
 *  paged mode here: belt-and-braces for the pre-first-result window where the
 *  scan has started but nothing has streamed in yet — without this, a filter
 *  touch in that window would fire the debounced cache refetch (loadResults)
 *  and race the incoming live stream. If a future caller needs to clear
 *  results while staying in browse/paged mode (e.g. a generic "clear" button
 *  unrelated to starting a scan), move this pagedMode flip to the scan-start
 *  call path instead (ScanControls.handleStart / scanner.startScan). */
export function clearResults() {
  results.set([]);
  stats.set({ total: 0, missing: 0, upgrade: 0, library: 0 });
  selectedKeys.set(new Set());
  selectedDetail.set(null);
  focusedIndex.set(-1);
  activeScanResultCount = 0;
  fromCache.set(false);
  pagedMode.set(false);
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
    const grabbedRank = resolutionRank(grabbed.resolution);
    const grabbedDovi = grabbed.dovi ?? false;
    // Reclassify still-missing siblings so the deck updates instantly (matching
    // the server's read-time overlay in _shape_results): a sibling that's
    // genuinely better than what you grabbed — higher resolution, or same
    // resolution with a Dolby Vision gain — stays 'missing' (grabbable, and out
    // of the Upgrades tab), just annotated with what you already grabbed;
    // anything equal/lower becomes 'downloaded_similar' (you have a copy —
    // non-actionable, leaves the deck).
    let missingDelta = 0;
    const next = items.map((it) => {
      if (
        it.group_key === grabbed.group_key &&
        it.url !== grabbedUrl &&
        (it.status || '').toLowerCase().includes('missing')
      ) {
        const rank = resolutionRank(it.resolution);
        const isBetter = rank > grabbedRank || (rank === grabbedRank && (it.dovi ?? false) && !grabbedDovi);
        if (isBetter) return { ...it, prior_grab: note };
        // Leaving the 'missing' pool for 'downloaded_similar' — keep the
        // Missing counter honest (same bookkeeping markDownloaded does).
        missingDelta--;
        return { ...it, status: 'downloaded_similar', prior_grab: note };
      }
      return it;
    });
    if (missingDelta) {
      stats.update((st) => ({ ...st, missing: Math.max(0, st.missing + missingDelta) }));
    }
    return next;
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
    const payload: Record<string, string> = {};
    const pa = get(postedAfter); if (pa) payload.posted_after = pa;
    const pb = get(postedBefore); if (pb) payload.posted_before = pb;
    await api.selectAll(Object.keys(payload).length ? payload : undefined);
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

// ── Debounced refetch on filter/sort change (paged mode) ──────────────
/** Whenever the server-relevant filter/sort inputs change, re-fetch page 1
 *  after a short debounce — but only in paged mode, and never on the initial
 *  subscribe (which fires immediately with the current values). */
const _filterKey = derived(
  [statusFilter, searchFilter, genreFilter, languageFilter, quickFilters, categoryFilter, resolutionFilter, sortBy, postedAfter, postedBefore],
  (vals) => JSON.stringify(vals)
);
let _filterDebounce: ReturnType<typeof setTimeout> | undefined;
let _filterKeyPrimed = false;
_filterKey.subscribe(() => {
  if (!_filterKeyPrimed) { _filterKeyPrimed = true; return; } // skip initial fire
  if (!get(pagedMode)) return;
  clearTimeout(_filterDebounce);
  _filterDebounce = setTimeout(() => loadResults(true), 250);
});
