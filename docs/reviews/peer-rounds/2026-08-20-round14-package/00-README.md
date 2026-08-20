# Round 14 review package — M13-1 remediation and watermark evidence

**Self-contained.** The full diff travels with the package; you should not need
to fetch code from the branch.

## Identity

```text
repository    LstDtchMn/ScanHound
branch        fix/round12-attestation-authority
code head     0c0f5d12b48535991ec1b31ee58e7d3834210e44
base          6ac5cd2aefb81bb7d85354577a69af269b8e05e5   (main, 0 behind)
working tree  clean
deployed      NOTHING. The running container predates all media-kind work.
```

## Contents

| File | What it is |
|---|---|
| `01-request.md` | **Start here.** What was fixed, the regression I introduced and caught, and the evidence about your watermark model. |
| `02-code-changes.patch` | Complete diff of `backend/` and `tests/` against `main`. |
| `03-evidence.md` | Commands and results: mutation kills, suite figures against the like-for-like control, and the corpus measurement. |
| `04-provenance.md` | SHAs, blob hashes, container identity, what is NOT covered. |

## The three things worth your attention

1. **A regression I introduced, in the M13-1 fix itself.** Masking `media_kind`
   alone made one row shape *less* safe: the contradiction guard stopped firing
   and a held movie-with-a-season emitted a full `tv_season` identity. The hold
   granted the permission it exists to withdraw. Fixed by withdrawing the whole
   semantic identity; that broader mask is the first thing I want challenged.

2. **Your order key already exists.** 100% of 4,000 cached rows carry a
   `posted_date` at minute resolution. But it comes from the DETAIL page, and the
   listing selector extracts no date at all — which is awkward precisely for the
   already-cached releases that most need attesting. `01-request.md` proposes a
   way through and asks three questions about it.

3. **The watermark model is NOT built**, deliberately. Deriving coverage from
   previously stored dates is exactly the kind of step where I could relocate the
   trust problem instead of solving it, which is what round 13 caught me doing.

4. **What IS built: the listing-claim ledger.** Your line "persist claims before
   releases age off" is ruling-independent and perishable, so I built the
   recording half only. `url_type_claim` was a function-local dict rebuilt every
   crawl — the sightings were being destroyed continuously. The new table
   **authorizes nothing**, and a test asserts that.

## Round-13 dispositions

```text
M13-1   fail-closed revocation      FIXED    hold -> erase -> mark -> release
        restart behaviour           FIXED    durable-journal option, using the
                                             conflict mark that already exists
L13-1   parser health as coverage   FIXED    an arm earns coverage by parsing
        real-producer tests         FIXED    tests drive _crawl_pages() itself
        coverage proof / watermark  NOT BUILT -- evidence gathered, questions asked
        persist claims before ageing BUILT     -- ruling-independent, inert ledger
        legacy aged-off policy      ACCEPTED  permanently unknown, reported as a class
```
