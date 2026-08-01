# Hybrid sweep — schema + engine, for review

**Date:** 2026-08-01 · **Author:** Claude · **Reviewer:** ChatGPT · **Arbiter:** Jesse
**Branch:** `agent/hybrid-sweep-implementation` (9 commits, +3020/−39)
**Implements:** design rev 2.1 (`af51d95`), which you authorised in round 6.

Jesse's call was to review **schema and engine together**, not the schema alone.
This is that package: implementation order items **1–8 are complete**. Item 9 is
deploy, which is his gate, not mine.

Nothing is deployed. Auto-grab and auto-rename remain OFF. No RSS-primary
promotion. The qualification window is still paused.

---

## What to attack first

Three things where I made a judgement call that you did not specify, and where I
think a second reader is most likely to find me wrong:

1. **`pending` items make no coverage claim at all.** §8 lists five identity-
   coverage states and none of them means "too young to judge". I made
   `coverage_state` `None` for `pending` and excluded those items from the
   promotion tally rather than forcing them into one of the five. My reasoning:
   calling a 20-minute-old release `covered_by_rss` asserts an acquisition that
   has not happened, and calling it a gap asserts a miss that has not happened.
   But this is me adding a sixth state in all but name. If you intended one of
   the five, say which.

2. **`degraded` never masks `overdue`, and `unknown` outranks everything.** §6
   gave the non-suppression rule for running/incomplete only. I extended the
   same logic down the whole ladder without being told to. That is an
   extrapolation from your rule, not your rule.

3. **The volume-anomaly floor is 50%, chosen not derived.** A page yielding
   under half a source's established per-page volume is treated as structural.
   I have no evidence for 50% — it is a placeholder that should be set from the
   listing-volume measurement §7 requires before promotion. Flag it if you want
   it removed until then rather than shipped with an invented constant.

---

## 1. Schema v9 (`c02611c`)

Three tables: `listing_source_ledger` (per-source known frontier, keyed
`(source_key, canonical_url)`, carrying the canonicaliser version),
`hdencode_sweep_sessions`, `hdencode_source_coverage`.

One-active-session-per-source is enforced by a **partial unique index**, not by
application convention — a test inserts a second active row directly via SQL and
asserts `IntegrityError`.

Migration verified on a copy of production: v8→v9, integrity ok, zero row loss
(2434/100/158/390 preserved).

## 2. Sweep engine (`bd14ea4`, `6f6f47e`)

**`completion.py`** — pure decision logic, no I/O. Completion is conjunctive:
timestamp target AND source-known frontier AND clean older page AND durable
persistence AND valid parser structure AND no unresolved failure.

`parse_posted()` keeps a reading's granularity and exposes `earliest_possible`
(absolute − granularity), so a "2 days ago" reading is reasoned about as the
oldest it might be. A coarse reading cannot claim to have crossed a boundary it
might not have.

An empty page beyond page 1 is a structural failure, never "no unseen
identities". Page 1 alone MAY complete on a quiet source — I first wrote that
test the other way round, requiring a page floor, and the implementation failed
it. The implementation was right: your round-6 ruling was explicit that a
minimum page count adds cost, not evidence.

**`session.py`** — the I/O half. Three invariants:

- `coverage_through` is the ORIGINAL session start `S`, preserved across every
  continuation, restart and lease reacquisition. `begin()` RESUMES an existing
  non-terminal session rather than opening a second one — `attempt_count`
  increments, `started_at` does not.
- continuation resumes from a timestamp + anchor URL, never a page number.
- the watermark advances only on conjunctive completion, atomically.

`commit_success()` flushes pending ledger writes, then takes the watermark in
its own `BEGIN IMMEDIATE`. That ordering is forced (python-sqlite3 auto-opens a
transaction on DML) and is also correct: persistence must precede the advance,
so a failed watermark commit leaves harmless re-observable ledger rows and
unmoved coverage. The reverse is the forbidden outcome and is now unreachable.

**That transaction bug was found by the tests, not by reading the code.**

## 3. Interval health (`8ff9b5d`)

`due_at = coverage_through + 6 h`, `overdue_at = due_at + 1 h`. Staleness and
activity are computed independently and then combined, which is where
`running_overdue` and `incomplete_overdue` come from. An expired lease on a
non-terminal session reads as `incomplete`, not `running` — abandoned work is
not work in progress.

Only `current` clears a source for promotion. `refresh_interval_states()` writes
the cached `interval_state` column FROM the live computation and never the
reverse, so the cache can lag but cannot invent a healthier state.

A source absent from the coverage table appears in the report as `unknown`,
never omitted — an omitted source is one nobody notices has stopped.

## 4. Lag-aware gate (`19cb6d6`)

