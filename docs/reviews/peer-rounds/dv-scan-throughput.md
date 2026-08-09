# DV detection throughput — the current design cannot finish

**Date:** 2026-08-09
**Author:** Claude
**Reviewer:** ChatGPT (design review; no code changed)
**Status:** Measurement + options. Nothing implemented, nothing deployed.

> **REVIEWER: read this file from the repository, not any chat summary of it.** If you
> cannot read it directly via the GitHub connector, **stop and say so.**

---

## 0. The finding

DV detection reads **every byte of every file**. `backend/rename/dv_detect.py:13`
documents this in its own comment:

```
dovi_tool extract-rpu "<file>" -o <rpu.bin>          # full pass, no decode
```

Measured against the four configured roots
(`Y:/Movie 1 (14TB)/4K DV`, `Y:/Movie 2 (8TB)/4K DV`, `Y:/Movie 3 (2TB)/4K DV`,
`//TURTLELANDSRV2/4K Magellan/DV`):

| | |
|---|---|
| Files | **730** |
| Total bytes | **32.64 TB** |
| Mean file size | **45.8 GB** |
| Largest | **89.9 GB** |
| Files over 40 GB | **494 of 730** |

Observed throughput, from the 2026-08-09 12:35 run: `Death Wish 3 (1985).mkv`,
**60.95 GB**, `dovi_tool extract-rpu` at ~90% of one core for **~155 minutes** wall —
**6.7 MB/s**.

**At that rate one full pass over the library is 1,417 hours — about 59 days of
continuous reading.**

## 1. Why this is not merely slow

**The backlog is the problem, not the steady state.** Change detection
(`sig_mtime`/`sig_size`, `DV_MTIME_TOL = 2.0`) means an unchanged file is skipped, so once
the library is fully scanned only new acquisitions cost anything — a few files a week,
fine. But `dv_scan` currently holds **466** real detections against **730** files in the
roots, so roughly **264 files have never been detected**. At 45.8 GB mean and 6.7 MB/s
that backlog alone is **~500 hours ≈ 21 days** of scanning.

**The scheduler cannot deliver that.** `ScanHound-DVScan` runs every 4 hours with
`ExecutionTimeLimit = PT6H` and `MultipleInstances = IgnoreNew`. Observed on 2026-08-09:
the 12:35 run was still on its **first file** at 15:00, so the scheduled 15:00 occurrence
was refused (`0x800710E0`). Long files therefore displace scheduled runs indefinitely.
Progress is real and durable — `_upsert()` commits per file
(`scripts/host-detector/dv_host_scan.py:146`, inside the loop at `:207`), so a timeout
loses only the file in flight — but the rate is the binding constraint.

**6.7 MB/s is the anomaly worth attacking.** `Y:` is SMB to `\\TURTLELANDSRV2`. A gigabit
path should sustain roughly 100-110 MB/s, so we are achieving **~6%** of the available
bandwidth. That points at `dovi_tool`'s read pattern — small, unbuffered, latency-bound
reads — rather than the link. The same class of problem is already recorded for the
container path in `docker-windows-bindmount-syscall-latency` (dovi_tool's unbuffered
Matroska path against 9p `msize=65536`); this is the SMB instance of it.

**Unverified:** I have not measured raw sequential SMB throughput on this host, nor
`dovi_tool` against a local file. Both are needed before ranking the options below. See §4.

## 2. What the tool is actually being asked for

`detect_layer()` runs `extract-rpu` to obtain the RPU stream, then reads its info to
classify `fel` / `mel` / `profile8` / `profile5`. Distinguishing **FEL from MEL** requires
RPU content, not container metadata — that is why `ffprobe` cannot substitute (it reports
that DV is present, not which layer). So the *classification* genuinely needs RPU data.

The open question is whether it needs **all** of it. Dolby Vision RPUs are interleaved per
frame throughout the elementary stream, so this is not a header that can be read from the
first megabyte. But a **layer verdict** may be obtainable from far fewer frames than the
whole title — and `dovi_tool` may or may not expose a bounded mode.

## 3. Options, unranked pending §4

**A. Copy locally, then process.** Read the file over SMB sequentially at full bandwidth
into local scratch, run `dovi_tool` against the local copy, delete. Wins only if raw copy
throughput is much higher than 6.7 MB/s and local `dovi_tool` is faster than
network-bound `dovi_tool`. Costs ~46 GB of scratch churn per title.

**B. Bound the read.** If `dovi_tool` can be limited to N frames or a byte range while
still yielding a trustworthy FEL/MEL verdict, cost falls by orders of magnitude.
**Correctness risk:** `dv_detect.py:96` records that a mixed title with *some* FEL frames
counts as FEL — a bounded read could see only MEL frames and mislabel it. That is a
downgrade, and downgrades matter here (a prior audit found a scoring bug that overwrote 4K
DV with 1080p).

**C. Run the detector on the NAS.** Eliminate the network from the read path entirely by
executing where the files live. Biggest architectural change; may not be possible on that
device.

**D. Prioritise instead of accelerating.** Scan smallest-first or newest-first so useful
labels appear sooner, accepting that the tail may take weeks. Cheapest to implement,
changes nothing about the underlying rate.

**E. Accept it.** Steady state is fine. Only the 264-file backlog is slow, and it does make
progress. This is the honest do-nothing baseline any option must beat.

## 4. What must be measured before choosing

1. **Raw sequential SMB throughput** from this host to `\\TURTLELANDSRV2` — the ceiling
   option A is chasing. If it is also ~7 MB/s, option A is dead and the link is the
   problem.
2. **`dovi_tool` against a LOCAL file** of comparable size. Isolates tool cost from network
   cost. If local is also ~7 MB/s, the tool is the bottleneck and only B or C help.
3. **Whether `dovi_tool` supports a bounded read** at all (frame count, byte range, early
   exit), and whether a bounded verdict agrees with a full-pass verdict on known FEL, MEL
   and mixed titles. Without that agreement measurement, option B is unsafe.
4. **The real backlog count**, not my estimate. 730 minus 466 assumes those sets are
   comparable, and they are keyed on different path forms — the same vacuous-join hazard
   recorded elsewhere in these reviews. Normalise before trusting 264.

## 5. Questions

1. **Is option A worth measuring first**, given it is the only one needing no correctness
   argument? Or is measuring `dovi_tool` locally (§4.2) the better first cut because it
   partitions tool-versus-network in one test?
2. **Is option B acceptable in principle?** A bounded read that can downgrade FEL to MEL
   trades correctness for speed on the one field this feature exists to produce. Is there a
   bounded strategy that is *conservatively* correct — e.g. bounded scan to prove FEL
   present, full pass only to prove FEL absent?
3. **Should the schedule change regardless?** A 4-hour trigger with a 6-hour limit against
   2.5-hour files means scheduled occurrences are routinely refused. Is one long-running
   pass more honest than a schedule that mostly cannot fire?
4. **Is option E defensible?** The steady state genuinely is fine. Am I treating a one-time
   21-day backlog as an architectural defect when it is really a one-time cost that needs
   only patience and better progress reporting?
5. **What have I not considered?** Every prior round found something outside my frame — the
   commit history that refuted a theory, a policy module of mine that auto-resumed what it
   should not. I would rather be told what is missing than have these five options ranked.
