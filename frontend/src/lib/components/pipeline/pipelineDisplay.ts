import type { BadgeVariant } from '$lib/components/Badge.svelte';

/** Spec'd mapping - all nine categories. */
export const CATEGORY_VARIANT: Record<string, BadgeVariant> = {
  verified: 'success',
  rename_failed: 'error',
  download_failed: 'error',
  not_in_plex: 'error',
  pending_rename: 'warning',
  awaiting_plex_refresh: 'warning',
  never_started: 'warning',
  downloading: 'accent',
  unknown: 'default',
};

/** Categories whose items have reached a rename job - the only ones a poster
 *  can exist for (no identified title before that). */
export const POSTER_CATEGORIES = new Set([
  'pending_rename', 'rename_failed', 'awaiting_plex_refresh', 'verified', 'not_in_plex',
]);

/** "5m ago" / "3h ago" / "2d ago" from a timestamp. Two real formats reach
 *  this function: sqlite's naive UTC 'YYYY-MM-DD HH:MM:SS' (every
 *  CURRENT_TIMESTAMP-backed column: checked_at, grabbed_at, and renamed_at's
 *  detected_at fallback) with no offset, and Python's
 *  datetime.now(timezone.utc).isoformat() (renamed_at's processed_at, e.g.
 *  '2026-07-14T02:35:15.946949+00:00'), which already carries an explicit
 *  offset. Only the former needs a 'T' and trailing 'Z' bolted on to parse as
 *  UTC — doing that unconditionally to the latter produces an unparsable
 *  '...+00:00Z' (Invalid Date), which is why renamed_at silently failed to
 *  render for applied/reverted rows. Returns '' for an empty/malformed
 *  timestamp so callers render nothing instead of "NaNd ago". */
export function checkedAgo(sqliteTs: string, now: Date = new Date()): string {
  if (!sqliteTs) return '';
  const hasOffset = /(Z|[+-]\d{2}:\d{2})$/.test(sqliteTs);
  const dt = hasOffset ? new Date(sqliteTs) : new Date(sqliteTs.replace(' ', 'T') + 'Z');
  if (Number.isNaN(dt.getTime())) return '';
  const mins = Math.max(0, Math.floor((now.getTime() - dt.getTime()) / 60000));
  if (mins < 60) return `${mins}m ago`;
  const hours = Math.floor(mins / 60);
  if (hours < 24) return `${hours}h ago`;
  return `${Math.floor(hours / 24)}d ago`;
}

/** CSS var reference for a category's accent color, for inline styling of the
 *  category label. `default`/unknown categories fall back to the secondary
 *  text color rather than an undefined `--default` var. */
export function categoryColor(cat: string | null): string {
  const variant = CATEGORY_VARIANT[cat ?? ''] ?? 'default';
  if (variant === 'default') return 'var(--text-secondary)';
  return `var(--${variant})`;
}