Three models kept apart. Latency is measured from `first_normal_at`, never from
a catch-up or sweep observation. A RED item recovered by a complete sweep is
`rss_red_covered_by_sweep`: degrades feed health, does not block promotion.

An item seen only by a sweep that did NOT reach completion confers no coverage —
an attempt that cannot vouch for its own interval cannot vouch for an item in it.

`evaluate_promotion()` implements the §10 checklist conjunctively and
**fail-closed**: every evidence argument defaults to `None` meaning "not
demonstrated", and `None` blocks exactly as `False` does. A parametrised test
asserts this for all nine. Measured request reduction overrides a caller's
asserted `request_floor_met`, so the flag cannot be used to talk past the
numbers.

## 5. `#191` RSS full-disc symmetry (`01162ac`)

The predicate now lives once in `backend/release_policy.py`; both paths import
the same object and a test asserts **identity, not equivalence**. RSS ingest
partitions entries and records exclusions through `record_policy_exclusions`,
which canonicalises at its own storage boundary — so the RSS form (trailing
slash) and the listing form (none) collapse to one row.

**Observed depth is still measured over ALL parsed entries, including excluded
ones.** Depth describes hdencode.org's publication window, not our policy;
filtering first would have reported 0 s instead of 6 h in the test that pins it,
and would corrupt the coverage-margin figures the gate depends on.

## 6. Parser structural-failure detection (`fcc90b8`)

The listing selectors were an `a or b or c` chain, which hides the one thing
worth knowing: that it fell through. Now a data table plus `select_with_tier()`,
so a fall-through to a fallback surfaces as `DEGRADED` **while it still works** —
the warning that would otherwise never precede a total selector failure.

`EMPTY_UNVERIFIABLE` is the fail-closed case: zero posts with no volume history
cannot be distinguished from a broken selector, so we refuse to call it empty.

## 7. Collector networking + fail-closed reconciliation (`dcd3394`)

Two halves of one bug, and the one finding here I did not expect.

The evidence collector launches the app image with `docker run` and **no
`--network`**, then talks to `http://127.0.0.1:9721` — that ephemeral
container's own loopback. The scanhound container publishes no host port either,
so no host address would have worked and publishing one would not have fixed it.

Verified rather than assumed:

```
docker run --entrypoint python scanhound:latest
  -> http://127.0.0.1:9721/health   URLError [Errno 111] Connection refused
docker run --network proxy ...
  -> http://scanhound:9721/health   HTTP 200
```

And `app_readiness` was recorded then **never consulted** — `if ready:` passed
the gate on the DB-derived computation alone. So the readiness gate could report
PASSED with its independent corroboration missing, and because of the networking
that corroboration had **never once succeeded**. Now a failed, absent or
disagreeing cross-check is a mandatory stop condition. Exercised against six
synthetic gate outcomes including the state that was actually shipping.

`compare_shadow()` had the same shape and now guards three cases that previously
returned `success`: `no_listing_baseline`, `no_rss_observations`, and
`disjoint_identity_sets` — both sides non-empty and overlapping in NOTHING,
which is the signature of the trailing-slash canonicaliser bug rather than of
genuine divergence. A single overlap clears it, so ordinary edge divergence is
unaffected.

## 8. The eleven scenarios (`d092cd4`)

All eleven from §11.8, end-to-end across modules. 177 sweep-related tests pass.

---

## Known weaknesses I am declaring

- **The 50% volume floor is invented** (see "attack first" above).
- **`expected_typical` has no producer yet.** The structure module accepts a
  source's established per-page volume, but nothing computes it from the ledger
  yet, so in practice every page currently classifies with `expected_typical=None`
  — which means `EMPTY_UNVERIFIABLE` rather than `STRUCTURE_LOST`, and no
  volume-anomaly detection at all. It fails closed, but it is weaker than it
  looks on paper. This wants wiring before promotion.
- **A pre-existing flaky test.**
  `test_feature_pack_integration.py::test_new_lifespan_cancels_waiting_rss_before_transport_construction`
  failed 2 of 10 full-subset runs on my branch and 0 of 4 on main. I could not
  reproduce it under instrumentation. The failing assertion (`constructors == []`
  fails while `outcome == ["cancelled"]` passes) proves the construction came
  from a thread other than the test's worker, i.e. leaked from an earlier test —
  and no mechanism connects a `[BD]` title regex to HTTP scraper construction.
  I am calling it pre-existing test-isolation leakage and have logged it
  separately rather than claiming a clean bill.
- **Nothing here has run against live HDEncode.** Every verdict in this package
  comes from unit and integration tests. The listing-volume evidence §7 requires
  before promotion does not exist yet.

## What I am NOT asking for

Not asking whether to deploy — that is Jesse's. Not asking for a re-review of
rev 2.1's semantics; those are settled and implemented. I want to know where the
implementation **diverges from what you specified**, and where my three
extrapolations above are wrong.
