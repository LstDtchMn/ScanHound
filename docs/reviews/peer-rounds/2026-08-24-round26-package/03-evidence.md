# Round 26 — evidence

Every claim in this package, with the measurement behind it. Raw transcripts are
`evidence-01-findings.txt` and `evidence-02-regression-G-mutation.txt`.

Rule I am applying after the round-23 incident, where I published an empirical
claim I had never measured: **if a sentence in this package asserts that code
behaves a certain way, a command in this file produced that result.** Where I
have not measured something, it says so.

---

## 1. The "11 pre-existing failures" baseline, and a retraction OF my retraction

Previous packages stated the suite had 11 pre-existing failures, named as 8 in
`test_clicknload_fallback_wiring.py`, 1 in `test_dv_settings.py`, and 2 in
`test_round20_auto_resume_log_once.py`.

### What I first wrote here, which was wrong

I searched for those files, found two of them missing, and wrote that the
baseline was "partly fictional":

```
repo working tree : no match for clicknload / auto_resume_log_once
git ls-files      : no match
container         : no match
```

**Every one of those searches was scoped to this branch.** When I later merged
`origin/main` in, `tests/test_clicknload_fallback_wiring.py` arrived — 17 tests,
present on `main` all along:

```
                                          main   this branch
tests/test_clicknload_fallback_wiring.py     1             0
tests/test_round20_auto_resume_log_once.py   0             0
tests/test_dv_settings.py                    1             1
```

So the corrected position:

- `test_clicknload_fallback_wiring.py` **exists**, on `main`. My claim that it
  does not was wrong, and the "8 clicknload failures" in the old baseline were
  most likely real, measured on a tree that had main's tests.
- `test_round20_auto_resume_log_once.py` — **this too was wrong, corrected
  2026-08-24.** It exists on local `main`: 142 lines, 5 tests, all passing, in
  commit `ab3f92e`, which has never been pushed. It is invisible to
  `git ls-tree origin/main`, to the feature branch, and to any container built
  from either — which is every place I looked.
- The old "11" was therefore **not fictional**. All three files it named are
  real — one on `origin/main`, one on both, one in unpushed local work — so it
  was a correct measurement on a working tree that had merge content and
  unpushed commits. I concluded absence from a search whose scope I never
  established, and then did it twice more while correcting myself:

```
                                     origin/main  local main  branch
test_clicknload_fallback_wiring.py        1           1          1
test_dv_settings.py                       1           1          1
test_round20_auto_resume_log_once.py      0           1          0
```

  Each correction widened the search space slightly and stopped again. A claim
  that gets weaker every time the space widens is an unfounded negative, and
  that was visible at the first correction.

### Why this one is worth your attention

This is the failure mode named in my own standing rule *"verify identity before
claiming absence — positive control before trusting any zero"*, and I ran three
searches without once checking that the search **scope** could have found the
thing. Three consistent negatives felt like corroboration. They were the same
negative three times.

It is also the second time in this package that a confident claim of mine
dissolved on contact with a wider view — the first being the `require_complete`
docstring in §9. Both were caught, but neither by review of the diff.

**The measured baseline** in §13 stands unaffected: it was produced after this,
from complete `git archive` trees on both sides, and does not depend on any of
the above.

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

## 11. R25-2's blast radius, measured rather than argued

In `01-request.md` §1.3 I asked you to check whether making `get_connection()`
raise breaks a caller. Rather than leave that as an open question I measured it,
and it is bigger than I implied — so here is the data.

**30 call sites degraded gracefully and now propagate.** Every site that tested
`if not conn:` had a plan for failure; `evidence-04-get-connection-callers.txt`
classifies all of them:

```
backend/database.py                graceful=29  raise-like=3
backend/api/routes/analytics.py    graceful=1   raise-like=0
```

The 29 + 1 graceful ones returned a safe default — `False`, `[]`, `{}`, `0`,
`None` — and now let a `sqlite3.Error` out instead. The 3 raise-like ones
already raised; only the exception type changes (`RuntimeError` /
`RenameJobDBError` → `sqlite3.OperationalError`).

