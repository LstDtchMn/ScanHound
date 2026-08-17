import type { DownloadResult } from '$lib/api/types';

/** Lowercase, strip a trailing/embedded (YYYY) year and punctuation, collapse
 *  whitespace — mirrors the backend's title normalization for grouping
 *  (see `clean_string()` in backend/app_service.py). */
export function normalizeTitle(s: string): string {
  const normalized = (s || '')
    .toLowerCase()
    .replace(/\((?:19|20)\d{2}\)/g, ' ')
    .trim();

  // Strip standalone years, but only keep that result if something is left —
  // otherwise the whole title WAS the year (e.g. "1917", "2012", "1984") and
  // stripping it would collapse unrelated movies onto the same '' key.
  const yearStripped = normalized.replace(/\b(?:19|20)\d{2}\b/g, ' ').trim();
  const base = yearStripped ? yearStripped : normalized;

  return base
    .replace(/[^a-z0-9\s]/g, ' ')
    .replace(/\s+/g, ' ')
    .trim();
}

/** Rank a package's resolution parsed from its name (JD names carry "[4K]" etc.). */
export function resRank(name: string): number {
  const n = (name || '').toLowerCase();
  if (n.includes('4k') || n.includes('2160p')) return 4;
  if (n.includes('1080p')) return 3;
  if (n.includes('720p')) return 2;
  return 1;
}

/** The season a package covers, parsed from the JD name — `''` when it carries
 *  no marker.
 *
 *  WHY THIS EXISTS. `title` is resolved server-side and has the season STRIPPED:
 *  every season of a show arrives as `'The Repair Shop [1080p]'`. Grouping on
 *  title alone therefore collapsed S02, S04, S07, S08, S11 and S12 into one
 *  group captioned "10 duplicates" — six unrelated seasons the user was being
 *  invited to de-duplicate. Different seasons are not duplicates of each other.
 *
 *  Parsed from `name` as a LEGACY FALLBACK, not as the authority. The backend
 *  already receives and stores the season for ScanHound-originated grabs and
 *  builds the canonical package name from it; the right long-term fix is to
 *  carry that identity on the wire and use this only for rows that predate it
 *  or came from outside ScanHound (peer review 2026-08-17).
 *
 *  `resRank` parses the same field, but it is NOT an equivalent precedent: a
 *  wrong resolution picks the wrong "best" WITHIN an established group, while a
 *  wrong season decides whether two unrelated things are the same thing at all —
 *  and that answer authorises deletion.
 *
 *  Measured against 361 live rows: 61 carry the `S01` form, and NONE carry
 *  `S01E02`, `1x02`, or a bare `Season N` — `Season N` is matched anyway as a
 *  common release form. Rows with no marker at all (300 of 361, all older
 *  history) return `''`, which means UNKNOWN IDENTITY, not "no season" — see
 *  `identityKnown` on the group.
 */
export function seasonKey(name: string): string {
  const n = name || '';

  // THE INVARIANT: return a positive identity only when the name proves exactly
  // ONE content unit. A range or several markers cannot, and must not be
  // reduced to the first token — "Show S01-S03" is not season 1 and
  // "Show S01E01-E10" is not episode 1. Giving a whole-run package the same
  // identity as a single season lets one be cancelled against the other.
  //
  // Both spellings are counted TOGETHER before anything is returned. Scanning
  // Sxx first and only falling back to "Season N" made "Season 1-S3" parse as
  // S03: the Sxx pass saw one marker with no range text after it and never
  // looked left at the "Season 1" (peer review 2026-08-17, round 2).
  const sxx = [...n.matchAll(/\bS(\d{1,2})(?:\s*E(\d{1,3}))?\b/gi)];
  const words = [...n.matchAll(/\bSeason\s*(\d{1,2})\b/gi)];
  if (sxx.length + words.length !== 1) return '';

  if (sxx.length === 1) {
    const m = sxx[0];
    // A trailing range the token count cannot see: "S01-03", "S01E01-E10".
    if (/^\s*[-–—]\s*(?:[SE]\s*)?\d/i.test(n.slice((m.index ?? 0) + m[0].length))) return '';
    const s = `S${m[1].padStart(2, '0')}`;
    return m[2] ? `${s}E${m[2].padStart(2, '0')}` : s;
  }

  const w = words[0];
  if (/^\s*[-–—]\s*\d/.test(n.slice((w.index ?? 0) + w[0].length))) return ''; // Season 1-3
  return `S${w[1].padStart(2, '0')}`;
}

/** States that count as "in flight" — not yet a finished/historical row. */
const ACTIVE = new Set(['queued', 'downloading', 'extracting']);

/** True if a result is still in progress (queued/downloading/extracting) rather
 *  than a finished, failed, or otherwise historical row. */
