# Completion contract — re-attestation after review round 4 (R4-94-3)

**Date:** 2026-08-29 · **Author:** Claude · **Supersedes:** rev3.8
**Base:** `main @ 0a2751d`, **0 behind**

## Why rev3.8's frozen head moved

rev3.8 renewed the same commitment rev3.6 and rev3.7 made: "no further commits
to this branch until the R-7 sign-off. If anything must change, this document is
superseded and says so." The adversarial verifier confirmed the R4-94-2 fix is
real — its own 131-case fixpoint sweep drove the live `/scan/rescan-item` route
3–5× over all 128 seed shapes and passed at `1965399` while failing at
`c5a5ab4` — and then found **four remaining defects**, all inside the functions
this branch edits. The owner chose to close them here rather than split them
out. Fixing reported defects is the named exception; this document is the
"says so".

Two commits now sit on top of `c5a5ab4`.

## One of rev3.8's own claims was FALSE, and one was too narrow

Stated first, because rev3.8 is the document an R-7 sign-off would read.

> "two listings disagreeing about a release is not a route to trust, and
> **`_media_item_from_dict` already refused the same row**."

**False as a general claim.** Executed at `1965399`, `cached_media_type` — the
function `_media_item_from_dict` delegates that decision to — short-circuits on
a stored `media_type` without consulting `category_conflict`. All four of these
returned a routable verdict; none returned `'ambiguous'`:

| conflicted row | at `1965399` |
|---|---|
| `{category:'tv', conflict:True, is_tv:True}` | `('tv', True)` |
| `{category:'tv', conflict:True, is_tv:True, season:3}` | `('tv', True)` |
| `{category:'tv', conflict:True, is_tv:True, media_type:'tv', provisional:True}` | `('tv', True)` |
| `{category:'4k', conflict:True, media_type:'movie'}` | `('movie', True)` |

The sentence was true of the **one** row rev3.7's behaviour-change section was
written about — `{category:'tv', conflict:True, is_tv:False, season:None}`, a
legacy row with no other evidence — and rev3.8 generalised it without executing
it. See "What the code now does" below for the corrected statement.

> "Scope of rows affected: only rows recording `category_conflict`. This does
> not move on the first rescan alone — **as of this commit it does not move on
> any subsequent rescan either**, which is the point."

**Too narrow, and therefore misleading about the very property it claims.** It
is true for a row that already recorded the conflict when the rescan began. It
is false when the conflict is recorded **between** two rescans, which is the
ordinary case for the deployed corpus — see C1. The row does move, in the wrong
direction, on the rescan after the conflict is recorded.

---

## C1 (HIGH) — conflict suppression was ORDER-DEPENDENT

`cached_type_evidence` blanks the crawl route when a row records a
`category_conflict`. `cached_verdict_evidence` never consulted that flag. And a
stored **provisional** verdict is *by definition* that same route's answer —
"provisional means nothing above ROUTE spoke", its own docstring — so it
re-entered at ROUTE authority, unopposed, and **survived the suppression of the
exact route that produced it**.

Reproduced by the verifier through the real HTTP route, and re-reproduced here:

```
seed {category:'tv', is_tv:False}
rescan                              -> media_type 'tv', provisional, persisted
mark_scan_category_conflict([url])    the in-place blob write that exists
                                      precisely for rows a crawl SKIPS as
                                      already cached
rescan                              -> media_type 'tv', is_tv True,
                                       category_conflict True
```

`web_item_facts` sets `is_tv = (media_type == 'tv')` and `_match_against_plex`
branches on it (`scanner_service.py:1829/1851`), so the conflicted release **was
compared against the TV library**.

**The discriminating control is ORDER ALONE.** The identical final row with the
conflict recorded *before* any rescan gives `'ambiguous'`, stably. Every
conflict test on this branch before today seeded a row with no stored
`media_type`, so not one of them could see this.

