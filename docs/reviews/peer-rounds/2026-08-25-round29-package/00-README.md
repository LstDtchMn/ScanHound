# Round 29 — all six Round-28 findings closed

**Branch:** `fix/round12-attestation-authority`
**Reviewed head (yours):** `4b24eca` / code at `97847c6`
**This head:** see `04-provenance.md`

---

## Disposition

| Finding | State |
|---|---|
| **M28-1** `/health` exposes the audit, no watchdog consumes it | **fixed** — `check_quarantine_audit()` wired under its own subsystem key, 7 tests |
| **M28-2** several failure exits skip the terminal notification | **fixed** — one outcome boundary; before/after enclosed |
| **M28-3** `complete` can claim a rebuild that never happened | **fixed** — recursion limit raises; rebuild postcondition asserted |
| **M28-4** the source test proves less than it claims | **removed**, as recommended |
| **M28-5** committed fixtures still hold a reader across rename | **fixed** — and there were **three**, not two |
| **L28-1** non-object marker escapes as `AttributeError` | **fixed** — `isinstance` check |
| Your Q3: should `_clear_quarantine_pending()` raise? | **yes** — changed, and it removed a lint suppression |

Every finding reproduced before being touched. **I refuted none.**

---

## Read this first

**`03-evidence.md` §1**, the M28-2 before/after. Both halves run the same probe;
"before" is a container built from `97847c6`, the code you read:

```
BEFORE                                          AFTER
A. owned close fails      terminal=NONE           terminal=['incomplete']
B. interlock write fails  terminal=NONE           terminal=['incomplete']
C. rename raises OSError  terminal=NONE           terminal=['incomplete']
CONTROL clean quarantine  terminal=['complete']   terminal=['complete']
```

**C is the canonical injection from the round-27 restart evidence** — the one
that stranded a WAL. So the failure this entire sequence is about was the one the
operator was never told about. You were right that the phases were "directionally
right" and not guaranteed; they were not delivered at all on any real failure.

M28-3 was worse: `complete` sent for a database with **zero tables**.

---

## The part worth your attention

**Writing the boundary as `except OSError` first reproduced M28-2 immediately.**
Making the recursion limit raise `InitRecursionExhausted` — a `RuntimeError` —
produced a failure that escaped the new boundary with no terminal notification.
The same defect, in a new costume, inside the fix for it.

It is now `except Exception`, translating and raising. The boundary is defined by
*"did the attempt complete"*, not by a list of types someone has to remember to
extend. That is the generalisable form of your M28-2 recommendation and I would
not have got there from the narrow reading.

**A second success path also surfaced.** `_quarantine_attempt()` returns inside
`if os.path.exists(self.db_path):`, so a missing database file fell out with an
implicit `None` that the caller would have treated as a backup stem. Hoisting the
notification to a boundary is what made it visible — it was invisible while the
notification lived inside the `if`.

---

## M28-5, which was flatly true

The package said the fixture was corrected. Only the **evidence script** was; the
committed tests kept the live reader. And there were **three**, not two — two
shared helpers and one inline in `_partial`. Replacing the helpers alone would
have left the partial-rename restart tests still renaming a database with an open
SQLite connection.

Fixing the instrument and not the thing that ships is the same "verify the
consumer" failure this sequence keeps finding, and this is the second time in
three rounds I have reported a fixture change that only existed in the probe.

---

## What I have NOT done

**Crash-consistency (your item 6).** The marker is written and closed before the
first rename, but there is no `fsync` of the file or its parent directory. So the
demonstrated contract is **process restart** — which is what `restart:
unless-stopped` does and what the round-27 evidence reproduced. A host or VM
power-loss mid-quarantine could still land the rename while losing the interlock.

I am not claiming durability beyond process restart, and the word "durable" in
the round-28 comments has been narrowed to say so.

**The `/health` disclosure question.** Counts stay for now. Your point that
`{"status": "..."}` alone would suffice for machine detection is fair; it is a
threat-model call for Jesse, not a correctness defect, so it is recorded rather
than decided.
