import type { RenameJob } from '$lib/api/types';

export type RenameCategory = 'all' | 'movies' | 'tv' | '4k' | '1080p' | 'remux';

export const RENAME_CATEGORIES: readonly RenameCategory[] = [
  'all', 'movies', 'tv', '4k', '1080p', 'remux',
];

/** Membership set for a job across filter chips (a job can belong to several). */
export function categoryOf(job: RenameJob): Set<RenameCategory> {
  const cats = new Set<RenameCategory>();
  const mt = (job.media_type ?? '').toLowerCase();
  const res = (job.resolution ?? '').toLowerCase();
  const names = `${job.new_filename ?? ''} ${job.original_filename ?? ''}`.toLowerCase();

  if (mt === 'movie') cats.add('movies');
  if (mt === 'tv' || mt === 'show') cats.add('tv');
  if (/2160p|4k|uhd/.test(res)) cats.add('4k');
  if (res.includes('1080p')) cats.add('1080p');
  if (res.includes('remux') || names.includes('remux')) cats.add('remux');
  return cats;
}
