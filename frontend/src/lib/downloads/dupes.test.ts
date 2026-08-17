import { describe, it, expect } from 'vitest';
import {
  normalizeTitle, resRank, groupDownloads, isActive, seasonKey, isCanonicalSeasonName
} from './dupes';
import type { DownloadResult } from '$lib/api/types';

function r(over: Partial<DownloadResult>): DownloadResult {
  return { id: 0, package_uuid: null, name: 'X [1080p]', title: 'X', host: 'rg.net', bytes_total: 100,
    bytes_loaded: 0, downloaded: 0, extraction: 'na', state: 'downloading', error: null, updated_at: '',
    ...over };
}

describe('normalizeTitle', () => {
  it('lowercases and strips year + punctuation', () => {
    expect(normalizeTitle('Killing Faith (2025)')).toBe('killing faith');
    expect(normalizeTitle('Dr. Quinn, Medicine Woman')).toBe('dr quinn medicine woman');
  });

  it('does not collapse a bare-year title to an empty string', () => {
    // Regression: the whole title IS a year (e.g. the movies "1917", "2012",
    // "1984", "2010") — stripping standalone years must not erase them, or
    // unrelated movies would all normalize to the same '' key.
    expect(normalizeTitle('1917')).not.toBe('');
    expect(normalizeTitle('2012')).not.toBe('');
    expect(normalizeTitle('1917')).not.toBe(normalizeTitle('2012'));
  });

  it('normalizes a title with no year at all', () => {
    expect(normalizeTitle('The Matrix')).toBe('the matrix');
  });
});

describe('resRank', () => {
  it('ranks 4K > 1080p > 720p > other', () => {
    expect(resRank('Foo [4K]')).toBeGreaterThan(resRank('Foo [1080p]'));
    expect(resRank('Foo [1080p]')).toBeGreaterThan(resRank('Foo [720p]'));
  });
});

describe('seasonKey', () => {
  it('parses the form the real data actually uses', () => {
    // 61 of 361 live rows carry this; none carried S01E02, 1x02 or "Season N"
    // at the time of the fix, but the latter is common enough to match.
    expect(seasonKey('The Repair Shop (2017) S02 [1080p]')).toBe('S02');
    expect(seasonKey('Breaking Bad (2008) S1 [4K]')).toBe('S01'); // padded
    expect(seasonKey('Some Show S03E07 [1080p]')).toBe('S03E07');
    expect(seasonKey('Some Show Season 4 [1080p]')).toBe('S04');
  });

  it('is empty for a movie, so movies keep grouping on title alone', () => {
    expect(seasonKey('Notting Hill (1999) [4K]')).toBe('');
    expect(seasonKey('Law & Order; LA (2010) [1080p]')).toBe('');
  });

  it('does not mistake ordinary title text for a season marker', () => {
    // A bare S needs digits immediately after it. These must not parse.
    expect(seasonKey('Se7en (1995) [4K]')).toBe('');
    expect(seasonKey('S.W.A.T. (2003) [1080p]')).toBe('');
  });

  it('rejects a range whose second endpoint carries no season syntax', () => {
    // The token count cannot see these — only one marker is recognisable, so
    // the adjacent text has to be read. Separators beyond the dash were the
    // round-3 finding.
    expect(seasonKey('Some Show Season 1 & 2 [1080p]')).toBe('');
    expect(seasonKey('Some Show S01 & 02 [1080p]')).toBe('');
    expect(seasonKey('Some Show Season 1 to 3 [1080p]')).toBe('');
    expect(seasonKey('Some Show S01 to 03 [1080p]')).toBe('');
    expect(seasonKey('Some Show Season 1 / 2 [1080p]')).toBe('');
    expect(seasonKey('Some Show S01+02 [1080p]')).toBe('');
    expect(seasonKey('Some Show S01, 02 [1080p]')).toBe('');
    expect(seasonKey('Some Show S01-03 [1080p]')).toBe('');
  });

  it('does not read an ordinary word starting with "to" as a range', () => {
    // `to\b` must not fire on "Tokyo". If it did, the separator list would be
    // quietly eating real single-season names.
    expect(seasonKey('Some Show S01 Tokyo Cut [1080p]')).toBe('S01');
  });
});

