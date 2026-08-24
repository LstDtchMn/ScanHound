# Round 28 — evidence

Every claim here has a command behind it. Where I have not measured something,
it says so. Raw transcripts are the `evidence-*.txt` files.

---

## 1. R26-1 — you were right, and §11 is retracted

Round 26 §11 said: *"30 call sites degraded gracefully and now propagate."*

That was a static inventory of `if not conn:` branches. Your objection was that
an unreachable branch does not mean the failure escapes, because an enclosing
handler may catch the new exception and return the same default.

Measured by injecting the setup failure and observing each boundary
(`evidence-01-r26-1-behaviour-matrix.txt`):

```
  BOUNDARY                           OUTCOME   VALUE / EXCEPTION
  ----------------------------------------------------------------------
  _query (default=[])                RETURNED  []
  _query_dicts (default=[])          RETURNED  []
  _mutate                            RETURNED  False
  _insert_returning_id               RETURNED  None
  incomplete_quarantine_audits       RETURNED  []
  load_plex_cache (fail-soft)        RETURNED  []
  list_plex_cache_movies_strict      RAISED    OperationalError

  §11 said the graceful sites 'now propagate'.
  Measured, that is WRONG for 6 of 7 boundaries tested.
```

`_query()` wraps `self.get_connection()` inside `try:` and ends with
`except Exception as e: ... return default`, so the new exception is caught and
the outward behaviour is identical to before.

**The part worth recording is not that I was wrong. It is where.** §11 sits three
sections after §12, in which I wrote the lesson *"local text that looks like
propagation does not establish caller-visible propagation"* — and then made
exactly that inference about a different function, in the same document, on the
same day. Writing a lesson down is not the same as applying it.

The last line of the matrix is the contrast that makes the whole thing legible:
`list_plex_cache_movies_strict` **raises** while every fail-soft path returns.
The repository already draws the distinction; two of round 26's defects were on
the wrong side of it.

## 2. R25-1c — the restart hazard, before and after

Both halves are reproducible and both are enclosed, run in containers built the
same way from `git archive` trees.

**BEFORE** (`evidence-02-r25-1c-before.txt`, container built from `a7f7b13` —
the head you reviewed plus the main merge; `grep -c quarantine_pending` = **0**):

```
  -wal holding committed rows: 16512 bytes
  quarantine RAISED QuarantineIncomplete, as round 26 intends
  the committed WAL is stranded at the original path : True
  a persistent marker of the incomplete quarantine   : False

  --- simulating `restart: unless-stopped` -> a NEW DatabaseManager ---
  the restarted manager CONSTRUCTED SUCCESSFULLY
  a database now exists at the original path, 41 tables
  rows in `precious`: table absent

  refusal held across the restart      : False
  a fresh DB was built over the hazard : True
  the committed WAL data is now orphaned: True
```

Round 26's refusal bought exactly one process lifetime. `docker-compose.yml:6`
is `restart: unless-stopped`, so the second start did what the first refused to
do and the committed rows were gone.

**AFTER** (`evidence-03-r25-1-after.txt`):

```
  the restarted manager REFUSED: QuarantineIncomplete
  refusal held across the restart      : True
  a fresh DB was built over the hazard : False
  the committed WAL data is now orphaned: False
```

## 3. R25-1a/b/d, with the controls that stop a "safe" answer being vacuous

An interlock that refused everything would pass the restart test and break every
normal start. A close-precondition that never fires would pass by construction.
So each refusal is paired with the case that must still work
(`evidence-03-r25-1-after.txt`):

```
  interlock file written                               ok
  a restarted manager refuses                          ok
  no fresh database at the original path               ok
  the fixture really left a hot WAL                    ok
  the manager holds no open connection                 ok
  interlock cleared                                    ok
  a fresh database WAS created                         ok
  the bundle carried its -wal                          ok
  a later start opens normally                         ok
  opens, closes and reopens fine                       ok
  refuses with QuarantineIncomplete                    ok
  performed ZERO renames                               ok
  did not falsely record the connection as gone        ok

  13 checks, 0 failed
```

**R25-1b** is the middle group: with the close failing, quarantine performs
**zero** renames and does not clear `self.conn`. Previously
`except sqlite3.Error: pass` recorded the connection as gone and renamed anyway.
Your framing was the useful one — no explicit `raise` was involved, so grepping
for raises could never have found it; the rule has to target **absorbing
handlers at safety boundaries**.

