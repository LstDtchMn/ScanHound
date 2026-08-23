# ScanHound — Round 20, PLAN review (before implementation)

**This is not a code review.** Two of your four Round-19 findings are closed and
are summarised briefly below. The other two — M19-1 (arm identity) and M19-2
(migration date loss) — are **not built**, deliberately, because both change
things that are hard to undo:

- M19-1 changes the arm identity, which goes into the durable ledger permanently
- M19-2 rewrites existing evidence rows on a live database

I would rather you attacked the plan than the code. Reviewing a plan costs one
round; discovering the migration order was wrong after M19-1 and M19-2 are built
on top of it costs three.

---

## 1. State

```text
repository    LstDtchMn/ScanHound
branch        fix/round12-attestation-authority
head          8765293
base          main @ 6ac5cd2   (0 behind)

DEPLOYED      The RUNNING container is still the ROUND-14 code.
              Nothing from rounds 16-20 is deployed.
```

Live ledger, read-only, measured 2026-08-23 01:0xZ:

```text
listing_claims           266 rows across 3 keys
    hdencode:tv          105
    hdencode:4k           93
    hdencode:remux        68

listing_claim_aliases    TABLE DOES NOT EXIST
```

That last line matters and I did not appreciate it until now: the alias table is
a round-15 artefact and the deployed container predates it. So on first
deployment `init_db` will CREATE it and seed it from the 266 claim rows, and only
then does any migration have alias rows to move. The alias-collision case you
raised cannot occur on the first run — there is nothing to collide with.

## 2. Round-19 findings — disposition

| | | |
|---|---|---|
| **M19-1** | arm identity omits request semantics; collision refusal not in the production path | **PLANNED, §3** |
| **M19-2** | migration erases a newly observed date disagreement | **PLANNED, §4** |
| **M19-3** | first live migration must not be a lazy side effect | **CLOSED**, `8765293` |
| **M19-4** | per-crawl claim dedup uses the raw URL | **CLOSED**, `0993ddf` |

### M19-3, closed

`scan_once()`'s call to `migrate_listing_claim_arm_keys()` is removed, with
nothing put in its place. The method remains, disarmed, for the operator tool.

Your ruling was right for a reason I had not seen. I assumed a claim-row
collision required a deploy/rollback/redeploy. It does not:
`backfill_listing_claim_posted_dates` writes **today's** date string onto the
new-arm row while the legacy row still holds an older one, so the M19-2 date
disagreement fires on the **first ordinary run**.

6 tests. The load-bearing assertion is `call_count == 0` — a test checking that
the scan completes, or that claims were recorded, passes either way, because the
migration ran and *succeeded* before the change. Mutation: re-arming the call
kills 3 of 6, three positive controls surviving.

### M19-4, closed

`_arm = (post_url, ...)` → `(_canonical_url, arm_key)`. You were right that I
had done half of it in round 19 — I changed the arm component and left the URL
raw. `canonicalize_listing_url()` was also being called three times per post;
now once, so the sighting identity, the duplicate check, the seen-set and the
claim key cannot drift apart.

Verified the assignment dominates every use: the only early exit before it is
the empty-`post_url` `continue`, which skips the assignment *and* all four uses.
My first check for this looked only at loop-body-level `continue`s and would
have missed a nested one; redone at any depth.

7 tests through the real crawler and a real `DatabaseManager`, including the
control in the other direction — two DIFFERENT releases must still produce two
claims. Mutation: reverting the key kills 2 of 7, five controls surviving.

---

## 3. PLAN — M19-1, the arm identity

### 3.1 Shape

Per your specification, plus a decision the owner has made on naming:

```text
ArmSpec
    arm_id                      arm.hdencode.4k-2160p     stable, declared
    source                      hdencode
    category                    4k
    listing_type                movie
    request_template            normalized host + path + query + PAGINATION FORM
    request_definition_version  digest of request_template
    parser_version              select_posts/1
```

`arm_id` uses **dots, not colons**, deliberately. The old key was
`hdencode:4k:2160p`; anything still splitting on `:` would silently mis-parse a
new key rather than fail. A different separator makes that a crash, not a
wrong answer.

`request_template` includes the **pagination form**, not just path and query.
The crawler builds page-N URLs in three different shapes; two feeds identical in
path and query but paginated differently are not the same request.

### 3.2 Validation in the production path

Your finding was that `ArmRegistry` refuses collisions but production never
builds one — `default_registry()` was called in exactly one place, inside the
migration, whose exception was caught.

Planned: construct and validate the complete registry at application startup and
on config reload, **before the scanner is ready**, and have traversal and ledger
production consume validated `ArmSpec` objects rather than recomputing a key
from an arbitrary descriptor. The `traversal_arms.setdefault(arm_key, arm)` fold
is deleted; an unstamped or unregistered descriptor raises a typed `UnknownArm`
**before any fetch**.

Four refusals, not one:

```text
duplicate arm_id
duplicate request_template
ambiguous supersedes mapping
a supersedes entry equal to a registered arm_id
```

The blast-radius review found a **second** real construction site I would have
missed — `ui/controllers/scanner_controller.py:414` — so validation must happen
in both, and re-validate inside `run_scan`.

