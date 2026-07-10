/** Centralized status/color mappings for ScanHound UI. */

import type { BadgeVariant } from './components/Badge.svelte';

/** Map scan result status → Badge variant. */
export const STATUS_VARIANTS: Record<string, BadgeVariant> = {
  missing: 'error',
  missing_season: 'warning',
  upgrade: 'warning',
  dv_upgrade: 'accent',
  in_library: 'success',
  // Must precede 'downloaded' — statusVariant() does substring matching, and
  // 'downloaded_similar' contains 'downloaded'.
  downloaded_similar: 'orange',
  downloaded: 'accent',
  downloading: 'info',
  // Rename jobs: transient state while a queued background file move runs.
  applying: 'info',
};

/** Human-readable label for scan result status. */
const STATUS_LABELS: Record<string, string> = {
  missing: 'Missing',
  missing_season: 'Missing Season',
  upgrade: 'Upgrade',
  dv_upgrade: 'DV Upgrade',
  in_library: 'In Library',
  downloaded_similar: 'Downloaded Similar',
  downloaded: 'Downloaded',
  downloading: 'Downloading',
  applying: 'Applying…',
};

export function formatStatus(status: string | null | undefined): string {
  if (!status) return 'Unknown';
  return STATUS_LABELS[status.toLowerCase()] ?? status.replace(/_/g, ' ').replace(/\b\w/g, c => c.toUpperCase());
}

/** Resolve a status string to a Badge variant (substring match). */
export function statusVariant(status: string | null | undefined): BadgeVariant {
  if (!status) return 'default';
  const lower = status.toLowerCase();
  for (const [key, val] of Object.entries(STATUS_VARIANTS)) {
    if (lower.includes(key)) return val;
  }
  return 'default';
}

/** Left-border color for a scan-result row, keyed to its status. */
export function statusBorderColor(status: string | null | undefined): string {
  switch (statusVariant(status)) {
    case 'error': return 'var(--error)';
    case 'warning': return 'var(--warning)';
    case 'success': return 'var(--success)';
    case 'accent': return 'var(--accent)';
    case 'info': return '#3b82f6';
    case 'orange': return '#f97316';
    default: return 'var(--border)';
  }
}

/** Left status bar shared by individual rows AND collapsed group rows so they
 *  align into one continuous vertical strip. Always a 6px-wide bar painted into
 *  a transparent left border (reserving the 6px offset). Pass one color for a
 *  solid bar, or several for a vertical multi-segment bar (mixed-status groups). */
export function statusBarStyle(colors: string[]): string {
  const cs = colors.length ? colors : ['var(--border)'];
  const n = cs.length;
  const stops = cs.map((c, i) =>
    `${c} ${((i / n) * 100).toFixed(2)}%, ${c} ${(((i + 1) / n) * 100).toFixed(2)}%`).join(', ');
  return [
    'border-left: 6px solid transparent',
    `background-image: linear-gradient(to bottom, ${stops})`,
    'background-size: 6px 100%',
    'background-position: left top',
    'background-repeat: no-repeat',
    'background-origin: border-box',
  ].join('; ') + ';';
}

/** Map download history status → Badge variant. */
export function historyStatusVariant(status?: string): BadgeVariant {
  if (!status) return 'default';
  switch (status.toLowerCase()) {
    case 'completed': case 'complete': return 'success';
    case 'pending': case 'queued': case 'in_progress': case 'sending': case 'in-progress': return 'warning';
    case 'failed': case 'error': return 'error';
    default: return 'default';
  }
}

/** Human-readable label for download history status. */
export function historyStatusLabel(status?: string): string {
  if (!status) return '';
  switch (status.toLowerCase()) {
    case 'completed': case 'complete': return 'Completed';
    case 'pending': case 'queued': return 'Queued';
    case 'in_progress': case 'sending': case 'in-progress': return 'In Progress';
    case 'failed': case 'error': return 'Failed';
    default: return status;
  }
}

/** Border color for download history entries by status. */
export function historyBorderColor(status?: string): string {
  if (!status) return 'var(--border)';
  switch (status.toLowerCase()) {
    case 'completed': case 'complete': return 'var(--success)';
    case 'in_progress': case 'sending': case 'in-progress': case 'pending': case 'queued': return 'var(--warning)';
    case 'failed': case 'error': return 'var(--error)';
    default: return 'var(--border)';
  }
}

/** Priority level → Badge variant. */
export function priorityVariant(priority: number): BadgeVariant {
  switch (priority) {
    case 3: return 'error';
    case 1: return 'default';
    default: return 'accent';
  }
}

