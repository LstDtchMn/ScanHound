# Round 17 request — the traversal contract and the coverage evaluator

Built to your §7 ruling and §8 rules. `category_attested` is still never written,
and no caller sets `attest_coverage=True`.

## What exists now

```text
ScannerService._crawl_pages()  ->  TraversalReport    what was seen, in order
CoverageEvaluator              ->  CoverageProof      what that justifies
attestation writer             ->  does not exist yet
```

`backend/coverage.py` imports only `dataclasses`, `datetime` and `typing`. It has
no database handle, and a test asserts it. The crawler emits observations and a
test asserts the report carries no `covered` / `proof` / `frontier` / `attested`
field — deriving that is the evaluator's job.

## I got the sticky defence wrong first, and your example is what caught it

Worth leading with, because the first version would have shipped the exact bug it
was written to prevent.

My first cut refused on **listing-order inversions**. That handles a pinned post
in the middle of a page. Your counterexample puts it at the bottom:

```text
Aug 20, Aug 20, Aug 19, Jan 2024   <- sticky
```

Those dates **descend**. No inversion fires, and the walk adopts January 2024 —
months of coverage conjured from a single page. Which is `min(observed
posted_date)` by another route, i.e. precisely what you rejected.

The fix is corroboration: an anchor becomes the frontier only once a **later
anchor** confirms it by being no newer.

```text
sticky in the middle   the next anchor is newer -> inversion -> refuse
sticky at the end      nothing corroborates it -> held back, frontier stays
```

**The cost, stated plainly:** the deepest anchor of any traversal is never
claimed. The frontier is always one release short. That direction can only refuse
a proof we might have been entitled to, never grant one we were not — but it is a
real cost and I would rather you saw it than found it.

## The rules from §8, and where each lives

```text
8.1  no missing/failed page before the frontier   Page.usable, walk stops
8.2  parser recognition, not HTTP 200             parser_state, set by the crawler
8.3  actual listing order, never min()            the corroboration walk
8.4  repeats retained but cannot anchor           duplicate_in_run flagged, not dropped
8.5  equal-minute does not prove crossing         strictly-older comparison
8.6  unknown dates cannot anchor, do not block    _anchor_date returns None, walk continues
8.7  posted_date_changed cannot support coverage  `unstable` set, never an anchor
8.8  parser/evaluator version is part of the proof  carried on CoverageProof
```

Crossing is target-relative per §9: the question is always *did we get older than
R*, never *did we read N pages*.

## Mutation — the one you named as critical, first

```text
frontier = min(observed posted_date)     kills 8, including ALL THREE sticky tests
deepest anchor uncorroborated            kills 7
duplicate_in_run ignored                 kills 1
equal timestamps accepted as crossing    kills 1
unusable pages traversed anyway          kills 2
```

Your fixtures are all present: old sticky on page 1, repeated URL across pages,
equal-minute timestamps, unknown-date policy exclusion, HTTP-200 unparseable
page, page error before the frontier, and a clean multi-page monotonic traversal
as the positive control.

## A bug I introduced while wiring the emission

My edit script matched `types_covered: Set[str] = set()` as a **substring**, so it
also matched `self._last_crawl_types_covered: Set[str] = set()` and replaced the
`__init__` initialiser — inserting `id(sources)` into a scope with no `sources`.
That would have raised `NameError` on every `ScannerService` construction.

`ast.parse` said "syntax OK" and told me nothing; the broken version was valid
Python. What caught it was reading the line numbers the edit reported and noticing
they were in `__init__`. Recorded because the near-miss is more useful than the
fix.

## Three things I want challenged

1. **Is the corroboration rule sound, or merely conservative?** It is the whole
   sticky defence, and it rests on an assumption I should state: that a genuine
   listing is monotonically non-increasing in publication time, so a later anchor
   being no newer is evidence the earlier one was really part of the sequence. If
   a source ever interleaves — bumped posts, edited timestamps, multi-editor
   queues — that assumption fails quietly rather than loudly.

2. **An empty-but-valid listing is recorded as `unrecognised`.** I cannot
   distinguish "the parser broke" from "this page genuinely has no posts", so
   both fail closed. That is deliberate, but it means a source that legitimately
   ends mid-page can never prove coverage of its own tail.

3. **Where should proofs be persisted, and at what granularity?** You said to keep
   enough raw traversal evidence to reconstruct the decision for any run capable
   of minting attestation. At ~180 sightings per cycle that is cheap now, but a
   target-relative deep crawl would be much larger, and I have not estimated it.
   I would rather have your shape than invent a schema and have it be wrong in a
   way that is expensive to migrate later.

## Verification

```text
code head    89706b7

                              failed   passed   skipped
main control (origin/main)         1     5320         4
this branch                        1     5420         4
```

Same single pre-existing failure both sides. **+100 passing, zero net new
failures.** Host/container md5 parity asserted for the run.

## Not done

- persisting proofs
- any caller that sets `attest_coverage=True`
- writing `category_attested` — still nothing, anywhere
