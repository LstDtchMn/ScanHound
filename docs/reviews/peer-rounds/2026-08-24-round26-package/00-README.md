# Round 26 — every Round-25 finding closed, plus the item deferred three times

**Branch:** `fix/round12-attestation-authority`
**Reviewed head (yours):** `e26c2f7`
**This head:** see `04-provenance.md`
**Patch enclosed:** `02-code-changes.patch`

---

## What is in this package

| File | What it is |
|---|---|
| `00-README.md` | this |
| `01-request.md` | what I am asking you to review, and where I think it is weakest |
| `02-code-changes.patch` | the complete diff since the head you reviewed |
| `03-evidence.md` | every claim in this package, with the measurement behind it |
| `04-provenance.md` | branch vs `origin/main` vs the running container |
| `05-retired-test-mapping.md` | the mapping document, now with a seventh correction |
| `evidence-01-findings.txt` | raw transcript: all five findings re-measured against this head |
| `evidence-02-regression-G-mutation.txt` | raw transcript: regression G's guard shown firing on the real defect |
| `evidence-03-r25-2-blast-radius.txt` | raw transcript: can R25-2 turn contention into quarantine? (no, with a positive control) |
| `evidence-04-get-connection-callers.txt` | all 33 `if not conn:` sites classified by what they did |
| `evidence-05-r25-1-refusal.txt` | raw transcript: the R25-1 refusal shown INERT, then shown firing |
| `evidence-06-refusal-mutation.txt` | raw transcript: which half of the refusal fix is load-bearing (not the half I claimed) |

---

## The seven items you raised, and their state

| # | Finding | State |
|---|---|---|
| R24-1 | extended corruption codes classified as NOT corruption | **fixed** — reduced to the primary result code |
| R25-1 | quarantine moved one file, stranding a hot `-wal` | **fixed** — the whole bundle moves, and it refuses to proceed if a journal is left behind. **The refusal was inert as first written; see §12** |
| R25-2 | a failed PRAGMA still returned a configured-looking connection | **fixed** — built in a local, published only on success. Blast radius measured: 30 callers change behaviour, but contention still never quarantines (§11) |
| R23-2 | the alias rebuild invented an association from a lossy survivor | **fixed** — derived from the LIVE association, and only when unambiguous |
| R23-1b | an ABSENT semantic field counted as agreement at the live writer | **fixed** — absence is now unknown, not consent, for the writer only |
| — | seventh overstated **A** in the retired-test mapping | **found and reclassified**, with a mutation check |
| — | two documentation drifts | **fixed** |

Plus one item that was **not** in your Round-25 list:

| Regression **G** | surface historical audits whose `rows_affected` exceeds surviving snapshots | **closed** |

Regression G had been carried as deferred across three rounds. It sits directly
on the R23-2 code I was already changing, so I closed it here rather than report
it deferred a fourth time. That is one item beyond the scope Jesse set for this
round, and I am flagging it rather than folding it in silently.

---

## The one thing to read first

`03-evidence.md` §12 — a defect I introduced **in this round**, while closing one
of yours, and which survived my own review of the diff.

The R25-1 fix refuses to create a fresh database if a journal cannot be moved
aside. I wrote that refusal as `raise OSError(...)`. The same method ends with a
pre-existing `except OSError: logger.critical(...)`. **The refusal raised into
its own method's catch-all and could never fire.**

What makes it nasty is that the visible outcome looked correct — no fresh
database was created, so inspecting the directory would have passed. The defect
was that `_quarantine_corrupt_db()` returned *normally*, so the caller resumed as
though recovery had succeeded on a half-quarantined database.

It was found only by injecting a rename failure, because the rule is that a guard
must be shown to FAIL. Fixed with a non-`OSError` refusal type and four
regression tests including an anti-vacuity control.

The question I actually want answered is in that section: **how many more of
these are there?** The shape is *a raise that lands inside a handler in its own
call path*. I have no systematic check for it.

Second priority is `§6` — the Round-24 fix for R23-2 recovered an alias's
`listing_type` from the surviving old quarantine row. You rejected that and you
were right; measured, it relabels a movie alias as `tv`:

```
raw-movie -> 'tv'      (invented)
raw-tv    -> 'tv'
```

The survivor is the R23-2 casualty itself. That is the third repair in this
sequence I have written that asserts more than the underlying data supports.

---

## What I could not verify

- The 11-failure baseline I quoted in previous packages **was wrong** — two of
  the three test files it named do not exist anywhere (`03-evidence.md` §1). My
  first attempt to re-measure was also wrong, for a different reason: a partial
  tree copy invented 77 failures on main. Both corrections, and the method that
  finally produced trustworthy numbers, are in `04-provenance.md` §6.

  The measured result: `origin/main` **1 failed, 5356 passed**; this branch
  **0 failed, 5769 passed**. Main's single failure is main's own (§5b).
- The "frozen ledger" carried through rounds 24 and 25 is now **diagnosed** and
  is not an incident: the feature has never been merged to `main`, so the
  deployed image has no ledger writer at all, and the 266 rows are residue from
  branch code run manually against the live database. `04-provenance.md` §3 has
  the evidence including positive controls. The real consequence is that **no
  production positive control exists** for any writer claim in this package.