/** Priority level → label. */
export const PRIORITY_LABELS: Record<number, string> = { 1: 'Low', 2: 'Normal', 3: 'High' };

/** Watchlist status → text color class. */
export const WATCHLIST_STATUS_COLORS: Record<string, string> = {
  wanted: 'text-[var(--warning)]',
  found: 'text-[var(--success)]',
  downloaded: 'text-[var(--accent)]',
  in_library: 'text-[var(--success)]',
};

/** Resolution label — normalizes "?" to "Unknown". */
export function resolutionLabel(res: string | null | undefined): string {
  if (!res || res === '?') return 'Unknown';
  return res;
}

/** Rank a resolution for "is this an upgrade?" comparisons (higher = better).
 *  Shared by the row-level and group-level owned-version comparison so they
 *  can't drift apart. */
const RES_RANK: Record<string, number> = {
  '4k': 4, '2160p': 4, '1440p': 3, '1080p': 2, '720p': 1, '480p': 0,
};
export function resolutionRank(res: string | null | undefined): number {
  return RES_RANK[(res ?? '').toLowerCase()] ?? 0;
}

/** Parse a human size string ("7.6 GB", "900 MB", "1.2 TB") to gigabytes. */
export function sizeToGB(size: string | null | undefined): number {
  const m = String(size ?? '').match(/([\d.]+)\s*(TB|GB|MB)?/i);
  if (!m) return 0;
  let v = parseFloat(m[1]);
  const u = (m[2] ?? 'GB').toUpperCase();
  if (u === 'TB') v *= 1024;
  else if (u === 'MB') v /= 1024;
  return v;
}

/** Compact number formatting for vote/review counts: 1370 → "1.4K", 107813 → "108K", 1.2M. */
export function formatCount(n: number | null | undefined): string {
  if (n == null) return '';
  if (n < 1000) return String(n);
  return new Intl.NumberFormat('en', { notation: 'compact', maximumFractionDigits: 1 }).format(n);
}

/** Download-host options for the JDownloader hand-off (`value` = backend `service_type`). */
export interface DownloadHostOption { value: string; short: string }
export const DOWNLOAD_HOSTS: DownloadHostOption[] = [
  { value: 'Rapidgator', short: 'RG' },
  { value: 'Nitroflare', short: 'NF' },
  { value: '1Fichier', short: '1F' },
];

/** Rename-pipeline status → Badge variant (distinct from scan STATUS_VARIANTS). */
export const RENAME_STATUS_VARIANTS: Record<string, BadgeVariant> = {
  needs_review: 'warning',
  matched: 'accent',
  applied: 'success',
  reverted: 'default',
  failed: 'error',
  pending: 'info',
};

/** Dolby Vision layer → Badge variant. */
export const DV_LAYER_VARIANTS: Record<string, BadgeVariant> = {
  fel: 'error',
  mel: 'orange',
  p8: 'accent',
  p5: 'info',
};

export function renameStatusVariant(status: string | null | undefined): BadgeVariant {
  if (!status) return 'default';
  return RENAME_STATUS_VARIANTS[status.toLowerCase()] ?? 'default';
}

export function dvLayerVariant(layer: string | null | undefined): BadgeVariant {
  if (!layer) return 'default';
  return DV_LAYER_VARIANTS[layer.toLowerCase()] ?? 'default';
}

/** Confidence % → variant: ≥95 success, 70–94 warning, <70 error, null/undefined default. */
export function confidenceVariant(pct: number | null | undefined): BadgeVariant {
  if (pct == null || Number.isNaN(pct)) return 'default';
  if (pct >= 95) return 'success';
  if (pct >= 70) return 'warning';
  return 'error';
}

/** CSS color for a Dolby Vision layer, consistent with DV_LAYER_VARIANTS and Badge colors. */
export function dvLayerColor(layer: string): string {
  switch (layer.toLowerCase()) {
    case 'fel': return 'var(--error)';
    case 'mel': return '#f97316'; // Badge `orange` variant — keep in sync
    case 'p8': return 'var(--accent)';
    case 'p5':
    case 'info': return '#3b82f6';
    default: return 'var(--text-secondary)';
  }
}

export function renameStatusBorderColor(status: string | null | undefined): string {
  switch (renameStatusVariant(status)) {
    case 'error': return 'var(--error)';
    case 'warning': return 'var(--warning)';
    case 'success': return 'var(--success)';
    case 'accent': return 'var(--accent)';
    case 'info': return '#3b82f6';
    case 'orange': return '#f97316';
    default: return 'var(--border)';
  }
}
