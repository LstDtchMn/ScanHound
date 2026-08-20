# ScanHound Peer Review Request — Round 13: the round-12 remediation

**Repository:** `LstDtchMn/ScanHound`
**Branch:** `fix/round12-attestation-authority`
**Base:** `main @ 6ac5cd2`, 0 behind
**Nothing is deployed.** The running container still predates all of this.

## Verdict accepted in full

Round 12 returned REQUEST CHANGES on two MEDIUM findings and one LOW. I am not
contesting any of them. M12-1 in particular was the finding I had the wrong end
of: I came into the round worried the attestation was too STRICT, having
measured that ~82% of the corpus would stay unknown. You pointed out that the
unknown state is correct and safe, and that the real defect runs the other way —
the attestation that *does* happen is unearned.

## M12-1 — reproduced before it was fixed

I did not want to build against your prose, so I wrote the regression first and
ran it on unmodified `6ac5cd2`:

```text
4 failed, 1 passed

test_a_crawl_with_the_tv_arm_switched_off_must_not_attest   assert '4k' is None
test_an_early_stopped_crawl_must_not_attest                 assert '4k' is None
test_a_crawl_with_page_errors_must_not_attest               assert '4k' is None
test_a_cancelled_crawl_must_not_attest                      assert '4k' is None
```

These drive the real `BackgroundScanner.scan_once()` authority decision rather
than calling `attest_scan_categories()` directly, which is the vacuity you
identified in the existing tests.

The 5th passed and had to: a partial crawl must still be able to RECORD a
conflict. That asymmetry is the load-bearing idea in the fix.

**The config-reachable case deserves naming.** With
`background_scan_categories=["4k"]`, `_build_sources` emits only the movie arm.
The TV listing is never fetched, so a contradiction is not observable even in
principle — and every 4K row was still certified clean. No crash and no race
required, just a config an operator is invited to set.

### There were three producers, not one

A mapping pass over every write path found that gating only the backfill would
have missed two more:

```text
scanner_service.py:1049   dict literal   category_attested: True    UNGUARDED
scanner_service.py:1213   assignment     details[...] = True        UNGUARDED
database.py               attest_scan_categories()                  the backfill
```

Both crawler-side producers stamped a bare `True` on every fresh post. The
comment above the second stated the false premise outright: *"This crawler
checks every sighting for a conflict, so anything it produces has been checked."*
It checks every sighting it MAKES. That is a different claim from "no
contradicting listing exists", and only the second licenses the flag.

Both now defer. A single post-crawl gate, `crawl_attestation_verdict()`, decides
for fresh and legacy rows alike:

```text
attesting crawl?                     else no   (the scheduled cycle never claims it)
early stopped?                       then no
page errors?                         then no
termination == complete?             else no
every contradicting type covered?    else no
```

Your point that even `complete` is insufficient for a bounded 3-page crawl is
handled by the first condition rather than the last: the scheduled crawl does
not claim attesting coverage at all, and `attest_coverage` is forced off
whenever `early_stop` is set.

**On the ordering argument you made.** The same partiality signal was already
trusted a few lines earlier to block a *purge*. The code knew the crawl was too
partial to delete against, and attested from it anyway.

### One more defect, found while mapping rather than by the review

`attest_scan_categories` guarded on key PRESENCE, not truth. Any row carrying
the key with a FALSE value could never be attested again by any crawl, however
thorough. A manual rescan wrote exactly such a row — which makes your LOW finding
worse than LOW: one rescan permanently disabled the media kind for that release,
not merely until the next crawl.

It would also have broken my own fix, since the producers now write `False`.
The guard tests truth now.

## M12-2 — one transaction, retraction first

`record_classification_conflicts_and_retract_kinds()` does both halves in a
single transaction, erase first, as you specified. An unreadable or missing
cache row cannot prevent the erase; a test removes the cache row entirely and
asserts the retraction still happens.

On the question you asked me to decide — what happens when the revocation itself
cannot commit — the failure is no longer swallowed. The URLs are held on the
scanner and retried next cycle, and the cycle reports `revocation_failed`. The
transaction rolls back as a unit, so the stale kind is still live in that
window; what the fix removes is the case where we knew a release was unsafe and
then forgot.

I also moved attestation behind the `err` check. The block sat outside the
`if not err:` guard, so a source that RAISED could still attest whatever partial
seen-set the crawl had accumulated before it died.

The fault-injection regression drives the real `scan_once()` sequence, not a
direct call.

## M12-3 — the fourth field, and a named structure

`rescan_classification()` returns a `CarriedClassification` NamedTuple with
`category`, `is_tv`, `category_conflict`, `category_attested`. You were right
that a positional tuple is what let the fourth field go missing at the call site
without anything looking wrong.

