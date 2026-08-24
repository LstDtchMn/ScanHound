# Round 23 — evidence

Every number re-measured for this package on 2026-08-24. The live database was
never written to; measurements against real data use a `VACUUM INTO` copy.

---

## 1. The deployed ledger

```
rows: 266, user_version: 9
max last_seen_at: 2026-08-22T15:50:43.046582+00:00
```

Unchanged and still frozen, so nothing moved underneath the work.

## 2. Real-data rehearsal, at this head

```
deployed snapshot: 266 rows

after shape migration:
  unattributed  arm_id=None rdv=None pv=None legacy=hdencode:4k      93
  unattributed  arm_id=None rdv=None pv=None legacy=hdencode:remux   68
  unattributed  arm_id=None rdv=None pv=None legacy=hdencode:tv     105
content identical to the deployed rows: True

CHECK constraint refuses: attributed-with-no-arm_id, unattributed-carrying-an-
arm_id, unattributed-with-no-legacy-key, duplicate unattributed row

attribution: attributed=266 merged=0 quarantined=0
  arm.hdencode.4k-2160p 93 / arm.hdencode.remux 68 / arm.hdencode.tv-packs 105

non-arm_id values that reached the arm_id column: 0
  legacy-shaped  -> unattributed  legacy=hdencode:tv
  unregistered   -> unattributed  legacy=unregistered:request-v1:deadbeef
  declared-one   -> attributed    arm_id=arm.hdencode.tv-packs
```

## 3. The intermediate shape (R22-2), over the six-case matrix

Driven through the real `_init_db` against a hand-built round-20 database:

| Case | Result |
|---|---|
| 1/2 never-attributed legacy row | unattributed, evidence unaltered |
| 3 attributed with `legacy_arm_key` | **stays attributed at its exact revision** |
| 4 fresh revision-stamped, `legacy_arm_key` NULL | **stays attributed**, not stranded |
| 5 alias variant existing only in the alias table | survives, attached to the right claim |
| 6 retired parser revision | **stays retired**, not rewritten to active |

Case 4 is the one that was permanently unrecoverable before: demoting it wrote
its `arm_id` into `legacy_arm_key`, and `legacy_migration_plan()` skips a legacy
key equal to a live arm id, so nothing could ever re-attribute it.

## 4. `/health` reports the lifecycle

Against a fresh database:

```
arm_revisions: {'no_evidence': 9}
```

Counts only — no arm ids on an unauthenticated route. Verified that the key is
present (so the report has a consumer), that no key begins with `arm.`, and that
a failing sub-report still returns HTTP 200 with `status: ok`.

## 5. Mutation results

Each fix verified by restoring the defect.

### R22-2 — demote every intermediate row (the round-21 behaviour)

```
mutated: _attr := "1 = 0"
8 of 8 TestTheIntermediateShapeKeepsItsRevisions tests FAIL
```

All eight, including the alias cases — and the equivalence guard now raises
during `_init_db` rather than reporting the rebuild equivalent.

### R22-5 — take `listing_type` back out of the unattributed key

```
4 failed, 2 passed
  FAILED test_both_observations_survive_as_separate_rows
  FAILED test_the_earlier_type_is_not_destroyed
  FAILED test_the_REAL_consumer_narrows_the_release
  FAILED test_attribution_refuses_to_absorb_a_contradictory_type
```

`test_a_REPEAT_of_the_same_type_still_collapses` **passes** under the mutant,
which is the anti-vacuity control: the tests discriminate rather than
always-fail. The third failure is the consumer-level one — it calls
`consume_cross_crawl_conflicts()` and asserts a live download's authority is
actually withdrawn.

### R21-12 — remove only the source comparison

```
2 failed, 7 passed
  FAILED test_a_semantic_mutation_refuses_to_resolve[source renamed to a same-pagination mirror]
  FAILED test_it_cannot_emit_a_declared_revision_either[source ...]
```

Exactly the two source cases; type and category keep passing. The old control
used `hdencode -> ddlbase`, which also switches the pagination form and so
changes the request digest — it would have passed with the source comparison
deleted, which is the weakness it existed to rule out.

### R21-5 — a count-preserving corruption

```
count 266 -> 266   (the check this replaced passed exactly this)
rows differing: 255
CAUGHT: not content-preserving (only in old=255, only in new=255, 266 -> 266)
```

### Carried forward

R21-13's request-blind contract lookup still kills exactly the two intended
tests while the matching positive control passes.

## 6. R22-4, by injection

No valid input produces the lookup miss — the reviewer could not construct one
and neither could I. So it is injected: the `claim_id` lookup is made to return
nothing while the insert proceeds normally.

```
the write is REFUSED with "aliases would be lost"
nothing is left behind -- the claim is rolled back too
the same write succeeds without the injection, and records its alias
```

The third is the anti-vacuity control: the refusal must come from the injected
miss, not from the claim being malformed.

## 7. R22-1, at the proof boundary

```
policy resolution yields the declared active revision
the active revision present            -> satisfies
a LONE retired revision                -> "not traversed at all"   (was: satisfied)
an EXTRA retired revision alongside it -> irrelevant, not "undecidable"
two arms at the IDENTICAL revision     -> "undecidable"
an empty requirement                   -> never vacuously true
an undeclared required id              -> ArmRegistryError at the resolver
coverage.py imports of arms            -> none
```

The third and fourth lines are the pair that matters: round 21 could only refuse
on ambiguity, so a lone retired revision satisfied an active requirement, and an
extra one poisoned the whole arm. Both are now correct and they are opposite
errors.

## 8. Test suite

```
identity file:  173 tests collected, all passing
full suite:     11 failed, 5676 passed, 4 skipped in 910.02s (0:15:10)
```

The 11 are the same ones that fail at `origin/main`, baselined in the same
container earlier in this session: 8 `test_clicknload_fallback_wiring.py`, 1
`test_dv_settings.py`, 2 `test_round20_auto_resume_log_once.py`.

Passing count across this branch: 5559 → 5592 → 5634 → 5652 → 5676.
