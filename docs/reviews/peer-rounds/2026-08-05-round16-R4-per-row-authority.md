# Round-16 review request — R-4 per-row authority

**Repository:** LstDtchMn/ScanHound
**Branch:** `agent/hybrid-sweep-rebased`
**Head:** `a2559d7b3c5e130f25e964dfb5fccee19bc6321f`
**Code head:** `5dc8395d73d3bb1cd17d61903d18bba5a3681e31`
**Base:** `main@7adb17bd8661633cade99b4ed7ca98bab3a9c8eb`

Read the branch through the GitHub connector. **Review the code, the tests and
the measurement — not this summary.** If you find yourself assessing my
description rather than the diff, stop and say so.

---

## Your round-15 blocker was correct, and larger than stated

I verified all three of your premises against the code before changing anything:
`_candidate_updates` omits any field a payload lacks ("absence never means
false" — its own docstring); the sink COALESCEs all 17 protected columns;
`_reparse_completed_feed_only` updated exactly `title_year` and then stamped the
whole feed leg current. Confirmed.

Then I measured it on the live database — read-only, committed at
`docs/reviews/evidence/2026-08-05-per-row-authority-live-measurement.txt`:

```
completed rows                     2466 of 3431
rows fully detail-authoritative       0
protected fields detail claims     8-10 of 16, every row

feed-owned on 100% of rows:  title_year, media_type,
                             media_type_provisional, media_type_because,
                             hevc_evidence, episode_end
feed-owned on a majority:    season 52%, episode 72%
```

So the round-14 repair handled **one** of the 6–8 feed-owned fields per row. On
every one of 2,466 completed rows, 5 to 7 protected fields were frozen at the
old grammar's parse under a current feed stamp — **and the media-type triple was
among them at 100%**, i.e. the stale-forever set included the media-type verdict
R-1 exists to protect.

## What replaces it (`eabcf92`, `5dc8395`)

The sink records which protected fields **that row's** detail supplied
(`detail_authority_fields`); the repair re-derives every field the row's detail
did not claim. Guards, each pinned by a test:

1. **A NULL or undecodable claim set repairs nothing.** Unknown is not empty.
2. **Coupled groups move together or not at all** — media-type triple, size
   text/gb, HDR evidence/formats, season/episode/episode_end.
3. **A row whose detail owns everything is not stamped**, because nothing was
   re-derived.
4. **The claim set is cumulative.** See below.
5. **Backfill** reconstructs the claim set for pre-column rows from their
   retained payloads: 2,466 dry-run, 2,466 reconstructed, 0 undecodable.

## A defect in your recommended design, found by testing my implementation of it

Your smaller-compatible design says to persist "the exact protected keys
supplied by `candidate_updates`" — per payload. Because the sink COALESCEs, that
is wrong. Measured against the real sink:

```
after rich hydration : 2160P  ['clean_title', 'resolution']
after sparse refetch : 2160P  ['clean_title']
```

The stored value is still detail's — COALESCE kept it when the refetch omitted
the field — but a per-payload claim set stops saying so, and the repair would
then overwrite a **detail** fact with a feed one. That is the downgrade the
authority model exists to prevent, reached by following the recommendation
literally. The claim set is therefore a union.

**I want you to check my reasoning that union is right**, specifically: a union
never shrinks, so once detail supplies a field it stays detail-owned even if
detail permanently stops supplying it. My argument that this is correct rather
than a leak: a detail-derived value's staleness is governed by
`DETAIL_PARSE_VERSION`, and a detail capability change bumps that and forces a
refetch. Feed grammar changes are not supposed to invalidate detail-derived
facts. If that reasoning is wrong, the union is a permanent freeze and I need to
know.

## Where else I would attack this

1. **Is the coupled-group list complete?** I chose four. Candidates I
   considered and rejected: `clean_title`/`title_year` (independent
   derivations), `media_type`/`description_complete` (different concerns). Say
   if you disagree.
2. **Guard 3 leaves `feed_parse_version` permanently behind** on rows where
   detail owns everything, so the repair re-selects them every pass. I checked
   the only other reader of that column: the `derived_state = 'stale'` sweep
   filters `hydration_state != 'completed'`, so those rows are never marked
   stale. Cost is one row read per pass, and `healed` does not count them.
   **Confirm I have not missed a consumer.**
3. **`hevc_evidence` has never been supplied by detail** across 2,466 live
   payloads. Round-13 recorded that a detail producer for it now exists. It is
   consistent with the positive-only exact-token rule, but the column is
   feed-owned in practice. That is a contract claim worth re-checking on its own
   terms, separately from R-4.
4. **The backfill trusts a retained payload as the authority record.** It
   re-runs the real `_candidate_updates`, so it is reconstruction rather than
   inference — but it assumes the stored payload is the one that produced the
   stored values. If a payload were ever overwritten by a later hydration whose
   updates were then partly COALESCEd away, the reconstruction would be wrong.
   I believe the union guard makes this safe. **Check that.**

## Contract (your Q2)

All four binding defects fixed in `a47a2c3`: base SHA, R-3 `old=`, R-4 closure
now bound, O-6 artifact bound by both code SHA and blob. A fifth you did not
ask for: the R-4 row *asserted* the implementation you refuted, so it now
records the refutation and withdraws the claim — a precise SHA on a false
sentence is worse than the abbreviation. Verified no abbreviated SHA-shaped
token remains.

## Attestation

- **4,805 passed / 0 failed / 4 skipped** at head `a2559d7`, against a
  **same-container baseline of the unchanged tree (4,793 / 0 / 4)** — so "no
  regression" is measured, not inferred from a bare total. Full progression,
  every run in the same container against a complete tree:

  ```
  baseline   775fbe98   4793 passed / 0 failed
  R-4 fix    eabcf92    4801 passed / 0 failed   (+8 new tests)
  +backfill  5dc8395    4804 passed / 1 FAILED   (+4, and one caught me)
  +test fix  a2559d7    4805 passed / 0 failed
  ```

  The failure at `5dc8395` is disclosed rather than smoothed over: adding
  `detail_authority_backfilled` to `reconcile_derived_versions()`'s result
  tripped an exact-equality assertion whose own comment says a new counter
  must be a deliberate change and not something that quietly appears. It
  worked. Fixed in `a2559d7` by updating the assertion, not by loosening it.
- **CI green** on this branch, 12 / 12 / 15 steps executed. Billing is restored;
  the earlier red runs had 0 steps, which is the quota signature.
- Harness correction worth knowing: a `backend tests`-only container copy omits
  `docs/feature-pack-review/qualification-evidence/collect_shadow_evidence.py`
  and manufactures 23 failures that read exactly like regressions. With the
  whole tree the baseline reproduces the previous `7093985` artifact **exactly**,
  which independently confirms that artifact.

## What I am least sure of

The union argument in the section above. Everything else here is measured; that
one is a reasoned claim about interacting version stamps, and it is the piece
that would quietly freeze fields if I have it wrong.