A rescan carries attestation and never creates it; both directions are tested.

## A test was deliberately flipped

`test_a_fresh_post_is_marked_attested` asserted that a single-arm crawl mints
attestation. That is the defect stated as a requirement, so it now asserts
`False` and carries the reasoning in its docstring rather than being quietly
edited.

## Mutation evidence, in both directions

The over-strict direction matters as much as the over-permissive one here: a
gate that never fires would satisfy every negative test, so the positive
controls are what separate "correctly stricter" from "permanently broken".

```text
verdict always True            kills 5    over-permissive
verdict always False           kills 2    over-strict  <- anti-vacuity control
guard back to key presence     kills 1
rescan drops attestation       kills 1
failed revocation forgotten    kills 1
```

## NOT DONE, stated rather than implied

**No production caller passes `attest_coverage=True`.** Nothing certifies
anything today. That is your preferred posture — legacy rows stay unknown — but
it means the dedicated conflict-aware backfill is **not built**, and the
media-kind capability stays dark until it is. I did not build it because its
coverage model is a design decision I would rather you ruled on than guessed at:
a full unbounded crawl of every arm is expensive, and for releases that have
aged off the listing entirely there is no listing evidence to be had at all.

There are exactly two `run_scan` callers, and neither sets the flag:

```text
api/routes/scanner.py:258   manual scan   early_stop defaults False, flags are user-chosen
background_scanner.py:759   scheduled     early_stop=True, so permanently ineligible
```

**The manual route is the candidate I would suggest**, and I want to argue
against myself about it. It already traverses without early stop, and when the
user selects every category the type-coverage condition is genuinely satisfied —
my gate would refuse a single-arm manual scan on its own. What it does *not*
settle is your bounded-coverage objection: `req.pages` is still a page budget,
so a release on page 40 of TV Packs is invisible to a 5-page manual scan of
every arm, and the crawl would nonetheless satisfy every condition I wrote.

So I think the type-coverage half is sound and the depth half is still unearned,
which is precisely the part I did not want to decide alone.

**And there is a concrete reason it cannot be closed with what exists.** The page
loop is `for page_num in range(1, pages + 1)`, and its only early exit is the
cached-frontier `early_stop` break. There is **no end-of-listing detection at
all** — nothing distinguishes *"I ran out of page budget"* from *"I reached the
end of the listing"*. `_last_crawl_termination == "complete"` means only that
every REQUESTED page was fetched, which is exactly the objection you raised, and
I can now confirm no other signal in the crawler carries it either.

Closing it properly means detecting an exhausted listing (a page yielding zero
posts, distinguished from a page that failed to parse — `_last_crawl_page_errors`
gives one half of that, and I do not think it gives the other). That is the
dedicated coverage model you deferred rather than something I should bolt on,
and I would rather build it to your ruling than guess and be wrong twice in the
same area.

Still open from earlier rounds, unchanged: the **I1 ruling** on #94, the
**reason-code enum**, and the **grab-time resolver measurement**.

## Your round-13 closure list, addressed

1. **Attestation authority** — done; unknown stays unknown for switched-off arms,
   early stop, page errors, cancellation, and source error. Positive controls
   prove a qualified crawl still attests.
2. **Atomic/fail-closed revocation** — done, with an injected-failure regression
   through the production sequence.
3. **Rescan preservation** — all four fields, positive and negative controls.
4. **Exact-head verification** — figures below, at the frozen head.

## Verification

```text
code head    64815c5          branch head adds this document only
targeted     13 passed        tests/test_round12_attestation_authority.py

                        failed   passed   skipped
main control (origin/main)   1     5320         4
this branch                  1     5333         4
```

The single failure is identical on both sides and pre-existing:
`test_dv_settings.py::test_all_frontend_editable_settings_keys_are_in_model`.
**+13 passing, zero net new failures** — the 13 are the new file.

**On how that figure was obtained, because I got it wrong first.** My initial run
reported *74 failed*. That was my instrument, not the code: the throwaway
container had no `docs/` or `scripts/`, and `test_version_labeler` reads
`docs/kometa/version_badges.yml` while `test_verification_hold` reaches into
`scripts/`. I ran `origin/main` through the identical method in its own
container and it also reported 74, which is what identified the artifact rather
than a regression. Both figures above are from containers provisioned the same
way, in the same session, with the code trees differing only as intended
(md5 `b9e5184a` vs `49f09c81` on `background_scanner.py`).

## The question for this round

Does the gate actually earn the claim now, or does it only relocate the unearned
step? Specifically: is "the crawl declared attesting coverage" a real guarantee,
or have I moved the trust boundary onto a flag nobody sets — and when the
dedicated backfill is built, what coverage model would you accept as sufficient
for releases that have aged off the listing entirely?
