# Round 24 — evidence

Every number re-measured for this package on 2026-08-24. The live database was
never written to; measurements against real data use a `VACUUM INTO` copy.

---

## 1. The deployed ledger

```
rows: 266, user_version: 9
max last_seen_at: 2026-08-22T15:50:43.046582+00:00
```

Unchanged and still frozen.

## 2. The four findings, measured BEFORE the fix

I verified each Round-23 claim against the running code rather than accepting
the report. All four reproduced:

```
R23-3 guard NOT independent                CONFIRMED
R22-3 unregistered becomes attributed      CONFIRMED
R23-2 quarantine loses a snapshot          CONFIRMED
R23-1 semantic type change collapses       CONFIRMED
```

Detail worth quoting, because it is worse than the report stated. For R23-1:

```
declared listing_type tv    -> revision (arm.hdencode.tv-packs, request-v1:5a4a…, select_posts/1)
same arm, listing_type movie-> revision (arm.hdencode.tv-packs, request-v1:5a4a…, select_posts/1)
a semantic type change leaves the revision IDENTICAL: True
attributed rows after writing movie then tv: [('movie', 2)]
```

The contradicting observation did not merely vanish. It was counted as a second
**sighting** of the claim it contradicts — corroboration built out of a
disagreement.

For R23-2:

```
live unattributed rows: ['movie', 'tv']
report says quarantined = 2
quarantine table holds  = 1  ['tv']
```

For R22-3:

```
state=attributed  arm_id=arm.unregistered.bbbbbbbbbbbbbbbb  legacy=None
is that arm declared? False
unscheduled search: state=attributed arm_id=arm.unscheduled.search
```

## 3. And AFTER

```
R23-1  attributed rows after movie then tv: [('movie', 1), ('tv', 1)]
R23-2  live [movie, tv] | report 2 | quarantine 2 [movie, tv] | alias audit 2
R22-3  arm.unregistered.bbbb…   REFUSED
       arm.unscheduled.search   REFUSED
R23-3  see §5
```

## 4. The semantic pin

```
registry builds: 9 arms
redeclaring listing_type under a stable id -> SemanticRedeclaration
redeclaring source (same pagination)       -> SemanticRedeclaration
redeclaring category                       -> SemanticRedeclaration
a NEW arm with no pin                      -> SemanticRedeclaration
an unchanged declaration                   -> builds
```

Two shipped arms legitimately share a fingerprint —
`arm.ddlbase.remux-4k` and `arm.ddlbase.remux-1080p` both mean
ddlbase/remux/movie and differ only in what they fetch. That is asserted, not
worked around: a fingerprint is not an identity, it is the thing that must not
drift under one.

## 5. R23-3 — two INDEPENDENT mutants

The check this replaces was handed its old-side projection by the migration, so
one edit moved both sides together. Now:

```
MUTANT 1 -- change ONLY the forward migration classifier
  validator RAISED: ... 1 row-state(s) the source requires are absent and
                        1 present that it does not permit ...

MUTANT 2 -- change ONLY the validator, leave the migration correct
  validator RAISED on correct output
  -> it is load-bearing, not decorative

REGRESSION -- a faithful migration of the deployed shape still works
  deployed-shape row: ('unattributed', 'hdencode:tv', 7)
```

**And the correction to the round-23 package.** That document claimed the old
guard raised under a consistent `_attr` mutation. It did not, and I had never
run it. Measured against a database with one attributed row and NO aliases, so
that only the guard could object:

```
guard did NOT raise under the mutation
the attributed row became: ('unattributed', None, None)
```

Corrected in `38aed58`.

## 6. Mutation results

| Fix | Mutation | Result |
|---|---|---|
| R23-3 | change only the migration classifier | validator raises |
| R23-3 | change only the validator | raises on correct output |
| R22-2 | demote every intermediate row | 8 of 8 behavioural tests fail |
| R23-1 | — | the pin refuses at construction; three mutations tested |
| R21-10a | writer keeps only the claim `raw_url` | 3 of 3 consumer tests fail, incl. the precondition |
| R22-5 | remove `listing_type` from the unattributed key | 4 fail; the same-type-repeat control still passes |
| R21-12 | remove only the source comparison | exactly the 2 source cases fail |

## 7. Two defects found in my own work

### The validator would have refused every migration

`sqlite3.Row` never compares equal to a tuple, and the production connection
sets `row_factory` while the unit fixtures do not. Both mutants still "raised",
so it looked like a working guard — they were raising for the wrong reason.
Rows are coerced now, and there are tests on a production-shaped connection with
an anti-vacuity companion.

### A migration defect was filed as database corruption

`init_db()` catches `sqlite3.DatabaseError` and quarantines the file.
`sqlite3.IntegrityError` is a subclass. Measured on the real 266-row copy, with
a mutation that made every row collide:

```
BEFORE: DATABASE CORRUPTION DETECTED at /tmp/… — quarantining and rebuilding
        Renamed corrupt DB to /tmp/….corrupt.1787592441. Creating fresh DB.

AFTER:  REFUSED as a migration defect: listing_claims rebuild violated a
        constraint during the shape migration (UNIQUE constraint failed: …)
        quarantine files created: none
        rows still in the original database: 266
```

## 8. Log spam

`/health` now reads the lifecycle report, and a watchdog polls `/health`
continuously.

```
ten reads of an unchanging problem -> 1 warning line
a change in which arms are affected -> a second line
a clean registry                    -> nothing
```

The 2026-08-23 precedent: one unchanging queue condition produced 993 of 3,172
log lines, 31% of the file.

## 9. Test suite

```
identity file:  217 tests, all passing
full suite:     11 failed, 5720 passed, 4 skipped in 912.47s (0:15:12)
```

The 11 are the same ones that fail at `origin/main`, baselined in the same
container earlier in this session.

Passing count across this branch:
5559 → 5592 → 5634 → 5652 → 5676 → 5690 → 5720.
