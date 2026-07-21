import { describe, expect, it } from 'vitest';
import {
  canCancelAction,
  canCopyActionLinks,
  canRetryAction
} from './actions';

describe('RSS action controls', () => {
  it('allows cancellation only before submission', () => {
    expect(canCancelAction({ state: 'queued' })).toBe(true);
    expect(canCancelAction({ state: 'retrieving_links' })).toBe(true);
    expect(canCancelAction({ state: 'submitted' })).toBe(false);
    expect(canCancelAction({ state: 'needs_review' })).toBe(false);
  });

  it('allows retry only for safely retryable terminal states', () => {
    expect(canRetryAction({ state: 'failed' })).toBe(true);
    expect(canRetryAction({ state: 'cancelled' })).toBe(true);
    expect(canRetryAction({ state: 'needs_review' })).toBe(false);
  });

  it('copies links only after retrieve-only completion', () => {
    expect(canCopyActionLinks({
      state: 'links_ready',
      links: ['https://rapidgator.net/file/1']
    })).toBe(true);
    expect(canCopyActionLinks({ state: 'links_ready', links: [] })).toBe(false);
    expect(canCopyActionLinks({ state: 'submitted', links: ['x'] })).toBe(false);
  });
});
