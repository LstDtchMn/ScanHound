import type { RenameJob } from '$lib/api/types';

export type ReviewBucket = 'ready' | 'needsReview' | 'inactive';

export function classifyJob(job: RenameJob): ReviewBucket {
  const status = job.status;
  // 'applying' is the transient state while a queued apply/overwrite is still
  // moving the file — treat it as inactive so the deck auto-advances instead
  // of leaving a stuck card with live buttons and inflating the review count.
  if (status === 'applied' || status === 'reverted' || status === 'pending' || status === 'applying') return 'inactive';
  const conf = job.match_confidence ?? 0;
  const clean = status === 'matched' && conf >= 100 && !job.warning_message && !job.destination_conflict;
  return clean ? 'ready' : 'needsReview';
}

function byConfidenceAsc(a: RenameJob, b: RenameJob): number {
  const av = a.match_confidence, bv = b.match_confidence;
  if (av == null && bv == null) return 0;
  if (av == null) return -1;   // nulls first (most-needing-scrutiny lead)
  if (bv == null) return 1;
  return av - bv;
}

export function partitionJobs(jobs: RenameJob[]): { ready: RenameJob[]; needsReview: RenameJob[] } {
  const ready: RenameJob[] = [], needsReview: RenameJob[] = [];
  for (const j of jobs) {
    const b = classifyJob(j);
    if (b === 'ready') ready.push(j);
    else if (b === 'needsReview') needsReview.push(j);
  }
  needsReview.sort(byConfidenceAsc);
  ready.sort(byConfidenceAsc);
  return { ready, needsReview };
}

export type ReviewScope = 'needsReview' | 'all';

/** Ordered work queue for RenameReviewDeck: needsReview-only, or
 *  ready-then-needsReview when scope is 'all' (both sub-lists are already
 *  confidence-ascending via partitionJobs). */
export function deckQueue(jobs: RenameJob[], scope: ReviewScope): RenameJob[] {
  const { ready, needsReview } = partitionJobs(jobs);
  return scope === 'needsReview' ? needsReview : [...ready, ...needsReview];
}

export function hasDestinationConflict(job: RenameJob): boolean {
  if (job.destination_conflict) return true;
  return /already exists/i.test(job.warning_message ?? '');
}

export function matchesQuery(j: RenameJob, q: string): boolean {
  if (!q) return true;
  const hay = `${j.title ?? ''} ${j.original_filename ?? ''} ${j.new_filename ?? ''}`.toLowerCase();
  return hay.includes(q.toLowerCase());
}