**Pre-existing, not a regression.** The same probe at `c5a5ab4` gives `'tv'`
with provisional `False`. R4-94-2 improved the row and did not close it.

| | at `c5a5ab4` | at `1965399` | at this head |
|---|---|---|---|
| conflict recorded BEFORE | `'ambiguous'` prov True | `'ambiguous'` prov True | `'ambiguous'` prov True |
| conflict recorded AFTER | `'tv'` prov **False** | `'tv'` prov True | **`'ambiguous'` prov True** |

## C2 — the same hole in `cached_media_type`

The table at the top of this document. `cached_media_type` carries a stored
verdict verbatim, and had no conflict clause, so a conflicted row's provisional
verdict passed straight through the reader rev3.8 cited as the authority.

## C3 — the L3 invariant was enforced in one reader only

`_media_item_from_dict` carried `is_tv` verbatim while setting `media_type`
independently, so the invariant the route now enforces — `is_tv is (media_type
== 'tv')` — was violated in the sibling cache→item reader that R4-94-2's own
rationale cites as the authority for what a cached row means. Executed at
`1965399`:

```
{category:'',   is_tv:True, media_type:'ambiguous'} -> 'ambiguous' with is_tv True
{category:'4k', is_tv:True, media_type:'movie'}     -> 'movie'     with is_tv True
```

Both are exactly the contradiction R4-94-2 removed one route over. Impact is
limited today — the matcher re-derives via `web_item_facts`, and `rematch_cache`
persists only status/Plex fields, verified by reading its update loop — but
"limited today" is how the last three findings started, and an invariant should
hold in both readers or in neither.

## C4 — `category_attested` was dropped on the rescan path

`rescan_classification` returned `(category, category_conflict)` and the route
never set `details['category_attested']`, so `_create_media_item` wrote `False`
and the route persisted it.

That is destructive, not merely untidy. `attest_scan_categories` writes the flag
**only where the key is absent** (a one-time backfill as each release is next
observed), and `get_scan_category` reads its absence as NEVER CHECKED and
returns `None` — deliberately, so a pre-conflict-detection row cannot read as
positively unconflicted. Executed at `1965399`:

```
attest_scan_categories([url]) -> row category_attested True
get_scan_category(url)        -> 'tv'
POST /scan/rescan-item        -> row category_attested False
get_scan_category(url)        -> None
```

Fail-closed, so never a wrong *answer* — but a rescan silently withdrew the
server-owned media kind that authorises Keep-best, until some future crawl
happened to observe the release again. **Decided: carry it.** It is the same
kind of thing as the two facts beside it — a fact recorded *about the crawl*
that a rescan cannot re-observe — not a re-added verdict. The value R4-94-2
removed from this tuple was a derived OR over type signals; this is not that.
`rescan_classification` now returns
`(category, category_conflict, category_attested)`.

---

## What the code now does (the corrected statement rev3.8 owed)

One new named rule, `conflict_suppresses_stored_verdict()`, which is the
branch's two existing rules composed rather than a third one:

> **A conflict is evidence about the cross-listing ROUTE and nothing else, so it
> suppresses exactly what rests on the route.**

- a stored **provisional** `'tv'`/`'movie'` rests on nothing above ROUTE →
  suppressed, in `cached_verdict_evidence` *and* in `cached_media_type`;
- a stored **decided** verdict had TITLE-or-better behind it (a season token, a
  detail filename); two listings disagreeing about which category page carried a
  release says nothing about that → **survives**;
- a stored `'ambiguous'` is not a routable answer, so there is nothing to
  suppress — and re-deriving over it would let a conflicted row become *more*
  decided than the row itself recorded → **not suppressed**;
- a recorded **season** and a legacy row's recorded **is_tv** are TITLE and
  DETAIL evidence about the filename, not about the listing → **untouched**.

Deliberately *not* folded into `stored_media_type()`: that answers "is this row
current-format", which a conflict does not change. `is_tv` is still a shadow on
a conflicted current-format row and must still not re-enter as observation.

