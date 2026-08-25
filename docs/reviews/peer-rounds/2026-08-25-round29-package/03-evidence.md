# Round 29 — evidence

Every claim has a command behind it. Where something was not measured, it says
so. Raw transcripts are the `evidence-*.txt` files.

---

## 1. M28-2 — before and after, same probe, both reproducible

The probe replaces `_notify_corruption` with a recorder and drives each real
failure path, so it counts the phases **actually emitted** rather than reading
the control flow.

**BEFORE** (`evidence-01-m28-2-3-before.txt`, container built from `97847c6` —
the code you reviewed):

```
  A. owned close fails        raised QuarantineIncomplete  phases=['detected']  terminal=NONE
  B. interlock write fails    raised QuarantineIncomplete  phases=['detected']  terminal=NONE
  C. rename raises OSError    raised QuarantineIncomplete  phases=['detected']  terminal=NONE
  CONTROL clean quarantine    returned                     phases=['detected', 'complete']

  failure paths with NO terminal notification : ['A close', 'B marker', 'C rename']
  M28-2 reproduced: True
```

**AFTER** (`evidence-02-m28-2-3-after.txt`):

```
  A. owned close fails        phases=['detected', 'incomplete']  terminal=['incomplete']
  B. interlock write fails    phases=['detected', 'incomplete']  terminal=['incomplete']
  C. rename raises OSError    phases=['detected', 'incomplete']  terminal=['incomplete']
  CONTROL clean quarantine    phases=['detected', 'complete']    terminal=['complete']

  failure paths with NO terminal notification : none
  M28-2 reproduced: False
```

**C is the injection from the round-27 restart evidence** — the one that stranded
a committed WAL. The failure that this whole review sequence exists to address
was the one that emitted no terminal message.

Your framing was generous. The phases were not "directionally right but not
guaranteed"; on every real failure path they were **not delivered at all**.

## 2. M28-3 — `complete` for a database with zero tables

Same transcripts, second section. Before:

```
  phases emitted            : ['detected', 'complete']
  tables in the 'rebuilt' db: 0
  --> 'complete' sent for a database that was never rebuilt: True
```

After:

```
  refused instead of claiming a rebuild: True
  --> 'complete' sent for a database that was never rebuilt: False
```

`init_db()`'s recursion limit logged `"Giving up"` and `return`ed — a
success-shaped `None`. It now raises `InitRecursionExhausted`, and
`_assert_rebuilt()` checks the postcondition (file exists, opens, carries
schema) rather than inferring success from a function having returned.

Your diagnosis was exact: *"asserting an outcome based on control-flow return
rather than a verified postcondition"*.

## 3. The fix reproduced the defect it was fixing

Worth recording on its own.

The boundary was written first as:

```python
except QuarantineIncomplete as qe: ...
except OSError as os_err: ...
```

Then making the recursion limit raise produced `InitRecursionExhausted`, a
`RuntimeError` — which **escaped the new boundary with no terminal
notification**. M28-2 again, inside the fix for M28-2, within the same hour.

The type-list approach was the defect. It is now:

```python
except Exception as failure:
    qe = QuarantineIncomplete(...)
    self._notify_corruption(qe, phase="incomplete")
    raise qe from failure
```

The boundary is *"did the attempt complete"*, not a list of types someone must
remember to extend. It translates and raises; it never absorbs. That is the
generalisable form of your recommendation, and the narrow reading of it is what
reintroduced the bug.

## 4. A second success path, made visible by moving the notification

`_quarantine_attempt()` has its `return backup_name` inside
`if os.path.exists(self.db_path):`. A missing database file therefore fell out
of the method with an implicit `None`, which the new boundary would have handed
to `_assert_rebuilt()` as a backup stem.

This was invisible while the notification lived **inside** the `if` — the old
code simply never notified on that path. Hoisting it exposed the second exit.
It is now explicit: nothing to quarantine, so release the interlock, rebuild,
and return `""`.

## 5. M28-5 — you were right, and there were three

The package claimed the R25-1a fixture was corrected. Only
`verify_r25_1_full.py` was. The committed suite kept:

