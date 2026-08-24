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

---

## The seven items you raised, and their state

| # | Finding | State |
|---|---|---|
| R24-1 | extended corruption codes classified as NOT corruption | **fixed** — reduced to the primary result code |
| R25-1 | quarantine moved one file, stranding a hot `-wal` | **fixed** — the whole bundle moves, and it refuses to proceed if a journal is left behind |
| R25-2 | a failed PRAGMA still returned a configured-looking connection | **fixed** — built in a local, published only on success |
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

`03-evidence.md` §6. The Round-24 fix for R23-2 recovered an alias's
`listing_type` by joining the surviving old quarantine row. You rejected that,
and you were right — I measured it and it relabels a movie alias as `tv`:

```
raw-movie -> 'tv'      (invented)
raw-tv    -> 'tv'
```

The survivor is the R23-2 casualty itself. Reading a type back off it is
inventing lineage from the very row that proves lineage was lost. That is the
third time in this sequence I have written a repair that asserts more than the
underlying data can support, and it is the failure mode I would most like you to
keep hunting.

---

## What I could not verify

- The 11-failure baseline I have quoted in previous packages **was wrong.**
  See `03-evidence.md` §1. Two of the test files I named as pre-existing
  failures do not exist in the repository, are not tracked by git, and are not
  in the container. The real baseline is re-measured here against `origin/main`
  in the same session, same container image, same dependency set.
- The "frozen ledger" carried through rounds 24 and 25 is now **diagnosed** and
  is not an incident: the feature has never been merged to `main`, so the
  deployed image has no ledger writer at all, and the 266 rows are residue from
  branch code run manually against the live database. `04-provenance.md` §3 has
  the evidence including positive controls. The real consequence is that **no
  production positive control exists** for any writer claim in this package.
