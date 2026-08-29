# Completion contract — re-attestation after review round 4 (R4-94-2)

**Date:** 2026-08-28 · **Author:** Claude · **Supersedes:** rev3.7
**Base:** `main @ 0a2751d`, **0 behind**

## Why rev3.7's frozen head moved

rev3.7 renewed rev3.6's commitment: "no further commits to this branch until
the R-7 sign-off. If anything must change, this document is superseded and says
so." The adversarial verifier confirmed the R4-94-1 fix works — and then found
that the fix introduced a **feedback loop** one layer down. Fixing a reported
defect is the named exception; this document is the "says so".

One commit sits on top of `c5a5ab4`.

## Two of rev3.7's own claims were FALSE, and are withdrawn

Stated plainly before anything else, because rev3.7 is the document an R-7
sign-off would have read:

> "They now agree, in the refusing direction." … "**No other outcome moves for
> a row with no other evidence.**"

Both false past the **first** rescan. A second rescan of the same row, with
nothing new observed, moved the outcome — see R4-94-2 below. rev3.7's
behaviour-change section described only what the first click did.

## R4-94-2 (HIGH) — the route's own verdict re-entered as observation

`/scan/rescan-item` writes the legacy `is_tv` field from the verdict it just
reached and persists the whole object. `cached_type_evidence` then re-admitted
that same boolean at **DETAIL** authority on the next read. So the route's
output became the next run's input **one authority level above the evidence it
was built from**.

Shown through the real HTTP route, seed
`{category:'tv', is_tv:False, season:None}`, detail page carrying nothing:

| | at `c5a5ab4` | at this head |
|---|---|---|
| rescan 1 | `media_type='tv'`, provisional **True** | `'tv'`, provisional **True** |
| rescan 2 | `media_type='tv'`, provisional **False** | `'tv'`, provisional **True** |

This was a **regression introduced by R4-94-1**, not pre-existing: at
`c5a5ab4~1` both rescans returned provisional `True`.

**Why it matters — traced, and the reviewer's stated mechanism corrected.** The
finding says `media_type_provisional` is a live safety gate because
`backend/hdencode_action_service.py:505` raises `auto_media_type_provisional`.
The *field* is that gate, but it reads **`hdencode_candidates`**, fed by the RSS
feed parser and `hdencode_candidate_service._candidate_updates` — **not**
`background_scan_cache`, which is the table the rescan route writes. Traced end
to end: there is no path from this route to that raise on this head, so the
blast radius is not the action gate. What *is* demonstrated is the loop itself
and its second consequence below: a deliberately-suppressed conflict reversing
into a decided TV verdict changes which library the release is compared against,
with no gate involved. The doctrine — a verdict must not clear its own
provisional flag — is this branch's own and holds wherever the flag is read.

And the same loop reversed R4-94-1's one named behaviour change. Seed
`{category:'tv', category_conflict:True, is_tv:False, season:None}`:

| | at `c5a5ab4` | at this head |
|---|---|---|
| rescan 1 | `'ambiguous'`, provisional True — the intended refusal | same |
| rescan 2 | **`'tv'`, provisional False** | `'ambiguous'`, provisional True |

A conflict the branch deliberately suppresses became a decided TV verdict two
clicks later.

### The fix

**A cached `is_tv` is admitted as DETAIL evidence only on a LEGACY row.** A row
that records a `media_type` is current-format, and every writer of such a row
derives `is_tv` *from* that verdict — `web_item_facts`
(`'is_tv': item.media_type == 'tv'`), `_process_posts`' worker
(`is_tv = verdict.media_type is TV`) and, as of this commit, the rescan route.
On those rows the boolean is a **shadow**, and the row's decision is carried
instead by `cached_verdict_evidence`, at the authority its own
`media_type_provisional` flag records. Only a legacy row — main's `#93` rows,
which record `is_tv` and no `media_type` at all — carries an `is_tv` that is
genuine recovered observation, and there it keeps DETAIL authority.