So rows 1 and 2 of the C2 table still resolve `'tv'`, and that is correct rather
than unfixed: neither rests on the route. Rows 3 and 4 now resolve
`'ambiguous'`.

`_media_item_from_dict` now derives `is_tv=(cached_type == 'tv')` — the same
rule as `web_item_facts`, `_process_posts`' worker and the rescan route. Nothing
is lost: on a legacy row the stored `is_tv` still reaches the verdict as DETAIL
evidence through `cached_type_evidence`, so a legacy `is_tv=True` row still
resolves `'tv'` and still yields `True`. The old `season is not None` fallback is
gone for the reason `web_item_facts` states: a season pack, a complete series and
a mini-series are all TV and none carries a numeric season.

### Behaviour changes, named

1. A conflicted row carrying a **provisional** stored verdict resolves
   `'ambiguous'` where it resolved that verdict — on the rescan route and in
   `rematch_cache`. This is the C1 fix; it makes the answer independent of when
   the conflict was recorded.

   **User-visible, and named because it is persisted.** `_match_against_plex`
   handles `'ambiguous'` explicitly: `ScanStatus.MEDIA_TYPE_UNRESOLVED`,
   `plex_info = "Media type unresolved"`, and `continue` — it does *not* fall
   through to the movie library. `rematch_cache` persists status and
   `plex_info`. So the next re-match after this deploy moves such a row to
   "Type unresolved — review" and keeps it there until a re-crawl clears the
   conflict. That is precisely the outcome rev3.7 named and rev3.8 claimed was
   already reached for these rows; a rescan cannot clear a conflict, because it
   re-reads a detail page and learns nothing about which listings carried the
   release. The remedy is a re-crawl, not a rescan.

   Bounded, from the sweep: this is the only direction any row moves, and only
   rows recording a conflict move at all.
2. A rescan of a **legacy** row with `is_tv=True` and `media_type='ambiguous'`
   or `'movie'` now reports `is_tv=False` from `_media_item_from_dict`, matching
   the `media_type` beside it. Nothing persists that field from this reader.
3. A rescan **preserves** `category_attested` instead of clearing it, so
   `get_scan_category` keeps answering for an attested clean row.

Nothing here weakens a refusal, and no row becomes *more* decided than it was.

---

## Blast radius, measured rather than argued

`tests/tools/r4_94_3_route_sweep.py`, committed with this change. It drives the
**real** `/scan/rescan-item` route over
`4 categories × is_tv × season × 6 stored verdicts × fresh-detail × attested`
= **384 row shapes**, each rescanned **twice**, in **two orders** — the conflict
already recorded when the first rescan runs, and the conflict recorded by the
production writer *between* the two rescans — so **768 sequences, 1536 route
steps**. It asserts four properties and exits nonzero if any fails.

| | at `1965399` | at this head |
|---|---|---|
| order-dependent shapes | **30** | **0** |
| attestation lost by a rescan | **384** | **0** |
| invariant violations (`is_tv` vs `media_type`) | 0 | 0 |
| non-fixpoint pre-arm sequences | 32 | 32 — *identical set* |

Steps whose verdict moved: **102 of 1536**, and **every one is a row recording a
conflict**. Only two move shapes exist, both toward refusal:

```
('movie', prov, is_tv False) -> ('ambiguous', prov, False)   n=56
('tv',    prov, is_tv True)  -> ('ambiguous', prov, False)   n=46
```

No row anywhere becomes *more* decided, and **no unconflicted row moves at
all**. The `1965399` baseline is committed as
`docs/reviews/evidence/2026-08-29-r4-94-3-route-sweep-1965399.json`, so the
comparison is re-runnable rather than a number in a document:

```
python tests/tools/r4_94_3_route_sweep.py \
    --baseline docs/reviews/evidence/2026-08-29-r4-94-3-route-sweep-1965399.json
```