**Two of those sites deserved individual attention.**

`checkpoint_wal` (line 492, `return False`) reads as the worst case — its
docstring says "Called once after startup init". It has **no callers at all**:

```
$ grep -rn "checkpoint_wal" --include=*.py backend/ | grep -v "def checkpoint_wal"
(no output)
```

`init_db` (line 656, bare `return`) is the one that matters, because it runs
inside the corruption-recovery handler. That raises the only question here worth
anything: **can ordinary contention now get the live database quarantined?** A
data-availability incident caused by a fix for a data-integrity finding would be
the worst possible trade.

Measured, with a positive control so a clean "no" cannot be a silently broken
test (`evidence-03-r25-2-blast-radius.txt`):

```
CASE 1  a LOCKED database (ordinary contention, fully recoverable)
  Database connection setup FAILED at /tmp/blast_locked.db (OperationalError)
  Transient DB operational error during init (not corruption — not quarantining)
  DatabaseManager(path) raised OperationalError: database is locked
  files now present : ['blast_locked.db']
  QUARANTINED       : none
  --> a healthy locked database was quarantined: False

CASE 2  POSITIVE CONTROL -- a genuinely corrupt file MUST quarantine
  DATABASE CORRUPTION DETECTED — quarantining and rebuilding
  QUARANTINED       : ['blast_corrupt.db.corrupt.1787609057']
  --> the control fired: True

RESULT: SAFE -- raising did not turn contention into quarantine,
        and real corruption is still caught.
```

The classifier work from R24-1 is exactly what makes this hold: `SQLITE_BUSY`
has primary code 5, so `is_corruption_evidence()` returns `False` and the handler
re-raises instead of quarantining. **The two findings are load-bearing for each
other** — had R24-1 been left matching substrings, `"database is locked"` would
not have matched either, but a future message containing a marker word could
have, and R25-2 is what routes these errors to that classifier in the first
place.

**What I am NOT claiming.** That the other 28 graceful sites are all fine. They
are only reachable when `self.conn` is unset — once established, `get_connection()`
returns it without re-running any PRAGMA — so they are exposed exactly when the
database is genuinely unavailable, where propagating is defensible. I have
verified the destructive axis, not every caller's error handling. If you think a
specific one of those 28 should still degrade, name it.

## 12. A defect I shipped into this round, and only found by running it

**The R25-1 refusal was inert.** This is the most important thing in this
package, because it is not a finding you gave me — it is one I created while
closing one of yours, and it survived my own review of the diff.

The bundle move has two halves. Moving the files is the easy half. The half that
protects data is the refusal: if a persistent journal cannot be moved, quarantine
must **not** go on to create a fresh database at that path, because the stranded
journal would then be applied to it. I wrote that refusal as:

```python
if _stranded:
    raise OSError("could not move %s with the database; refusing to ...")
```

and the same method ends with a pre-existing handler:

```python
except OSError as os_err:
    logger.critical("Failed to recover DB: %s", os_err)
```

**The refusal raised into its own method's catch-all.** It could never fire.

I only found it because the memory rule says a guard must be shown to FAIL, so I
injected a rename failure on the `-wal` rather than trusting the code:

```
CASE 1  the -wal CANNOT be moved -> quarantine must REFUSE
  Failed to recover DB: [Errno 13] injected: cannot move the write-ahead log
  raised: NOTHING
  a -wal is still at the original path : True
  a fresh database was created anyway  : False
  --> the guard REFUSED rather than proceeding: False      <-- INERT
```

Note what makes this nasty: the observable outcome looked *fine*. No fresh
database was created, so a spot-check of the directory would have passed. The
defect is that `_quarantine_corrupt_db()` **returned normally** — so the caller
resumed as though recovery had succeeded, with the database half-quarantined.

**The fix**, and why the exception type is load-bearing: `QuarantineIncomplete`
is deliberately **not** an `OSError`, and the pre-existing handler now re-raises
as that type after logging rather than absorbing the failure. Any incomplete
quarantine is now reported, not just my one explicit case.

