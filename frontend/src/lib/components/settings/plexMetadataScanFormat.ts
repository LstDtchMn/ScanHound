/** Formats an ETA in seconds for the Plex library metadata-scan panel.
 *  null (no rate estimate yet, e.g. before any file has completed) -> '--'. */
export function formatEta(seconds: number | null): string {
  if (seconds === null) return '--';
  const total = Math.floor(seconds);
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  if (h > 0) return `${h}h ${m}m`;
  if (m > 0) return `${m}m ${s}s`;
  return `${s}s`;
}
