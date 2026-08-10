# Widening DV detection to the local 4K drives — proposal

**Status:** proposal only. No config changed, nothing deployed, nothing scanned
on the strength of this document. Jesse's call.

---

## 1. The gap, measured

The detector is configured for four roots holding **730 files**. On the *same
machine that runs the scan* there are eight more 4K volumes, mounted as
junctions under `C:\4K Drives\` plus `G:` directly:

```
5,292 video files on the local 4K drives      171.8 TB
  distinct titles by filename                  4,996
  movies >= 15 GB                              2,827
  of those, ever DV-scanned                        87
```

**Only 87 of 2,827 local movies have ever had a Dolby Vision check.** The local
drives are almost entirely disjoint from the configured roots — this is not a
small extension of the current scan, it is most of the library.

## 2. "Backup" drives are not redundant copies — this is the load-bearing finding

Four volumes are named `BU`/`Backup` (Quantum, Rickover, Ulysses & Yuri
Gagarin, Arnold) and my first inventory excluded them on that basis. That was
wrong, and Jesse caught it. Deduplicating by filename across **all** drives:

```
5,292 files  ->  4,996 distinct titles  =  296 duplicate copies in total
```

296 duplicates across 5,292 files is not what a backup tier looks like. These
volumes hold **distinct titles under legacy names**, so excluding them dropped
~1,429 real movies and understated the library by roughly the amount Jesse said
was missing. They should be scanned.

## 3. What the folders actually contain

Enumerated 2026-08-10. Mean file size is the discriminator: ~2.8 GB is TV,
~35–66 GB is a 4K movie.

**Recommended — add these (movies, non-backup names):**

| files | size | mean | path |
|---|---|---|---|
| 333 | 19.27 TB | 57.9 GB | `C:\4K Drives\4K Gambino\4K DV` |
| 422 | 15.93 TB | 37.7 GB | `C:\4K Drives\4K Jefferson & Truman BU\Jefferson 4K` |
| 334 | 11.79 TB | 35.3 GB | `C:\4K Drives\4K Columbo\Movies 2` |
| 344 | 11.47 TB | 33.3 GB | `G:\Movies 1` |
| 240 | 10.89 TB | 45.4 GB | `C:\4K Drives\4K Columbo\Movies 1` |
| 164 | 7.88 TB | 48.0 GB | `C:\4K Drives\4K Jefferson & Truman BU\Truman 4K` |
| 113 | 4.63 TB | 40.9 GB | `C:\4K Drives\4K Gambino\4K HDR` |

**Recommended — also add these (legacy `BU` names, distinct content per §2):**

`4K Quantum\Quincy Backup (10TB)` · `4K Quantum\Roosevelt Backup (8TB)` ·
`4K Rickover\Rickover BU 1 (8TB)` · `4K Rickover\Rickover BU 2 (8TB)` ·
`4K Rickover\Rickover BU 3` · `4K Ulysses & Yuri Gagarin BU\Ulysses BU` ·
`4K Ulysses & Yuri Gagarin BU\Yuri Gagarin BU` · `4k HDR Arnold\BU1 8TB` ·
`4k HDR Arnold\BU2 8TB` · `4k HDR Arnold\BU3 2TB`

**Do NOT add:**

| what | why |
|---|---|
| `C:\4K Drives\4K Columbo\Library` | 1,593 files, mean **2.8 GB** — TV, not movies |
| `G:\Downloads`, `4K Columbo\downloads` | staging areas; files move and churn |
| `G:\$RECYCLE.BIN` | deleted items |
| `C:\4K Drives\4K Columbo\Movie` | 46 files, mean 5.9 GB — inspect before including; too small to be UHD remuxes |

## 4. Paths must be the junction form, not drive letters

`A:\Movie\file.mkv` and `C:\4K Drives\4K Gambino\Movie\file.mkv` are the same
bytes, but **ScanHound resolves neither junctions nor volume GUIDs** — matching
is string normalization only. Plex stores the junction form, so the config must
use it or every row silently fails to match and no badge appears. That is
exactly the failure mode that lost all 371 Y:-drive files on 2026-07-11.

Verified by volume GUID rather than by the similar names — `E:` is labelled
"4K HDR Columbo" while its junction is "4K Columbo", and `G:` has no junction
at all and is therefore correct *as* a drive letter.

## 5. Cost

The bounded FEL accelerator changes this arithmetic completely — without it the
proposal would not be worth making.

```
~2,827 movies to scan
  ~40% prove FEL from a 1000-frame sample   ~1,130 x ~3.4 s   ~=   1.1 h
  ~60% need a full pass                     ~1,700 x ~7 min   ~= 198 h
                                                        total ~= 200 h
at ~18 scanning hours/day                                     ~= 11 days
```

One-time. Steady state afterwards is new acquisitions only, because change
detection skips everything already scanned — and with newest-first ordering a
fresh grab is the first thing each run looks at rather than the last.

Local drives read at ~150 MB/s versus the SMB path's 145–221 MB/s, so per-file
cost is comparable; the volume is what makes it eleven days, not the speed.

## 6. Prerequisites

1. **Deploy this branch first.** Without the stall watchdog, any wedged file on
   these drives burns 30 minutes per run instead of 3, and there are 2,827 more
   chances to find one. Without the bounded accelerator the estimate above is
   roughly 330 hours instead of 200.
2. **`--mode steady` should be wired up before, not after.** With a backlog this
   size the retry sweep and the backfill compete for the same 6-hour window; the
   modes exist precisely to separate them.
3. **Expect more wedged files.** Two turned up in 730. If the rate holds, ~8
   more are waiting in 2,827 — which is an argument for the watchdog, not
   against the widening.

## 7. What this does NOT decide

Whether the ~1,130 FEL titles found by tonight's probe get written. That is the
separate gated operation in `dv-mass-write-gate-results.md`, and it is about
titles already probed. This document is about what the *scheduled* scan covers
from now on.
