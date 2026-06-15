import { writable, derived } from 'svelte/store';
import { api } from '$lib/api/client';
import { connection } from './connection';
import type { ScanResult, ScanStats } from '$lib/api/types';

export type StatusFilter = 'all' | 'missing' | 'upgrade' | 'library';
export type ViewMode = 'grid' | 'list';
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

export const results = writable<ScanResult[]>([]);
export const statusFilter = writable<StatusFilter>('all');
export const searchFilter = writable<string>('');
export const genreFilter = writable<string>('');
export const languageFilter = writable<string>('');
export const viewMode = writable<ViewMode>('grid');
export const stats = writable<ScanStats>({
  total: 0,
  missing: 0,
  upgrade: 0,
  library: 0
});
export const sortBy = writable<SortOption>('title-asc');
export const selectedKeys = writable<Set<string>>(new Set());
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

export const filteredResults = derived(
  [results, statusFilter, searchFilter, genreFilter, languageFilter, sortBy],
  ([$results, $filter, $search, $genre, $language, $sort]) => {
    let items = $results;
    if ($filter !== 'all') {
      items = items.filter(
        (i) => i.status === $filter || (i.status && i.status.includes($filter))
      );
    }
    if ($search) {
      const q = $search.toLowerCase();
      items = items.filter((i) => i.title.toLowerCase().includes(q));
    }
    if ($genre) {
      items = items.filter((i) => i.genres?.includes($genre));
    }
    if ($language) {
      items = items.filter((i) => i.language === $language);
    }
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

export function clearResults() {
  results.set([]);
  stats.set({ total: 0, missing: 0, upgrade: 0, library: 0 });
  selectedKeys.set(new Set());
  selectedDetail.set(null);
  focusedIndex.set(-1);
  activeScanResultCount = 0;
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
