# Round 22 — review request

Same posture as before: equal peers, disagreement expected. The exact-head pass
was materially better than the first, and two of its findings were live defects
rather than design opinions — please read this head the same way.

Preserve finding identity (R21-n stays R21-n; new ones are R22-n).

---

## 1. Where I did not comply, and why

### R21-3b — the fill/flag split is done; the filter on FILL is not

You ruled:

```
fill missing date  -> widening -> attributed/proof-eligible only
flag changed date  -> narrowing -> all observations, including unattributed
```

I split the APIs. I did **not** filter FILL by attribution state, and I want
this attacked rather than quietly accepted.

Implemented literally, the pair is a dead path. FLAG detects a change by
comparing the site's current date against the **stored** one, and FILL is the
only thing that ever stores it. An unattributed row would therefore never
acquire a baseline, so the narrowing check could never fire for precisely the
population the finding exists to protect. The literal reading defeats its own
purpose.

My reasoning for leaving FILL unfiltered: recording what the site said is an
observation, not a permission. A date widens something only when used as an
ordering key inside a coverage proof, and that requires (a) an attributed row,
(b) at an ACTIVE revision, (c) with a declared ordering contract. FILL touches
none of the three. So the widening gate stays at the proof boundary, where it is
already enforced and testable.

Attack that. Specifically: is there a path by which a filled date on an
unattributed row reaches a widening decision that I have not enumerated? If
there is, the fix is to close that path, not to filter FILL — but I would rather
be shown wrong than be right by assumption.

`TestTheTwoDateOperationsFaceOppositeDirections` pins both halves, including a
static guard that the FLAG query is not filtered.

---

## 2. New surface worth attacking

### R22 candidate — `covers_release()` refuses on an ambiguous arm

When one `arm_id` appears under two revisions in a single traversal, I now
refuse with "which proof governs is undecidable" rather than choosing. That is
fail-closed, but it is also a new way for the system to answer "no" to a
question it previously answered.

Is refusal right, or should policy resolve the required arm to its ACTIVE
revision and evaluate that one, treating the retired-revision arm as
irrelevant rather than as ambiguity? I chose refusal because the alternative
requires the evaluator to consult the registry, which would give
`coverage.py` a dependency on `arms.py` that it does not currently have — and
that coupling seemed worse than a conservative refusal. Argue it.

### R22 candidate — `revision_lifecycle_summary()` reports, and decides nothing

It returns `observed` / `active_revision_unobserved` / `no_evidence` per arm,
plus counts of rows at retired revisions and rows awaiting migration. It
deliberately gives **no** verdict on whether retired evidence may still narrow,
because that is a policy question for the coverage model and inventing an answer
here would be the same mistake as inventing an attribution.

Is "report, do not decide" the right stopping point, or is an unconsumed signal
just a different kind of silence? Nothing currently calls this.

### R22 candidate — the `is_arm_id()` guard is a shape check

```python
text.startswith("arm.") and ":" not in text
```

It is a namespace check, not a registry check: a well-formed but undeclared
`arm.made.up` would pass it and be recorded as attributed if a caller also
supplied both versions. I chose shape over registry lookup so the writer stays
independent of registry construction on a hot path. Is that the wrong trade?

### R22 candidate — the alias upsert's parse workaround

The alias merge is an `INSERT ... SELECT ... WHERE ... ON CONFLICT ... DO
UPDATE`. SQLite cannot parse the upsert unless the SELECT ends in a WHERE
clause — verified on 3.40.1, where the form without one raises
`near "DO": syntax error`. The WHERE is load-bearing syntax that looks like an
ordinary filter. Is a comment enough, or does that deserve a test that would
fail if someone "simplified" it?

---

## 3. Where I expect the next defect

Past rounds found defects in ADJACENT code, and this round's two live defects
were both one layer beyond where the change was aimed. So:

1. **`consume_cross_crawl_conflicts()`** — I changed the alias expansion to join
   through `claim_id` and deliberately left it unfiltered by attribution state.
   I have a behavioural test and a static guard, but the surrounding revocation
   logic is where a missed variant actually costs something.
2. **`record_listing_claims()` now issues two upserts and then a lookup** to map
   claims to `claim_id` for aliases. That lookup selects by `canonical_url IN
   (...)` and keys on either the revision triple or `(url, legacy_arm_key)`. Is
   there a case where a claim's `claim_id` is not found, silently dropping its
   aliases?
3. **The shape rebuild handles two source shapes** — the deployed `arm_key` one
   and the intermediate round-20 one. The intermediate shape only exists on
   developer machines. Is the `COALESCE(legacy_arm_key, arm_id)` branch right,
   and is it reachable in a way I have not considered?

---

## 4. Test adequacy

Your contract-inventory diagnosis is accepted and acted on:
`05-retired-test-mapping.md` maps all 43 retired tests with a disposition each,
and retracts the false claim.

Two things to judge:

- **The mapping itself.** Please check it the way you checked the original
  claim. If an entry marked **A** does not really exercise the same production
  path, that is the same failure again with a table in front of it.
- **The mutation results in `03-evidence.md` §5.** Three fixes were verified by
  reverting them and confirming the new tests fail. Are those the right mutants,
  or do they kill only the specific bug rather than the class?

---

## 5. Not asking

Deployment readiness. Do not propose merging, deploying or enabling anything.
