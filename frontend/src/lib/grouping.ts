import type { ScanResult } from './api/types';

/** Pure title-grouping helpers, extracted from routes/+page.svelte so the
 *  phone Scan view (mobile/MobileScanView.svelte) can reuse the exact same
 *  grouping logic as the desktop grid/list without duplicating it. Every
 *  function here is store-free: callers pass in whatever store values the
 *  original inline versions closed over. */

export interface ResultGroup {
  /** Unique grouping key (group_key or the composite fallback) -- use THIS,
   *  never the bare display title, for keyed {#each} blocks, expand-state
   *  tracking, and sibling-count lookups. Two groups can share a `title`
   *  (e.g. Dune 1984 vs Dune 2021) but never a `key`. */
  key: string;
  title: string;
  items: ScanResult[];
}

/** The same key groupResults()/computeSiblingCounts() use to distinguish
 *  same-title/different-year releases: the canonical group_key, falling
 *  back to a composite for legacy rows lacking one. */
function siblingKey(item: ScanResult): string {
  return item.group_key || `${item.title}|${item.year ?? ''}|S${item.season ?? ''}`;
}

export interface GroupFormats {
  res: string[];
  dv: boolean;
  hdr: boolean;
}

/** Group a list of results by their canonical group_key (falls back to a
 *  composite title|year|season key for legacy rows lacking group_key), so
 *  same-title/different-year releases (e.g. Dune 1984 vs 2021) stay in
 *  separate groups while genuine variants sharing a group_key still group
 *  together. Preserves first-seen order. */
export function groupResults(items: ScanResult[]): ResultGroup[] {
  const groups: ResultGroup[] = [];
  const map = new Map<string, ResultGroup>();
  for (const item of items) {
    const key = siblingKey(item);
    let group = map.get(key);
    if (!group) {
      group = { key, title: item.title, items: [] };
      map.set(key, group);
      groups.push(group);
    }
    group.items.push(item);
  }
  return groups;
}

/** Sibling counts across ALL filtered results — server counts in paged mode
 *  (covers rows not yet loaded into the render window), local tally in live mode. */
export function computeSiblingCounts(
  allFiltered: ScanResult[],
  titleCounts: Record<string, number>,
  paged: boolean
): Map<string, number> {
  if (paged && Object.keys(titleCounts).length) {
    return new Map(Object.entries(titleCounts));
  }
  const counts = new Map<string, number>();
  for (const item of allFiltered) {
    const key = siblingKey(item);
    counts.set(key, (counts.get(key) || 0) + 1);
  }
  return counts;
}

export function isDuplicateGroup(group: ResultGroup, siblingCounts: Map<string, number>): boolean {
  return (siblingCounts.get(group.key) || group.items.length) > 1;
}

function parseSizeGB(size: string | undefined): number {
  if (!size) return 0;
  const m = size.match(/([\d.]+)\s*(GB|MB|TB)/i);
  if (!m) return 0;
  const v = parseFloat(m[1]);
  if (m[2].toUpperCase() === 'MB') return v / 1024;
  if (m[2].toUpperCase() === 'TB') return v * 1024;
  return v;
}

export function groupSizeRange(items: ScanResult[]): string {
  const sizes = items.map(i => parseSizeGB(i.size)).filter(s => s > 0);
  if (!sizes.length) return '';
  const min = Math.min(...sizes);
  const max = Math.max(...sizes);
  const fmt = (gb: number) => gb < 1 ? `${Math.round(gb * 1024)} MB` : `${gb.toFixed(1)} GB`;
  return Math.abs(max - min) < 0.05 ? (items[0].size || '') : `${fmt(min)} – ${fmt(max)}`;
}

function shortDate(d: string): string {
  if (!d) return '';
  // "June 25, 2026 at 02:55 PM" → "Jun 25"
  const m = d.match(/^(\w{3})\w*\s+(\d+)/);
  if (m) return `${m[1]} ${m[2]}`;
  return d.split(' at ')[0].replace(/,?\s*\d{4}/, '').trim();
}

export function groupDateRange(items: ScanResult[]): string {
  const dates = items.map(i => i.posted_date).filter(Boolean) as string[];
  if (!dates.length) return '';
  // Sort chronologically, not lexically: "July 3" must come after "June 25"
  // (a plain string sort puts "July" before "June"). Parse to a timestamp,
  // dropping the " at " so Date can read "June 25, 2026 02:55 PM".
  const ts = (d: string) => {
    const t = Date.parse(d.replace(' at ', ' '));
    return Number.isNaN(t) ? 0 : t;
  };
  const unique = [...new Set(dates)].sort((a, b) => ts(a) - ts(b));
  const first = shortDate(unique[0]);
  if (unique.length === 1) return first;
  const last = shortDate(unique[unique.length - 1]);
  return first === last ? first : `${first} – ${last}`;
}

// Severity order for the group status bar/summary (most actionable first).
const STATUS_ORDER = ['missing', 'missing_season', 'upgrade', 'dv_upgrade', 'downloaded', 'in_library'];

/** Per-status counts for a group, ordered by severity, for the header badges. */
export function groupStatusSummary(items: ScanResult[]): { status: string; count: number }[] {
  const counts = new Map<string, number>();
  for (const it of items) {
    const s = (it.status ?? '').toLowerCase();
    if (s) counts.set(s, (counts.get(s) ?? 0) + 1);
  }
  const ordered = STATUS_ORDER.filter(k => counts.has(k)).map(k => ({ status: k, count: counts.get(k)! }));
  const extra = [...counts.keys()].filter(k => !STATUS_ORDER.includes(k)).map(k => ({ status: k, count: counts.get(k)! }));
  return [...ordered, ...extra];
}

export function groupFormats(items: ScanResult[]): GroupFormats {
  const resSet = new Set<string>();
  let dv = false, hdr = false;
  for (const it of items) {
    if (it.resolution) resSet.add(it.resolution === '2160p' ? '4K' : it.resolution);
    if (it.dovi) dv = true;
    if (it.hdr && it.hdr !== 'SDR' && !it.dovi) hdr = true;
  }
  const order = ['4K', '1080p', '720p', '480p'];
  const res = [...resSet].sort((a, b) => {
    const ai = order.indexOf(a), bi = order.indexOf(b);
    return (ai === -1 ? 9 : ai) - (bi === -1 ? 9 : bi);
  });
  return { res, dv, hdr };
}