describe('isCanonicalSeasonName — the authorization grammar', () => {
  // seasonKey answers "can I find a season token?" and is allowed to be
  // permissive because it only decides which card a row lands on. THIS answers
  // "is this the shape ScanHound itself emits?", and it gates cancellation.
  // Enumerating range spellings is an endless negative list; a narrow positive
  // grammar fails closed on syntax nobody has seen yet (peer review round 3).

  it('accepts the canonical form the backend builds', () => {
    // compute_package_name(): "Title (YYYY) SNN [resolution]".
    expect(isCanonicalSeasonName('The Repair Shop (2017) S02 [1080p]')).toBe(true);
    expect(isCanonicalSeasonName('Breaking Bad (2008) S01 [4K]')).toBe(true);
    // The bracket is optional because the PRODUCER's is: compute_package_name
    // appends [resolution] only when a resolution is known.
    expect(isCanonicalSeasonName('Some Show S04')).toBe(true);
  });

  it('accepts ONLY what compute_package_name can actually emit', () => {
    // The backend writes f" S{season:02d}" and never appends an episode. Both
    // of these name one unit and neither is unsafe, but accepting them made the
    // trust argument ("this is the shape we emit") false -- and that argument
    // is the entire reason this predicate may authorise deletion.
    //
    // Measured before narrowing: across 361 download_results names and 430
    // package names, all 218 season markers are zero-padded SNN, and none is
    // single-digit or episode-level. This rejects nothing that exists.
    expect(isCanonicalSeasonName('Breaking Bad (2008) S1 [4K]')).toBe(false);
    expect(isCanonicalSeasonName('Some Show S03E07 [1080p]')).toBe(false);
    // ...and both still GROUP, which is the half that stays permissive.
    expect(seasonKey('Breaking Bad (2008) S1 [4K]')).toBe('S01');
    expect(seasonKey('Some Show S03E07 [1080p]')).toBe('S03E07');
  });

  it('rejects every range form, including ones seasonKey also catches', () => {
    for (const n of [
      'Some Show Season 1 & 2 [1080p]',
      'Some Show S01 & 02 [1080p]',
      'Some Show Season 1 to 3 [1080p]',
      'Some Show S01 to 03 [1080p]',
      'Some Show Season 1 / 2 [1080p]',
      'Some Show S01+02 [1080p]',
      'Some Show S01-03 [1080p]',
      'Some Show S01-S03 [1080p]',
      'Some Show S01 - S03 [1080p]',
      'Some Show Season 1-S3 [1080p]'
    ]) {
      expect(isCanonicalSeasonName(n), n).toBe(false);
    }
  });

  it('rejects a season token that is not where the canonical form puts it', () => {
    // The anchor is the point: the token must be LAST, before an optional
    // bracket. Anything structurally more complicated is unknown.
    expect(isCanonicalSeasonName('Some Show S01 Extended Cut [1080p]')).toBe(false);
    expect(isCanonicalSeasonName('Some Show (2017) S02 [1080p] (2)')).toBe(false);
  });

  it('rejects a token preceded by a separator rather than the title or year', () => {
    // `1 & S02` ends in a legal-looking suffix and carries exactly one Sxx
    // token, so neither the anchor nor the token count rejects it alone.
    expect(isCanonicalSeasonName('Some Show 1 & S02 [1080p]')).toBe(false);
    expect(isCanonicalSeasonName('Some Show 1 - S02 [1080p]')).toBe(false);
    expect(isCanonicalSeasonName('Some Show 1 to S02 [1080p]')).toBe(false);
    expect(isCanonicalSeasonName('Some Show 1, S02 [1080p]')).toBe(false);
  });

  it('accepts a title ending in punctuation even with no year', () => {
    // The separator check is a DENYLIST for exactly this reason. An allowlist
    // of preceding characters (`[\w)\]]`) rejected these, and compute_package_
    // name really does emit them: a title ending in '!' or '?' with year=None
    // puts that character immediately before the season token.
    expect(isCanonicalSeasonName('Weird But True! S02 [1080p]')).toBe(true);
    expect(isCanonicalSeasonName('Whose Line Is It Anyway? S05 [1080p]')).toBe(true);
    expect(isCanonicalSeasonName('Malcolm in the Middle... S03')).toBe(true);
    // The year form was never affected — ')' was in the old allowlist.
    expect(isCanonicalSeasonName('Weird But True! (2017) S02 [1080p]')).toBe(true);
  });

  it('rejects a SECOND season token that the suffix alone cannot see', () => {
    // THE test for the token-count guard, which the suffix tightening made
    // look redundant. It is not: the character before ` S03` here is the digit
    // '1', so the suffix matches and only the token count refuses.
    // Deleting the guard passed the entire suite before this existed.
    expect(/\sS(\d{2})(?:\s*\[[^\]]{0,40}\])?\s*$/i.test('Season 1 S03 [1080p]')).toBe(true);
    expect(isCanonicalSeasonName('Season 1 S03 [1080p]')).toBe(false);
    expect(isCanonicalSeasonName('Season 1 S03')).toBe(false);
  });

  it('does not backtrack quadratically on a hostile name', () => {
    // `name` comes from JDownloader, not from us, and this runs per row on
    // every render. An unbounded [^\]]* before an end anchor rescans to
    // end-of-string from every candidate start: measured 4x per doubling
    // (14 KB 13 ms -> 28 KB 53 ms -> 56 KB 206 ms) before the bound.
    //
    // A DELIBERATELY HUGE input against a DELIBERATELY LOOSE budget. A tight
    // budget on a small input is a flaky test — wall-clock assertions drift
    // under parallel suite load. At 1 MB the old behaviour extrapolates to
    // well over a minute while the windowed version is a slice plus one short
    // match, so the two are separated by orders of magnitude and no plausible
    // machine load can blur them.
    const hostile = 'a S01 ['.repeat(150000); // ~1 MB
    const started = performance.now();
    expect(isCanonicalSeasonName(hostile)).toBe(false);
    expect(performance.now() - started).toBeLessThan(2000);
  });

  it('rejects a spelling ScanHound never emits, even when unambiguous', () => {
    // The accepted cost of a narrow grammar. "Season 4" is one clear season,
    // but it is not our form, so it groups without authorising.
    expect(seasonKey('Some Show Season 4 [1080p]')).toBe('S04');
    expect(isCanonicalSeasonName('Some Show Season 4 [1080p]')).toBe(false);
  });

  it('rejects a movie and junk input', () => {
    expect(isCanonicalSeasonName('Notting Hill (1999) [4K]')).toBe(false);
    expect(isCanonicalSeasonName('Se7en (1995) [4K]')).toBe(false);
    expect(isCanonicalSeasonName('')).toBe(false);
  });
});

