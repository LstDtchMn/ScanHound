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

/** Every season marker the name carries, in both spellings, scanned together.
 *
 *  Fresh literals on each call deliberately: a shared `/g` RegExp carries
 *  `lastIndex`, and while `matchAll` clones rather than advancing it, a reader
 *  should not have to know that to trust the token count. */
function seasonTokens(n: string) {
  return {
    sxx: [...n.matchAll(/\bS(\d{1,2})(?:\s*E(\d{1,3}))?\b/gi)],
    words: [...n.matchAll(/\bSeason\s*(\d{1,2})\b/gi)]
  };
}

/** A continuation that introduces a SECOND unit right after a parsed token:
 *  `S01-03`, `Season 1 & 2`, `S01 to 03`, `Season 1 / 2`, `S01+02`, `S01, 02`.
 *
 *  Not `/g` — `.test()` on a global RegExp advances `lastIndex` and would
 *  alternate between true and false on identical input. */
const CONTINUES_INTO_ANOTHER_UNIT = /^\s*(?:[-–—&+/,]|to\b)\s*(?:[SE]\s*)?\d/i;

/** RETIRED: `isCanonicalSeasonName()` and its length cap, suffix grammar and
 *  separator denylist are GONE, along with ~180 lines of accumulated rules.
 *
 *  It was the permission boundary for cancelling downloads, decided by reading
 *  a JDownloader display name. Six review rounds tightened it and each one
 *  found the last still incomplete -- `Season 1 & 2`, then `S01 to 03`, then
 *  `1 and S02`, then `Foo 1 | S02`. The premise was the defect: no finite
 *  denylist turns a display string into semantic proof, and
 *  `Show 1 to 2 S03 [1080p]` defeats every possible list, because what
 *  precedes the season token there is a digit, not a separator.
 *
 *  Authorization now reads `semanticKey()` -- what the backend RECORDED,
 *  joined on proven provenance (PR #87). Deleted rather than kept as a
 *  'diagnostic', for the reason this codebase already states about the retired
 *  name-based link resolver: an unused name matcher is a loaded gun for the
 *  next caller who reaches for it (peer review round 7).
 *
 *  `seasonKey()` below SURVIVES, demoted to what it always safely was: a
 *  display hint that keeps S02, S04 and S07 on separate cards for rows the
 *  backend cannot identify. It has no authority over deletion. */

/** The season a package covers, parsed from the JD name — `''` when it carries
 *  no marker.
 *
 *  A GROUPING DISCRIMINATOR, NOT AN AUTHORIZATION. It decides which rows share
 *  a card when the backend could not identify them; `semanticKey` decides
 *  whether a destructive action may be offered. Reading a positive result here
 *  as proof of identity is exactly the bug that made `Season 1 & 2`
 *  cancellable against a real `S01`.
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

  // THE INVARIANT IT ACTUALLY HOLDS: return a key only when exactly one season
  // marker is recognised and nothing adjacent reads as a range. That is a good
  // grouping signal, NOT a proof of one content unit — an unrecognised spelling
  // still yields a confident-looking key. Safety comes from `semanticKey`,
  // which is why this may stay permissive.
  //
  // Both spellings are counted TOGETHER before anything is returned. Scanning
  // Sxx first and only falling back to "Season N" made "Season 1-S3" parse as
  // S03: the Sxx pass saw one marker with no range text after it and never
  // looked left at the "Season 1" (peer review 2026-08-17, round 2).
  const { sxx, words } = seasonTokens(n);
  if (sxx.length + words.length !== 1) return '';

  // A trailing range the token count cannot see, because the second endpoint
  // carries no season syntax of its own: "S01-03", "Season 1 & 2", "S01 to 03".
  const one = sxx.length === 1 ? sxx[0] : words[0];
  const after = n.slice((one.index ?? 0) + one[0].length);
  if (CONTINUES_INTO_ANOTHER_UNIT.test(after)) return '';

  if (sxx.length === 1) {
    const s = `S${one[1].padStart(2, '0')}`;
    return one[2] ? `${s}E${one[2].padStart(2, '0')}` : s;
  }
  return `S${one[1].padStart(2, '0')}`;
}

/** THE RECORDED IDENTITY, shaped for GROUPING: proven TV rows with a title and
 *  a season, carrying the year when it is known and an explicit `null` when it
 *  is not.
 *
 *  THE TITLE IS USED AS RECORDED, only trimmed. It is deliberately NOT passed
 *  through `normalizeTitle()`, which exists for fuzzy DISPLAY grouping and
 *  replaces everything outside `[a-z0-9\s]` with spaces. That is not injective:
 *  `進撃の巨人` and `鬼滅の刃` both reduce to the empty string, so with the same
 *  year and season they produced ONE identity and became mutually cancellable --
 *  the exact false positive the semantic wire was introduced to remove,
 *  reintroduced by borrowing a display normalizer for authority. `A+B` vs `A B`
 *  and `Room 2012` vs `Room` collapse the same way (peer review round 8).
 *
 *  Live data has no such collision today -- all 42 provenance-backed titles are
 *  distinct trimmed, and none is non-ASCII -- so this costs nothing and closes
 *  the mechanism before it can fire. Not case-folded either: no live pair
 *  differs only by case, and for a destructive boundary a false negative is the
 *  acceptable direction.
 *
 *  JSON-encoded rather than delimiter-joined, so a title containing the
 *  separator cannot forge a different tuple. */