### 3.3 Contracts

`ORDERING_CONTRACTS` becomes keyed on
`(arm_id, request_definition_version, parser_version)`. A suffix-only change to
a feed changes the digest, so a contract cannot inherit request semantics it
never examined. `covers_release(required_arm_keys)` must move to `arm_id` in the
same commit.

### 3.4 The consumer surface

Everything keying on `arm_key`: `listing_claims` and `listing_claim_aliases`
(both PRIMARY KEYs), `ORDERING_CONTRACTS`, `media_kind_coverage_summary`,
`consume_cross_crawl_conflicts`, the coverage evaluator, ~17 test files.

Three consumers have **no arm filter at all** — `consume_cross_crawl_conflicts`,
`media_kind_coverage_summary`, and the date backfill — so a quarantined or
unmigrated row still acts through them. I intend to state that explicitly rather
than quietly rely on it.

---

## 4. PLAN — M19-2, the migration

### 4.1 The defect, reproduced

Not argued — run:

```text
BEFORE   two rows, one release, DIFFERENT dates, both changed=0
           hdencode:4k         posted_date_raw = August 19, changed = 0
           hdencode:4k:2160p   posted_date_raw = August 20, changed = 0

AFTER    posted_date_raw     = August 20, 2026 at 10:00 PM
         posted_date_changed = 0            <-- should be 1
         sightings           = 2
```

The merge updates `first_seen_at`, `last_seen_at`, `sightings` and
`posted_date_changed`, and never touches `posted_date_raw`. So the migration
observes two dates and records that the date never changed — and that flag is
exactly what disqualifies a release from anchoring a frontier.

### 4.2 Corrected semantics

```text
both dates NULL        -> NULL, changed unchanged
one NULL               -> keep the non-null value
equal                  -> keep it, changed unchanged
UNEQUAL                -> posted_date_changed = 1, and BOTH raw values
                          retained in a durable conflict record before the
                          losing row is deleted
```

Survivor selection when unequal is currently implicit in `sorted()` over key
spelling, which is arbitrary; it becomes `last_seen_at`.

### 4.3 Migrate exactly once

The live ledger holds only the 2-part legacy shape. The 3-part shape from round
19 **has never reached it**, because the migration was only ever called from the
lazy path that is now removed and the branch was never deployed.

Therefore: **land the identity change first, then migrate 2-part → `arm_id`
directly.** There is no intermediate state to unwind. This is the single most
important sequencing decision in the plan and I would like it challenged.

Resolvability is preserved: `hdencode:tv`, `hdencode:4k` and `hdencode:remux`
each map to exactly one registered arm. `ddlbase:remux` still maps to two and is
still refused — those rows stay legacy and are logged.

### 4.4 The live run

Explicit, per your ruling. The blast-radius review surfaced three traps I would
have walked into:

1. **`DatabaseManager.__init__` opens a revocation session** (`database.py:120`),
   appending `SESSION_OPEN` to the live journal. An unclosed session withdraws
   `media_kind` app-wide. The runner must use bare `sqlite3.connect`.
2. **4.0 MB sits in `crawler.db-wal`**, same mtime as the database. A copy of the
   `.db` alone silently omits it. `PRAGMA wal_checkpoint(TRUNCATE)` first, and
   snapshot `.db` + `-wal` + `-shm`.
3. **A runtime writer lock is held** by the running container. A concurrent
   crawl's claims are silently swallowed, so the container stops first.

Order: deploy → `init_db` creates and seeds the alias table → dry run against a
copy **outside `/dbvol`** → resolvability gate → execute on the copy → verify
invariants → execute live in one transaction → retain the report.

`dry_run` must raise a sentinel to force rollback; `transaction()` has no
rollback-on-success, so without it the invariants are predictions rather than
measurements.

---

## 5. What I want challenged

1. **The sequencing in §4.3.** I claim one migration, 2-part → `arm_id`, because
   the 3-part shape never reached live data. If that is wrong, or if a
   half-migrated ledger is reachable some way I have not considered, the
   consequence lands on evidence rows.
2. **`request_definition_version` as a digest.** A digest changes on any
   normalisation change, including a cosmetic one, silently invalidating
   contracts. Is an explicit hand-incremented version safer, at the cost of
   someone forgetting to bump it?
3. **The three unfiltered consumers in §3.4.** Is documenting them sufficient
   for this round, or must they gain an arm filter before any migration runs?
4. **Whether quarantined rows can latch `identity_migration_incomplete`
   forever.** `ddlbase:remux` is permanently unresolvable by design. If the
   capability keys on "any unmigrated row", it never clears. What should the
   clearing condition be?
5. **Anything in §4.4 that is still unsafe.** Three traps were found by review
   rather than by me, which is not a reassuring ratio.

## 6. Not covered

No CI attestation for this head. Gates 5, 6 and 9 — required-arm policy,
persistence, deployed persisted-run inspection — remain deliberately unbuilt.
`ORDERING_CONTRACTS` is still empty, no caller sets `attest_coverage=True`, and
nothing writes `category_attested`.