`stored_media_type()` is the single reader of that distinction; three rules turn
on it and must not drift apart. `'ambiguous'` counts as **recorded** — it is the
row saying it decided nothing, which is still a decision this row made.

### The second half: the shadow is now a shadow

The route built `is_tv` as an OR of the verdict, the fresh detail flag and
`rescan_classification`'s carried boolean. That OR could contradict the verdict
sitting beside it: the conflicted row above resolved `'ambiguous'` — refusing to
decide its type — while the OR still asserted `is_tv=True` off the very route
the conflict suppresses, and both were persisted. The route now derives
`is_tv` from the verdict, the same rule the listing worker already used.

Nothing is lost, and this is checkable rather than asserted: each of the three
sources the OR conflated is carried into the verdict by `cached_type_evidence`
at its own authority — the crawl route at ROUTE, a recorded season at TITLE, a
legacy row's recorded `is_tv` at DETAIL — and a fresh detail `is_tv` is DETAIL
evidence there too. Every input that used to force `True` still resolves TV.
`rescan_classification` accordingly returns `(category, category_conflict)`; its
third value had no consumer left, and keeping a dead OR beside the verdict is
what allowed the contradiction.

## The behaviour change, named properly this time (rev3.7's L5)

rev3.7 said the change "returns `media_type='ambiguous'` where it returned
`'movie'`". That is the mild half. **For a conflicted row whose recorded
category is `'tv'`, the move is `tv → ambiguous`: a television release becomes
unroutable** — it is compared against neither library and shows as
"Type unresolved — review". That is the case an R-7 sign-off most needs to see,
and it is deliberate: two listings disagreeing about a release is not a route to
trust, and `_media_item_from_dict` already refused the same row. A rescan
re-reads a detail page; it learns nothing about which listings carried the
release, so it cannot clear the conflict either. The user-visible remedy is a
re-crawl, not a rescan.

Scope of rows affected: only rows recording `category_conflict`. This does not
move on the first rescan alone — as of this commit it does not move on any
subsequent rescan either, which is the point.

## Test-quality findings, both fixed

**L3 — the contradiction test was blind.**
`test_the_two_type_fields_never_contradict_each_other` asserted only
`not (item['is_tv'] and item['media_type'] == 'movie')`, which cannot see
`'ambiguous'`; and the two rows it ran on could not produce the case anyway. It
now asserts **equality** — `is_tv is (media_type == 'tv')`, on the returned item
*and* on the row read back out of the DB — over nine row shapes including both
conflicted ones.

**L4 — the restored round-trip pin did not discriminate.** rev3.7 claimed the
fixture's own heuristics "re-derive to movie". They did not: the fixture also
carried `is_tv=True`, which `cached_type_evidence` admitted, so the
`media_type` assertion passed on coincidental re-derivation and only the
`provisional` line discriminated. The old control flipped `is_tv` to `False` —
answering for a **different row**. Now: the control re-derives from the *exact
dict production serialised*, unedited, through the production functions (and
that answer is `'movie'` precisely because of the R4-94-2 rule above); and a
second fixture disagrees in the other direction — a `'tv'` crawl route and an
`S02` title carrying a recorded `'movie'` verdict — so the pin does not rest on
that one rule.

A third fixture defect surfaced while retargeting
`tests/test_rescan_preserves_classification.py` onto the real composition: every
row in it carried the title `"A Show S02"`. Harmless while the helper it drove
never read the title; once it drives the composition, that title is TITLE-
authority TV evidence and would have satisfied five tests by itself. The default
title is now neutral, and the film control — the only test there whose expected
answer was `False`, hence the only one the shared title could break rather than
silently satisfy — is what caught it.

## What was RUN today, at this head

Environment: host Windows, Python 3.12.9, pytest 9.0.2.