Re-measured after the fix (`evidence-05-r25-1-refusal.txt`):

```
  refusal fires when a journal is stranded (must be True) : True
  happy path still quarantines the bundle  (must be True) : True
  RESULT: the refusal is real, and it is not refusing everything.
```

Four regression tests are added, including
`test_the_refusal_is_not_an_OSError`, which is the whole defect in one
assertion, and an anti-vacuity control — a guard that refused *everything* would
satisfy the other three while destroying recovery.

### The mutation, which corrected my own account of the fix

A test that passed before the fix and passes after it, with a *different*
expectation each time, is the classic shape of a test edited to match whatever
the code does. So the refusal was mutated in both of its halves
(`evidence-06-refusal-mutation.txt`):

```
CONTROL  unmutated
  test_init_depth_resets_after_recovery_failure     1 passed
  TestQuarantineRefusesRatherThanHalfFinishing      4 passed

MUTANT B  the OSError handler goes back to swallowing
  test_init_depth_resets_after_recovery_failure     KILLED  (1 failed)
  TestQuarantineRefusesRatherThanHalfFinishing      KILLED  (2 failed, 2 passed)

MUTANT A  the stranded-journal refusal becomes an OSError again
  test_init_depth_resets_after_recovery_failure       survived  (1 passed)
  TestQuarantineRefusesRatherThanHalfFinishing        survived  (4 passed)
```

**Mutant A survives, and that is the interesting result.** I had described the
fix as "changed the refusal to a non-`OSError` type". The mutation shows that is
*not* the load-bearing part. The explicit `raise` sits inside the same `try:`,
so with the handler re-raising, an `OSError` from it is caught and re-raised as
`QuarantineIncomplete` regardless — the two are behaviourally identical:

```
try:
    raise QuarantineIncomplete(...)      <- mutant A changes this line
except QuarantineIncomplete:
    raise
except OSError as os_err:
    ...
    raise QuarantineIncomplete(...) from os_err
```

So mutant A is an **equivalent mutant**, and surviving it is correct rather than
a gap in the tests. The half that actually fixes the defect is the handler
re-raise (mutant B), which both tests kill.

That distinction matters for review: the general remedy here is *"a catch-all at
the end of a method must not report success"*, not *"pick a different exception
type"*. Had I only changed the type and left the handler absorbing, every
incomplete quarantine arising some other way — a failed `-journal` rename, a
permissions change mid-flight — would still have been swallowed. I would have
believed the finding closed on the strength of a passing test.

**What I want from you on this.** Not agreement that the fix is right. I want to
know how many more of these there are. The general shape is *a raise that lands
inside a handler in its own call path*, and I have no systematic check for it —
the diff reads correctly, the tests around it pass, and only fault injection
exposes it. If there is a mechanical way to find the rest, that is worth more
than any individual finding in this round.

## 13. The suite

```
origin/main  3c3369d    1 failed, 5356 passed, 4 skipped   (13:54)
this branch  3d75680+   0 failed, 5769 passed, 4 skipped   (16:04)
```

Same method both sides, described in `04-provenance.md` §6: `git archive` trees
copied whole into fresh containers from one image, pinned test dependencies,
caches cleared, same session.

Main's one failure is main's own (`04-provenance.md` §5b). **The branch has
none.**

One failure on the branch was real and mine, and is worth recording because of
what it was. After the §12 refusal fix,
`test_database.py::TestInitDb::test_init_depth_resets_after_recovery_failure`
failed: it mocks `os.rename` to raise, then asserts `init_db()` returns and the
recursion depth resets.

**That test was passing because a half-quarantined database was reported to the
caller as success** — the exact defect §12 describes. Its neighbour
`test_a_non_corruption_error_refuses_instead_of_recovering` already had the right
shape: expect the refusal, and still assert the depth resets. The depth is reset
in a `finally`, so the invariant under test is untouched; only the expectation
about what reaches the caller changed. Updated to match, with the reasoning in
the docstring so nobody later reads it as a test loosened to fit the code — and
mutation-checked in §12 for exactly that reason.

