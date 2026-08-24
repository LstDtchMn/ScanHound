# Round 28 — all six Round-27 findings closed, two of them retractions of mine

**Branch:** `fix/round12-attestation-authority`
**Reviewed head (yours):** `b8433f1`
**Patch base:** `a7f7b13` (that head, plus the `origin/main` merge)
**This head:** see `04-provenance.md`

---

## Contents

| File | What it is |
|---|---|
| `00-README.md` | this |
| `01-request.md` | deliberately narrow — attack the code I wrote in response, not the findings |
| `02-code-changes.patch` | complete diff since the reviewed head |
| `03-evidence.md` | every claim, with the measurement behind it |
| `04-provenance.md` | branch vs main vs the running container |
| `evidence-01-r26-1-behaviour-matrix.txt` | what each boundary really does when `get_connection()` fails |
| `evidence-02-r25-1c-before.txt` | the restart hazard reproduced against `a7f7b13` |
| `evidence-03-r25-1-after.txt` | 13 checks incl. controls, after the fix |
| `evidence-04-r26-3-busy-timeout.txt` | the measurement that refutes my own causal claim |

---

## Disposition

| Finding | State |
|---|---|
| **R25-1a** rename while a connection is open | **fixed** — fixture no longer holds a reader across the rename |
| **R25-1b** close failure swallowed | **fixed** — close is a precondition; zero renames on failure |
| **R25-1c** refusal does not survive a restart | **fixed** — durable interlock, checked before `sqlite3.connect()` |
| **R25-1d** notification claims success early | **fixed** — three phases |
| **Regression G** fail-soft read = false clean | **fixed** — strict read, **and wired to `/health`** |
| **R26-1** blast-radius accounting invalid | **you were right — §11 retracted, replaced with a measured matrix** |
| **R26-2** unsafe default | **fixed** — keyword-only and required |
| **R21-10** eighth overstated A | **fixed** |
| **R26-3** busy-timeout causal claim | **you were right — retracted** |

All six were confirmed by reproduction before being touched. I refuted none.

---

## Read this first

**`03-evidence.md` §2** — the R25-1c before/after pair, because it is the one
with a data-loss outcome and both halves are reproducible:

```
BEFORE (a7f7b13, no interlock)          AFTER
  restarted manager CONSTRUCTED           restarted manager REFUSED
  41 tables, `precious` ABSENT            no fresh DB over the hazard
  committed WAL orphaned: True            committed WAL orphaned: False
```

Round 26's refusal bought exactly one process lifetime, and
`docker-compose.yml:6` is `restart: unless-stopped`.

**Then `01-request.md` §1.** The interlock is the riskiest thing in this change
and the thing I most want broken: too permissive and a restart rebuilds over a
stranded journal; too strict and ScanHound will not start until a file is
deleted by hand. It is an hour old and nobody but me has read it.

---

## What I got wrong, again

Two of your nine findings were retractions of claims I published.

**R26-1** is the one worth your attention. §11 of the round-26 package sits three
sections after §12, where I wrote *"local text that looks like propagation does
not establish caller-visible propagation"* — and then made exactly that inference
about `_query()`, in the same document, on the same day. Measured: **6 of 7
boundaries return rather than propagate.**

**R26-3** is the third wrong causal claim I have published in three rounds. All
three were about mechanism rather than outcome. All three would have been caught
by measuring instead of reasoning.

I do not have a process fix for this yet beyond "measure the claim, not just the
behaviour", which is what §5 of the evidence now does.

---

## What I have not done

- **Your AST swallowing-handler lint and the mutation policy for recovery
  handlers.** Both are worth building. Neither is here, deliberately — I did not
  want to bundle a new tool with the fixes it would have found, and the tool
  deserves its own review rather than riding in on this one.
- **Decided whether each fail-soft boundary in the matrix is correct.** You named
  `/scan-history` as one that should keep degrading and I agree. The rest are
  unreviewed by either of us.
- **Fixed the `null`-vs-absent ambiguity in `/health`.** A watcher cannot
  distinguish "the read failed" from "this build has no such report". That is the
  same defect class one level up, it affects `arm_revisions` and `jd_poll` too,
  and I flagged rather than fixed it — see `01-request.md` §2.
