/** Pure formatting/comparison helpers for the RenameReviewCard two-file
 *  conflict-compare view (Existing vs Incoming). Kept dependency-free of
 *  Svelte so it's unit-testable under plain jsdom vitest — see
 *  conflictView.test.ts. */
import { resolutionRank } from '$lib/constants';
import type { FileSpec, ConflictAnalysis } from '$lib/api/types';

const EM_DASH = '—';

/** Human file size, mirroring TrashPanel.formatSize (KB/MB/GB/TB, 1 decimal). */
export function formatBytes(bytes: number | null | undefined): string {
  if (bytes == null) return EM_DASH;
  if (bytes < 1024) return `${bytes} B`;
  const units = ['KB', 'MB', 'GB', 'TB'];
  let n = bytes / 1024;
  let i = 0;
  while (n >= 1024 && i < units.length - 1) {
    n /= 1024;
    i++;
  }
  return `${n.toFixed(1)} ${units[i]}`;
}

/** Bitrate (bits/sec, as probed by ffprobe) -> "NN.N Mbps". <=0 counts as an
 *  unknown/failed probe rather than a real zero bitrate. */
export function formatMbps(bitrate: number | null | undefined): string {
  if (bitrate == null || bitrate <= 0) return EM_DASH;
  return `${(bitrate / 1e6).toFixed(1)} Mbps`;
}

function formatDuration(min: number | null | undefined): string {
  if (min == null) return EM_DASH;
  return `${min} min`;
}

/** HDR / DV cell text: shows the layer alongside "Dolby Vision" once known,
 *  otherwise just "Dolby Vision" (the on-demand scan fills in the layer). */
function hdrLabel(spec: FileSpec | null | undefined): string {
  if (!spec || !spec.present) return EM_DASH;
  if (spec.hdr === 'Dolby Vision') {
    return spec.dv_layer ? `Dolby Vision (${spec.dv_layer.toUpperCase()})` : 'Dolby Vision';
  }
  return spec.hdr ?? 'SDR';
}

const HDR_RANK: Record<string, number> = {
  'dolby vision': 4, 'hdr10+': 3, hdr10: 3, hlg: 2, hdr: 1, sdr: 0,
};
function hdrRank(spec: FileSpec | null | undefined): number | null {
  if (!spec || !spec.present) return null;
  return HDR_RANK[(spec.hdr ?? 'sdr').toLowerCase()] ?? 0;
}

/** Which side wins a row given two comparable numbers (higher = better).
 *  A missing value loses to a present one; both missing is an unknown tie. */
function cmp(a: number | null, b: number | null): 'existing' | 'incoming' | 'tie' | null {
  if (a == null && b == null) return null;
  if (a == null) return 'incoming';
  if (b == null) return 'existing';
  if (a === b) return 'tie';
  return a > b ? 'existing' : 'incoming';
}

export interface SpecRow {
  label: string;
  existing: string;
  incoming: string;
  /** Which side looks technically better on this row alone (independent of
   *  the backend's holistic ConflictComparison.recommended verdict — the
   *  component highlights that column separately). null = not objectively
   *  comparable (e.g. codec/audio track names) or both sides unknown. */
  better: 'existing' | 'incoming' | 'tie' | null;
}

/** Builds the Existing-vs-Incoming compare-table rows for a conflict.
 *  `present === false` (destination free) or a null spec renders as em
 *  dashes for that column, matching the "destination is free" collapse. */
export function specRows(
  existing: FileSpec | null | undefined,
  incoming: FileSpec | null | undefined
): SpecRow[] {
  const e = existing?.present ? existing : null;
  const i = incoming?.present ? incoming : null;

  return [
    {
      label: 'Resolution',
      existing: e?.resolution ?? EM_DASH,
      incoming: i?.resolution ?? EM_DASH,
      better: cmp(e ? resolutionRank(e.resolution) : null, i ? resolutionRank(i.resolution) : null),
    },
    {
      label: 'HDR / DV',
      existing: hdrLabel(existing),
      incoming: hdrLabel(incoming),
      better: cmp(hdrRank(existing), hdrRank(incoming)),
    },
    {
      label: 'Video',
      existing: e?.video_codec ?? EM_DASH,
      incoming: i?.video_codec ?? EM_DASH,
      better: null,
    },
    {
      label: 'Audio',
      existing: e?.audio ?? EM_DASH,
      incoming: i?.audio ?? EM_DASH,
      better: null,
    },
    {
      label: 'Bitrate',
      existing: formatMbps(e?.bitrate ?? null),
      incoming: formatMbps(i?.bitrate ?? null),
      better: cmp(e?.bitrate ?? null, i?.bitrate ?? null),
    },
    {
      label: 'Size',
      existing: formatBytes(e?.size_bytes ?? null),
      incoming: formatBytes(i?.size_bytes ?? null),
      better: cmp(e?.size_bytes ?? null, i?.size_bytes ?? null),
    },
    {
      label: 'Duration',
      existing: formatDuration(e?.duration_min ?? null),
      incoming: formatDuration(i?.duration_min ?? null),
      better: null,
    },
  ];
}

/** True when this side is Dolby Vision but the FEL/MEL/P8/P5 layer hasn't
 *  been probed yet — drives the on-demand "Scan DV layers" button. */
export function needsDvScan(spec: FileSpec | null | undefined): boolean {
  if (!spec) return false;
  return spec.hdr === 'Dolby Vision' && !spec.dv_layer;
}

/** Concise one-line row diff, showing only the axes that DIFFER between
 *  existing and incoming — replaces the old raw-byte warning_message
 *  tooltip. Pure, never throws on missing/degraded input. */
export function conflictSummary(analysis: ConflictAnalysis | null | undefined): string {
  if (!analysis) return '';
  const e = analysis.existing?.present ? analysis.existing : null;
  const i = analysis.incoming?.present ? analysis.incoming : null;
  const parts: string[] = [];

  const eRes = e?.resolution ?? EM_DASH;
  const iRes = i?.resolution ?? EM_DASH;
  const eHdr = hdrLabel(e);
  const iHdr = hdrLabel(i);
  const eSize = formatBytes(e?.size_bytes ?? null);
  const iSize = formatBytes(i?.size_bytes ?? null);

  const axisDiffers = eRes !== iRes || eHdr !== iHdr;
  const existingBits = [axisDiffers ? eRes : null, axisDiffers ? eHdr : null, eSize]
    .filter((v): v is string => !!v && v !== EM_DASH);
  const incomingBits = [axisDiffers ? iRes : null, axisDiffers ? iHdr : null, iSize]
    .filter((v): v is string => !!v && v !== EM_DASH);

  parts.push(`Existing ${existingBits.join('·') || EM_DASH}`);
  parts.push(`→ Incoming ${incomingBits.join('·') || EM_DASH}`);

  let summary = parts.join(' ');
  if (!analysis.degraded && analysis.recommended && analysis.recommended !== 'tie') {
    const who = analysis.recommended === 'existing' ? 'Existing' : 'Incoming';
    summary += ` · keep ${who} ★`;
  }
  return summary;
}
