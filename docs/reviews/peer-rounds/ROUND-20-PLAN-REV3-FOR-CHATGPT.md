# ScanHound — Round 20 plan, REVISION 3

Addresses R20R2-1 through R20R2-5. Two of the five are closed by measurement
rather than by design change, and two of your "open gate" items are closed with
positive controls run since revision 2.

Your ruling was that implementation may begin once R20R2-1 through R20R2-4 are
specified. This document specifies them.

---

## 1. Disposition

| | | |
|---|---|---|
| **R20R2-1** | write sequence not atomic | **specified**, §2 |
| **R20R2-2** | quarantine has no durable contract | **specified**, §3 |
| **R20R2-3** | empty quarantine set unproven | **CLOSED by audit**, §4 |
| **R20R2-4** | 36-ALTER refactor too broad | **scope cut**, §5 |
| **R20R2-5** | monotonic sandwich too weak | **CLOSED by measurement**, §6 |
| gate 9 | cross-container lock unproven | **CLOSED**, §6 |
| gate 11 | restore never rehearsed | **CLOSED**, §6 |

Your three rulings are adopted without argument: the frozen ledger does not relax
preflight; reconstructed `parser_version` is acceptable now that producer
provenance is closed; the date backfill is split into two functions.

---

## 2. R20R2-1 — one atomic transaction

You were right and my error was basic: I wrote "commit or roll back as a unit"
and then placed two write phases outside the transaction. `apply_schema()` is DDL
and alias seeding is DML, both before `BEGIN IMMEDIATE`. Either they commit
independently and survive a failed migration, or the seed opens an implicit
transaction and `BEGIN IMMEDIATE` fails outright.

Adopted sequence, verbatim from your ruling:

```text
acquire cross-process lock                        [gate 9 PROVEN, §6]
prove application stopped / lock exclusion control [PROVEN, §6]
capture and verify backup                          [PROVEN, §6]
open live connection, isolation_level=None
integrity_check + foreign_key_check
freeze pure manifest, build read-only plan
recompute and compare input fingerprint
BEGIN IMMEDIATE
    create only migration-required schema
    seed required aliases
    apply migrated/quarantined partition
    run all invariants
    write completed audit record + output fingerprint
COMMIT
checkpoint, verify checkpoint result
```

`isolation_level=None` so the driver opens no implicit transaction and
`BEGIN IMMEDIATE` is the only transaction boundary. Explicit `ROLLBACK` in an
`except` covering everything after `BEGIN IMMEDIATE`.

**Failure injection**, one test per write phase — after schema, after seeding,
after partition, after invariants, after the audit write. Each asserts the live
database returns to the exact pre-transaction logical fingerprint and contains
**no completion record**. That second half matters more than the first: a
rollback that leaves a completion latch behind would make a rerun believe the
work was done.

---

## 3. R20R2-2 — quarantine as a durable contract

Adopted, including your correction that keying on `arm_key` alone is wrong
because evidence can differ between rows sharing a legacy key.

### 3.1 Audit table

```sql
CREATE TABLE listing_claim_migration_audit (
    migration_id                       TEXT NOT NULL,
    canonical_url                      TEXT NOT NULL,
    legacy_arm_key                     TEXT NOT NULL,
    input_row_digest                   TEXT NOT NULL,
    status                             TEXT NOT NULL,   -- migrated | quarantined
    reason_code                        TEXT,            -- stable; NOT NULL when quarantined
    reason_detail                      TEXT,
    manifest_version                   TEXT NOT NULL,
    target_arm_id                      TEXT,            -- NULL only when quarantined
    target_request_definition_version  TEXT,
    target_parser_version              TEXT,
    provenance_class                   TEXT NOT NULL,   -- recorded | reconstructed
    classified_at                      TEXT NOT NULL,
    output_fingerprint                 TEXT,
    PRIMARY KEY (migration_id, canonical_url, legacy_arm_key)
);
```

`input_row_digest` is taken over the legacy row's full content before any
rewrite, so a later reader can prove which bytes were classified.

### 3.2 Runtime shape

- authoritative rows live only in the revision-keyed `listing_claims` and
  `listing_claim_aliases`
- unresolvable legacy rows **move intact** to `listing_claims_quarantine` and
  `listing_claim_aliases_quarantine`, carrying `migration_id`
- **narrowing** consumers read one explicit helper that unions authoritative and
  quarantined observations
- **widening** consumers query authoritative only
- metrics report the two populations separately and never sum them

The union helper is a named function, not an inlined `UNION ALL`, so the six
consumer rules point at one auditable definition. Its docstring states the
asymmetry: a contradiction is a contradiction regardless of which feed saw it,
so unattributable evidence may narrow authority and may never widen it.

### 3.3 Invariants, inside the transaction

```text
legacy input rows          = migrated ∪ quarantined
migrated ∩ quarantined     = ∅
every migrated row         resolves to exactly one active ArmRevision
every quarantined row      has a non-empty stable reason_code
no unclassified legacy row remains
```

The disjoint-partition proof is what closes the latch: `migration_epoch_complete`
depends on every input row being *classified*, not on every row being *migrated*.
`ddlbase:remux` stays permanently quarantined and visible without disabling
unrelated proofs, and `legacy_quarantined_rows > 0` is reported as its own metric
so permanent quarantine is never disguised as incomplete execution.

