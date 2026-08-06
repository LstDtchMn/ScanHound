# Peer review request — RSS miss accounting, Round 4

**Round:** 4 (response to REQUEST CHANGES at `ddfde91`)
**Branch:** `agent/rss-miss-accounting`
**Base:** `main` @ `d909b44`
**Date:** 2026-08-06

---

## All ten closure items addressed

| # | Item | Status |
|---|---|---|
| 1 | flag every row unsupported by its own provenance | done |
| 2 | reconcile counts for all provenance-aware cycles, incl. stored zero | done |
| 3 | validate persisted media-type vocabulary | done |
| 4 | validate derived-marker shape; detect orphan rows | done |
| 5 | the four missing fail-open regression cases | done (9 added) |
| 6 | expose detailed findings separately, still blocking | done |
| 7 | snapshot and hash a stable database before measuring | done |
| 8 | commit a redacted replay input with hashed per-cycle sets | done |
| 9 | remove `is_tv=False` as movie evidence | done |
| 10 | rerun exact-head CI | pushed; see note |

---

## Finding 1 — the hole, and the test that certified it

Your construction was exact. The check had no `else` on
`feed_observation_valid`, and reconciled counts only `WHERE
relevant_miss_count > 0`:

```
provenance = {movies_all: failed, tv_all: failed}
relevant_miss_count = 1, one movie row present
→ present(1) == stored(1)  → no disagreement
→ validity false           → attributed = 0
→ reports 0 misses, integrity = []
```

You also caught the part that matters more: my own
`test_a_degraded_cycle_with_no_valid_relevant_feed_does_not_block` builds that
store and asserted **only** the zero count. I wrote the check, then wrote a test
certifying its hole. That test now demands the integrity flag.

**Every row is sorted into supported / unsupported / corrupt**, so nothing falls
off the end. Six contradictions block where five were tolerated:

| Finding | Was |
|---|---|
| `miss_row_unsupported_by_provenance` | silently discarded |
| `count_row_disagreement` (now incl. stored zero) | unchecked when stored = 0 |
| `media_type_invalid` | NULL/off-vocabulary coerced to `unknown`, could count |
| `derived_marker_unknown` | any marker value accepted |
| `derived_marker_contradicts_cycle` | never compared |
| `orphan_miss_rows` | invisible to the join |

On orphans: I used a `NOT EXISTS` scan rather than trusting the declared foreign
key, for the reason you gave — this connection does not enable
`PRAGMA foreign_keys`, so the constraint is not evidence.

Findings are categorised in `miss_evidence_integrity_by_category`, so an operator
can separate *coverage miss* from *evidence store corrupt* while readiness blocks
either way. Nine regression cases, including a **positive control** on all three
legitimate `media_type` values so the blocker cannot pass by always firing.

Your answer to attack 3 is adopted as written: blocking for readiness, separated
for diagnosis.

---

## Finding 2 — the digest now binds, and the manifest is replayable

You were right that hashing `crawler.db` after querying it bound nothing: WAL
means returned rows can live in `crawler.db-wal`, and the reads and the hash
observed different moments. My artifact's claim that the digest *"fixes WHICH
bytes produced these counts"* was false.

`VACUUM INTO` now produces a consistent snapshot **before any measurement
query**, and that file — not the live database — is hashed and queried.
`--no-snapshot` still permits a live read but labels its digest non-binding
rather than pretending otherwise.

**`--replay-out` emits the redacted replay dataset**, carrying the hashed
per-cycle `listing_only` and `feed_only` sets — the input the algorithm consumes,
not the conclusions it emitted.

I verified it by using it rather than by asserting it. Recomputing one record's
resolving cycle from the replay dataset alone:

```
recomputed from replay : fa66ddab-4d4a-4368-ad51-2223cccb7d3d
artifact says          : fa66ddab-4d4a-4368-ad51-2223cccb7d3d
```

Committed at
`docs/feature-pack-review/qualification-evidence/2026-08-06-replay-dataset.json`
(315 cycles, 159 misses).

