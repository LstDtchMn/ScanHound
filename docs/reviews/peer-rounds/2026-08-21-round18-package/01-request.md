# Round 18 request — M17-1 through M17-4 closed

All four round-17 findings are addressed. `category_attested` is still never
written, no caller sets `attest_coverage=True`, and after M17-1 the evaluator
cannot authorise anything even if one did.

## M17-1 — I wrote a false claim, and you disproved it

Leading with this because the failure is more instructive than the fix. My
docstring said being one anchor shallow

> can only ever refuse a proof we might have been entitled to, never grant one we
> were not.

Your counterexample:

```text
Aug 20, Aug 19, Jan 2024 (sticky A), Dec 2023 (sticky B)
```

Dates never ascend, so no inversion fires. Sticky B corroborates sticky A and the
frontier becomes January 2024. **Corroboration defeats exactly one terminal
anomaly**, and my claim silently assumed a source has at most one. You are right
that no fixed `k` helps, since `k+1` defeats it.

### So the limitation is now structural, not a comment

A comment is precisely what failed. `CoverageProof` carries `authoritative`, set
only when the source has a declared ordering contract, and:

```python
ORDERING_CONTRACTS: Dict[str, str] = {}      # deliberately empty
```

`covers_release()` refuses any non-authoritative proof, naming the arm. Every
frontier this derives today is inspectable telemetry that cannot mint anything —
enforced in code, and a test asserts it. Adding an entry to that dict is a
reviewed decision, not configuration.

There is a test that documents the DEFEAT rather than hiding it: two terminal
outliers do still produce a frontier at January 2024, and the assertion is that
the proof is `authoritative == False`.

### The reachable variant, and a gap in my own testing

`duplicate_in_run` keyed on the RAW href while identity and dates are canonical,
so one terminal post under two cosmetic variants gave two eligible anchors and
the second corroborated the first. Now canonical.

**Mutation found that my alias test could not see this.** Reverting the keying
left the whole evaluator suite green, because
`TestOneCanonicalPostUnderTwoRawAliases` builds its `Sighting`s by hand with
`duplicate_in_run=True`. It proves the evaluator HONOURS the flag and proves
nothing about whether the crawler SETS it — the producer-versus-component gap
this project keeps rediscovering, this time inside the fix for a finding about
exactly that. There is now a producer test driving a real crawl, and reverting
the keying kills 2.

## M17-2 — validate, do not normalise

The evaluator sorted the pages it was handed and never noticed an ABSENT one.

```text
numbers must be non-empty, unique, start at 1, and be strictly consecutive
positions must be unique within each page
```

Sorting a broken sequence produces a tidy sequence, which is exactly the wrong
response to a gap.

**And the producer now emits a page for every attempted page.** The generic
exception handler counted the error and recorded nothing, so `[1, 3]` was
reachable. Detection is the safety net; the observation is the evidence, and both
belong. Guarded, since the exception can fire after the page was already
recorded.

Two more from your §M17-2: the report was built BEFORE the termination chain ran,
so every ordinary report carried `termination="not_run"`; and the run id is now a
`uuid4` rather than `timestamp + id()`, which was never a durable key.

## M17-3 — universal, by arm key

`covers_release()` grouped by `listing_type` and accepted a type as soon as ANY
arm crossed. The required set is now passed explicitly by stable arm key, every
one must exist, be proven and cross, and an EMPTY required set refuses rather
than passing vacuously.

Regressions cover: shallow same-type arm, unusable same-type arm, required arm
absent, all required arms crossing (the positive control).

## M17-4 — endpoint identity

`arm_key` is now `source:category:endpoint`. DDLBase's Remux 4K and Remux 1080p
no longer merge into one arm carrying two interleaved orders.

## Mutation — all five, all killed

```text
duplicate_in_run keyed on raw href again     kills 2   (producer test)
page continuity validation removed           kills 3
only the first required arm is checked       kills 3
every proof marked authoritative             kills 3
failed pages emit no observation             kills 1
```

## Your round-18 gate, item by item

```text
1 close M17-1..M17-4                         DONE
2 explicit required arm_key POLICY           NOT DONE -- see below
3 ordering contract, or non-authoritative    DONE, non-authoritative, structurally
4 persist evidence snapshot + proof versions NOT DONE -- see below
5 end-to-end mutations for the four cases    DONE
6 rerun main/branch like-for-like            DONE, figures below
7 deploy fixed producer/evaluator dark       NOT DONE -- depends on 4
```

**On 2.** `covers_release()` takes the required keys explicitly, which closes the
existential bug, but nothing yet DERIVES that set from a target's claimed type,
and nothing versions the policy. I did not want to invent the derivation: which
arms can contradict a given release is a domain judgement, and getting it wrong
produces a confidently wrong negative.

**On 4 and 7.** Your persistence shape is detailed enough to build from, and I
have not started it. One thing in it I want to flag before I do: you note the
proof is not reconstructible unless the exact `posted_date_raw` and eligibility
state used for every anchor are retained or referenced immutably. Today the
evaluator receives a MUTABLE `dates` map and `unstable` set from the caller, so a
persisted proof would reference values that can change underneath it. That seems
like the part most likely to be got subtly wrong, and I would rather agree the
snapshot mechanism than discover later that stored proofs cannot be replayed.

## Verification

```text
code head    4c0f1de

                              failed   passed   skipped
main control (origin/main)         1     5320         4
this branch                        1     5432         4
```

Same single pre-existing failure both sides. **+112 passing, zero net new
failures.** Host/container md5 parity asserted.

## Live deployment

Still the ROUND-14 code, dark since 08:05. Nothing from rounds 16, 17 or 18 is
deployed.

```text
listing_claims        198 claims / 190 releases
movie-vs-tv conflicts   0
category_attested       0
downloads.media_kind    NULL on all 684 rows
errors                  0
```
