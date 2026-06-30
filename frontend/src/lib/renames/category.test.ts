// @vitest-environment node
import { describe, it, expect } from 'vitest';
import { categoryOf } from './category';
import type { RenameJob } from '$lib/api/types';

function job(over: Partial<RenameJob>): RenameJob {
  return {
    id: 1, package_name: null, original_path: '/x', original_filename: null,
    new_filename: null, destination_path: null, status: 'matched',
    media_type: null, title: null, year: null, season: null, episode: null,
    tmdb_id: null, imdb_id: null, resolution: null, match_confidence: null,
    match_source: null, move_method: null, warning_message: null,
    error_message: null, plex_sort_title: null, detected_at: null,
    processed_at: null, reverted_at: null, ...over
  } as RenameJob;
}

describe('categoryOf', () => {
  it('classifies a 4K movie under both movies and 4k', () => {
    const c = categoryOf(job({ media_type: 'movie', resolution: '2160p' }));
    expect(c.has('movies')).toBe(true);
    expect(c.has('4k')).toBe(true);
    expect(c.has('tv')).toBe(false);
  });
  it('treats media_type "show" as tv', () => {
    expect(categoryOf(job({ media_type: 'show' })).has('tv')).toBe(true);
  });
  it('matches uhd/4k/2160p case-insensitively for 4k', () => {
    expect(categoryOf(job({ resolution: 'UHD' })).has('4k')).toBe(true);
    expect(categoryOf(job({ resolution: '4K' })).has('4k')).toBe(true);
  });
  it('classifies 1080p', () => {
    expect(categoryOf(job({ resolution: '1080p' })).has('1080p')).toBe(true);
  });
  it('detects remux from filename when resolution lacks it', () => {
    const c = categoryOf(job({ resolution: '2160p', new_filename: 'Movie.2024.REMUX.mkv' }));
    expect(c.has('remux')).toBe(true);
  });
  it('returns an empty set for a job with no media_type/resolution', () => {
    expect(categoryOf(job({})).size).toBe(0);
  });
});
