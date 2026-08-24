# Round 21 — evidence

Every number below was measured on 2026-08-23/24 for this package. Nothing is
carried forward from an earlier round.

The live database was never written to. Measurements against real data use a
`VACUUM INTO` copy — not a file copy, because a naive `.db` copy taken 183 ms
after a good one was previously found to be missing 27 individually-named
committed rows while still passing `integrity_check`, `quick_check` and
`foreign_key_check`.

---

## 1. The deployed ledger, re-measured at package time

Read-only connection to `/dbvol/crawler.db` in the running `scanhound` container:

```
total: 266
  hdencode:tv        105
  hdencode:4k         93
  hdencode:remux      68
max last_seen_at: 2026-08-22T15:50:43.046582+00:00
user_version: 9
```

Two things follow, and both matter to the review:

- **Only three legacy keys exist, and all three resolve unambiguously.** The
  ambiguous key `ddlbase:remux` — the one two live feeds both claim, and the
  reason quarantine exists — **is not present in the live data at all.** So on
  the real database the quarantine path moves **zero** rows. It is there for
  correctness, not because it is load-bearing today. I would rather state that
  plainly than let the design read as though it were doing live work.
- **`listing_claim_aliases` does not exist in the deployed database** (sqlite
  reports "no such table"). It is branch-only, which is why its DDL has no
  rebuild path.

The ledger is frozen: `max(last_seen_at)` has not moved since 2026-08-22, a
known consequence of deploying main-based work. No live harm; it removes the
concurrent-writer hazard during this work.

## 2. Shape migration against the real 266 rows

`check_shape.py`, run in a throwaway container against the `VACUUM INTO` copy.
Rows are compared as **per-row tuples**, not counts — equal counts can hide a
swap.

```
before: 266 rows, {'hdencode:4k': 93, 'hdencode:remux': 68, 'hdencode:tv': 105}, user_version=9
after:  266 rows

rows lost:   0
rows gained: 0
arm_id != legacy_arm_key:      0
rows with a version filled in: 0   (must be 0 -- attribution is gated)
per-arm counts: {'hdencode:4k': 93, 'hdencode:remux': 68, 'hdencode:tv': 105}
primary key: ['canonical_url', 'arm_id', 'request_definition_version', 'parser_version']
listing_claims_quarantine              present
listing_claim_aliases_quarantine       present
listing_claim_migration_audit          present
user_version: 9 (was 9)
second _init_db: 266 rows, identity preserved: True
```

`user_version` staying at 9 is not cosmetic:
`docs/feature-pack-review/qualification/scripts/05_shadow_evidence.py` **blocks**
on `user_version != 9`, so a bump would flip the live RSS shadow qualification
to not-ready for a change that only adds tables.

## 3. Attribution against the real 266 rows

Dry run first:

```
DRY RUN  applied=False attributed=266 merged=0 quarantined=0
         resolved: {'hdencode:4k': 'arm.hdencode.4k-2160p',
                    'hdencode:remux': 'arm.hdencode.remux',
                    'hdencode:tv': 'arm.hdencode.tv-packs'}
         ledger unchanged, no audit rows written
```

Then apply:

```
APPLY    attributed=266 merged=0 quarantined=0 skipped=[]
  arm.hdencode.4k-2160p    request-v1:bfef71ffaa152e8 select_posts/1   93 rows
  arm.hdencode.remux       request-v1:1b02820cd4d1bd4 select_posts/1   68 rows
  arm.hdencode.tv-packs    request-v1:5a4a454086cacd0 select_posts/1  105 rows
  every row carries an ACTIVE revision

AUDIT
  1 hdencode:4k      attributed  arm.hdencode.4k-2160p   rows=93   preimage=205B reconstructed
  2 hdencode:remux   attributed  arm.hdencode.remux      rows=68   preimage=205B reconstructed
  3 hdencode:tv      attributed  arm.hdencode.tv-packs   rows=105  preimage=193B reconstructed

RE-APPLY attributed=0 merged=0
```

`provenance_class='reconstructed'` is deliberate and is the honest label: the
deployed writer had **no parser-version constant at all**, so `select_posts/1`
is justified for the migrated rows by **byte identity of the parser to commit
`ef2fb188`**, not by reading a recorded value. The audit says so rather than
implying the version was observed.

## 4. Injected cases the live data cannot exercise

```
INJECTED merged=1 quarantined=1 skipped=['ddlbase:remux']

MERGE of a pair that DISAGREE on posted_date_raw -> 1 row
  posted_date_raw      August 19, 2026 at 09:00 AM
  posted_date_changed  1
  sightings            11   (4 + 7)
  first_seen_at        2026-08-01T10:00:00.000000+00:00
  last_seen_at         2026-08-20T10:00:00.000000+00:00

QUARANTINE 1 row
  u/ambig  ddlbase:remux  sightings=2
  left in the ledger: [('ddlbase:remux', '')]
```