export function semanticGroupKey(r: DownloadResult): string | null {
  if (r.identity_source !== 'provenance') return null;
  // MOVIES ARE NOW POSSIBLE, where the backend RECORDED the kind. The wire
  // used to declare `movie` and never emit it, because nothing stored whether
  // a grab was a film or a show. It does now (downloads.media_kind, written
  // from the scan source's own classification), so a proven movie can finally
  // carry an identity -- and the de-duplicate action, withheld from every film
  // until now, becomes possible for them.
  const kind = r.identity_kind;
  if (kind !== 'tv_season' && kind !== 'movie') return null;
  const title = (r.identity_title || '').trim();
  if (!title) return null;
  const season = r.identity_season;
  if (kind === 'tv_season') {
    // A TV identity is which SEASON; without it two seasons of one show are
    // indistinguishable.
    if (typeof season !== 'number' || !Number.isInteger(season)) return null;
  } else if (season !== null && season !== undefined) {
    // Recorded as a film yet carrying a season: contradictory. The backend
    // already refuses this, and refusing it here too means the UI cannot be
    // talked into it by a stale or hand-built payload.
    return null;
  }
  // EXPLICITLY ABSENT is not the same as MALFORMED. `null` is a real answer --
  // the backend records TV rows whose year it never captured -- and those get
  // the partial class below. Anything else non-integer (a string, a float, NaN,
  // or the key missing entirely) is a contract violation: #87 always emits
  // `identity_year`, so its absence means the row never went through the
  // annotator. Collapsing those into the same `null` bucket would let a
  // malformed row display-group with a genuinely year-less one. Neither can
  // authorise -- `semanticKey` demands an integer -- so this is exhaustiveness,
  // not a safety fix (peer review round 9).
  const year = r.identity_year;
  if (year !== null) {
    if (typeof year !== 'number' || !Number.isInteger(year)) return null;
  }
  // The KIND is part of the tuple, so a film and a show can never share an
  // identity even if their titles and years match exactly.
  return JSON.stringify(['sem', kind, title, year, kind === 'movie' ? null : season]);
}

/** THE AUTHORITATIVE IDENTITY — what may authorise cancelling other downloads.
 *
 *  The grouping identity PLUS a recorded year, which grouping tolerates as null
 *  and authorization does not. Without it `Battlestar Galactica (1978) S01` and
 *  `(2004) S01` are one identity, and the backend really does emit `tv_season`
 *  with no year -- one live row, `Frankie vs the Internet S01`, is in exactly
 *  that state.
 *
 *  SPLIT FROM GROUPING deliberately. Demoting a proven-but-year-less row all the
 *  way to name parsing let it share a display card with a genuinely unproven
 *  row, which made the claim "proven and unproven rows never share a card"
 *  false. It now groups on what the backend did record and is still refused the
 *  action (peer review round 8). Three intentional key classes result:
 *
 *      ["sem", title, 2017, 2]   proven, actionable
 *      ["sem", title, null, 2]   proven, grouped, NOT actionable
 *      legacy|...                unproven, display only
 *
 *  When the year is present this returns exactly the grouping key, so the two
 *  can never disagree about which rows belong together. */