---

## 4. R20R2-3 — CLOSED

Full evidence: `docs/reviews/peer-rounds/ROUND-20-PRODUCER-AUDIT.md`.

You ruled that listing the scratch scripts was a disclosure, not a resolution,
and that a row inserted by a script inherits no parser revision from a matching
image. Audited by **capability** rather than execution, because absence of
execution evidence proves nothing while absence of the capability to write does.

```text
callers of record_listing_claims   EXACTLY ONE: background_scanner.py:695,
                                   the crawl path. Three other hits are comments.
HTTP surface                       no API route writes claims
repo scripts                       8 reference the live DB, 3 are writable,
                                   NONE references listing_claims, none is
                                   invoked by automation
scratchpad, 209 files              every /dbvol connection is mode=ro;
                                   every writable connection targets
                                   tempfile.mkdtemp() or /tmp
```

The 67 scratchpad files that matched `listing_claims` by text are source-patching
scripts that edit `backend/*.py` and never open a database — which is what made
them look like candidate writers on the first pass.

**Quarantine set for the existing 266 rows: empty.** Stated as a capability
argument, not a transcript: `scan_history` is empty, so no first-party execution
record exists anywhere. And it is a statement about these 266 rows only — whether
`ddlbase`/`adithd` are enabled live is still unread.

---

## 5. R20R2-4 — scope cut

Accepted. The shared-schema extraction is dropped from this migration's critical
path.

The maintenance command depends on a small stdlib-only primitive creating **only**
what listing-claim revision identity, quarantine and audit require:

```text
listing_claims                     revision-keyed
listing_claim_aliases              revision-keyed
listing_claims_quarantine
listing_claim_aliases_quarantine
listing_claim_migration_audit
their indexes
```

No legacy column backfills, no table rebuilds, no `user_version` write, no
unrelated ALTERs. The 36-ALTER consolidation, the `download_results` PK rebuild
and the `jd_confirmed_name` gate stay where they are, to be reviewed separately
if ever. `SCHEMA_VERSION` stays at 9 — bumping it flips the live RSS shadow
qualification to not-ready, and nothing here needs it.

---

## 6. R20R2-5 and gates 9 and 11 — closed by measurement

All three were run against the live system since revision 2.

### Cross-container lock — gate 9, both directions

```text
app holding the lock  -> second container on the same volume, same UID: REFUSED
                         "Another ScanHound instance is already using this
                          data directory."
app stopped           -> lock acquires cleanly and releases
```

Run with the production volume `scanhound_scanhound_db` and production UID 0.

### Backup under stopped-writer — R20R2-5

Exact equality, as you required, replacing the monotonic sandwich:

```text
source          62,648,320 B + 4,124,152 WAL + 32,768 SHM
VACUUM INTO     59,527,168 B in 0.17 s
                integrity_check ok, foreign_key_check ok, user_version 9
source == backup   TRUE across all 37 tables, 81,904 rows, ZERO differing
source unchanged   TRUE
```

### Restore rehearsal — gate 11, end to end

```text
original moved aside (never deleted), backup placed as crawler.db
app started    journal_mode self-healed delete -> wal, user_version 9,
               integrity ok, served real queries
counts         266 / 753 / 516 / 538 / 4360 -- identical to source
reverted       original restored, every count matches pre-stop, integrity ok
```

Two findings from the run: the rehearsal window wrote **zero** durable rows, so
reverting lost nothing; and `VACUUM INTO` yields a database in `delete` journal
mode which self-heals to `wal` on first open — assumed previously, now observed.

Not yet done: this rehearsal ran as production UID 0 on the same host. A restore
from *durable* storage rather than from the volume, and available-space checks,
remain untested.

---

## 7. The date backfill split — your ruling 3

```text
fill_missing_dates_from_attributable_claims()   WIDENING
    excludes quarantine IN SQL, before LIMIT
flag_changed_dates_from_all_observations()      NARROWING
    includes quarantined evidence
```

Separate queries, separate transactions, separate metrics, separate tests,
separate call sites. A shared row-decoding helper only. Your reasoning is
recorded in both docstrings: keeping opposite authority directions in one
function lets a future cleanup or early return silently turn fail-closed into
fail-open.

---

## 8. Still open, unchanged

- **No round-20 code has ever been executed.** No rehearsal against a copy of the
  migration itself, no suite run, no image built. Your rehearsal requirement
  remains unmet and this document does not claim otherwise.
- **Restore from durable storage** is untested; only the volume-local path was
  rehearsed.
- **`apply_schema()` first-run delta** — now moot for the broad refactor, but the
  narrow primitive's delta against live still needs enumerating before it runs.
- **Whether `ddlbase`/`adithd` are enabled live** — unread, and it decides whether
  the quarantine set stays empty after redeploy.
- **`scan_history` is empty**, so no first-party scan log exists to corroborate
  anything.

## 9. One question

The audit table stores `provenance_class` per row. For the 266 rows it will read
`reconstructed` uniformly, because the deployed image had no parser-version
constant. Is a uniform `reconstructed` classification across an entire migration
worth flagging as its own condition — something a later proof consumer could
refuse on — or is per-row provenance sufficient given the manifest records the
source commit and blob hash?