describe('groupDownloads — seasons are not duplicates', () => {
  // The reported bug: "10 duplicates — The Repair Shop [1080p]" over six
  // distinct seasons. `title` has the season STRIPPED server-side, so grouping
  // on title alone collapsed them and offered to cancel all but one — which
  // would have discarded entire seasons the user deliberately queued.
  const repairShop = (season: string, id: number) =>
    r({ id, title: 'The Repair Shop [1080p]',
        name: `The Repair Shop (2017) ${season} [1080p]`, state: 'queued' });

  it('does NOT flag different seasons of one show as duplicates', () => {
    const groups = groupDownloads([
      repairShop('S02', 1), repairShop('S04', 2), repairShop('S07', 3)
    ]);
    expect(groups).toHaveLength(3);
    expect(groups.every((g) => !g.isDuplicate)).toBe(true);
    expect(groups.every((g) => !g.canKeepBest)).toBe(true);
  });

  it('DOES still flag two releases of the SAME season', () => {
    // The case the feature exists for. Splitting by season must not break it.
    const groups = groupDownloads([
      repairShop('S02', 1),
      r({ id: 2, title: 'The Repair Shop [1080p]',
          name: 'The Repair Shop (2017) S02 [1080p]', state: 'queued', bytes_total: 999 })
    ]);
    expect(groups).toHaveLength(1);
    expect(groups[0].isDuplicate).toBe(true);
    expect(groups[0].canKeepBest).toBe(true);
    expect(groups[0].best.id).toBe(2); // larger of the two
  });

  it('leaves movies grouping on title alone', () => {
    // Negative control: no season marker anywhere, so behaviour is unchanged
    // from before the fix.
    const groups = groupDownloads([
      r({ id: 1, title: 'Notting Hill [4K]', name: 'Notting Hill (1999) [4K]', state: 'extracted' }),
      r({ id: 2, title: 'Notting Hill [4K]', name: 'Notting Hill (1999) [4K]', state: 'extracted' })
    ]);
    expect(groups).toHaveLength(1);
    expect(groups[0].isDuplicate).toBe(true);
  });

  it('names the season in the heading so split cards are distinguishable', () => {
    // Once seasons are separate groups they would otherwise all render as
    // "The Repair Shop [1080p]" with nothing to tell them apart.
    const groups = groupDownloads([repairShop('S02', 1), repairShop('S11', 2)]);
    expect(groups.map((g) => g.title).sort()).toEqual([
      'The Repair Shop [1080p] · S02',
      'The Repair Shop [1080p] · S11'
    ]);
  });

  it('does not put a season suffix on a movie heading', () => {
    const groups = groupDownloads([
      r({ id: 1, title: 'Notting Hill [4K]', name: 'Notting Hill (1999) [4K]' })
    ]);
    expect(groups[0].title).toBe('Notting Hill [4K]');
  });

  it('keeps two different shows apart even at the same season number', () => {
    const groups = groupDownloads([
      r({ id: 1, title: 'Workaholics [1080p]', name: 'Workaholics (2011) S02 [1080p]' }),
      r({ id: 2, title: 'Wellington Paranormal [1080p]', name: 'Wellington Paranormal (2018) S02 [1080p]' })
    ]);
    expect(groups).toHaveLength(2);
    expect(groups.every((g) => !g.isDuplicate)).toBe(true);
  });
});

