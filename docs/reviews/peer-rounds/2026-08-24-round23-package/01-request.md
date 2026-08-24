# Round 23 — review request

Same posture. Your exact-head passes have found live defects in two consecutive
rounds, and in both cases the defect was one layer past where the change was
aimed. Please read this head the same way.

Preserve finding identity: R22-n stays R22-n, new ones are R23-n.

---

## 1. What I want attacked hardest

### The R22-2 fix, because the previous fix looked complete too

The guard now compares the revision columns, and the round-21 projection could
not. But note what that means: **the guard is only as honest as its projection**,
and the projection is written by the same hand as the INSERT it checks. If both
change together in the same wrong direction, the guard passes again.

`TestTheGuardSeesTheIntermediateShapesRevisions` includes a control asserting
that the revision-BLIND projection would still pass the demotion, which is my
attempt to pin the blind spot explicitly. Is that sufficient, or is there a
structural way to make the guard independent of the migration it checks?

Specifically attack:
- the six-case matrix — is any reachable intermediate state missing from it?
- `COALESCE(legacy_arm_key, arm_id)` for a row with **both** null. Round-20
  `arm_id` was `NOT NULL`, so I believe it is unreachable. Confirm or refute.
- the alias join walks back through `c.arm_id` for attributed rows and
  `c.legacy_arm_key` for unattributed ones. With `listing_type` now in the
  unattributed key, one `(canonical, legacy)` can match two claims of different
  types — I use `INSERT OR IGNORE` and then assert no orphans. Is duplicating an
  alias across both type-rows right, or should it attach to only one?

### R22-5's follow-through, which is a policy decision I made alone

Two unattributed rows for one release disagree on type. One matches the declared
arm, one does not. I promote the matching one and quarantine the other, leaving
it unattributed.

The alternative I rejected: refuse to attribute **either**, on the grounds that a
release with contradictory observations is exactly the case where attributing
half the evidence is misleading. I chose partial attribution because the
matching observation genuinely belongs to that arm and suppressing it would lose
a true attribution to punish an unrelated one.

I am not confident. Argue it.

### R22-1's boundary, now that policy owns the resolution

`active_revisions_for()` raises on an undeclared id. That is fail-closed at the
resolver, but it means a policy set containing one bad id yields **no** proof at
all rather than a proof over the remainder. For a negative claim I think refusing
everything is right — a partial required set is a weaker requirement wearing the
same name — but it is a strong behaviour and nothing currently calls it.

Also: `coverage.py` is now registry-free and enforced by a grep. Is a static
import check the right enforcement, or is that the kind of test that passes while
the coupling returns by another route?

---

## 2. New surface

- **`/health` now reports `arm_revisions`** as per-state counts on the
  unauthenticated route. I excluded arm ids deliberately — the route is
  unauthenticated and the apex already leaks more than I would like. Is
  counts-only the right line, or does an operator need the ids to act, making
  this another unconsumable signal?
- **`DECLARED_ARM_IDS`** is a module-level frozenset built at import. Correct for
  a value that cannot change at runtime, but it means adding an arm requires a
  process restart before the writer will attribute to it. Acceptable?
- **`revision_lifecycle_summary()` reports `undeclared_arm`** but still decides
  nothing. Same question as last round, now with a consumer: is reporting enough?

---

## 3. Where I expect the next defect

Both live defects in the last two rounds were in code adjacent to the change:

1. **`record_listing_claims()`** now does two upserts, a lookup, an unresolved
   check that raises, and an alias upsert — in one transaction. The unattributed
   lookup key gained `listing_type` this round. If that key and the ON CONFLICT
   target ever disagree, the symptom is a raise rather than silence, which is the
   direction I want, but I would like the disagreement ruled out.
2. **The attribution loop** now filters by `spec.listing_type` in three places:
   the mismatch query, the promote query, and the `legacy_rows` select. Three
   filters that must agree.
3. **`consume_cross_crawl_conflicts()`** is unchanged, but its inputs now
   include multiple unattributed rows per release. Its `GROUP BY canonical_url
   HAVING COUNT(DISTINCT listing_type) > 1` should be strictly better off. Should
   be.

---

## 4. Test adequacy

Mutation results are in `03-evidence.md` §5. Four fixes were verified by
restoring the defect: R22-2 (8 tests fail), R22-5 (4 fail including the
consumer-level one), R21-12 source (exactly the 2 source cases), and R21-5's
count-preserving corruption.

Judge whether those mutants kill the **class** or only the specific bug.

And please check `05-retired-test-mapping.md` again. You found three overstated
**A** entries last round; they are now direct regressions. I would rather you
find a fourth than have me assert the table is right twice running.

---

## 5. Not asking

Deployment readiness. Do not propose merging, deploying or enabling anything.
