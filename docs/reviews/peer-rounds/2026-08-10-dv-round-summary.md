# DV scan round — consolidated summary

**Branch:** `agent/dv-scan-hang-and-starvation` · **base** `main` `6813260`
**Status:** NOT merged, NOT deployed, mass write NOT executed. All Jesse's calls.
One place to start from instead of six documents.

---

## 1. What the round was asked to do, and why the premise was wrong

The task arrived as: *throughput is the blocking defect; the 30-minute
`_EXTRACT_TIMEOUT` caps extraction at ~12 GB at 6.7 MB/s, so most of a 32 TB
library can never complete a pass; scale the cap by file size.*

Measurement refuted the premise in both directions previously argued.

| claim | measured |
|---|---|
| 6.7 MB/s | **57–153 MB/s** per file end-to-end (median ~95) |
| the link or dovi_tool's read pattern is the bottleneck | storage streams **145–221 MB/s**, including 221 MB/s straight across the stall offset |
| the 30-min cap is binding | at the slowest healthy rate it covers **103 GB** > the 89.9 GB largest file |
| "the scan is healthy, 2 failures are an open question" (the prior retraction) | those 2 failures were the entire defect |

**The actual fault:** two Profile 7 FEL titles wedge `dovi_tool`. Sampled live
over 60 s: **0 read bytes, 0 read operations, 95.7% of one core**, output file
0 bytes, after reading 27.37 GB of 74.3 GB. A hang, not slowness.

**Why the recommended fix was wrong:** a size-proportional cap grants a frozen
file *more* time. It would have taken the loss from ~1 h per run to ~3 h.

## 2. Why it compounded into a two-week outage

Four mechanisms, none dangerous alone:

1. a failed detection stores NULL signatures, so it retries every run;
2. both wedged files sit in `Y:/Movie 1 (14TB)/4K DV`, which `os.walk` reaches
   first and which is otherwise 231/231 complete — so they are the first two
   things every run attempts;
3. the cap is genuinely enforced (`run_cancellable` takes the non-cancellable
   branch when `cancel_requested is None`, which is how the host scanner calls
   it);
4. `_post_import()` sits *after* the loop, and the run never reaches it —
   Task Scheduler kills it at `PT6H`.

Result: **one hour of every six-hour run burned before any new work**, then the
run dies before publishing. `dv_scan`'s newest row was `2026-07-25` while the
host DB had grown to 494.

## 3. The hang is upstream, and not fixed by upgrading

Jesse asked whether 2.3.2 was simply stale — reasonable, since 2.3.3 shipped
ten days after the pinned binary with changelog entries that read like the
symptom. Tested side by side, and the whole file copied to local NTFS:

```
2.3.2  SMB          27,367,062,473        2.3.2  LOCAL disk   27,367,130,713
2.3.2  SMB rerun    27,367,062,473        2.3.3  LOCAL disk   27,367,066,636
2.3.3  SMB          27,367,064,938
```

**Five stalls inside a ~68 KB window, two versions × two storage paths.** SMB
eliminated, version eliminated. Frame-bracketed by bisection: completes at
`-l 68018`, hangs at `-l 69577` (Death Wish 3: 80,487 / 82,046). Report drafted
in `dovi-tool-extract-rpu-hang-report.md`, not yet filed.

## 4. The fix

`--limit N` answers both wedged files in 3–20 s, and both are FEL. Validated
against 22 titles with known full-pass labels: **22/22 agreed**, 1.8–9.6 s
versus 2–24 minutes.

Only the FEL half is used, because the semantics are asymmetric: a bounded
sample containing FEL **proves** FEL, while a sample showing only MEL proves
nothing. `probe_fel_bounded()` returns a bool, never a layer. This matches what
`dv_scan` already means to its consumer — `pick_layer` aggregates with "one part
proving Dolby Vision proves it for the title".

Stated gap: no mixed `(MEL, FEL)` title appeared in the 22, so the MEL half is
unvalidated **by construction** — which is exactly why nothing but FEL is
trusted.

