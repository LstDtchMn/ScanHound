import type { ScanResult } from '$lib/api/types';
import { resolutionRank } from '$lib/constants';

export interface FindAlternativeTarget {
  imdbId: string | null;
  title: string;
  season: number;
  excludeResolution: string;
}

function normalizeTitle(title: string): string {
  return title
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, ' ')
    .trim();
}

/** imdb_id first, normalized-title fallback -- mirrors this codebase's
 *  established identity-key pattern (see backend `_identity_key` /
 *  `find_library_duplicate`). Returns null when neither is available, so a
 *  titleless/idless item can never accidentally match anything. */
function identityKey(imdbId: string | null | undefined, title: string): string | null {
  if (imdbId) return `imdb:${imdbId}`;
  const norm = normalizeTitle(title);
  return norm ? `title:${norm}` : null;
}

/** Finds an already-cached same-show, same-season item at a different
 *  resolution than `target.excludeResolution`. Never a false match across
 *  shows or seasons -- returns null rather than guess. */
export function findCachedAlternative(
  items: ScanResult[],
  target: FindAlternativeTarget
): ScanResult | null {
  const targetKey = identityKey(target.imdbId, target.title);
  if (!targetKey) return null;
  for (const item of items) {
    if (item.season !== target.season) continue;
    // Compare by resolution TIER, not raw label -- "4K" and "2160p" (and any
    // other label sharing a resolutionRank bucket) are the same real tier,
    // just different site-tagging conventions (see constants.ts RES_RANK /
    // backend _RES_RANK). A raw string comparison here would let a same-tier,
    // differently-labeled cache item slip through as a false "alternative".
    if (resolutionRank(item.resolution) === resolutionRank(target.excludeResolution)) continue;
    const itemKey = identityKey(item.imdb_id, item.title);
    if (itemKey === targetKey) return item;
  }
  return null;
}

/** 4K/2160p implies 1080p is wanted (and vice versa) -- matches the stated
 *  use case exactly (found 4K, want 1080p), not a full resolution picker. */
export function targetResolution(current: string): string {
  return current === '4K' || current === '2160p' ? '1080p' : '4K';
}

export function seasonSearchQuery(title: string, season: number): string {
  return `${title} S${String(season).padStart(2, '0')}`;
}
