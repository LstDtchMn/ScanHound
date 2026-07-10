import type { DownloadResult } from '$lib/api/types';

/** Lowercase, strip a trailing/embedded (YYYY) year and punctuation, collapse
 *  whitespace — mirrors the backend's title normalization for grouping
 *  (see `clean_string()` in backend/app_service.py). */
export function normalizeTitle(s: string): string {
  const normalized = (s || '')
    .toLowerCase()
    .replace(/\((?:19|20)\d{2}\)/g, ' ')
    .trim();

  // Strip standalone years, but only keep that result if something is left —
  // otherwise the whole title WAS the year (e.g. "1917", "2012", "1984") and
  // stripping it would collapse unrelated movies onto the same '' key.
  const yearStripped = normalized.replace(/\b(?:19|20)\d{2}\b/g, ' ').trim();
  const base = yearStripped ? yearStripped : normalized;

  return base
    .replace(/[^a-z0-9\s]/g, ' ')
    .replace(/\s+/g, ' ')
    .trim();
}

/** Rank a package's resolution parsed from its name (JD names carry "[4K]" etc.). */
export function resRank(name: string): number {
  const n = (name || '').toLowerCase();
  if (n.includes('4k') || n.includes('2160p')) return 4;
  if (n.includes('1080p')) return 3;
  if (n.includes('720p')) return 2;
  return 1;
}

/** States that count as "in flight" — not yet a finished/historical row. */
const ACTIVE = new Set(['queued', 'downloading', 'extracting']);

/** True if a result is still in progress (queued/downloading/extracting) rather
 *  than a finished, failed, or otherwise historical row. */
export function isActive(r: DownloadResult): boolean {
  return ACTIVE.has(r.state);
}

export interface DownloadGroup {
  key: string;
  title: string;
  items: DownloadResult[];
  activeItems: DownloadResult[];
  isDuplicate: boolean;
  best: DownloadResult;
  canKeepBest: boolean;
}

/** Group downloads by normalized title. A group with >1 item is a duplicate
 *  group (covers both "same title, different releases" and "exact same package
 *  twice"). `best` is the highest-resolution then largest item among the
 *  ACTIVE (queued/downloading/extracting) rows when any exist — so a live
 *  re-grab is preferred over a higher-res but finished historical row — and
 *  falls back to ranking across all items when nothing is active. `canKeepBest`
 *  only offers the "keep best, cancel the rest" action when there are >=2
 *  active rows to actually choose between. */
export function groupDownloads(results: DownloadResult[]): DownloadGroup[] {
  const byKey = new Map<string, DownloadResult[]>();
  for (const r of results) {
    // `title` is resolved server-side and expected to always be set for real
    // results; the `|| r.name` fallback is a defensive last resort and could
    // in theory leak a resolution tag like "[1080p]" from a raw JD package
    // name into the grouping key if `title` were ever falsy.
    const key = normalizeTitle(r.title || r.name);
    const arr = byKey.get(key);
    if (arr) arr.push(r);
    else byKey.set(key, [r]);
  }
  const groups: DownloadGroup[] = [];
  for (const [key, items] of byKey) {
    const activeItems = items.filter(isActive);
    const rankPool = activeItems.length ? activeItems : items;
    const best = [...rankPool].sort(
      (a, b) => resRank(b.name) - resRank(a.name) || (b.bytes_total || 0) - (a.bytes_total || 0)
    )[0];
    groups.push({
      key,
      title: items[0].title || items[0].name,
      items,
      activeItems,
      isDuplicate: items.length > 1,
      best,
      canKeepBest: activeItems.length >= 2
    });
  }
  return groups;
}
