# Round 26 — evidence

Every claim in this package, with the measurement behind it. Raw transcripts are
`evidence-01-findings.txt` and `evidence-02-regression-G-mutation.txt`.

Rule I am applying after the round-23 incident, where I published an empirical
claim I had never measured: **if a sentence in this package asserts that code
behaves a certain way, a command in this file produced that result.** Where I
have not measured something, it says so.

---

## 1. A retraction: the "11 pre-existing failures" baseline was wrong

Previous packages stated the suite had 11 pre-existing failures, named as 8 in
`test_clicknload_fallback_wiring.py`, 1 in `test_dv_settings.py`, and 2 in
`test_round20_auto_resume_log_once.py`.

**Two of those three files do not exist.**

```
repo working tree : no match for clicknload / auto_resume_log_once
git ls-files      : no match
container         : no match
```

The only file of the three that exists is `test_dv_settings.py`. `git ls-files`
returns exactly one loosely-related file, `tests/test_auto_resume_diagnostics.py`.

So the baseline I have been subtracting from suite results for several rounds
was partly fictional. This is the same failure the memory rule *"a number that
moves is an instrument fault"* exists for, and I did not apply it: when the
failure count dropped from 11 to 1 after a clean container resync, my first
instinct was that the resync had fixed something. It had not. The 11 was never
real.

**Remedy:** the baseline below is re-measured against `origin/main`, extracted
with `git archive` (which cannot touch the working tree), in a container built
from the same image with the same dependency set, in this session.

## 2. R24-1 — extended corruption codes

`evidence-01-findings.txt` §R24-1. Reduced to the primary code (`code & 0xFF`):

```
  a real UNIQUE violation reports: SQLITE_CONSTRAINT_PRIMARYKEY (1555)
  SQLITE_CORRUPT             (  11) primary=11 -> True  expected True
  SQLITE_NOTADB              (  26) primary=26 -> True  expected True
  SQLITE_CORRUPT_VTAB        ( 267) primary=11 -> True  expected True
  SQLITE_CORRUPT_SEQUENCE    ( 523) primary=11 -> True  expected True
  SQLITE_CORRUPT_INDEX       ( 779) primary=11 -> True  expected True
  SQLITE_CONSTRAINT_UNIQUE   (2067) primary=19 -> False expected False
  SQLITE_BUSY                (   5) primary= 5 -> False expected False
  --> misclassified: none
```

The first line is the part that made this urgent rather than cosmetic: a plain
UNIQUE violation reports the **extended** name `SQLITE_CONSTRAINT_PRIMARYKEY`,
so real exceptions carry extended names as a matter of course. The bare names
the old code compared against would rarely have appeared at all, and because it
trusted a present code and stopped, `SQLITE_CORRUPT_INDEX` was being returned as
*proof of non-corruption*.

## 3. R25-1 — the quarantine bundle

`evidence-01-findings.txt` §R25-1. Measured with a reader holding a transaction
open so a checkpoint could not complete, leaving a genuinely hot WAL:

```
  sidecars present before quarantine: ['v25_wal.db-shm', 'v25_wal.db-wal']
  -wal size: 16512 bytes
  Quarantined corrupt DB as 3 file(s): ...corrupt.1787607588,
      ...corrupt.1787607588-wal, ...corrupt.1787607588-shm
  quarantine artifact includes a -wal: True
  --> committed WAL state detached from the quarantine: False
```

A 16 KB WAL holding a committed row now travels with the database it belongs to.

One reading worth pre-empting: the transcript also prints `a -wal remains beside
the NEW database: True`. That is the **fresh** database's own newly created log,
after `init_db()`. It is expected and is not the stranded file the finding was
about — the check that matters is the `False` on the last line.

## 4. R25-2 — atomic connection setup

`evidence-01-findings.txt` §R25-2. Measured against a database held under
`BEGIN EXCLUSIVE` by another connection:

```
  journal_mode on disk: delete
  Database connection setup FAILED at ... (OperationalError); no connection
      is returned: database is locked
  get_connection RAISED OperationalError: database is locked
  get_connection returned: None
  self.conn left set: False
  --> a connection was returned with its contract unmet: False
```

Before the fix this returned a live DELETE-mode connection with the default
timeout, and `self.conn` was left published.

## 5. R23-1b — absence is not agreement

`evidence-01-findings.txt` §R23-1b. A claim stamped with a valid revision but
**no `source` and no `listing_category`**:

```
  claim with NO source and NO category -> ('unattributed', None)
  --> unknown treated as agreement: False
```

It is recorded as `unattributed` with a null `arm_id`, rather than attributed on
the strength of fields nobody supplied.

## 6. R23-2 — the invented association, and why the Round-24 fix was wrong

This is the item I most want re-checked, because my Round-24 fix for it was
itself a repair that asserted more than the data supports.

Round 24 recovered an alias's `listing_type` by joining the surviving old
claim-quarantine row. Measured on the two-typed-claims-one-URL case:

```
      raw-movie -> 'tv'      (invented)
      raw-tv    -> 'tv'
```

The survivor **is** the R23-2 casualty: the old key omitted `listing_type` and
used `INSERT OR REPLACE`, so where two typed claims existed only the last
survived. Reading a type back off it relabels the other one. After the round-26
fix, which derives from the LIVE association and only when exactly one distinct
type matches:

```
      raw-movie -> ''
      raw-tv    -> ''
  --> a movie alias was relabelled from the surviving tv row: False
```

`''` here means **unknown**, not "no type". In this fixture there is no live
association to recover from, so unknown is the correct and only honest answer.

## 7. Regression G — the guard shown firing on the real defect

Six tests cover `incomplete_quarantine_audits()`, but five of them build the
historical state by hand, which proves only that the query can read a shortfall.
Per the memory rule *"a guard must be shown to FAIL"*, that is not sufficient.

So I reintroduced the R23-2 defect — put the old quarantine key back and
inverted **both** rebuild guards — and ran the **real** migration
(`evidence-02-regression-G-mutation.txt`):

```
  rebuild guard inverted at line 1951 / 1976
  MUTANT: quarantine key back to (migration_id, canonical_url, key)
  live quarantine PK carries listing_type: False

  migration reported quarantined : 2
  audit recorded rows_affected   : 2
  snapshots that SURVIVED        : 1
  incomplete_quarantine_audits() : [{'audited': 2, 'surviving': 1, 'missing': 1,
                                     'legacy_arm_key': 'ddlbase:remux', ...}]

  the R23-2 loss was reproduced by real code : True
  regression G's query SAW it                : True
  VERDICT: the guard fires on the real defect
```

Two process notes, because both were near-misses:

- A first attempt mutated the guard's opening line structurally and produced a
  `SyntaxError`. The only reason that did not read as "the mutation passed" is
  that Python refused to import. Had the edit been syntactically valid but
  semantically inert, I would have recorded a false negative.
- There are **two** guards, not one — a rename and a copy-back. Inverting only
  the first leaves a half-migrated database and proves nothing. The assertion in
  the mutation script is `len(hit) == 2` for exactly that reason.

## 8. The seventh overstated A, with a mutation check

`TestTheExactLiveKeyMappingIsAsserted` was added because the mapped destination
for `test_all_three_live_keys_move` asserts every row lands at **an** active
revision, never **which**.

Mutation: swap the two movie arms' `supersedes` payloads, by line number, at
`backend/arms.py:484` and `:487`:

```
   484: , supersedes=("hdencode:remux",)),
   487: S_1, supersedes=("hdencode:4k",)),
```

Result, in both directions:

```
ARM 1 — the NEW tests on the mutant:
  FAILED ...TestTheExactLiveKeyMappingIsAsserted::test_each_live_key_maps_to_its_own_arm
  FAILED ...::test_the_two_movie_keys_are_not_interchangeable
  FAILED ...::test_the_dry_run_report_names_the_targets
  3 failed in 0.74s

ARM 2 — the MAPPED destination on the same mutant:
  1 passed in 0.22s

CONTROL — the new tests on unmutated code:
  3 passed in 0.50s
```

Arm 2 is the finding: with `hdencode:4k` migrating into the remux arm, the test
the mapping document cited as covering this **still passes**. The **A**
classification is reclassified to **B**.

## 9. An error in my own comment, found by grep

The `require_complete` docstring I wrote for this round justified the parameter
by saying "the two callers genuinely differ", and named the legacy migration as
the permissive caller. Grepping for callers rather than trusting the comment:

```
$ grep -rn "semantic_mismatch" --include=*.py . | grep -v ./tests/
./backend/arms.py:803:def semantic_mismatch(...)
./backend/database.py:5715:        from backend.arms import semantic_mismatch
./backend/database.py:5778:            _mismatch = (semantic_mismatch(..., require_complete=True)
```

**One** production caller. The migration never calls it — legacy rows are
attributed through `supersedes`. The docstring is corrected in this round to say
so, and to record that `False` is a default exercised only by tests.

This is the same defect class as §10's drift: an assertion in a comment that a
reader cannot verify. It survived my own review of the patch.

## 10. The two documentation drifts

**Drift 1 — a function name that does not exist.** The mapping document credited
`rebuild_equivalence_failure()` with enforcing row survival in production. No
such function exists; it was the name of a first attempt, discarded when review
showed it compared plain tuples against `sqlite3.Row` and would therefore have
refused every migration. The real function is `validate_shape_migration()`,
confirmed present at `backend/database.py:222`.

**Drift 2 — a stale push claim.** The round-25 provenance said `e26c2f7` "has
not been pushed". `git branch -r --contains e26c2f7` now returns
`origin/fix/round12-attestation-authority`. Corrected in place with a dated
strike-through rather than an edit, because a provenance file whose past
statements are silently rewritten cannot be used to check anything.

## 11. The suite