Shipped alongside: a **bytes-read stall watchdog** (180 s of zero progress →
`unknown`/`stalled`, fail-safe when progress is unmeasurable), retry backoff
(6/24/72/168 h), work ordered never-scanned → changed → due-retries with the
never-scanned bucket sorted **newest mtime first**, per-file logging, a budget
that stops *between* files so the final import always runs, and periodic
imports.

## 5. What peer review found that the task did not mention

The review's most valuable finding was in adjacent code: `/dv-sync-labels`
omitted `additive_only`, so the manual button ran a **destructive full
reconciliation** — `may_remove = authoritative or not additive_only`, and an
unmatched title yields `layer=None` → `authoritative=False`.

Measured before changing anything: **444 labelled titles, all 444 matched by an
authoritative row → zero realised exposure.** Mechanism real, damage zero,
invariant absent, so fixed anyway. `DvSyncRequest.additive_only` now defaults
`True`.

## 6. State

```
host DB          494 -> 568 rows,  NULL signatures 2 -> 0,  FEL 182 -> 253
root 4 backlog   236 -> 162
suite            branch 4,656 passed / main 4,626 -- zero regressions, +30
                 same 3 pre-existing test_queue_recovery_policy date-bomb fails
mutation checks  5, each restored and re-verified
probe            2,738 movies, 716 FEL (26%), 0 errors, 109 min
gates            1/2/3/4 PASS on the final frozen set, script exit 0
                 711 pure additions, 0 replacements, 0 removals
written to Plex  nothing
deployed         nothing
```

## 7. Errors I made and corrected, since they are part of the evidence

1. **Units.** Reported the stall at "25.49 GB" (GiB) against a file size in
   decimal GB. Same measurement, wrong presentation; corrected to 27.37 GB.
2. **An invalid experiment.** The first local-disk test used a file truncated at
   28 GB, which dovi_tool rejected in 0 s with `rc=1` — and my script classified
   "returned quickly without stalling" as COMPLETED, printing "SMB is
   implicated". A fast nonzero exit is not a success. Retested with the full
   74,277,195,186 bytes.
3. **A vacuous path comparison.** The first blast-radius measurement compared
   raw casefolded strings and ignored `normalize_path`'s `Y:` ↔ UNC mapping,
   producing a frightening and wrong "459 at risk".
4. **A failed positive control I nearly read as a finding.** The second attempt's
   control failed on my own shell escaping rather than any real defect.
5. **Over-excluding backups.** I dropped 1,429 titles living only on `BU`-named
   volumes as redundant copies. Jesse pushed back that the count looked too low;
   deduplicating showed 5,292 files → 4,996 distinct titles, i.e. 296 duplicates
   in total. Those volumes hold distinct content under legacy names.
6. **Overstated severity.** I called the `Profiles:` regex bug live. That any
   unparsed summary becomes an authoritative `LAYER_NONE` (which authorises
   badge removal) is verified; that dovi_tool 2.3.2 ever emits the plural is
   not, and the string search returning zero is untrustworthy because it also
   returns zero for `Profile: `, which the binary demonstrably prints. Latent,
   not live.

## 8. Also found, not asked for

Coverage. The scan is configured for four roots holding 730 files. On the same
machine: **2,827 unscanned 4K movies (147 TB)** across eight local volumes, only
87 ever checked. Plex addresses them by junction path (`C:\4K Drives\...`), which
ScanHound does not resolve — so a config using drive letters would fail
silently. Closed with evidence: the 197 files in plain `4K` folders are
genuinely non-DV (12-sample, 0 with any RPU), and `4K HDR Colombo` is 11,547 TV
files. Proposal in `dv-coverage-widening-proposal.md`.

## 9. Documents

| file | what |
|---|---|
| `dv-scan-hang-and-starvation.md` | root cause, evidence, changes |
| `dovi-tool-extract-rpu-hang-report.md` | upstream report, both versions |
| `dv-scan-deploy-checklist.md` | ordered: deploy → write → sync |
| `dv-mass-write-double-check-for-chatgpt.md` | the four questions asked |
| `dv-mass-write-gate-results.md` | gate evidence, initial + final |
| `dv-coverage-widening-proposal.md` | the 2,827-movie question |
