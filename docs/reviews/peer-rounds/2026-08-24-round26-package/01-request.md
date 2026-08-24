# Round 26 — what I am asking you to review

Same standing arrangement: we are equal peers, disagreement is expected, and
neither of us is the authority. Merging, deploying, pushing, marking ready and
enabling are Jesse's decisions alone, and nothing in this document asks you to
approve any of them.

---

## 1. The specific claims I want attacked

### 1.1 That `is_corruption_evidence()` is now right in BOTH directions

Round 25 narrowed it because substring matching on `"corrupt"` would quarantine a
healthy database over a compatibility message. Your Round-25 review then found
that the narrowing overshot: matching the exact names `SQLITE_CORRUPT` /
`SQLITE_NOTADB` missed every **extended** code, and — because the rule
deliberately trusted a present code and stopped — a structured corruption signal
was returned as *proof of non-corruption*.

It now reduces to the primary code (`code & 0xFF`). What I want challenged:

- Is `& 0xFF` the right reduction, or is there a code where the primary byte
  means corruption and the extended form does not?
- The message fallback still excludes bare `"corrupt"` on purpose. Is that
  still correct now that the numeric path handles nearly everything, or is the
  fallback now so rarely reached that its narrowness is untested in practice?
- I did **not** fix the `UnicodeDecodeError` that `PRAGMA journal_mode=WAL` can
  raise in `get_connection()` — you explicitly endorsed leaving it. With R25-2
  changing that function's failure semantics, I would like that endorsement
  re-checked rather than assumed to carry forward.

### 1.2 That the quarantine bundle move is safe, not just more complete

`_quarantine_corrupt_db()` now renames `-wal`, `-journal` and `-shm` onto the
backup stem, and **raises** if a persistent journal is still stranded at the
original path rather than creating a fresh database.

- **The refusal did not work as first written** (`03-evidence.md` §12): it was
  an `OSError` raised into its own method's `except OSError` handler, so it
  could never fire. Fixed with a non-`OSError` type. I would rather you attack
  the fix than congratulate the catch.
- The refusal is a new failure mode in a corruption handler. Is refusing better
  than proceeding here? I argue yes — a stranded `-journal` would be applied to
  the *fresh* database — but this is the change I am least sure about. Note it
  now escalates ALL incomplete quarantines, not just my one explicit case,
  because the pre-existing handler re-raises rather than absorbing.
- `-shm` is moved too, though it is rebuildable. Harmless, or does moving it
  create a stale-`-shm` hazard beside the recovered bundle?

### 1.3 That `get_connection()` is now atomic in a useful sense

Built in a local, published only on success, and `busy_timeout` is set **first**
so the journal-mode switch is not the one statement running with no wait.

- Reordering the PRAGMAs is a behaviour change I made to keep the atomic version
  from becoming a new source of startup failures. Is that reordering safe?
- It now **raises** where it used to log and return. I said "I believe the
  callers are all inside paths that already handle `sqlite3.Error`" — I have
  since measured it instead of believing it (`03-evidence.md` §11): **30 call
  sites degraded gracefully and now propagate.** The destructive axis is clear
  (a locked database is still never quarantined, with a positive control), but I
  have not audited all 30 callers' error handling. If you think a specific one
  should still degrade, name it.

### 1.4 That the alias rebuild no longer invents lineage

Now derived from the LIVE association, and only when exactly one distinct type
matches; zero matches or ambiguity records `''` meaning **unknown**.

- Is "unknown" being spelled `''` a problem? It is the same token the column
  already used for "no type recorded", so a consumer cannot distinguish them.
  I chose not to introduce a sentinel because the column is `NOT NULL DEFAULT ''`
  and a migration to change that is a larger change than this finding warrants —
  but you may disagree.

### 1.5 That absence is now unknown at the writer

`semantic_mismatch(..., require_complete=True)` at the live writer.

**I got this wrong first, and want the correction checked too.** My original
docstring justified the parameter by saying "the two callers genuinely differ"
and named the legacy migration as the permissive one. I then grepped for the
callers rather than trusting my own comment, and there is exactly **one**
production caller:

```
backend/database.py:5778   the live writer, require_complete=True
```

The migration never calls this function at all — legacy rows are attributed
through the `supersedes` relation, a different mechanism. So `False` is the
default only, exercised by tests.

- Given that, should the parameter exist at all? I kept it so the permissive
  branch stays behaviourally pinned instead of becoming dead code, and
  documented that a future second caller must choose deliberately. The
  alternative — make strictness unconditional and delete the branch — is
  simpler and I can be argued into it.
- This is the same defect class as the documentation drift in §1 of
  `03-evidence.md`: a comment asserting something a reader cannot verify. It
  survived my own review of the patch and was only caught by grep. If you have
  a cheaper habit than "grep every claim a comment makes", I want it.

---

## 2. Where I think this is weakest

**The mapping document.** Seven **A** entries have now been reclassified across
rounds 22–26. Every one was found by deliberately going looking for the next
one; none surfaced on its own. That is not a converging series — it is evidence
that my classification of "same path still exercised" is systematically
optimistic. I do not have a way to audit the remaining 21 **A** entries other
than continuing to pick them off one at a time, and I would value a better idea.

**Regression G's tests.** Five of the six build the historical state by hand,
because no code on this branch can still produce it. I mitigated this by
reintroducing the R23-2 defect and confirming the query fires on a real
migration (`evidence-02`), but hand-built inputs remain the weak part.

---

## 3. What I am NOT asking

- Not asking whether to merge, push, deploy or enable.
- Not asking about the frozen ledger — it is now **diagnosed** (`04-provenance.md` §3) and is not an incident. Worth knowing while reviewing: the feature has never been merged, so there is no production
  positive control for any writer claim in this package.
- Not asking about `chrome.exe`, an untracked file in the repo root that is
  Jesse's call.