```python
reader = sqlite3.connect(dm.db_path)
reader.execute("BEGIN")
...
dm._quarantine_corrupt_db(...)      # rename with the reader still open
```

in **three** places — two `_hot_wal` helpers and one inline in
`TestQuarantineSurvivesTheRestartDockerIsConfiguredToDo._partial`. Replacing the
two helpers alone would have left the partial-rename restart tests renaming a
database with a live SQLite connection, which is the documented-undefined
behaviour R25-1a was about.

All three now use one shared `leave_hot_wal()` built on the abrupt-child exit:
child commits into the WAL, `os._exit(0)`, no clean shutdown, no live connection
in the test process. The ordering constraint you agreed was sound is enforced in
the helper's docstring and by construction — the manager is closed **before** the
child runs, because opening a hot-WAL database checkpoints the condition away.

Two rounds in three, I have reported a fixture change that existed only in the
probe. That is the "verify the consumer" rule, applied to my own instruments.

## 6. M28-4 — removed, and it broke exactly as predicted

`test_the_handler_re_raise_is_what_actually_propagates` failed on the round-29
restructure. It asserted `"raise QuarantineIncomplete(" in
src.split("except OSError as os_err:", 1)[1]` — everything after the header, not
the handler body — and pinned a mechanism rather than the contract.

Removed, with the reasoning left in place of it. The behavioural injection tests
are the safety proof; the codebase-wide "recovery handlers must not absorb" rule
lives in `scripts/lint_swallowed_failures.py`, which enforces it structurally
without hard-coding one function's text.

## 7. M28-1 — delivery, not rendering

`check_quarantine_audit()` in `scripts/host-detector/dv_health_check.py`, wired
into `main()` under its **own** subsystem key so an audit finding is not
suppressed by an unrelated JD or queue alert.

Three states, deliberately distinguished:

```
key missing        older build -> silent, not a finding
key present, null  the read FAILED -> alert as UNKNOWN, never "ok"
status incomplete  alert with counts
```

It uses `"quarantine_audit" not in body` rather than `.get()`, because `.get()`
cannot tell missing from null — the exact ambiguity you identified in the house
convention. For a new field there is no compatibility burden, so it does the
correct thing rather than the conventional one.

Seven tests exercise the **watchdog function**, not the endpoint, including that
absent and null are not the same and that `main()` registers the subsystem key.

## 8. L28-1 and the interlock release

`json.load()` accepts `null` and `[]`, so `rec.get(...)` raised `AttributeError`
past the `(OSError, ValueError)` handler. Now `isinstance(rec, dict)` — the
marker's **contents** are advisory detail; its **existence** is the interlock,
and that is decided before the parse.

`_clear_quarantine_pending()` now raises. Your reasoning was the deciding one:
the old behaviour was safe only because `init_db()` happened to re-enter
`get_connection()` and rediscover the same state. Making it explicit also removed
a `fail-soft-ok` suppression — the lint's suppression count dropped **12 → 11**,
which is the annotation tracking a real improvement rather than a re-labelling.

## 9. What I did NOT do

**Crash consistency.** No `fsync` of the marker or its parent directory before
the destructive rename. The demonstrated contract is a **process restart**, which
is what `restart: unless-stopped` produces and what the round-27 evidence
reproduced. A host or VM power-loss mid-quarantine could still land the rename
while losing the interlock. Not claimed, and the comments no longer imply it.

**The `/health` disclosure shape.** Counts stay. Your `{"status": "..."}`
alternative is a threat-model decision for Jesse.

## 10. The suite

```
origin/main  3c3369d                 1 failed, 5356 passed, 4 skipped
branch before this round  97847c6    0 failed, 5841 passed, 4 skipped
branch, this head         d096885    0 failed, 5853 passed, 4 skipped
```

Same method throughout (`04-provenance.md` §4). The +12 are this round's
regressions, minus the removed source-inspection test.

`scripts/lint_swallowed_failures.py backend/` exits 0 with **11** suppressions,
down from 12.

Worth repeating because it is the point: **all three M28-2 paths passed the
entire suite.** So did the inert guard, the fail-soft diagnostic and the
process-local refusal before them. A green suite says nothing broke in passing;
the failure-injection probes above are what say the failure paths work.

