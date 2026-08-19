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

/** Moved to $lib/time so the downloads views can share the same UTC handling.
 *  Re-exported here so existing importers (PipelineList) and this module's own
 *  tests keep working unchanged. */
export { checkedAgo } from '$lib/time';

/** CSS var reference for a category's accent color, for inline styling of the
 *  category label. `default`/unknown categories fall back to the secondary
 *  text color rather than an undefined `--default` var. */
export function categoryColor(cat: string | null): string {
  const variant = CATEGORY_VARIANT[cat ?? ''] ?? 'default';
  if (variant === 'default') return 'var(--text-secondary)';
  return `var(--${variant})`;
}
