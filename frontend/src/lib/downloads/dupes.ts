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

/** The season a package covers, parsed from the JD name — `''` when it carries
 *  no marker.
 *
 *  WHY THIS EXISTS. `title` is resolved server-side and has the season STRIPPED:
 *  every season of a show arrives as `'The Repair Shop [1080p]'`. Grouping on
 *  title alone therefore collapsed S02, S04, S07, S08, S11 and S12 into one
 *  group captioned "10 duplicates" — six unrelated seasons the user was being
 *  invited to de-duplicate. Different seasons are not duplicates of each other.
 *
 *  Parsed from `name` rather than added to the wire because `name` already
 *  carries it and `resRank` already parses that same field for resolution.
 *
 *  Measured against 361 live rows: 61 carry the `S01` form, and NONE carry
 *  `S01E02`, `1x02`, or a bare `Season N` — but `Season N` is common enough in
 *  release naming to be worth matching. Rows with no marker at all (300 of 361,
 *  all older history) fall back to title-only grouping, exactly as before.
 */
export function seasonKey(name: string): string {
  const n = name || '';
  const sxx = /\bS(\d{1,2})(?:\s*E(\d{1,3}))?\b/i.exec(n);
  if (sxx) {
    const s = `S${sxx[1].padStart(2, '0')}`;
    return sxx[2] ? `${s}E${sxx[2].padStart(2, '0')}` : s;
  }
  const word = /\bSeason\s*(\d{1,2})\b/i.exec(n);
  return word ? `S${word[1].padStart(2, '0')}` : '';
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

/** Group downloads by normalized title AND season. A group with >1 item is a
 *  duplicate group (covers both "same title, different releases" and "exact
 *  same package twice"). `best` is the highest-resolution then largest item
 *  among the ACTIVE (queued/downloading/extracting) rows when any exist — so a
 *  live re-grab is preferred over a higher-res but finished historical row —
 *  and falls back to ranking across all items when nothing is active.
 *  `canKeepBest` only offers the "keep best, cancel the rest" action when there
 *  are >=2 active rows to actually choose between.
 *
 *  THE SEASON IS PART OF THE KEY. Without it, every season of a show grouped
 *  together and the UI offered to cancel all but the "best" of them — which
 *  would have thrown away entire seasons the user deliberately queued. Two
 *  releases of the SAME season still group, which is the case the feature is
 *  actually for. */
export function groupDownloads(results: DownloadResult[]): DownloadGroup[] {
  const byKey = new Map<string, DownloadResult[]>();
  for (const r of results) {
    // `title` is resolved server-side and expected to always be set for real
    // results; the `|| r.name` fallback is a defensive last resort and could
    // in theory leak a resolution tag like "[1080p]" from a raw JD package
    // name into the grouping key if `title` were ever falsy.
    //
    // The season comes from `name` because `title` has it stripped. Joined with
    // a separator that normalizeTitle cannot produce, so "show" + "S02" can
    // never collide with a differently-split pair.
    const key = `${normalizeTitle(r.title || r.name)}|${seasonKey(r.name)}`;
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
    // Show the season in the heading. Splitting by season means a show's
    // seasons now render as separate cards, and without this they would all
    // read "The Repair Shop [1080p]" with nothing to tell them apart.
    const base = items[0].title || items[0].name;
    const season = seasonKey(items[0].name);
    groups.push({
      key,
      title: season ? `${base} · ${season}` : base,
      items,
      activeItems,
      isDuplicate: items.length > 1,
      best,
      canKeepBest: activeItems.length >= 2
    });
  }
  return groups;
}
