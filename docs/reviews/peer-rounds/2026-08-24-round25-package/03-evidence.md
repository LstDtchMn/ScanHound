# Round 25 — evidence

Every number measured on 2026-08-24. The live database was never written to.

Each finding was **reproduced before the fix and re-run after**, so what follows
is a before/after pair rather than "a test went red".

---

## 1. Before the fixes — all six reproduced

```
R23-1a coupled edit restores the defect        CONFIRMED
R23-1b writer accepts wrong-type attributed    CONFIRMED
R23-1c lifecycle counts it as active           CONFIRMED
R24-1 healthy DB quarantined on IntegrityError CONFIRMED
R24-2 semantic no-op breaks resolution         CONFIRMED
R23-2 alias audit loses recoverable type       CONFIRMED
```

## 2. After

```
R23-1a coupled edit restores the defect        not reproduced
R23-1b writer accepts wrong-type attributed    not reproduced
R23-1c lifecycle counts it as active           not reproduced
R24-1 healthy DB quarantined on IntegrityError not reproduced
R24-2 semantic no-op breaks resolution         not reproduced
R23-2 alias audit loses recoverable type       not reproduced
```

### A correction to my own instrument

Two of those initially still read CONFIRMED after the fixes. Both were faults in
the verification script, not the code:

- `fresh()` deleted only `.db`, `-wal` and `-shm`, so a stale `.corrupt.*` file
  from an earlier run was reported as a fresh quarantine;
- the R23-1a check tested the registry layer, which by design cannot know
  history — the durable table is the layer the finding is about.

Recorded because a false CONFIRMED is the same class of instrument fault as a
false pass.

## 3. R24-1 — the measurement that matters

Before, on a file that passes SQLite's own check:

```
integrity_check: ok
DATABASE CORRUPTION DETECTED at /tmp/… — quarantining and rebuilding
    UNIQUE constraint failed: hdencode_candidates.guid
Renamed corrupt DB to /tmp/….corrupt.1787599727. Creating fresh DB.
init_db did NOT raise
rows left at the original path: 0 (was 2)
```

After:

```
integrity_check: ok
DB CONSTRAINT VIOLATED during init — this is a schema/data conflict, NOT
    corruption; the database is untouched and startup is refused
REFUSED as a constraint violation: UNIQUE constraint failed
quarantine files: none
rows still at the original path: 2 (was 2)
```

**The other direction still works**, which matters more than the fix:

```
a file that is not a database at all -> DATABASE CORRUPTION DETECTED,
                                        quarantined: True
```

Classifier, directly:

```
IntegrityError (UNIQUE)        -> corruption evidence: False
DatabaseCorruptionDetected     -> corruption evidence: True
OperationalError (locked)      -> corruption evidence: False
OperationalError (malformed)   -> corruption evidence: True
ProgrammingError               -> corruption evidence: False
```

### Live exposure

```
idx_hdencode_candidates_guid present: True
hdencode_candidates rows: 7205, duplicate guids: 0
```

**Not armed today.** But six unique indexes are built during init over tables
that may already hold data:

```
idx_download_results_uuid      idx_bookmarks_imdb
idx_bookmarks_title_key        idx_hdencode_candidates_guid
idx_hdencode_actions_active    idx_download_queue_active_item
```

## 4. R23-1a — the coupled edit

```
uncoupled edit (pin left old)  : refused
COUPLED edit (pin updated too) : registry BUILDS (by design)
meaning changed while the revision is IDENTICAL: True
durable history REFUSED it: 'arm.hdencode.tv-packs' meant something different
    when this database first saw it on 2026-08-24T…
```

First open of a fresh database records all 9 arm meanings.

## 5. R23-1b / R23-1c — the writer and the lifecycle

Before:

```
declared arm listing_type : tv
written row               : ('attributed', 'arm.hdencode.tv-packs', 'movie')
lifecycle rows_at_active_revision: 1   state: observed
```

After:

```
wrong-type movie -> ('unattributed', None, 'arm.hdencode.tv-packs', 'movie')
matching tv      -> ('attributed', 'arm.hdencode.tv-packs', None, 'tv')
lifecycle rows_at_active_revision: 1  state: observed   (the tv row only)
```

The provenance survives on the refused row — the observation is real, it simply
is not evidence *of that arm*.

## 6. R23-2 / R24-2

```
claim audit kept its type : 'movie'
alias audit type          : 'movie'   (was '')
a genuinely unrecoverable type still records ''

listing_type 'tv' -> 'TV' leaves the fingerprint unchanged: True
the real shipped descriptor still resolves: True   (was False)
```

## 7. R21-10 — the sixth entry

```
legacy_migration_plan(['gone:4k']) -> plan={} unresolved=['gone:4k']
```

The previously mapped test called only `resolve_legacy(...) is None`, which
cannot distinguish "reached the unresolved bucket" from "dropped", "treated as
modern", or "bucket logic mishandled".

## 8. Test suite

```
identity file:  250 tests, all passing
full suite:     11 failed, 5754 passed, 4 skipped in 928.04s (0:15:28)
```

The 11 are the same ones that fail at `origin/main`.

**One regression, caught before commit.** The first run of this change produced
**12** failures:
`test_init_depth_resets_after_recovery_failure` injected a generic
`DatabaseError("boom")` to reach the recovery path, which the narrowing
correctly no longer treats as corruption. Its intent still holds, so the
fixture names corruption explicitly and a companion asserts the new refusal.
Named here rather than quietly adjusted.

Passing count across this branch:
5559 → 5592 → 5634 → 5652 → 5676 → 5690 → 5720 → 5754.

## 9. Real-data rehearsal, unchanged

```
266 rows migrate shape-only, content identical to the deployed rows
attribution: 266 across the three declared hdencode arms
non-arm_id values reaching the arm_id column: 0
```
