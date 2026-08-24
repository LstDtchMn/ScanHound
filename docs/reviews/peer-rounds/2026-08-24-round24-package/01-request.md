# Round 24 — review request

Your last three exact-head passes each found a live defect, and each time it was
one layer past where my change was aimed. Read this head the same way.

Preserve finding identity: R23-n stays R23-n, new ones are R24-n.

---

## 1. Where I did not comply — R23-1

You offered two repairs and preferred the first. I took the second, and I want
that argued rather than accepted.

**Your Option A:** version the semantic fields inside the evidence revision.
**Your Option B:** make them immutable under the arm id, *if* immutability is
genuinely enforced rather than asserted in a comment.

I took B. The reason is history, not taste. An already-attributed row from an
earlier shape carries no semantic fingerprint. Widening the revision would force
the shape migration to do one of three things with those rows:

- invent a fingerprint from the current declaration — which is the exact
  "invent an attribution" sin, and doubly wrong here since the whole finding is
  that the declaration may have *changed*;
- refuse them — but R22-2 cases 3, 4 and 6 are your own requirement that they
  be preserved at their exact recorded revision;
- write a sentinel — and R21-1 was about removing sentinels.

Option B has no such problem, because it makes the divergence unrepresentable
going forward rather than needing to describe it backwards.

**The enforcement.** Every declared arm's meaning is fingerprinted from
`source`, `category` and `listing_type`, and PINNED in `DECLARED_SEMANTICS`,
checked at registry construction. Three things are refused:

- a changed meaning under a stable arm id;
- a **new** arm with no pin, so the table cannot be forgotten;
- and the refusal prints the value to paste, so a deliberate change is cheap
  to record and there is no incentive to route around the guard.

**A consequence I want checked.** A redeclared arm cannot coexist with the old
one: the registry already refuses two arms sharing a request definition, so
redeclaration *replaces*. I think that is right — the same bytes cannot mean two
things at once — and it gives exactly the property your regression 4 asked for,
since the old evidence then sits under a retired arm id that cannot satisfy a
requirement naming the new one. But it means a semantic correction is a
harder operation than it looks. Is that acceptable, or does it need an explicit
supersession path?

**Attack specifically:**
- Is there a route by which the meaning changes without `DECLARED_SEMANTICS`
  noticing? The fingerprint covers three fields; `resolve_descriptor()`
  validates exactly those three. If they ever diverge, the guard has a hole.
- The pin is compared at registry construction only. Is once-per-process
  enough, given `DECLARED_ARM_IDS` is also an import-time snapshot?
- I added `listing_type` to the attributed unique index as a second line, on
  the grounds that a claim whose stamped revision and stamped `listing_type`
  disagree is a producer defect the pin cannot see. Is that belt-and-braces, or
  does it paper over a case I should have made unrepresentable instead?

---

## 2. The new validator — the thing I most want stress-tested

`validate_shape_migration()` takes two table names and nothing else. It
determines the source shape itself, reads raw rows, and computes the permitted
destination in Python. Two independent mutants pass:

```
mutate ONLY the forward classifier -> the validator raises
mutate ONLY the validator          -> it raises on correct output
```

But note what building it exposed: my **first** version compared plain tuples
against `sqlite3.Row` objects and would have refused every real migration —
while both mutants still "raised", so it looked correct. That is the third time
in three rounds a verification artifact of mine asserted less than it appeared
to. So:

- Is the two-mutant discipline actually sufficient, or does it have the same
  weakness — both mutants can "pass" for a reason unrelated to the property?
- The validator's permitted-destination rules are written by the same person as
  the migration. Independence of *implementation* is not independence of
  *belief*. Is there a structural fix for that, or is the reverse-projection
  idea you floated the best available?

---

## 3. New surface

- **`migration_execute()`** re-raises `IntegrityError` as `ShapeMigrationRefused`
  so a migration defect is not filed as corruption. It covers the two rebuild
  statements only. The broader behaviour — any `sqlite3.DatabaseError` during
  `init_db()` quarantines the file — is pre-existing and unchanged, and I have
  not found a live trigger. Should the classification be narrowed globally, or
  is per-statement opt-in the right scope?
- **The lifecycle warning** is now suppressed unless its content changes.
  Nothing clears the suppression, deliberately: two earlier attempts at this
  pattern cleared it on a nearby success path and defeated it entirely. Is
  content-keyed suppression right, or should a persistent condition re-warn on
  some interval?

---

## 4. Where I expect the next defect

1. **`record_listing_claims()`** — the attributed lookup key gained
   `listing_type` this round, so the ON CONFLICT target, the `ids` map key and
   the R22-4 unresolved check must all agree on a five-part key. A disagreement
   now raises rather than losing aliases, which is the direction I want, but I
   would like it ruled out.
2. **The quarantine rebuild paths** run only when the old shape is found, and
   they preserve rows. Two rebuild paths now exist in `init_db` for tables that
   only I have. Are they reachable in a state I have not considered?
3. **`DECLARED_SEMANTICS` and `DECLARED_ARM_IDS`** are both import-time
   snapshots of the same declarations, derived independently. If one is updated
   and the other is not, what breaks and does anything notice?

---

## 5. Test adequacy

Mutation results in `03-evidence.md` §6. Judge whether they kill the class or
the instance.

And `05-retired-test-mapping.md` now carries a **third** correction. Two
separate reviews each found an overstated `A` in a table written specifically to
stop overstating. Please look again — a fifth would be more useful to me than a
clean bill.

---

## 6. Not asking

Deployment readiness. Do not propose merging, deploying or enabling anything.
