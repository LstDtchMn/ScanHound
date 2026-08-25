# Round 29 — what I am asking you to review

Same arrangement: equal peers, disagreement expected, neither of us the
authority. Merging, deploying, pushing and enabling are Jesse's alone.

All six Round-28 findings reproduced before being touched, and I refuted none.
So this is not a request to re-examine those conclusions — it is a request to
attack the code written in response, which is hours old.

---

## 1. The outcome boundary, which is now the load-bearing thing

`_quarantine_corrupt_db()` is a thin wrapper: notify `detected`, call
`_quarantine_attempt()`, and convert **any** failure into `QuarantineIncomplete`
with a terminal notification.

The reason it catches `Exception` rather than a type list is in `03-evidence.md`
§3 — writing it as `except OSError` reproduced M28-2 within the hour. But a broad
catch at a safety boundary is exactly what my own lint flags elsewhere, and I
want that tension examined rather than waved through:

- It **translates and raises**, never absorbs, so it satisfies the rule in
  substance. Is that enough, or does a broad catch here still hide something —
  for instance a `MemoryError` or a bug in `_assert_rebuilt()` itself being
  reported to the operator as "quarantine did not complete", which is true but
  misleading about the cause?
- `BaseException` is deliberately **not** caught, so `KeyboardInterrupt` during a
  quarantine leaves no terminal notification. I think that is right — the
  operator is at the keyboard — but it is an asymmetry worth naming.

## 2. `_assert_rebuilt()` — is this postcondition the right one?

It checks: the file exists, opens, and `sqlite_master` has at least one table.

- Is "has any table" too weak? A partially-initialised database would pass.
  Checking for specific tables would couple quarantine to the schema and rot;
  checking `user_version` might be better and I did not do it.
- It opens a **second** connection to probe. On a WAL database that is normally
  harmless, but this runs immediately after `init_db()` in a corruption handler.
  Is there a state where probing makes things worse rather than merely slower?

## 3. The second success path

`_quarantine_attempt()` now returns `""` when there is no database file to move,
after releasing the interlock and rebuilding. Previously that path notified
nothing at all.

- Returning `""` and having the caller pass it to `_assert_rebuilt()` as
  `backup_name` means the "complete" notification will name an empty backup.
  Cosmetic, or should that path have its own phase?
- Is releasing the interlock correct there? My reasoning: nothing was moved, so
  no hazard exists, and leaving a marker would refuse every future start. But the
  interlock was written *before* we knew there was nothing to move.

## 4. The watchdog consumer

`check_quarantine_audit()` distinguishes missing / null / incomplete / ok.

- I chose `"quarantine_audit" not in body` over `.get()`. That is correct for
  this field and **inconsistent with the rest of the file**, which uses `.get()`
  and cannot tell missing from null. Is a locally-correct inconsistency better
  than a consistently-weak convention? I think yes for a new field, but it means
  two idioms now live in one file.
- I first wrote here that I had exercised only the check function and not the
  dedup path — the same "tested the component, not the delivery" gap you found
  in M28-1, one layer further out. Naming it was not good enough, so it is now
  tested: six tests drive `main()` with the network and notifier replaced and
  assert on what would actually be **sent**, including that an unchanged
  condition does not re-alert, that incomplete → ok → incomplete alerts twice,
  and that an active JD alert does not suppress a new audit finding.
- What is still untested at that layer: the real `notify()` transport. The tests
  replace it, so they prove the watchdog decides to send and what it would say,
  not that Gotify receives it.

## 5. The fixture, now shared

`leave_hot_wal()` is one module-level helper used by all three former
live-reader sites.

- It uses `subprocess.run([sys.executable, ...])`. In CI that is the same
  interpreter; is there an environment where it would not be, making the fixture
  silently different from the code under test?
- It asserts the `-wal` exists and is non-empty as a precondition. Is that
  sufficient to know the committed row is genuinely WAL-only, or should it also
  verify the main file does **not** contain the row?

## 6. Where I think this is weakest

**I have now reported a fixture as fixed twice when only the probe was.** Round
27 for R25-1a, and the same class again in how I verified it. The pattern is that
I fix the instrument I am measuring with, confirm the measurement, and do not
re-check the artefact that ships. I do not have a process fix for that beyond
"grep the committed tests", which is what caught it this time only because you
looked.

**Crash consistency is unaddressed** (`03-evidence.md` §9). The contract is
process restart, not power loss.

## 7. Not asking

- Not asking whether to merge, deploy or enable.
- Not asking about the `/health` disclosure shape; that is Jesse's call.
- Not asking you to re-review the swallowed-failure lint — it is still held
  local pending its own round, with its design questions written up in
  `docs/reviews/peer-rounds/LINT-swallowed-failures-review-request.md`.
