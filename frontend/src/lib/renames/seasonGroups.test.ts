import { describe, it, expect } from 'vitest';
import { groupJobsBySeason, seasonSummary } from './seasonGroups';
import type { RenameJob } from '$lib/api/types';

const job = (o: Partial<RenameJob>): RenameJob => ({
  id: 1, package_name: null, original_path: '', original_filename: null,
  new_filename: null, destination_path: null, status: 'matched',
  media_type: 'movie', title: 'X', year: null, season: null, episode: null,
  tmdb_id: null, imdb_id: null, resolution: null, match_confidence: null,
  match_source: null, move_method: null, warning_message: null,
  error_message: null, plex_sort_title: null, detected_at: null,
  processed_at: null, reverted_at: null,
  ...o,
} as RenameJob);

describe('groupJobsBySeason', () => {
  it('groups TV episodes by imdb_id + season', () => {
    const jobs = [
      job({ id: 1, media_type: 'tv', title: 'Severance', season: 2, episode: 1, imdb_id: 'tt11280740' }),
      job({ id: 2, media_type: 'tv', title: 'Severance', season: 2, episode: 2, imdb_id: 'tt11280740' }),
    ];
    const groups = groupJobsBySeason(jobs);
    expect(groups).toHaveLength(1);
    expect(groups[0]).toMatchObject({ type: 'season', show: 'Severance', season: 2 });
    expect((groups[0] as any).jobs).toHaveLength(2);
  });

  it('falls back to normalized title when imdb_id is null', () => {
    const jobs = [
      job({ id: 1, media_type: 'tv', title: 'The Bear', season: 1, episode: 1, imdb_id: null }),
      job({ id: 2, media_type: 'tv', title: 'the   bear', season: 1, episode: 2, imdb_id: null }),
    ];
    const groups = groupJobsBySeason(jobs);
    expect(groups).toHaveLength(1);
    expect((groups[0] as any).jobs).toHaveLength(2);
  });

  it('never merges two different shows sharing a season number', () => {
    const jobs = [
      job({ id: 1, media_type: 'tv', title: 'Show A', season: 1, episode: 1, imdb_id: 'tt1' }),
      job({ id: 2, media_type: 'tv', title: 'Show B', season: 1, episode: 1, imdb_id: 'tt2' }),
    ];
    const groups = groupJobsBySeason(jobs);
    expect(groups).toHaveLength(2);
  });

  it('leaves movies and season-less jobs as individual entries in original position', () => {
    const jobs = [
      job({ id: 1, media_type: 'movie', title: 'A Movie' }),
      job({ id: 2, media_type: 'tv', title: 'No Season', season: null }),
    ];
    const groups = groupJobsBySeason(jobs);
    expect(groups).toEqual([
      { type: 'single', job: jobs[0] },
      { type: 'single', job: jobs[1] },
    ]);
  });

  it('positions a season group at its first member\'s original index', () => {
    const jobs = [
      job({ id: 1, media_type: 'movie', title: 'Movie First' }),
      job({ id: 2, media_type: 'tv', title: 'Show', season: 1, episode: 1, imdb_id: 'tt1' }),
      job({ id: 3, media_type: 'movie', title: 'Movie Last' }),
      job({ id: 4, media_type: 'tv', title: 'Show', season: 1, episode: 2, imdb_id: 'tt1' }),
    ];
    const groups = groupJobsBySeason(jobs);
    expect(groups.map((g) => g.type)).toEqual(['single', 'season', 'single']);
  });
});

describe('seasonSummary', () => {
  it('tallies statuses and conflicts correctly', () => {
    const jobs = [
      job({ status: 'matched' }),
      job({ status: 'matched' }),
      job({ status: 'needs_review' }),
      job({ status: 'applied' }),
      job({ status: 'matched', destination_conflict: true } as any),
    ];
    const s = seasonSummary(jobs);
    expect(s).toEqual({ matched: 3, needsReview: 1, conflicts: 1, applied: 1, other: 0 });
  });

  it('handles an empty list', () => {
    expect(seasonSummary([])).toEqual({ matched: 0, needsReview: 0, conflicts: 0, applied: 0, other: 0 });
  });
});