Smaller corrections all taken: full 64-character URL digests; the resolving
cycle's **UUID** alongside its timestamp; `observation_end < admission_end`
rejected with exit 2 (it would deny late-admitted misses any window and silently
manufacture PENDING/AMBIGUOUS); and the CLI help fixed — it claimed
`--admission-start` defaulted to "the first eligible cycle" when it admits every
cycle.

---

## Finding 3 — `is_tv=False` removed, and my prose figures made executable

`is_tv=False` is gone as movie evidence. A parser negative means the TV pattern
did not match, not that the item is a film — the same inference already removed
from the slug. `is_tv=True` remains affirmative TV evidence.

You were right to treat my 3,134-row category distribution as author-attested:
nothing on the branch could produce it. It is now
`docs/.../scripts/measure_category_coverage.py` with committed output.

**Re-measuring found more than the prose did:**

```
rows 3136, unparseable 0
4k 1702   tv 1236   remux 198   absent 0
season_set 1214   episodes_set 1214
tv_category_without_season   22
season_without_tv_category    0
```

The last two lines are the useful ones. **No row carries a season without a TV
category**, so the two independent TV signals never conflict in the direction
that would force `unknown`. And **22 TV-category rows have no season** — exactly
the shape of your false-pass construction, so that defect was live in the corpus
rather than theoretical.

(The prose said 3134/1234. The cache grew by two rows, which is why re-measuring
beat reusing a figure.)

On your preference for persisting the source descriptor's explicit `type` as
`listing_media_type` rather than inferring from a quality category: I agree it is
the more durable contract and have **not** done it, because it changes the
listing-side data model rather than the accounting under review. Flagged as the
obvious next step rather than silently skipped — say if it should be in scope now.

---

## Measurements, from a snapshotted and hashed cohort

```
admission        2026-07-22T00:00:00Z .. 2026-08-05T23:59:59Z
observation end  2026-08-06T23:59:59Z          cohort_is_fixed: true
queried          VACUUM INTO snapshot; digest covers the exact queried file
```

| | Measured | Required |
|---|---|---|
| Cycles admitted (eligible) | **258** of 300 | 20 |
| Observed days | **14.941** | 7 |
| Request reduction | **85.12%** | > 0 |

**Conservative bound — 60 records:** 60 GREEN, 0 blocking. Latency median
**1.172 h**, max **4.061 h**.

Unchanged and deliberately narrow: every record admitted by the conservative
bound was later observed in the validated normal feed. **Not** that no coverage
was lost; the bound cannot establish overall health.

---

## Verification

- Full suite: **3 failed, 4346 passed, 4 skipped** (694 s).
- Baseline `main` @ `d909b44`: the **same 3** fail, same test ids — no frontend
  build output, no selenium, no notification backend.
- `emit_measurement_artifact.py` and `measure_category_coverage.py` both run
  clean against a fresh schema as well as the live snapshot.

**On item 10:** the Round 3 matrix failure was GitHub returning `Service
Unavailable` during runner setup for Python 3.12, with siblings cancelled — your
diagnosis, not a code failure. This head is pushed and will re-run. I cannot
assert a green matrix I have not seen, so please confirm from the run rather than
from this document.

## What I would most like attacked

1. **Is the integrity blocker now too aggressive on a real corpus?** One corrupt
   row blocks the whole gate. You judged that correct for readiness, and I agree
   — but the live window has 150 legacy rows on the conservative path, and I have
   not run the new checks against a corpus that contains deliberately damaged
   rows at scale.
2. **Does the replay dataset actually let *you* recompute the result?** I verified
   one record. If a full independent recomputation disagrees anywhere, the
   dataset is insufficient regardless of my spot check.
3. **Is `unknown` requiring both feeds still right** given that `absent` is 0 in
   the live cache? The rule now almost never fires there, which is reassuring —
   but it also means it is barely exercised by real data.
4. **Should `listing_media_type` land now?** See Finding 3 above.

## Still not addressed, deliberately

`ready` remains **False**. The readiness rule blocks on any miss regardless of
grade, so 60 green records cannot pass it. That is a behavioural policy change,
not an accounting fix, and it is the owner's decision.
