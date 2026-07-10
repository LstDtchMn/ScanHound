// @vitest-environment node
import { describe, it, expect } from 'vitest';
import { classifyJob, partitionJobs, hasDestinationConflict, deckQueue } from './review';
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
  it('applying → inactive', () => expect(classifyJob(job({ status: 'applying' }))).toBe('inactive'));
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

describe('deckQueue', () => {
  const jobs = [
    job({ id: 1, status: 'matched', match_confidence: 100 }),          // ready
    job({ id: 2, status: 'needs_review', match_confidence: 80 }),
    job({ id: 3, status: 'needs_review', match_confidence: null }),
  ];

  it('needsReview scope returns only jobs needing review, confidence-asc (nulls first)', () => {
    expect(deckQueue(jobs, 'needsReview').map((j) => j.id)).toEqual([3, 2]);
  });

  it('all scope includes ready items ahead of needsReview items, position 1 / N', () => {
    const all = deckQueue(jobs, 'all');
    // ready (id 1) first, then needsReview confidence-asc/nulls-first (id 3, id 2)
    expect(all.map((j) => j.id)).toEqual([1, 3, 2]);
    expect(all.length).toBeGreaterThan(deckQueue(jobs, 'needsReview').length);
    // '1 / N' position shown by the deck for the first item:
    expect(`${1} / ${all.length}`).toBe('1 / 3');
  });

  it('shows completion state (empty queue) when nothing matches the scope', () => {
    expect(deckQueue([], 'needsReview')).toEqual([]);
    expect(deckQueue([], 'all')).toEqual([]);
    const allReady = [job({ id: 1, status: 'matched', match_confidence: 100 })];
    expect(deckQueue(allReady, 'needsReview')).toEqual([]); // scoped-empty even though jobs exist
  });
});
