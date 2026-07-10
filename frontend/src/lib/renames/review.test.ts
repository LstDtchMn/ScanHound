// @vitest-environment node
import { describe, it, expect } from 'vitest';
import { classifyJob, partitionJobs, hasDestinationConflict } from './review';
import type { RenameJob } from '$lib/api/types';

const job = (o: Partial<RenameJob>): RenameJob => ({
  id: 1, package_name: null, original_path: '/x', original_filename: 'x', new_filename: 'y',
  destination_path: '/d', status: 'matched', media_type: 'movie', title: 'X', year: 2024,
  season: null, episode: null, tmdb_id: null, imdb_id: null, resolution: '2160p',
  match_confidence: 100, match_source: 'deterministic', move_method: null,
  warning_message: null, error_message: null, plex_sort_title: null, detected_at: null,
  processed_at: null, reverted_at: null, ...o,
}) as RenameJob;

describe('classifyJob', () => {
  it('matched 100, clean → ready', () => expect(classifyJob(job({}))).toBe('ready'));
  it('matched 100 with warning → needsReview',
    () => expect(classifyJob(job({ warning_message: 'A file already exists' }))).toBe('needsReview'));
  it('matched 99 → needsReview', () => expect(classifyJob(job({ match_confidence: 99 }))).toBe('needsReview'));
  it('needs_review → needsReview', () => expect(classifyJob(job({ status: 'needs_review' }))).toBe('needsReview'));
  it('failed → needsReview', () => expect(classifyJob(job({ status: 'failed' }))).toBe('needsReview'));
  it('applied → inactive', () => expect(classifyJob(job({ status: 'applied' }))).toBe('inactive'));
  it('pending → inactive', () => expect(classifyJob(job({ status: 'pending' }))).toBe('inactive'));
});

describe('partitionJobs', () => {
  it('orders needsReview by confidence ascending, nulls first', () => {
    const { needsReview } = partitionJobs([
      job({ id: 1, status: 'needs_review', match_confidence: 80 }),
      job({ id: 2, status: 'needs_review', match_confidence: null }),
      job({ id: 3, status: 'needs_review', match_confidence: 50 }),
    ]);
    expect(needsReview.map((j) => j.id)).toEqual([2, 3, 1]);
  });
});

describe('hasDestinationConflict', () => {
  it('true on already-exists warning', () =>
    expect(hasDestinationConflict(job({ warning_message: 'A file already exists at /d/y' }))).toBe(true));
  it('true on destination_conflict flag', () =>
    expect(hasDestinationConflict(job({ destination_conflict: true } as Partial<RenameJob>))).toBe(true));
  it('false otherwise', () => expect(hasDestinationConflict(job({}))).toBe(false));
});