| Row | Evidence | Result today | rev3.7 said |
|---|---|---|---|
| R-3 | `scripts/r3_differential_harness.py` | **`old=c17152976 new=<this head> cases=71 identical=40 differing=31` · every divergence matches the committed expected file · exit 0** | same counts at `9ed50d6` |
| R-1/R-6 | `tests/tools/mutation_check.py` | **all 10 DISCRIMINATE · 0 survived · exit 0** | 10/10 |
| **R4-94-1/2** | `tests/tools/r4_94_1_mutation_check.py` | **19 mutants · 0 survived · baseline and restored both 281 passed** | 13 mutants, 0 survived |
| bundle | `qualification/scripts/selftest.py` | **ALL SELFTESTS PASSED** | pass |
| bundle | `SHA256SUMS` | **14 files, 0 mismatches, 0 missing** | 0 mismatches |

Full suite: **6099 passed / 0 failed / 5 skipped (19:56).** rev3.7 recorded
6085 passed with one failure — `test_dv_host_scan.py::test_post_rows_direct_
success_delivers_key`, which it called an ordering flake. It passed in this
run, which supports that reading without proving it. **Delta over rev3.7: +14
passing, 0 failing** (6 new tests, the contradiction test re-parametrised from
2 cases to 9, and rev3.7's one failure now passing).

### R4-94-2 mutation results

Six mutants are new; the thirteen inherited ones were re-run at this head after
their line numbers were re-pointed (every edit prints the line it replaced, and
all 23 printed lines match their intended target).

| Mutant | Result |
|---|---|
| baseline | 281 passed |
| M1 restore the R4-94-1 defect | 5 failed |
| M2 drop `cached-is-tv` evidence | 8 failed |
| M3 drop `cached-season` evidence | 5 failed |
| M4 drop `cached-category` evidence | 7 failed |
| M5 do not carry a stored verdict | 2 failed |
| M6 stored verdict always DETAIL authority | 3 failed |
| M7a stored `'ambiguous'` counts as MOVIE | 4 failed |
| M7b stored `'ambiguous'` counts as TV | 4 failed |
| M8 conflict no longer suppresses the route | 3 failed |
| M9 drop the fresh detail evidence | 4 failed |
| M10 answer TV unconditionally | 15 failed |
| M11 answer MOVIE unconditionally | 25 failed |
| M12 route does not persist the verdict | 16 failed |
| M13 drop the listing-title fallback | 1 failed |
| **M14 re-admit `is_tv` on current-format rows** | **4 failed** ← THE FINDING |
| M15 never admit a cached `is_tv` (the other side) | 8 failed |
| M16 `'ambiguous'` is not a recorded verdict | 1 failed |
| **M17 route ORs the legacy `is_tv` beside the verdict** | **2 failed** ← L3 |
| M18 route inverts the shadow | 13 failed |
| restored | 281 passed |

**0 survivors.** M14 is killed by both route-level loop tests, the stored-
`'ambiguous'` pin, **and** `TestItSurvivesTheCache::test_the_fixture_really_
does_disagree_with_the_heuristics` — which is the direct demonstration that
L4's control now discriminates and did not before. M17 is killed by the
contradiction test's `row7`: the conflicted `'tv'`-route row, the exact case
the old assertion form could not see.

## What is INHERITED from rev3.7 (NOT re-run)

- **R-1, R-4, R-5 rows** — unchanged files; covered by the full suite.
- **R-3 reference corpus and expected-divergence file** — committed definitions.
- **Live measurements**, **the review-round history**, **R-2b / reason-code enum
  / grab-time resolver measurement** (still open), and **I1 (guard precedence,
  deliberately unresolved)** — exactly as rev3.7 states.

Merged-is-not-deployed still applies: nothing here says anything about the
running container.

## The commitment, renewed

No further commits to this branch until the R-7 sign-off. If anything must
change, this document is superseded and says so — and rev3.7 is the reason that
sentence has to be taken literally: it was written about a head whose second
rescan it had never executed.
