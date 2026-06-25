/** Centralized status/color mappings for ScanHound UI. */

import type { BadgeVariant } from './components/Badge.svelte';

/** Map scan result status → Badge variant. */
export const STATUS_VARIANTS: Record<string, BadgeVariant> = {
  missing: 'error',
  missing_season: 'warning',
  upgrade: 'warning',
  dv_upgrade: 'accent',
  in_library: 'success',
  downloaded: 'accent',
};

/** Human-readable label for scan result status. */
const STATUS_LABELS: Record<string, string> = {
  missing: 'Missing',
  missing_season: 'Missing Season',
  upgrade: 'Upgrade',
  dv_upgrade: 'DV Upgrade',
  in_library: 'In Library',
  downloaded: 'Downloaded',
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
    default: return 'var(--border)';
  }
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