## 5. Atomicity, proven by behaviour

Reading the context manager is not sufficient: sqlite3 in autocommit mode makes
`rollback()` a silent no-op, and the docstring would still claim atomicity. So
the failure was injected. The registry is wrapped so the **last** of three keys
raises, meaning the first two have already written when it lands:

```
injected failure fired after 2 of 3 keys had written
after failure: 266 rows, 0 attributed, 0 audit rows
ROLLED BACK: 161 rows had been written and none survived
```

161 = 93 + 68. No attribution survived, and no audit row survived either — the
audit is written in the same transaction as the work it describes, so a rollback
cannot leave a record of work that was undone.

## 6. Mutation testing — do the tests have teeth?

My first draft of the merge test could **not** distinguish the fix from the bug:
both rows ended with the same `last_seen_at`, so the target row won on the `>=`
tiebreak rather than on recency. The fixture now sets the two values explicitly
and asserts they differ.

Reverting the fix (`date = t_date if newer_is_target else l_date` →
`date = t_date`, the round-19 drop-the-date behaviour) gives:

```
FAILED TestCollisionsAreMergedNotClobbered::test_the_LEGACY_date_wins_when_the_legacy_row_was_seen_later
1 failed, 7 passed, 55 deselected
```

Exactly one test fails, and it is the one added after the weakness was found —
confirming the earlier 62-test version would have passed a reverted fix.

## 7. Pagination, checked against the crawler's real descriptors

Descriptors come from the actual `_build_sources()`, not hand-written:

```
producer emitted 10 descriptors
  4K Movies            -> arm.hdencode.4k-2160p
  Remux Movies         -> arm.hdencode.remux
  TV Packs             -> arm.hdencode.tv-packs
  DDLBase WEB-DL 4K    -> arm.ddlbase.webdl-4k
  DDLBase Remux 4K     -> arm.ddlbase.remux-4k
  DDLBase Remux 1080p  -> arm.ddlbase.remux-1080p
  Adit-HD 4K           -> arm.adithd.4k
  Adit-HD Remux        -> arm.adithd.remux
  Adit-HD TV           -> arm.adithd.tv-packs
  Search: dune         -> arm.unscheduled.search   (never proof-eligible)

every scheduled feed resolved to a declared arm
declared-but-unproducible: none
all pages match on all 9 scheduled feeds (pages 1, 2, 7)

anti-vacuity: adithd with the WRONG form -> .../tv-packs/page/2/?x=1
              the crawler really builds   -> .../tv-packs/page/2/
              they differ, so the check has teeth
```

Also confirmed directly, because it is the reason pagination is in the digest:
`julianday()` parses the real timestamp shape, and the mixed-shape trap is real —
string comparison reports `'2026-08-21T09:00:00+00:00' > '2026-08-21 23:00:00'`
as **true**, because `'T'` sorts above `' '`. All ordering in the migration uses
`julianday()`.

## 8. Test suite

New file `tests/test_round20_arm_identity.py`: **70 tests, all passing**
(re-run at package time).

Full suite, in a throwaway container with the **whole** tree copied in:

```
11 failed, 5568 passed, 4 skipped in 895.65s (0:14:55)
```

**Failures are attributed by baseline, not by eye.** `origin/main` versions of
the three changed backend files and the two deleted test files were extracted
with `git show HEAD:…` — no checkout, no stash, so the required uncommitted
`docker-compose.yml` modifications were never at risk — and the same eight test
files were run in the same container, same session:

```
BASELINE (HEAD): 11 failed, 97 passed
```

The same 11:

| Count | File | Mine? |
|---|---|---|
| 8 | `test_clicknload_fallback_wiring.py` | no — fails at HEAD |
| 1 | `test_dv_settings.py` | no — fails at HEAD |
| 2 | `test_round20_auto_resume_log_once.py` | no — fails at HEAD |

Six failures **were** caused by this change and are all fixed; all 170 tests
across the affected files now pass. Their root cause is worth reporting because
it was a real coverage hole, not just churn: the shared crawl harness
`_source()` built a **made-up** feed URL (`https://hdencode.org/4k/`), which
under revision identity resolves to an `arm.unregistered.*` id. Every test using
that harness — including one of my own new ones — was running against an
undeclared arm and proving nothing about the feeds that ship. `_source()` now
builds the real declared descriptors, which fixed three of the six failures
outright and strengthened the rest.
