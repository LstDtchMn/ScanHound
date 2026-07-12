import type { RenameJob } from '$lib/api/types';

export type GroupedEntry =
  | { type: 'season'; key: string; show: string; season: number; jobs: RenameJob[] }
  | { type: 'single'; job: RenameJob };

export interface SeasonSummary {
  matched: number;
  needsReview: number;
  conflicts: number;
  applied: number;
  other: number;
}

/** Lowercase + collapse whitespace -- enough to fold cosmetic differences
 *  ("the   bear" vs "The Bear") without the year-stripping the backend's
 *  own normalize_title does (RenameJob.title is already a matched, clean
 *  title, not a raw scraped one). */
function normalizeTitle(title: string | null): string {
  return (title ?? '').toLowerCase().trim().replace(/\s+/g, ' ');
}

function seasonKey(job: RenameJob): string | null {
  if (job.media_type !== 'tv' || job.season == null) return null;
  const identity = job.imdb_id ? `imdb:${job.imdb_id}` : `title:${normalizeTitle(job.title)}`;
  return `${identity}|S${job.season}`;
}

/** Groups TV episodes by (imdb_id ?? normalized title, season); movies and
 *  season-less jobs pass through as individual entries. A season group's
 *  position in the output is its first member's position in the input --
 *  grouping never fights the caller's existing sort order. */
export function groupJobsBySeason(jobs: RenameJob[]): GroupedEntry[] {
  const groups = new Map<string, { show: string; season: number; jobs: RenameJob[] }>();
  const order: string[] = []; // first-seen order of group keys
  const singles: Array<{ index: number; job: RenameJob }> = [];

  jobs.forEach((job, index) => {
    const key = seasonKey(job);
    if (key === null) {
      singles.push({ index, job });
      return;
    }
    let g = groups.get(key);
    if (!g) {
      g = { show: job.title ?? 'Unknown', season: job.season as number, jobs: [] };
      groups.set(key, g);
      order.push(key);
    }
    g.jobs.push(job);
  });

  // First-seen-index per group key, for position interleaving with singles.
  const firstIndexByKey = new Map<string, number>();
  jobs.forEach((job, index) => {
    const key = seasonKey(job);
    if (key !== null && !firstIndexByKey.has(key)) firstIndexByKey.set(key, index);
  });

  type Positioned = { pos: number; entry: GroupedEntry };
  const positioned: Positioned[] = [
    ...singles.map(({ index, job }): Positioned => ({ pos: index, entry: { type: 'single', job } })),
    ...order.map((key): Positioned => {
      const g = groups.get(key)!;
      return { pos: firstIndexByKey.get(key)!, entry: { type: 'season', key, show: g.show, season: g.season, jobs: g.jobs } };
    }),
  ];
  positioned.sort((a, b) => a.pos - b.pos);
  return positioned.map((p) => p.entry);
}

/** Pure status tally for a season group's collapsed header. */
export function seasonSummary(jobs: RenameJob[]): SeasonSummary {
  const s: SeasonSummary = { matched: 0, needsReview: 0, conflicts: 0, applied: 0, other: 0 };
  for (const j of jobs) {
    if ((j as any).destination_conflict || (j as any).library_duplicate) s.conflicts++;
    switch (j.status) {
      case 'matched': s.matched++; break;
      case 'needs_review': s.needsReview++; break;
      case 'applied': s.applied++; break;
      default: s.other++;
    }
  }
  return s;
}
