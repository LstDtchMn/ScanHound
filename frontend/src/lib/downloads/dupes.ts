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

/** ScanHound's own canonical season suffix, which `compute_package_name()`
 *  builds as `Title (YYYY) SNN [resolution]` — the season token last, before an
 *  optional bracketed tag, and preceded by the year or a title word rather than
 *  by a separator (so `Show 1 & S02` is not mistaken for it).
 *
 *  LITERALLY the producer's grammar, not a family that resembles it. The
 *  backend writes `f" S{season:02d}"` and never appends an episode, so this
 *  takes exactly two digits and no `Exx`. An earlier version allowed `S1` and
 *  `S03E07`; both are single-unit-shaped and neither was unsafe, but accepting
 *  them made the docstring's trust argument — "this is the shape we emit" —
 *  false, and that argument is the whole reason the predicate may authorise
 *  deletion (peer review round 4). Measured before narrowing: across 361
 *  download_results names and 430 package names, all 218 season markers are
 *  zero-padded `SNN` and NONE is single-digit or episode-level, so the
 *  tightening rejects nothing that exists.
 *
 *  The bracket stays optional because the producer's is: `compute_package_name`
 *  appends `[resolution]` only when a resolution is known.
 *
 *  Anchored at the END on purpose. `S01-S03 [1080p]` cannot satisfy it (nothing
 *  separates the range endpoint from the bracket the way the canonical form
 *  does), and `Season 1 & 2 [1080p]` has no `SNN` there at all.
 *
 *  The bracket payload is LENGTH-BOUNDED and the whole NAME is length-capped
 *  (below). An unbounded `[^\]]*` before an end anchor rescans to
 *  end-of-string from every candidate position, which is quadratic on a name
 *  that repeats `a S01 [`: measured 14 KB -> 13 ms, 28 KB -> 53 ms,
 *  56 KB -> 206 ms, i.e. 4x per doubling. `name` comes from JDownloader rather
 *  than from us, and this runs per row on every render. */
const CANONICAL_SEASON_SUFFIX = /\sS(\d{2})(?:\s*\[[^\]]{0,40}\])?\s*$/i;

/** The producer's OWN length contract. `compute_package_name()` returns at most
 *  50 characters on every path — it trims the title to fit the suffix, and its
 *  fallback branch truncates the whole string — so a longer name did not come
 *  from us and cannot be vouched for.
 *
 *  This replaced a "look at the last N characters" window, which was unsound in
 *  two ways the round-6 review named: slicing can cut away the very separator
 *  that disqualifies a name, and a window silently ACCEPTS names outside the
 *  contract instead of refusing them. Enforcing the real bound is both stricter
 *  and simpler, and it makes the regex input bounded, so the quadratic case
 *  cannot arise at all rather than being merely survivable.
 *
 *  Measured: all 437 `downloads.package_name` values are <= 50 (max exactly
 *  50). Four `download_results.name` rows exceed it, all hand-added scene names
 *  such as `The.World.Will.Tremble.2025...-SPHD`; the only one carrying a
 *  season token is `Wild.Kratts.S07E13E14...`, a multi-episode range that must
 *  never authorise and is already refused. So this rejects nothing that
 *  currently authorises. */
const MAX_PRODUCER_NAME = 50;

/** A continuation separator sitting immediately BEFORE the season token, which
 *  makes the token a range endpoint rather than the whole unit: `Show 1 & S02`.
 *
 *  A DENYLIST of separators, not an allowlist of title characters. The previous
 *  version required the preceding character to be `[\w)\]]`, which also
 *  rejected every title ending in punctuation when no year followed it —
 *  measured: `Weird But True! S02 [1080p]` and
 *  `Whose Line Is It Anyway? S05 [1080p]` are both producible by
 *  compute_package_name and were both refused, so those shows could never offer
 *  the action at all.
 *
 *  WORD separators count too, which round 6 caught: `Season 1 and S02`,
 *  `1 through S03`, `1 til S02`. Rejecting a title that genuinely ends in one
 *  of these words is a false NEGATIVE — it withholds the button — so the list
 *  errs long deliberately. */
const PRECEDED_BY_CONTINUATION =
  /(?:[-–—&+/,~]|\b(?:to|and|through|thru|til|till|until|plus|versus|vs))\s*$/i;