describe('unknown identity must not authorise deletion', () => {
  // "Keep best" cancels every other active row. An absent season marker is
  // UNKNOWN identity, not proof of a movie — so it cannot be evidence that two
  // rows are the same thing. 300 of 361 live rows are in that state, including
  // fourteen identically-named Law & Order; LA rows that may be different
  // seasons (peer review 2026-08-17).
  const lawAndOrder = (id: number) =>
    r({ id, title: 'Law & Order; LA [1080p]',
        name: 'Law & Order; LA (2010) [1080p]', state: 'queued' });

  it('does NOT offer Keep best for two active rows of unknown season identity', () => {
    const g = groupDownloads([lawAndOrder(1), lawAndOrder(2)])[0];
    expect(g.identityKnown).toBe(false);
    expect(g.canKeepBest).toBe(false); // the safety boundary
    expect(g.isDuplicate).toBe(true);  // still SHOWN as a possible duplicate
  });

  // THE MIXED CARD. A previous version of these two tests paired a canonical
  // `S01` with a `Season 1 & 2` pack — a pair that CANNOT share a card, because
  // seasonKey returns '' for the pack and the '|' in the group key separates
  // them. `items.length === 2` never matched, the assertions sat inside an
  // `if`, and both `items[0]` and `some()` — the two fail-open mutations these
  // tests exist to catch — passed the whole suite.
  //
  // These names DO collide: seasonKey pads and is permissive, so each keys as
  // S01 beside the canonical row while failing isCanonicalSeasonName.
  const NONCANONICAL_S01 = [
    'Foo (2015) S1 [1080p]',       // single-digit — not the emitted form
    'Foo Season 1 [1080p]',        // a spelling we do not emit
    'Foo S01 Extended [1080p]',    // token present, but not as the suffix
    'Foo (2015) S01 [1080p] (2)'   // JD de-duplication suffix
  ];
  const CANONICAL_S01 = 'Foo (2015) S01 [1080p]';

  it('the mixed card is REACHABLE — these really do land in one group', () => {
    // The control for the two tests below. If this ever stops holding, they
    // silently stop testing anything, which is exactly what went wrong before.
    for (const other of NONCANONICAL_S01) {
      expect(seasonKey(other), other).toBe(seasonKey(CANONICAL_S01));
      expect(isCanonicalSeasonName(other), other).toBe(false);
      const groups = groupDownloads([
        r({ id: 1, title: 'Foo [1080p]', name: CANONICAL_S01, state: 'queued' }),
        r({ id: 2, title: 'Foo [1080p]', name: other, state: 'queued' })
      ]);
      expect(groups, other).toHaveLength(1);
      expect(groups[0].items, other).toHaveLength(2);
      expect(groups[0].activeItems, other).toHaveLength(2); // arity gate satisfied
    }
  });

  it('does NOT offer Keep best when a non-canonical row shares the card', () => {
    // With two ACTIVE rows the arity gate is satisfied, so canKeepBest is
    // decided by identityKnown alone — the axis this test names.
    for (const other of NONCANONICAL_S01) {
      const [g] = groupDownloads([
        r({ id: 1, title: 'Foo [1080p]', name: CANONICAL_S01, state: 'queued' }),
        r({ id: 2, title: 'Foo [1080p]', name: other, state: 'queued', bytes_total: 999 })
      ]);
      expect(g.identityKnown, other).toBe(false);
      expect(g.canKeepBest, other).toBe(false);
      expect(g.isDuplicate, other).toBe(true); // still SHOWN, just not actionable
    }
  });

  it('judges identity per ROW, so arrival order cannot change the answer', () => {
    // Kills the items[0] mutation specifically: with the non-canonical row
    // FIRST, reading items[0] gives false; with it second, true.
    for (const other of NONCANONICAL_S01) {
      const canonical = r({ id: 1, title: 'Foo [1080p]', name: CANONICAL_S01, state: 'queued' });
      const odd = r({ id: 2, title: 'Foo [1080p]', name: other, state: 'queued' });
      for (const order of [[canonical, odd], [odd, canonical]]) {
        const [g] = groupDownloads(order);
        expect(g.items, other).toHaveLength(2);
        expect(g.identityKnown, other).toBe(false);
        expect(g.canKeepBest, other).toBe(false);
      }
    }
  });

  it('DOES offer Keep best when the season is known and matches', () => {
    // The positive control. Without it, "never offer Keep best" would pass the
    // test above and silently kill the feature.
    const g = groupDownloads([
      r({ id: 1, title: 'The Repair Shop [1080p]',
          name: 'The Repair Shop (2017) S02 [1080p]', state: 'queued' }),
      r({ id: 2, title: 'The Repair Shop [1080p]',
          name: 'The Repair Shop (2017) S02 [1080p]', state: 'queued',
          bytes_total: 999 })
    ])[0];
    expect(g.identityKnown).toBe(true);
    expect(g.canKeepBest).toBe(true);
    expect(g.best.id).toBe(2);
  });

  it('a multi-season or episode-range package is UNKNOWN, not its first token', () => {
    // "Show S01-S03" is not season 1. Reducing it to the first marker would let
    // a whole-run package share an identity with a single season and be
    // cancelled against it.
    expect(seasonKey('Some Show S01-S03 [1080p]')).toBe('');
    expect(seasonKey('Some Show S01E01-E10 [1080p]')).toBe('');
    expect(seasonKey('Some Show Season 1-3 [1080p]')).toBe('');
    expect(seasonKey('Some Show S01 S02 [1080p]')).toBe('');
  });

  it('a MIXED-spelling range is unknown too', () => {
    // The Sxx scan runs before the "Season N" scan, so "Season 1-S3" found one
    // marker (S3) with no range text after it and returned S03 — giving a
    // whole-run package the same actionable identity as a real S03 release.
    expect(seasonKey('Some Show Season 1-S3 [1080p]')).toBe('');
    expect(seasonKey('Some Show Season 1 - S03 [1080p]')).toBe('');
    expect(seasonKey('Some Show S1-Season 3 [1080p]')).toBe('');
  });

  it('a mixed-range package never becomes actionable against a real season', () => {
    const groups = groupDownloads([
      r({ id: 1, title: 'Some Show [1080p]', name: 'Some Show S03 [1080p]', state: 'queued' }),
      r({ id: 2, title: 'Some Show [1080p]', name: 'Some Show Season 1-S3 [1080p]', state: 'queued' })
    ]);
    expect(groups).toHaveLength(2);
    expect(groups.every((g) => !g.canKeepBest)).toBe(true);
  });

  it('a range package does not group with the single season it starts at', () => {
    const groups = groupDownloads([
      r({ id: 1, title: 'Some Show [1080p]', name: 'Some Show S01 [1080p]', state: 'queued' }),
      r({ id: 2, title: 'Some Show [1080p]', name: 'Some Show S01-S03 [1080p]', state: 'queued' })
    ]);
    expect(groups).toHaveLength(2);
    expect(groups.every((g) => !g.canKeepBest)).toBe(true);
  });

  it('still ranks a best within an unknown-identity group, just cannot act on it', () => {
    // `best` remains meaningful for display; only the destructive offer is
    // withheld. Asserting this keeps the guard from being "quietly break the
    // whole group".
    // Same title on purpose: the server-resolved title carries the resolution
    // ('Foo [4K]' vs 'Foo [1080p]'), so differing resolutions already land in
    // separate groups before any of this. Size is what separates these two.
    const g = groupDownloads([
      r({ id: 1, title: 'Foo [1080p]', name: 'Foo (2001) [1080p]', bytes_total: 10, state: 'queued' }),
      r({ id: 2, title: 'Foo [1080p]', name: 'Foo (2001) [1080p]', bytes_total: 40, state: 'queued' })
    ])[0];
    expect(g.items).toHaveLength(2);
    expect(g.best.id).toBe(2);        // still ranked
    expect(g.canKeepBest).toBe(false); // but not actionable
  });
});

