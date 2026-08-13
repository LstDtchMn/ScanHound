/** "5m ago" / "3h ago" / "2d ago" from a timestamp. Two real formats reach
 *  this function: sqlite's naive UTC 'YYYY-MM-DD HH:MM:SS' (every
 *  CURRENT_TIMESTAMP-backed column: checked_at, grabbed_at, date_added, and
 *  renamed_at's detected_at fallback) with no offset, and Python's
 *  datetime.now(timezone.utc).isoformat() (renamed_at's processed_at, e.g.
 *  '2026-07-14T02:35:15.946949+00:00'), which already carries an explicit
 *  offset. Only the former needs a 'T' and trailing 'Z' bolted on to parse as
 *  UTC — doing that unconditionally to the latter produces an unparsable
 *  '...+00:00Z' (Invalid Date), which is why renamed_at silently failed to
 *  render for applied/reverted rows. Returns '' for an empty/malformed
 *  timestamp so callers render nothing instead of "NaNd ago".
 *
 *  Lives here rather than under components/pipeline because the downloads
 *  views need the same UTC handling; pipelineDisplay re-exports it so its
 *  existing importers and tests are unaffected. */
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

/** Absolute rendering of the same two timestamp formats, in the viewer's local
 *  zone, for the tooltip behind a relative label. '' for empty/malformed. */
export function exactTime(sqliteTs: string): string {
  if (!sqliteTs) return '';
  const hasOffset = /(Z|[+-]\d{2}:\d{2})$/.test(sqliteTs);
  const dt = hasOffset ? new Date(sqliteTs) : new Date(sqliteTs.replace(' ', 'T') + 'Z');
  if (Number.isNaN(dt.getTime())) return '';
  return dt.toLocaleString();
}