The harness fails loudly on the old code: run at `1965399` behaviour (mutant
`M0`) it exits 1 with *"order-dependent shapes: 30; attestation lost: 384"*.

**The 32 non-fixpoint pre-arm sequences are unchanged by this commit** — the set
is identical at both heads. A stored *decided* `'movie'` contradicted by a fresh
detail season token resolves `'ambiguous'` with provisional `False`, that
`'ambiguous'` is persisted, and the next rescan then sees only the fresh TV
evidence. Pre-existing and out of scope; named here so it is not mistaken for
something this commit introduced.

---

## What was run (at this head, in a throwaway container, whole tree copied in)

| | |
|---|---|
| full suite | **6126 passed / 0 failed / 4 skipped** (16:51) — baseline at `1965399`, measured the same way in the same session: **6093 passed / 0 failed / 4 skipped** (17:00). **Delta +33, exactly the 33 new tests.** |
| `tests/tools/r4_94_1_mutation_check.py` | 26 mutants, **0 survivors**; baseline and restored both **314 passed** |
| `tests/tools/mutation_check.py` | all 10 DISCRIMINATE, 0 survived, exit 0 |
| `tests/tools/r4_94_3_route_sweep.py` | 384 shapes / 768 sequences / 1536 steps, **ALL PROPERTIES HOLD**, exit 0 (exit 1 under `M0`) |
| `scripts/r3_differential_harness.py` | `old=c17152976 new=<this head> cases=71 identical=40 differing=31`, every divergence matches the committed expected file, exit 0. Re-run at this head; it loads only `backend/release_grammar.py` and `backend/detail_scraper.py`, neither of which this commit touches. |
| `docs/feature-pack-review/qualification/scripts/selftest.py` | ALL SELFTESTS PASSED |
| `docs/feature-pack-review/qualification/SHA256SUMS` | 14 files, 0 mismatches, 0 missing |

### The mutants added for these four findings

`M0` is the whole finding set at once. With its four edits the tree **behaves
exactly as `1965399` does** — the R4-94-3 probe under `M0` prints the `1965399`
column of every table above, character for character. That makes "this
reproduces at the reviewed head" executable rather than asserted.

| mutant | result | killed by (the case named for it) |
|---|---|---|
| **M0** all four defects at once | **20 failed** | every C1/C2/C3/C4 case below |
| **M19** conflict does not suppress the stored verdict (**C1**) | **8 failed** | `TestOrderIsNoLongerAVariable::test_conflict_after_a_rescan_matches_conflict_before` (all three route seeds), `::test_repeated_rescans_do_not_walk_the_answer_back`, `::test_the_conflicted_release_stops_reaching_the_tv_library`, `test_cached_verdict_evidence_on_a_conflicted_row[provisional, flag_absent]` |
| **M20** conflicted stored verdict carried verbatim (**C2**) | **3 failed** | `test_cached_media_type_on_a_conflicted_row[provisional, flag_absent]`, `test_rev38_four_shapes` |
| **M21** reader carries `is_tv` verbatim (**C3**) | **7 failed** | `test_the_two_executed_shapes[both]`, `test_the_invariant_over_every_shape[rows 0,1,2,4]` |
| **M22** rescan drops the attestation (**C4**) | **3 failed** | `test_an_attested_row_survives_a_rescan`, `test_a_conflicted_row_stays_unverifiable`, `test_rescan_classification_returns_the_attestation` |
| M23 suppression also removes a DECIDED verdict | 3 failed | `test_cached_media_type_on_a_conflicted_row[decided]`, `test_cached_verdict_evidence_on_a_conflicted_row[decided]` |
| M24 suppression ignores the conflict | 2 failed | `test_an_unconflicted_row_is_untouched[provisional, flag_absent]` — `[decided]` correctly still PASSES |
| M25 conflict also suppresses the recorded season | 1 failed | `test_conflict_after_a_rescan_matches_conflict_before[season_decided]` |