**R25-1a** is the fixture. Two things went wrong before it was right, both worth
recording because both produced a *passing-looking* result:

1. Closing the reader instead of holding it open let SQLite checkpoint and
   **delete** the `-wal`, so "the bundle carried its -wal" failed — there was
   nothing to carry. The fixture had destroyed the condition under test.
2. Building a `DatabaseManager` on the hot-WAL database **also** checkpointed it
   away. The test now runs quarantine without an intervening successful open,
   which matches production: quarantine happens *because* the open failed.

**R25-1d** is three notification phases — `detected`, `incomplete`, `complete` —
rather than one message asserting "quarantined and rebuilt a fresh database"
before anything had been attempted.

## 4. Regression G — strict, and actually wired

The read is now strict, mirroring `list_plex_cache_movies_strict`. Four tests
cover it, including a structural one that forbids `_query`/`_query_dicts` inside
the method so a later refactor cannot quietly reintroduce the collapse.

**And it now has a consumer.** Round 26's closure claim was *"the old historical
loss is now operator-visible"*, and you found no production caller. That claim
was false: a callable with no caller surfaces nothing. `/health` now reports:

```json
"quarantine_audit": {"status": "ok", "affected_migrations": 0, "rows_missing": 0}
```

with a read failure reporting `null`, never `{"status": "ok"}`. Counts only —
a test asserts a seeded `SECRET-MIGRATION` id and its legacy key appear nowhere
in the rendered body, because that route is unauthenticated.

This is the standing rule about verifying **delivery** rather than the call, and
I had not applied it.

## 5. R26-3 — my causal claim was false

Measured (`evidence-04-r26-3-busy-timeout.txt`):

```
  interpreter: CPython 3.12.14      sqlite3 3.40.1
  sqlite3.connect(path)            -> busy_timeout  5000 ms
  sqlite3.connect(path, timeout=0) -> busy_timeout     0 ms
  sqlite3.connect(path, timeout=5) -> busy_timeout  5000 ms
```

Round 26 said the journal-mode switch previously ran with SQLite's *"default of
no wait"*. A default connection already waits 5000 ms. The reordering is kept as
an explicit contract but it fixes nothing, and the code comment and test
docstring now say so.

Third wrong causal claim I have published in three rounds — after the round-23
"the guard now raises" that I never ran, and §11 above. All three were about
mechanism rather than outcome, and all three would have been caught by measuring
instead of reasoning.

## 6. R26-2 and R21-10

`semantic_mismatch` is now keyword-only and required, so omission is a
`TypeError` at call time rather than a silent selection of the permissive mode.
Four tests, including that both modes stay reachable when chosen — requiring the
choice must not delete a branch.

The eighth overstated **A** is corrected: the
`test_every_live_key_resolves_deterministically` row now points at
`TestTheExactLiveKeyMappingIsAsserted::test_each_live_key_maps_to_its_own_arm`.

That is eight reclassifications across rounds 22–28. Every one was found by
someone deliberately looking for the next one. I still have no way to audit the
remaining **A** rows other than one at a time, and your suggestion of parsing the
table and asserting the named symbols exist would catch stale destinations but
not overstated ones — which is the failure mode all eight were.

## 7. The suite

```
origin/main  3c3369d                1 failed, 5356 passed, 4 skipped
branch, before round 27  a7f7b13    0 failed, 5805 passed, 4 skipped
branch, this head                   0 failed, 5829 passed, 4 skipped
```

Same method throughout, described in the round-26 package: `git archive` trees
copied WHOLE into containers from one image, pinned test dependencies
(`pytest 9.1.1`, `pytest-asyncio 1.4.0`, `httpx 0.28.1`), bytecode caches
cleared, all in this session.

The +24 are round 27's own regressions: the interlock and its controls, the
close precondition, the strict read and its structural guard, the `/health`
wiring, and the required-keyword tests.

Main's single failure is its own (`04-provenance.md` §2) and `a7f7b13` fixes it
on this branch. **The branch has no failures.**

A green suite is not the point here and I want to say so plainly: every defect in
rounds 26 and 27 was on a failure path that an ordinary suite never exercises.
The inert guard passed every test in the repository. So did the fail-soft
diagnostic. So did the process-local refusal. The suite establishes that nothing
was broken in passing; it establishes nothing about whether the new failure paths
are right, which is what `01-request.md` asks you to attack.