export function semanticKey(r: DownloadResult): string | null {
  if (typeof r.identity_year !== 'number' || !Number.isInteger(r.identity_year)) return null;
  return semanticGroupKey(r);
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
  /** Whether every row in the group carries a PROVEN semantic identity from
   *  the backend — `identity_source='provenance'`, `identity_kind='tv_season'`,
   *  and a title, year and season. See `semanticKey`.
   *
   *  False for anything the backend could not prove, INCLUDING a row whose
   *  package name looks perfectly canonical. That is the point: the name is a
   *  display string and cannot carry the answer. One live name is recorded
   *  against 13 distinct seasons, and no denylist of range separators closes
   *  `Show 1 to 2 S03 [1080p]`, where the text before the season token is a
   *  digit rather than a separator.
   *
   *  EVERY row, not the first. The key IS the semantic identity, so a proven
   *  and an unproven row can never share a card — but reading the flag off
   *  `items[0]` would still be wrong if grouping ever changed, and `some()`
   *  would let one proven row vouch for the rest.
   *
   *  Requiring the YEAR closes the remake hole the parser documented and could
   *  not fix: `normalizeTitle` strips years, so
   *  `Battlestar Galactica (1978) S01` and `(2004) S01` collapsed to one
   *  identity. A `tv_season` row with no recorded year is therefore unproven
   *  here, and groups on what WAS recorded -- title and season with an
   *  explicit null year -- rather than falling back to the display name
   *  (peer review rounds 7 and 8). */
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
    // THE WIRE OUTRANKS THE STRING. A row the backend identified groups by that
    // identity; only a row it could not falls back to reading the display name.
    // The key shapes are disjoint, so a proven and an unproven row never share a
    // card — which is what stops a known row vouching for an unknown one.
    //
    // In the legacy half, `title` is resolved server-side and expected to be set
    // for real results; `|| r.name` is a defensive last resort that could leak a
    // tag like "[1080p]" into the key if it were ever falsy. The season comes
    // from `name` because `title` has it stripped.
    const key =
      semanticGroupKey(r) ?? `legacy|${normalizeTitle(r.title || r.name)}|${seasonKey(r.name)}`;
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
    const base = items[0].identity_title || items[0].title || items[0].name;
    // Every item in a group shares the key, so it shares the season too. Prefer
    // the RECORDED season over the parsed one for the same reason the key does.
    const semantic = semanticGroupKey(items[0]);
    const season = semantic
      ? `S${String(items[0].identity_season).padStart(2, '0')}`
      : seasonKey(items[0].name);
    // AUTHORIZATION IS THE WIRE, NOT THE NAME. Every row must carry a proven
    // semantic identity; because the key IS that identity, they are then
    // necessarily the same one. A name that merely looks canonical buys
    // nothing, which is the whole point of the change -- a finite denylist of
    // range separators can never turn a display string into semantic proof.
    // `Foo 1 | S02 [1080p]` satisfied the old grammar because `|` was not on
    // the list, and `Show 1 to 2 S03 [1080p]` satisfies it no matter what the
    // list contains (peer review round 7).
    const identityKnown = items.every((r) => semanticKey(r) !== null);
    groups.push({
      key,
      title: season ? `${base} · ${season}` : base,
      items,
      activeItems,
      isDuplicate: items.length > 1,
      best,
      // FAIL CLOSED. "Keep best" cancels every other active row, so it is
      // offered only where the BACKEND proved every row is the same release —
      // never because their names look alike. An unknown identity beside a
      // proven one cannot borrow it: the two land in different groups.
      //
      // seasonKey still splits unproven rows by their apparent season, so the
      // original bug stays fixed — S02, S04 and S07 render as separate cards
      // rather than one "10 duplicates" heading — but that grouping is a
      // display hint with no authority over deletion.
      canKeepBest: activeItems.length >= 2 && identityKnown,
      identityKnown
    });
  }
  return groups;
}