`M23`, `M24` and `M25` exist because the C1/C2 assertions must not be
satisfiable by any rule that merely distrusts provisional verdicts, or that
treats a conflict as "refuse everything". Each is killed by a **control**, not
by a finding test. Every failure was inspected individually: each mutant fails
on the assertion named for it, with the expected values — not on a fixture
check and not on an unrelated collection error.

Line numbers for `M1`–`M18` were recomputed after this commit's edits by
locating each target line's exact `HEAD` text in the new file — mechanically,
not by eye. The harness prints the line it replaced, so a bad recomputation
shows as a wrong `was:` rather than as a result. All eighteen still kill, at
this head, with the recomputed numbers:

```
BASELINE  314 passed          M9   5 failed    M17  5 failed
M1   12 failed                M10 21 failed    M18 19 failed
M2   10 failed                M11 33 failed    M0  20 failed
M3    6 failed                M12 24 failed    M19  8 failed
M4   11 failed                M13  1 failed    M20  3 failed
M5    5 failed                M14 11 failed    M21  7 failed
M6    3 failed                M15 10 failed    M22  3 failed
M7a   8 failed                M16  3 failed    M23  3 failed
M7b   8 failed                                 M24  2 failed
M8   12 failed                                 M25  1 failed
RESTORED  314 passed
```

**26 mutants, 0 survivors.** `BASELINE` and `RESTORED` were both `314 passed` in
each of the two harness invocations this took (`M1`–`M10` were re-run
separately after a console-width truncation lost their rows from the first
log).

### New regression file

`tests/test_rescan_conflict_suppression_is_order_independent.py` — 33 tests.

- **C1, the matched pair, through the real route.** For each of four seeds,
  arm A rescans → records the conflict with the production writer → rescans,
  arm B records the conflict first → rescans twice, and the two arms must
  agree. Equality alone is not enough — two arms agreeing on a *wrong* answer
  satisfies it — so each case also pins the expected verdict by name, and a
  fixture check asserts arm A's first rescan reached a routable verdict,
  without which there is no stored verdict for the conflict to be recorded
  against and the case tests nothing.
- `season_decided` is the control that keeps the rule narrow: a recorded season
  decides the first rescan, and the conflict does not take it away.
- **C2, every provisional value** — `True`, absent, `False` — on a row that
  records a `media_type`, directly and through the route; plus the rev3.8 four
  shapes; plus an unconflicted arm, without which any rule that merely
  distrusts provisional verdicts would pass.
- **C3**, the two executed shapes with a fixture check that `media_type` does
  *not* move (so the `is_tv` assertion cannot pass for the wrong reason), the
  invariant over seven shapes, and a legacy positive control so "always answer
  False" cannot satisfy it.
- **C4**, attested → rescan → still attested and `get_scan_category` still
  answers; plus the negative control that a rescan does not *invent* an
  attestation.

## What is INHERITED from rev3.8 (NOT re-run)

- **R-1, R-4, R-5 rows** — unchanged files; covered by the full suite.
- **R-3 reference corpus and expected-divergence file** — committed definitions.
- **Live measurements**, **the review-round history**, **R-2b / reason-code enum
  / grab-time resolver measurement** (still open), and **I1 (guard precedence,
  deliberately unresolved)** — exactly as rev3.8 states.
- The R4-94-2 analysis of `hdencode_action_service`'s
  `auto_media_type_provisional` raise: that gate reads `hdencode_candidates`,
  not `background_scan_cache`, so this route still does not reach it.

Merged-is-not-deployed still applies: nothing here says anything about the
running container.

## The commitment, renewed — with the standing correction

No further commits to this branch until the R-7 sign-off. If anything must
change, this document is superseded and says so.

rev3.7 was written about a head whose *second* rescan it had never executed.
rev3.8 was written about a head whose *conflict-recorded-in-between* case it had
never executed. The pattern is the same both times: a claim about what a row
does was checked against **one** ordering of the operations and stated as
though it covered all of them. Any successor to this document must state which
orderings it executed.
