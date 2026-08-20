import { describe, it, expect } from 'vitest';
import {
  normalizeTitle, resRank, groupDownloads, isActive, seasonKey, semanticKey, semanticGroupKey
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
    // Shown as a duplicate on the NAME alone; ACTIONABLE only once the backend
    // has identified both, which is the split this branch now enforces.
    const groups = groupDownloads([
      repairShop('S02', 1),
      r({ id: 2, title: 'The Repair Shop [1080p]',
          name: 'The Repair Shop (2017) S02 [1080p]', state: 'queued', bytes_total: 999 })
    ]);
    expect(groups).toHaveLength(1);
    expect(groups[0].isDuplicate).toBe(true);
    expect(groups[0].best.id).toBe(2);       // larger of the two
    expect(groups[0].canKeepBest).toBe(false); // no recorded identity yet
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

  // NAME PARSING NO LONGER AUTHORISES ANYTHING. These four names all key as
  // S01 beside the canonical one -- seasonKey is permissive and pads -- and
  // under the retired parser the mixed card was the whole safety question.
  // Now none of them carries a backend identity, so none is actionable, and
  // neither is the canonical-looking one. That is the demotion, asserted.
  const LOOKALIKE_S01 = [
    'Foo (2015) S01 [1080p]',      // the canonical form itself -- still unproven
    'Foo (2015) S1 [1080p]',       // single-digit
    'Foo Season 1 [1080p]',        // a spelling ScanHound does not emit
    'Foo S01 Extended [1080p]',    // token present, not as the suffix
    'Foo 1 | S02 [1080p]'          // round 7: `|` was missing from the denylist
  ];

  it('groups lookalike names together for DISPLAY, as before', () => {
    // The control for the test below: if these stopped sharing a card, the
    // safety assertion would pass without exercising anything.
    for (const other of LOOKALIKE_S01.slice(1, 4)) {
      const groups = groupDownloads([
        r({ id: 1, title: 'Foo [1080p]', name: LOOKALIKE_S01[0], state: 'queued' }),
        r({ id: 2, title: 'Foo [1080p]', name: other, state: 'queued' })
      ]);
      expect(groups, other).toHaveLength(1);
      expect(groups[0].activeItems, other).toHaveLength(2); // arity gate satisfied
    }
  });

  it('never authorises on the NAME, however canonical it looks', () => {
    // THE load-bearing test for the demotion. Two rows whose names are the
    // exact form the backend emits, both active, and still not actionable --
    // because neither carries a proven identity. Under the retired parser this
    // was the case that DID authorise.
    const [g] = groupDownloads([
      r({ id: 1, title: 'Foo [1080p]', name: LOOKALIKE_S01[0], state: 'queued' }),
      r({ id: 2, title: 'Foo [1080p]', name: LOOKALIKE_S01[0], state: 'queued',
         bytes_total: 999 })
    ]);
    expect(g.activeItems).toHaveLength(2);
    expect(g.identityKnown).toBe(false);
    expect(g.canKeepBest).toBe(false);
    expect(g.isDuplicate).toBe(true); // still SHOWN, just not actionable
  });

  it('is not fooled by the separator the denylist never had', () => {
    // `Foo 1 | S02 [1080p]` satisfied the retired grammar because `|` was not
    // on the list. Adding it would have been another tactical patch; the
    // premise is what changed, so this now fails for a reason no separator
    // list can undo.
    const [g] = groupDownloads([
      r({ id: 1, title: 'Foo [1080p]', name: 'Foo (2015) S02 [1080p]', state: 'queued' }),
      r({ id: 2, title: 'Foo [1080p]', name: 'Foo 1 | S02 [1080p]', state: 'queued' })
    ]);
    expect(g.canKeepBest).toBe(false);
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
    // Rows carry a RECORDED identity, because a name no longer authorises
    // anything. This test is about the arity gate, so the identity half is
    // held constant and satisfied rather than being what is under test.
    const proven = { identity_source: 'provenance' as const,
                     identity_kind: 'tv_season' as const,
                     identity_title: 'Foo', identity_year: 2015, identity_season: 1 };
    const g = groupDownloads([
      r({ id: 1, title: 'Foo', name: 'Foo S01 [2160p]', state: 'downloading', ...proven }),
      r({ id: 2, title: 'Foo', name: 'Foo S01 [1080p]', state: 'downloading', ...proven })
    ])[0];
    expect(g.canKeepBest).toBe(true);
    expect(g.best.id).toBe(1);

    // ...and the SAME rows with one inactive are not offered it, which is the
    // axis this test is named for.
    const one = groupDownloads([
      r({ id: 1, title: 'Foo', name: 'Foo S01 [2160p]', state: 'downloading', ...proven }),
      r({ id: 2, title: 'Foo', name: 'Foo S01 [1080p]', state: 'finished', ...proven })
    ])[0];
    expect(one.identityKnown).toBe(true);   // identity is NOT what withholds it
    expect(one.canKeepBest).toBe(false);
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

describe('semantic identity outranks the name', () => {
  // PR #87 put on the wire what the grab RECORDED, joined on proven
  // provenance. That is the authority now; the package name is a display
  // string that provably cannot carry the answer (one live name is recorded
  // against 13 distinct seasons).
  const tv = (over: Partial<DownloadResult>): DownloadResult =>
    r({
      state: 'queued',
      identity_source: 'provenance',
      identity_kind: 'tv_season',
      identity_title: 'The Repair Shop',
      identity_year: 2017,
      identity_season: 2,
      ...over
    });

  it('same recorded identity, DIFFERENT release names -> actionable', () => {
    // The positive control for the whole feature. Without it, "never
    // authorise" would satisfy every safety test here and the button would be
    // silently dead.
    const g = groupDownloads([
      tv({ id: 1, name: 'The.Repair.Shop.S02.1080p.WEB-DL-ABC' }),
      tv({ id: 2, name: 'the repair shop s02 [1080p] different releaser', bytes_total: 999 })
    ]);
    expect(g).toHaveLength(1);
    expect(g[0].identityKnown).toBe(true);
    expect(g[0].canKeepBest).toBe(true);
    expect(g[0].best.id).toBe(2);
  });

  it('different recorded SEASONS stay apart even with identical names', () => {
    // Proves the backend is authoritative rather than decorative: the names
    // are byte-identical, so anything reading them would merge these.
    const same = 'The Repair Shop (2017) S02 [1080p]';
    const g = groupDownloads([
      tv({ id: 1, name: same, identity_season: 1 }),
      tv({ id: 2, name: same, identity_season: 2 })
    ]);
    expect(g).toHaveLength(2);
    expect(g.every((x) => !x.canKeepBest)).toBe(true);
  });

  it('same title and season, DIFFERENT year -> separate (the remake hole)', () => {
    // The gap the parser documented and could not close: normalizeTitle strips
    // years, so these collapsed to one identity and became mutually
    // cancellable. The wire carries the year, so they no longer do.
    const g = groupDownloads([
      tv({ id: 1, name: 'Battlestar Galactica (1978) S01 [1080p]',
           identity_title: 'Battlestar Galactica', identity_year: 1978, identity_season: 1 }),
      tv({ id: 2, name: 'Battlestar Galactica (2004) S01 [1080p]',
           identity_title: 'Battlestar Galactica', identity_year: 2004, identity_season: 1 })
    ]);
    expect(g).toHaveLength(2);
    expect(g.every((x) => !x.canKeepBest)).toBe(true);
  });

  it('a PROVEN row never vouches for an unproven one', () => {
    // They cannot even share a card: the key shapes are disjoint. That is what
    // makes "one row lends its identity to the others" unrepresentable rather
    // than merely guarded against.
    // Same NAME and same TITLE, so the legacy key would put them together --
    // only the identity separates them. Without matching the title this test
    // passed for the wrong reason: the two would have split on title alone,
    // and a mutant that ignored the wire entirely still satisfied it.
    const NAME = 'The Repair Shop (2017) S02 [1080p]';
    const TITLE = 'The Repair Shop [1080p]';
    const g = groupDownloads([
      tv({ id: 1, name: NAME, title: TITLE }),
      r({ id: 2, name: NAME, title: TITLE, state: 'queued' })   // no identity
    ]);
    expect(seasonKey(NAME)).toBe('S02');          // they WOULD collide legacily
    expect(g).toHaveLength(2);
    expect(g.find((x) => x.identityKnown)?.items).toHaveLength(1);
    expect(g.every((x) => !x.canKeepBest)).toBe(true);
  });

  it('refuses every incomplete part of the tuple', () => {
    // Each field is required for its own reason; none may be inferred.
    const base = { id: 1, name: 'The Repair Shop (2017) S02 [1080p]' };
    expect(semanticKey(tv(base))).not.toBeNull();   // control
    for (const [label, over] of [
      ['unproven',        { identity_source: 'unknown' as const }],
      ['not tv',          { identity_kind: 'unknown' as const }],
      ['movie',           { identity_kind: 'movie' as const }],
      ['no title',        { identity_title: '' }],
      ['whitespace title',{ identity_title: '   ' }],
      ['no season',       { identity_season: null }],
      ['no year',         { identity_year: null }]
    ] as const) {
      expect(semanticKey(tv({ ...base, ...over })), label).toBeNull();
    }
  });

  it('a tv_season row with no recorded year is NOT actionable', () => {
    // The backend does emit tv_season without a year. That is precisely the
    // shape where two remakes are indistinguishable, so it groups by display
    // and stays unactionable rather than authorising on title+season.
    const g = groupDownloads([
      tv({ id: 1, name: 'Some Show (2019) S02 [1080p]', identity_year: null }),
      tv({ id: 2, name: 'Some Show (2019) S02 [1080p]', identity_year: null })
    ]);
    expect(g[0].identityKnown).toBe(false);
    expect(g[0].canKeepBest).toBe(false);
  });

  it('unproven rows still split by apparent season, so the ORIGINAL bug stays fixed', () => {
    // The user-visible reason this branch exists: six seasons of one show must
    // not render as one card captioned "10 duplicates". Demoting the parser
    // must not undo that.
    const g = groupDownloads(['S02', 'S04', 'S07'].map((s, i) =>
      r({ id: i, title: 'The Repair Shop [1080p]', state: 'queued',
          name: `The Repair Shop (2017) ${s} [1080p]` })));
    expect(g).toHaveLength(3);
    expect(g.every((x) => !x.canKeepBest)).toBe(true);
  });
});

describe('the semantic key is an identity, not a display normalization', () => {
  const tv = (over: Partial<DownloadResult>): DownloadResult =>
    r({ state: 'queued', identity_source: 'provenance', identity_kind: 'tv_season',
        identity_title: 'Show', identity_year: 2020, identity_season: 1, ...over });

  it('does NOT collapse two different non-ASCII titles', () => {
    // THE round-8 finding. semanticKey used normalizeTitle(), which replaces
    // everything outside [a-z0-9 whitespace] with spaces -- so both of these
    // reduced to the EMPTY string and, sharing a year and season, produced one
    // identity: two unrelated shows mutually cancellable, which is precisely
    // what the wire was introduced to prevent.
    const a = tv({ id: 1, identity_title: '進撃の巨人' });
    const b = tv({ id: 2, identity_title: '鬼滅の刃' });
    expect(semanticKey(a)).not.toBeNull();
    expect(semanticKey(a)).not.toBe(semanticKey(b));
    const g = groupDownloads([a, b]);
    expect(g).toHaveLength(2);
    expect(g.every((x) => !x.canKeepBest)).toBe(true);
  });

  it('does NOT collapse titles that differ only in punctuation or an inline year', () => {
    // The same defect without any Unicode: the display normalizer strips
    // punctuation and standalone years, so these pairs were equal.
    for (const [x, y] of [['A+B', 'A B'], ['Room 2012', 'Room'], ['Se7en', 'Se en']]) {
      expect(semanticKey(tv({ identity_title: x })), `${x} vs ${y}`)
        .not.toBe(semanticKey(tv({ identity_title: y })));
    }
  });

  it('is structured data that round-trips the exact recorded title', () => {
    // The key is a TUPLE, not a joined string. A delimiter-joined key could not
    // actually collide here -- the two trailing components are numbers, so a
    // title containing the separator shifts the field count rather than forging
    // a match -- but a joined key cannot be read back, and its safety would
    // depend on that numeric-suffix accident continuing to hold. Asserting the
    // round trip pins the property that makes the separator irrelevant.
    const key = semanticKey(tv({ identity_title: 'A|B', identity_year: 2020,
                                 identity_season: 1 }));
    expect(JSON.parse(key as string)).toEqual(['sem', 'tv_season', 'A|B', 2020, 1]);
  });

  it('treats case and surrounding whitespace as recorded, except for trimming', () => {
    // Trimmed, deliberately not case-folded: no live pair differs only by case,
    // and at a destructive boundary a false negative is the safe direction.
    expect(semanticKey(tv({ identity_title: '  Show  ' }))).toBe(semanticKey(tv({})));
    expect(semanticKey(tv({ identity_title: 'show' }))).not.toBe(semanticKey(tv({})));
  });
});

describe('three intentional key classes', () => {
  const tv = (over: Partial<DownloadResult>): DownloadResult =>
    r({ state: 'queued', identity_source: 'provenance', identity_kind: 'tv_season',
        identity_title: 'The Repair Shop', identity_year: 2017, identity_season: 2,
        title: 'The Repair Shop [1080p]',
        name: 'The Repair Shop (2017) S02 [1080p]', ...over });

  it('a proven row with NO year groups semantically but is not actionable', () => {
    // Round-8 LOW. Such a row used to fall all the way back to name parsing and
    // could share a display card with a genuinely unproven row, which made the
    // "proven and unproven never share a card" claim false. It now groups on
    // what WAS recorded, and is still refused the action.
    const proven = tv({ id: 1, identity_year: null });
    const unproven = r({ id: 2, state: 'queued', title: 'The Repair Shop [1080p]',
                          name: 'The Repair Shop (2017) S02 [1080p]' });
    expect(semanticGroupKey(proven)).not.toBeNull();  // grouped semantically...
    expect(semanticKey(proven)).toBeNull();           // ...but not authorised
    const g = groupDownloads([proven, unproven]);
    expect(g, 'proven-no-year must not share a card with an unproven row').toHaveLength(2);
    expect(g.every((x) => !x.canKeepBest)).toBe(true);
  });

  it('keeps the three classes apart from each other', () => {
    const full = tv({ id: 1 });
    const noYear = tv({ id: 2, identity_year: null });
    const unproven = r({ id: 3, state: 'queued', title: 'The Repair Shop [1080p]',
                          name: 'The Repair Shop (2017) S02 [1080p]' });
    const g = groupDownloads([full, noYear, unproven]);
    expect(g).toHaveLength(3);
    expect(g.filter((x) => x.identityKnown)).toHaveLength(1); // only the full one
    expect(g.every((x) => !x.canKeepBest)).toBe(true);        // all singletons
  });

  it('two proven year-less rows of the SAME show still group, still refused', () => {
    // The grouping half has to actually work, or the split above would be
    // indistinguishable from leaving them in legacy.
    const g = groupDownloads([
      tv({ id: 1, identity_year: null, name: 'a.different.release.name' }),
      tv({ id: 2, identity_year: null, name: 'another.one.entirely' })
    ]);
    expect(g).toHaveLength(1);
    expect(g[0].items).toHaveLength(2);
    expect(g[0].identityKnown).toBe(false);
    expect(g[0].canKeepBest).toBe(false);
  });
});

describe('an absent year and a malformed one are different things', () => {
  const tv = (over: Partial<DownloadResult>): DownloadResult =>
    r({ state: 'queued', identity_source: 'provenance', identity_kind: 'tv_season',
        identity_title: 'Show', identity_year: 2020, identity_season: 1, ...over });

  it('null year is a real answer and keeps the partial semantic class', () => {
    const k = semanticGroupKey(tv({ identity_year: null }));
    expect(k).not.toBeNull();
    expect(JSON.parse(k as string)).toEqual(['sem', 'tv_season', 'Show', null, 1]);
    expect(semanticKey(tv({ identity_year: null }))).toBeNull();  // still refused
  });

  it('a malformed year gets NO semantic identity, not the null class', () => {
    // Round-9 LOW. Collapsing these into the same bucket as a genuinely
    // year-less row would let a malformed row display-group with it. Neither
    // can authorise -- semanticKey demands an integer -- so this is
    // exhaustiveness over the wire contract rather than a safety fix.
    for (const bad of ['2020', 2020.5, NaN, Infinity, true, undefined] as unknown[]) {
      const row = tv({ identity_year: bad as number });
      expect(semanticGroupKey(row), String(bad)).toBeNull();
      expect(semanticKey(row), String(bad)).toBeNull();
    }
  });

  it('a malformed-year row does not share a card with a year-less one', () => {
    const yearless = tv({ id: 1, identity_year: null, name: 'a.release' });
    const malformed = tv({ id: 2, identity_year: '2020' as unknown as number,
                            name: 'a.release' });
    const g = groupDownloads([yearless, malformed]);
    expect(g).toHaveLength(2);
    expect(g.every((x) => !x.canKeepBest)).toBe(true);
  });
});

describe('a recorded MOVIE can finally carry an identity', () => {
  // The de-duplicate action was withheld from every film, because nothing
  // recorded whether a download was a movie or a show and the backend refused
  // to guess. It records it now, so a proven movie gets an identity.
  const film = (over: Partial<DownloadResult>): DownloadResult =>
    r({
      state: 'queued',
      identity_source: 'provenance',
      identity_kind: 'movie',
      identity_title: 'Notting Hill',
      identity_year: 1999,
      identity_season: null,
      ...over
    });

  it('two releases of the same film ARE actionable', () => {
    const g = groupDownloads([
      film({ id: 1, name: 'Notting.Hill.1999.1080p.BluRay-ABC' }),
      film({ id: 2, name: 'notting hill (1999) [4K] other releaser', bytes_total: 999 })
    ]);
    expect(g).toHaveLength(1);
    expect(g[0].identityKnown).toBe(true);
    expect(g[0].canKeepBest).toBe(true);
    expect(g[0].best.id).toBe(2);
  });

  it('two REMAKES stay apart', () => {
    // The hole the year requirement exists to close, now on the movie side.
    const g = groupDownloads([
      film({ id: 1, identity_title: 'The Thing', identity_year: 1982 }),
      film({ id: 2, identity_title: 'The Thing', identity_year: 2011 })
    ]);
    expect(g).toHaveLength(2);
    expect(g.every((x) => !x.canKeepBest)).toBe(true);
  });

  it('a film and a SHOW of the same name never share an identity', () => {
    // The kind is part of the tuple precisely so this cannot happen.
    const a = film({ id: 1, identity_title: 'Fargo', identity_year: 1996 });
    const b = r({
      id: 2, state: 'queued', identity_source: 'provenance',
      identity_kind: 'tv_season', identity_title: 'Fargo',
      identity_year: 1996, identity_season: 1
    });
    expect(semanticKey(a)).not.toBe(semanticKey(b));
    expect(groupDownloads([a, b])).toHaveLength(2);
  });

  it('a movie with NO recorded year is not actionable', () => {
    const g = groupDownloads([
      film({ id: 1, identity_year: null }),
      film({ id: 2, identity_year: null })
    ]);
    expect(g[0].identityKnown).toBe(false);
    expect(g[0].canKeepBest).toBe(false);
  });

  it('a movie carrying a SEASON is refused as contradictory', () => {
    expect(semanticKey(film({ identity_season: 2 }))).toBeNull();
  });

  it('an UNRECORDED kind is still not a movie', () => {
    // Every row grabbed before media_kind existed. Unknown, not inferred.
    expect(semanticKey(film({ identity_kind: 'unknown' }))).toBeNull();
  });
});