describe('groupDownloads', () => {
  it('groups same-title releases and flags duplicates, picking best', () => {
    const items = [
      r({ name: 'Heat (1995) [1080p]', title: 'Heat', bytes_total: 10 }),
      r({ name: 'Heat (1995) [4K]', title: 'Heat', bytes_total: 40 }),
      r({ name: 'Solo (2018) [1080p]', title: 'Solo' }),
    ];
    const groups = groupDownloads(items);
    const heat = groups.find((g) => g.title === 'Heat')!;
    expect(heat.items).toHaveLength(2);
    expect(heat.isDuplicate).toBe(true);
    expect(heat.best.name).toBe('Heat (1995) [4K]');   // higher res wins
    const solo = groups.find((g) => g.title === 'Solo')!;
    expect(solo.isDuplicate).toBe(false);
  });

  it('flags exact-same-name packages as duplicate', () => {
    const items = [r({ name: 'Foo [1080p]', title: 'Foo' }), r({ name: 'Foo [1080p]', title: 'Foo' })];
    const g = groupDownloads(items)[0];
    expect(g.isDuplicate).toBe(true);
    expect(g.items).toHaveLength(2);
  });

  it('breaks ties by size when resRank is equal', () => {
    const items = [
      r({ name: 'Foo (2020) [1080p]', title: 'Foo', bytes_total: 500 }),
      r({ name: 'Foo (2020) [1080p]', title: 'Foo', bytes_total: 2000 }),
    ];
    const g = groupDownloads(items)[0];
    expect(g.best.bytes_total).toBe(2000);
  });

  it('best is chosen among ACTIVE rows, not a finished historical row', () => {
    const g = groupDownloads([
      r({ id: 1, title: 'Foo', name: 'Foo.2160p', state: 'finished' }), // historical, higher res
      r({ id: 2, title: 'Foo', name: 'Foo.1080p', state: 'downloading' }) // live re-grab
    ])[0];
    expect(g.best.id).toBe(2); // the live one, NOT the finished 2160p
    expect(g.canKeepBest).toBe(false); // only 1 active row → not offered
  });

  it('canKeepBest true only with >=2 active rows', () => {
    // Names carry the BRACKETED canonical form because Keep-best additionally
    // requires a known identity — see the fail-closed block below. The brackets
    // are not decoration: all 61 live rows with a season marker have them, and
    // accepting a bare trailing token would make "Foo S01 02" canonical, which
    // is the range bug this axis is not supposed to be testing.
    const g = groupDownloads([
      r({ id: 1, title: 'Foo', name: 'Foo S01 [2160p]', state: 'downloading' }),
      r({ id: 2, title: 'Foo', name: 'Foo S01 [1080p]', state: 'downloading' })
    ])[0];
    expect(g.canKeepBest).toBe(true);
    expect(g.best.id).toBe(1);
  });
});

describe('isActive', () => {
  it('treats queued/downloading/extracting as active', () => {
    expect(isActive(r({ state: 'queued' }))).toBe(true);
    expect(isActive(r({ state: 'downloading' }))).toBe(true);
    expect(isActive(r({ state: 'extracting' }))).toBe(true);
  });

  it('treats downloaded/extracted/failed as inactive', () => {
    expect(isActive(r({ state: 'downloaded' }))).toBe(false);
    expect(isActive(r({ state: 'extracted' }))).toBe(false);
    expect(isActive(r({ state: 'failed' }))).toBe(false);
  });
});