export function isActive(r: DownloadResult): boolean {
  return ACTIVE.has(r.state);
}

export interface DownloadGroup {
  key: string;
  title: string;
  items: DownloadResult[];
  activeItems: DownloadResult[];
  isDuplicate: boolean;
  best: DownloadResult;
  canKeepBest: boolean;
  /** Whether the name proved exactly one SEASON TOKEN — no more than that.
   *
   *  False when the names carry no season marker, or carry a range/multiple
   *  markers. That is UNKNOWN IDENTITY — not proof of a movie, and not proof of
   *  the same season. 300 of 361 live rows are in that state, including
   *  fourteen identically-named `Law & Order; LA (2010) [1080p]` rows that may
   *  well be different seasons. Display may still group them; a destructive
   *  action may not act on them.
   *
   *  DELIBERATELY NOT called "same content unit", because it is weaker than
   *  that and the weaker reading is the safe one to hold. `normalizeTitle`
   *  strips the year, so two TV remakes sharing a title and a season number
   *  still collapse — `Battlestar Galactica (1978) S01` and
   *  `(2004) S01` both reduce to `battlestar galactica|S01` and would be
   *  actionable. That predates this field and is narrowed by it, not caused by
   *  it; closing it needs kind/year/season carried authoritatively from the
   *  backend rather than more display-string heuristics (peer review round 2). */
  identityKnown: boolean;
}

/** Group downloads by normalized title AND season. A group with >1 item is a
 *  duplicate group (covers both "same title, different releases" and "exact
 *  same package twice"). `best` is the highest-resolution then largest item
 *  among the ACTIVE (queued/downloading/extracting) rows when any exist — so a
 *  live re-grab is preferred over a higher-res but finished historical row —
 *  and falls back to ranking across all items when nothing is active.
 *  `canKeepBest` only offers the "keep best, cancel the rest" action when there
 *  are >=2 active rows to actually choose between.
 *
 *  THE SEASON IS PART OF THE KEY. Without it, every season of a show grouped
 *  together and the UI offered to cancel all but the "best" of them — which
 *  would have thrown away entire seasons the user deliberately queued. Two
 *  releases of the SAME season still group, which is the case the feature is
 *  actually for. */
export function groupDownloads(results: DownloadResult[]): DownloadGroup[] {
  const byKey = new Map<string, DownloadResult[]>();
  for (const r of results) {
    // `title` is resolved server-side and expected to always be set for real
    // results; the `|| r.name` fallback is a defensive last resort and could
    // in theory leak a resolution tag like "[1080p]" from a raw JD package
    // name into the grouping key if `title` were ever falsy.
    //
    // The season comes from `name` because `title` has it stripped. Joined with
    // a separator that normalizeTitle cannot produce, so "show" + "S02" can
    // never collide with a differently-split pair.
    const key = `${normalizeTitle(r.title || r.name)}|${seasonKey(r.name)}`;
    const arr = byKey.get(key);
    if (arr) arr.push(r);
    else byKey.set(key, [r]);
  }
  const groups: DownloadGroup[] = [];
  for (const [key, items] of byKey) {
    const activeItems = items.filter(isActive);
    const rankPool = activeItems.length ? activeItems : items;
    const best = [...rankPool].sort(
      (a, b) => resRank(b.name) - resRank(a.name) || (b.bytes_total || 0) - (a.bytes_total || 0)
    )[0];
    // Show the season in the heading. Splitting by season means a show's
    // seasons now render as separate cards, and without this they would all
    // read "The Repair Shop [1080p]" with nothing to tell them apart.
    const base = items[0].title || items[0].name;
    // Every item in a group shares the key, so it shares the season too.
    const season = seasonKey(items[0].name);
    const identityKnown = season !== '';
    groups.push({
      key,
      title: season ? `${base} · ${season}` : base,
      items,
      activeItems,
      isDuplicate: items.length > 1,
      best,
      // FAIL CLOSED. "Keep best" cancels every other active row, so it must be
      // offered only where the rows are provably the same content unit. An
      // absent season marker is unknown identity, not evidence of a movie —
      // two indistinguishable rows could be different seasons, and cancelling
      // one would discard content the owner deliberately queued.
      //
      // This does currently withhold the button from genuine movie duplicates
      // too, because DownloadResult carries nothing that separates "known
      // movie" from "TV row whose season we cannot read". Losing that
      // convenience is the cheaper mistake, and measured against live data it
      // costs nothing today: zero groups have >=2 active rows at all. Restore
      // it for movies when the wire carries an authoritative identity.
      canKeepBest: activeItems.length >= 2 && identityKnown,
      identityKnown
    });
  }
  return groups;
}
