# Response to review — 4 accepted, 1 disputed with evidence

**Branch head:** `71c54ce` (was `f1d9997` when you reviewed)
**Verdict accepted:** changes required. Parts 1–8 were not ready. Two of your
blockers are real defects in code I wrote, and my own tests missed both.

---

## 1. CRITICAL — never-acquired items reported as covered. ACCEPTED, FIXED.

You are right, and the mechanism is worse than "colour is a loose proxy".
`YELLOW` is reached by two structurally different routes:

* a **measured** `first_normal_at` 6–24 h after publication, and
* an item that simply **aged past the band without being acquired at all**.

Coverage keyed off the colour, so the second kind was labelled
`COVERED_BY_RSS` with the detail string *"acquired through the normal feed"* —
a sentence that was flatly false about an item no feed had ever returned.

Fixed exactly as you specified: `COVERED_BY_RSS` now requires
`first_normal is not None`.

My test failure is worth naming, because it is the reusable lesson. I had a
test for this input — `test_pending_becomes_yellow_once_the_band_expires` — and
it asserted only `rss_state is YELLOW`. It never asked what coverage that
produced. **I tested the axis I was thinking about and not the one the bug was
on.** New tests pin both axes, including two items of the *same colour* with
*opposite* coverage.

## 2. CRITICAL — coarse-time boundary evaluated in the unsafe direction. ACCEPTED, FIXED.

Right, and my docstring claimed the opposite of what the code did: it said the
sweep "refus[es] to claim we crossed a boundary we might not have" while the
comparison did precisely that.

Listing times round DOWN, so `"2 days ago"` means a true publication time in
`(NOW−3d, NOW−2d]`. Demonstrated before changing anything:

```
target        2026-07-30 00:00:00      (NOW − 2.5 d)
newest poss.  2026-07-30 12:00:00
oldest poss.  2026-07-29 12:00:00

old rule   earliest <= target : True   -> COMPLETE
new rule   newest   <= target : False  -> INCOMPLETE
```

If the true time is `NOW−2.1d` it is *after* the target — the sweep had not
reached it, and completing advanced `coverage_through` over ground never
traversed. Added `newest_possible` and switched the comparison to it.

Why my existing test did not catch it: `test_coarse_granularity_does_not_over_claim_the_boundary`
used **hour** granularity, where a 48 h reading clears a 47 h target on *both*
edges. It passed under either rule and therefore proved nothing. The new test
uses day granularity, where the edges disagree.

## 3. HIGH — reconciliation still optional. ACCEPTED, FIXED.

Correct, and my review memo overclaimed: it said a missing cross-check was a
mandatory stop when the code only enforced that if a token happened to exist.
Missing credentials silently downgraded the run to DB-only and could still
report `ready=True`.

Independence is the entire value of the cross-check, so "we had no credentials"
is not a lesser failure than "the check disagreed". It is now unconditional.
Exercised against six synthetic gate outcomes:

| case | verdict |
|---|---|
| reconciliation agrees | gate open |
| reconciliation FAILED (the state actually shipping) | BLOCKED |
| reconciliation DISAGREES | BLOCKED |
| requested but absent | BLOCKED |
| **no auth token, nominal DB-derived pass** | **BLOCKED** (was: open) |
| a real miss, everything else clean | BLOCKED |

## 4. HIGH — branch diverged from integration target. DISPUTED.

Your git facts are all correct. The conclusion does not follow.

```
sweep       f1d9997        integration 02ebf90        main 7cc5275
merge-base(sweep, integration) = 3a5706a
main IS an ancestor of sweep            -> sweep is based on current main
integration is NOT an ancestor of main  -> it was never merged
```

`agent/feature-pack-integration` is not the deployment candidate. It is a stale
branch, last touched **2026-07-22**, and its entire difference from `main` is:

```
RELEVANT_MISS_2026-07-22.md                    |  85 ++
05_shadow_evidence_20260722T105700Z.json       | 241 ++
05_shadow_evidence_20260722T165700Z.json       | 341 ++
3 files changed, 667 insertions(+)   — zero code
```

Three documentation/evidence files from the July shadow observation. The
deployment candidate is `main`, which the sweep branch already sits on top of.

I agree with the underlying principle — Part 9 must be judged against the exact
deployment candidate — and that is satisfied. If you intended those evidence
documents to be preserved, they should land on `main` on their own; they are
not an integration target for code.

## 5. The 50% threshold / unwired `expected_typical`. ACCEPTED.

Volume-anomaly detection is now **off by default** behind
`VOLUME_ANOMALY_ENABLED`, so an invented constant cannot create a mandatory
stop or clear one. Zero-post `STRUCTURE_LOST` detection is unaffected — "this
source always has posts and now has none" is categorical and carries no
threshold.

`expected_typical` still has no producer. Per your ruling it is a Part 9
deployment prerequisite, together with calibration from recorded live listing
volumes. **I am not claiming volume-anomaly protection operates today.**

---

## On your other observations

**Session transaction ordering** — agreed, and stated the same way in the
commit: not atomic as a unit, but the split preserves the invariant that
matters.

**`disjoint_identity_sets` only catches total non-overlap** — correct, and that
is a real residual. A partial canonicalisation failure surfaces as ordinary
`listing_only` misses. That is acceptable only because a relevant miss is
already a mandatory stop, so those misses cannot be quietly excused. Worth
saying plainly: this guard catches the catastrophic case, not the subtle one.

**No CI status on the branch head** — fair. My full-suite figure was
author-reported. The actual run, for the record:

```
4361 passed, 14 failed, 4 skipped in 586 s
```

All 14 failures investigated: **12 were missing directories in my throwaway
container** (I had copied only `backend/` and `tests/`; `test_dv_host_scan`,
`test_metadata_scan_runbook` and `test_dv_settings` need `scripts/`, `docs/`
and `frontend/`). After copying those, 12 pass. The remaining 2 assert
behaviour when `plyer` and `selenium` are **absent** — both are present in the
image, so the tests' premise does not hold there. None are in files this branch
touches. I have not attached CI; the number stays author-reported until it runs
somewhere you can see it.

**One flaky test, declared before you asked:**
`test_feature_pack_integration.py::test_new_lifespan_cancels_waiting_rss_before_transport_construction`
failed 2 of 10 full-subset runs on this branch, 0 of 4 on main, and would not
reproduce under instrumentation. Its failing assertion (`constructors == []`
fails while `outcome == ["cancelled"]` passes) proves the construction came
from a thread other than the test's worker — leaked from an earlier test. No
mechanism connects a `[BD]` title regex to HTTP scraper construction. Logged
separately as pre-existing test-isolation leakage.

---

## Position on the qualification window

Agreed on all four points, and they are already the operating assumption: the
previous window's evidence is invalid for promotion, no reuse, a complete fresh
gate, and any absent/failed/disagreeing cross-check is a mandatory stop.

Live state verified, not assumed: `auto_rename_enabled = False`,
`auto_grab_enabled = False`, `hdencode_rss_auto_grab_enabled = False`.

Nothing is deployed. Part 9 remains Jesse's gate, and I am not asking for it.
What I want from this round is whether 1, 2, 3 and 5 are correctly fixed, and
whether you accept the evidence on 4.
