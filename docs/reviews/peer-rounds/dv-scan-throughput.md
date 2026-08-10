# DV detection throughput — RETRACTED: the scan is healthy, my rate was wrong by 12x

**Date:** 2026-08-09 (rev 2, same day)
**Author:** Claude
**Reviewer:** ChatGPT (rev 1 reviewed at head `7eaeb749`; that review is superseded)
**Status:** **REV 1 IS WITHDRAWN.** Its central measurement was wrong and every conclusion
derived from it was wrong. Nothing was implemented. Nothing was deployed.

---

## 0. RETRACTION — read this before anything else

Rev 1 claimed DV detection could never finish. **That was false.** The scan is working
correctly and will clear its backlog in about two days.

| Rev 1 claimed | Actually measured |
|---|---|
| **6.7 MB/s** | **79 MB/s** |
| 1,417 hours for one full pass | **~39 hours** for the files that remain |
| **~59 days** | **~2.2 days** at ~18 scanning hours/day |
| ~500 hours / 3 weeks of backlog | ~39 hours |
| "the current design cannot finish" | It is finishing, at **6.0 files/hour** |
| Most files time out before completing | **31 of 33 succeeded** |

**The single root error.** I observed `dovi_tool.exe` pid 32936 at 12:35 and a *different*
pid 45220 at 15:10, and concluded one 60.95 GB file had taken 155 minutes. It had not — the
detector had moved through several files in between. From that one bad inference I derived
6.7 MB/s, and every figure in rev 1 inherited it: the 32.64 TB framing, the 59 days, the
"cannot converge" verdict, and the five options offered to fix a problem that does not
exist. I presented a derived guess as a measurement.

**The measurement that corrects it** — read from `data/dv_host.db` (copied aside with its
WAL to avoid a lock conflict) after the 2026-08-09 run:

```
rows written today : 33      first 17:05:54, last 22:33:56  (5.47 h)
  fel 11   mel 9   profile8 7   profile5 3   none 1   unknown 2
rate               : 6.0 files/hour  ->  ~79 MB/s at the 45.8 GB mean
host DB total      : 494 rows
remaining          : 730 - 494 = 236 files  ->  ~39 h of scanning
NULL signatures    : only the 2 unknowns (those retry; the 31 successes do not)
```

79 MB/s is an ordinary, healthy rate for this SMB path. There is no throughput defect.

## 1. What the library actually looks like

These figures from rev 1 were measured directly and stand:

| | |
|---|---|
| Files in the four configured roots | **730** |
| Total bytes | **32.64 TB** |
| Mean file size | **45.8 GB** |
| Largest | **89.9 GB** |
| Files over 40 GB | **494 of 730** |

`dv_detect.py:13` documents the read as a full pass (`dovi_tool extract-rpu`), which is
still true — it is simply fast enough not to matter.

## 2. ChatGPT's review of rev 1 — what survives

Its blocking finding was sound reasoning on my bad data, so it does not survive as a
blocker, but two findings do.

**The 30-minute cap is real but not operationally binding.** `_EXTRACT_TIMEOUT = 1800`
(`dv_detect.py:43`, used at `:162`) is genuinely enforced: `run_cancellable`
(`process_control.py:187`) takes the non-cancellable branch when `cancel_requested is
None` — which is how `dv_host_scan.py` calls it — and goes to
`subprocess.run(..., timeout=timeout)`. At 79 MB/s, 30 minutes covers ~142 GB, comfortably
past the 89.9 GB largest file. **Measured: 2 of 33 hit trouble, not "most".** So this is a
latent risk on the extreme tail plus genuinely slow moments, not the dominant behaviour.
Worth a size-proportional or work-budget bound eventually; not urgent.

**Retry starvation is real but bounded at this failure rate.** `classify_to_row()` stores an
`unknown` with NULL `sig_mtime`/`sig_size`, so it retries on every future run — confirmed:
only the 2 unknowns carry NULL signatures, the 31 successes do not. ChatGPT's "never
converge" pattern requires files to fail *systematically*; two per session does not crowd
out 236 never-scanned files. Retry metadata and never-scanned-first ordering remain good
hygiene.

**`--limit` exists, and the asymmetry is the useful part.** dovi_tool 2.3.2 supports
`-l/--limit N` including direct MKV input, so "does bounded mode exist" is answered. And
the semantics are one-sided: a bounded sample containing a FEL frame **proves** the title
contains FEL, while a sample containing only MEL **proves nothing** — a later frame may be
FEL. So the only safe bounded use is a FEL-positive accelerator
(`explicit FEL -> FEL`, `anything else -> NEEDS_FULL_SCAN`). Worth recording; **not worth
building**, since there is no throughput problem to accelerate.

**A separate real bug, unrelated to throughput.** `_PROFILE_RE` (`dv_detect.py:47`) matches
`Profile:` only. Upstream dovi_tool emits `Profiles:` when the RPU set contains multiple
profile values, so a mixed-profile stream would not parse. Track independently.

## 3. The conclusion rev 1 argued itself out of

**Option E — accept it — is correct.** The steady state is fine: change detection means
only new acquisitions cost anything. The backlog is ~39 hours of scanning, roughly two
days, and it makes durable progress (`_upsert()` commits per file,
`dv_host_scan.py:146`, inside the loop at `:207`), so a timeout loses only the file in
flight.

Do not build A (copy-locally-first), B (bounded reads), or C (run on the NAS). They exist
to fix a 12x-overstated rate.

**Two things still worth doing, neither urgent:**

1. **Progress visibility.** `run-dv-scan.ps1` folds the detector's output into its log only
   after the process exits, so a multi-hour run shows nothing. That absence is precisely why
   I could not see what was happening and inferred it wrongly instead. This is the highest-
   value item in the document.
2. **Import the results.** The host DB holds 494 rows while `dv_scan` in the container has
   466 and gained none today, so `POST /rename/dv-import` has not run since the scan
   started producing. Labels cannot update until it does.

**One open question I still have not resolved:** the two failures. If they are the 89.9 GB
titles, the timeout needs a size-proportional bound and the required headroom is knowable
exactly. If they are ordinary files, something else intermittent is happening.

## 4. Method note for the next person

Every wrong number here came from inferring a rate from two process snapshots instead of
reading what the detector recorded. The authoritative source was `dv_host.db` the whole
time — 33 rows with timestamps, which took one query. Read the artifact, not the process
list.