/** THE AUTHORIZATION PARSER. True only when the name is structurally ScanHound's
 *  own single-season form, and therefore proves ONE content unit.
 *
 *  WHY THIS IS SEPARATE FROM `seasonKey`. The two callers want opposite things.
 *  Grouping is a display decision and should read whatever it can — guessing
 *  wrong there splits or merges a card. "Keep best" CANCELS the other rows, and
 *  guessing wrong there destroys a season the owner queued. So this asks a
 *  narrow positive question ("is this the shape we emit?") rather than
 *  `seasonKey`'s permissive one ("can I find a season token anywhere?").
 *
 *  That direction matters more than any single separator. Enumerating range
 *  spellings is an endless negative list — `-`, `&`, `to`, `/`, `+`, `,`, and
 *  whatever a scene release invents next — and every one missed becomes a false
 *  positive that authorises deletion. A narrow positive grammar fails closed on
 *  syntax it has never seen (peer review 2026-08-17, round 3).
 *
 *  The cost is real and accepted: `Show Season 4 [1080p]`, `Show S1 [1080p]`
 *  and `Show S03E07 [1080p]` all name a perfectly clear single unit, but none
 *  is the form ScanHound emits, so none authorises. They still GROUP correctly
 *  — see `seasonKey`.
 *
 *  NOT semantic proof, and deliberately not documented as such. The separator
 *  denylist rejects `Show 1 & S02` and `Show 1 and S02`, but it cannot
 *  establish that the prefix is really a title: `Show 1 to 2 S03 [1080p]`
 *  satisfies the grammar because what precedes ` S03` is the digit 2, not a
 *  separator. A frontend suffix parser cannot close that; authoritative
 *  identity from the backend can, and is the follow-up (peer review round 4). */
export function isCanonicalSeasonName(name: string): boolean {
  const n = name || '';
  // The producer's own length contract, enforced FIRST. It refuses names we
  // cannot have written, and it bounds the regex input so the quadratic case
  // cannot arise. Checked before the match, never after — a window over the
  // tail would have silently accepted these instead.
  if (n.length > MAX_PRODUCER_NAME) return false;
  const m = CANONICAL_SEASON_SUFFIX.exec(n);
  if (!m) return false;
  if (PRECEDED_BY_CONTINUATION.test(n.slice(0, m.index))) return false;
  // STILL LOAD-BEARING after the suffix tightened, despite appearances:
  // `Season 1 S03 [1080p]` satisfies the suffix (the character before ` S03`
  // is the digit 1) and is rejected only here, by the second season token.
  const { sxx, words } = seasonTokens(n);
  return sxx.length === 1 && words.length === 0;
}

/** The season a package covers, parsed from the JD name — `''` when it carries
 *  no marker.
 *
 *  A GROUPING DISCRIMINATOR, NOT AN AUTHORIZATION. It decides which rows share
 *  a card; `isCanonicalSeasonName` decides whether a destructive action may be
 *  offered. Reading a positive result here as proof of identity is exactly the
 *  bug that made `Season 1 & 2` cancellable against a real `S01`.
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
  // still yields a confident-looking key. Safety comes from
  // `isCanonicalSeasonName`, which is why this may stay permissive.
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
  /** Whether EVERY row in the group carries ScanHound's own canonical season
   *  suffix — no more than that.
   *
   *  False when any name carries no season marker, a range, several markers, or
   *  a spelling we do not emit (`Season 4`). That is UNKNOWN IDENTITY — not
   *  proof of a movie, and not proof of the same season. 300 of 361 live rows
   *  are in that state, including fourteen identically-named
   *  `Law & Order; LA (2010) [1080p]` rows that may well be different seasons.
   *  Display may still group them; a destructive action may not act on them.
   *
   *  EVERY row, not the first, because a card CAN hold rows that disagree.
   *  `seasonKey` is permissive and pads, so all of these key as `S01` beside a
   *  canonical `Foo (2015) S01 [1080p]` while failing the narrow predicate:
   *
   *      Foo (2015) S1 [1080p]        single-digit — not the emitted form
   *      Foo Season 1 [1080p]         a spelling we do not emit
   *      Foo S01 Extended [1080p]     the token is not the suffix
   *      Foo (2015) S01 [1080p] (2)   a JD de-duplication suffix
   *
   *  Reading the flag off `items[0]` makes the answer depend on arrival order,
   *  and `some()` makes one canonical row vouch for the rest — either would
   *  authorise cancelling a package the gate never actually vetted.
   *
   *  An earlier version of this comment justified `every` with a canonical
   *  `S01` beside a `Season 1 & 2` pack. That pair CANNOT share a card:
   *  seasonKey returns `''` for the pack, so the `|` in the key separates them
   *  into two groups. The rule is right; the example was unreachable, and the
   *  tests built on it asserted nothing.
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
    // Deliberately NOT `season !== ''`. The key is a display discriminator and
    // is permissive by design; authorization asks the narrow question of every
    // row independently.
    const identityKnown = items.every((r) => isCanonicalSeasonName(r.name));
    groups.push({
      key,
      title: season ? `${base} · ${season}` : base,
      items,
      activeItems,
      isDuplicate: items.length > 1,
      best,
      // FAIL CLOSED. "Keep best" cancels every other active row, so it is
      // offered only where every name is ScanHound's canonical single-season
      // form. That is NOT the same as "provably the same content unit" — a
      // title+season match still cannot separate two TV remakes, as
      // `identityKnown` documents. It is the strongest claim a display string
      // supports, and everything weaker stays unknown: an absent, ranged, or
      // unfamiliar marker is unknown identity, not evidence of a movie — two
      // indistinguishable rows could be different seasons, and cancelling one
      // would discard content the owner deliberately queued.
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
