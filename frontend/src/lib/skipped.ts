export type SkippedItem = {
  url: string;
  title: string | null;
  dismissed_at: string | null;
};

/** Case-insensitive title substring filter; empty/whitespace query returns all.
 *  Items with a null title fall back to matching on their URL. */
export function filterSkipped(items: SkippedItem[], query: string): SkippedItem[] {
  const q = query.trim().toLowerCase();
  if (!q) return items;
  return items.filter((i) => (i.title ?? i.url).toLowerCase().includes(q));
}

/** Relative "skipped ..." label. null/empty -> "". < 60s -> "just now";
 *  < 60m -> "Nm ago"; < 24h -> "Nh ago"; < 30d -> "Nd ago"; else a locale date. */
export function relativeTime(iso: string | null, now: number): string {
  if (!iso) return '';
  const then = Date.parse(iso);
  if (Number.isNaN(then)) return '';
  const secs = Math.max(0, Math.floor((now - then) / 1000));
  if (secs < 60) return 'just now';
  const mins = Math.floor(secs / 60);
  if (mins < 60) return `${mins}m ago`;
  const hours = Math.floor(mins / 60);
  if (hours < 24) return `${hours}h ago`;
  const days = Math.floor(hours / 24);
  if (days < 30) return `${days}d ago`;
  return new Date(then).toLocaleDateString();
}
