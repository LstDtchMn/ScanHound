# Round 25 — review request

Four consecutive exact-head passes have each found a live defect, and each time
it was one layer past where my change was aimed. Read this head the same way.

Preserve finding identity: R24-n stays R24-n, new ones are R25-n.

---

## 1. Something I found and did NOT fix

While verifying R24-1 I hit a second path in the same area and deliberately left
it alone. Flagging it rather than burying it.

A severely corrupted file can raise `UnicodeDecodeError` from
`PRAGMA journal_mode=WAL` inside `get_connection()` — **before** `init_db()`'s
try block exists. It is not a `sqlite3` error, so it escapes classification
entirely and startup dies with an opaque traceback:

```
File "/app/backend/database.py", line 403, in get_connection
    self.conn.execute("PRAGMA journal_mode=WAL")
UnicodeDecodeError: 'utf-8' codec can't decode byte 0xad in position 41
```

It is pre-existing, fail-closed, and the data is untouched — so the outcome is
the safe one, just illegible. I did not fix it because doing so means
classifying decode errors on a connection path called constantly, and folding
that into a six-finding change is exactly how I produced the `sqlite3.Row`
defect two rounds ago.

**Is leaving it right?** And if it should be fixed: is "cannot decode the
result of a PRAGMA on this file" positive corruption evidence, or is refusing
startup with a clear message the better outcome given that quarantine is
destructive?

I did guard the `integrity_check` READ, where an undecodable report is itself
evidence of damage and the scope is one statement.

---

## 2. Where I want the hardest look

### R24-1's narrowing, in both directions

Getting this wrong in the other direction is worse than the original bug: a
genuinely damaged file that is no longer quarantined would keep failing
startup forever with no recovery path.

The classifier trusts, in order: `DatabaseCorruptionDetected` (raised only
after `integrity_check` says so), SQLite's own error code, and — only when no
code is available — message markers. Note the deliberate asymmetry: **if a code
is present and does not say corruption, I believe it and stop**, rather than
falling through to substring matching. That prevents an unrelated message
containing "corrupt" being misread, but it also means I am trusting the code
absolutely.

- Is that asymmetry right?
- `sqlite_errorname` requires Python 3.11+. The container is 3.12. On an older
  interpreter every exception falls through to message markers — is the marker
  set sufficient there?
- Are there corruption presentations that carry neither a corrupt/notadb code
  nor a matching message?

### R23-1a's durable history

`arm_semantic_history` records first sight and refuses a later change. Attack:

- It records on first sight rather than refusing an unknown arm, so a database
  created **after** a bad semantic edit records the new meaning as though it
  were always so. That is the honest limit of choosing immutability over
  versioning — but is it the limit you meant, or does it need the CI/baseline
  guard as well to cover the pre-first-sight window?
- The check runs at the end of `init_db`. A refusal leaves a fully-formed
  schema and an unstamped `user_version`. Is that the right failure state?
- Nothing writes a *retirement* record when an arm is removed, so the history
  keeps a fingerprint for an id nobody declares. Harmless today. Worth making
  explicit as the supersession metadata you suggested?

### R23-1b's admission rule

`semantic_mismatch()` treats an **absent** field as "not a contradiction", so a
claim omitting `source` still attributes. That felt right — absence is not
disagreement — but it means a producer that stops sending a field silently
loses that check. Wrong call?

---

## 3. New surface

- **`default_registry()` is now cached and validated once.** That closes the
  `DECLARED_ARM_IDS` side door you identified. But it also means a process that
  starts with a valid registry keeps it for its lifetime — intended, since
  declarations are module constants, and noted here because you flagged
  import-time snapshots before.
- **The lifecycle report now compares `listing_type`.** With the writer
  enforcing it, an attributed row's type should always match — so this is a
  second line for rows arriving through direct SQL or older history. Dead code,
  or worth keeping?

---

## 4. A regression I introduced and caught before commit

`test_init_depth_resets_after_recovery_failure` injected a generic
`DatabaseError("boom")` to reach the recovery path. The narrowing correctly
stops treating that as corruption, so the test failed — the suite went to 12
failures. Its *intent* still holds, so the fixture now names corruption
explicitly, with a companion asserting the new refusal behaviour.

Worth stating because it is the only new failure the change produced, and I
would rather you see it named than discover I had quietly adjusted a test to
match new behaviour.

---

## 5. Test adequacy

Mutation and verification results in `03-evidence.md`. Note the addition you
asked for: every finding was **reproduced before the fix and re-run after**, so
the evidence is a before/after pair rather than "the test went red".

Also — `05-retired-test-mapping.md` now carries corrections from rounds 22, 24
and 25. Three separate reviews have each found overstated entries in a table
written specifically to stop overstating. Please look again. A seventh would be
more useful to me than a clean bill.

---

## 6. Not asking

Deployment readiness. Do not propose merging, deploying or enabling anything.
