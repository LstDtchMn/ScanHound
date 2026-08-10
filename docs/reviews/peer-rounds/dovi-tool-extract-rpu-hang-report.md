# dovi_tool 2.3.2 — `extract-rpu` hangs at a fixed stream offset (100% CPU, zero read syscalls)

Draft upstream report for https://github.com/quietvoid/dovi_tool.
Everything below is measured; the one unresolved variable is called out in §6.

---

## 1. Summary

On two specific Profile 7 FEL titles, `dovi_tool extract-rpu` stops consuming
input partway through the file and never finishes. The process stays alive and
pegs one core at ~95%, but issues **zero further read syscalls** and never
writes its output file. It is a hang, not slow progress.

The stall is at a **fixed stream offset, reproducible to the byte** across runs
hours apart under different system load, and it is **frame-addressable**:
`--limit N` completes for every N below a boundary and hangs for every N above.

## 2. Environment

| | |
|---|---|
| dovi_tool | 2.3.2 (Windows x86_64) |
| OS | Windows 11 Pro 10.0.26200 |
| Command | `dovi_tool extract-rpu "<file>" -o <out>.rpu.bin` |
| Input | Matroska (.mkv), Dolby Vision Profile 7 FEL, UHD remux |

## 3. Observed behaviour

Sampled on the live process (pid 19848) over a **60 second** window using
`GetProcessIoCounters`:

```
read_bytes_delta : 0            <- no bytes read
read_ops_delta   : 0            <- no read operations AT ALL
cpu_percent      : 95.7         <- single thread, pegged
threads          : 1
total_read       : 27,367,062,473 bytes  of a 74,277,195,186 byte file (36.8%)
output .rpu.bin  : 0 bytes, not growing
```

It stays in that state indefinitely; observed runs were killed at a 1800 s cap
having made no further progress. Four such runs occurred in one day across the
two files, each hitting the cap exactly.

## 4. Two affected files, and where each stops

Bisected with `--limit N`, each probe abandoned once it went 90 s with no read
progress:

**Jurassic World Rebirth (2026)** — 74,277,195,186 bytes

```
--limit  50875  completed (260 s)     --limit  75812  HANGS (399 s)
--limit  63343  completed (289 s)     --limit 100750  HANGS (470 s)
--limit  66460  completed (304 s)     --limit 200500  HANGS (392 s)
--limit  68018  completed (321 s)     --limit 400000  HANGS (417 s)
                                      --limit  69577  HANGS (401 s)
```

→ boundary between frame **68,018** (ok) and **69,577** (hangs);
stalls at byte **27,367,062,473**.

**Death Wish 3 (1985)** — 65,447,127,320 bytes

```
--limit  50875  completed (262 s)     --limit  82046  HANGS (519 s)
--limit  75812  completed (373 s)     --limit  88281  HANGS (588 s)
--limit  78929  completed (474 s)     --limit 100750  HANGS (449 s)
--limit  80487  completed (414 s)     --limit 200500  HANGS (423 s)
                                      --limit 400000  HANGS (537 s)
```

→ boundary between frame **80,487** (ok) and **82,046** (hangs).

Different absolute frames and different fractions of each file, so this is not
a fixed count or a fixed proportion — it looks content-dependent, consistent
with a parser loop on a particular RPU/NAL structure.

## 5. What is ruled out

**Not the storage or the link.** A plain sequential read of the *same bytes*,
including 4 GB spanning the exact stall offset, streams at full speed:

```
first 2 GB                          162.9 MB/s
4 GB across the stall offset        221.3 MB/s   <- fastest of the three
Death Wish 3, first 2 GB            145.7 MB/s
```

**Not a blocked read.** A thread waiting on I/O would be idle. This one holds
95.7% of a core while issuing no read operations at all.

**Not a truncated or unreadable file.** Both files play normally and both are
fully readable end to end.

**Not transient.** Independent runs hours apart stalled at the identical byte
offset.

## 6. The one variable not yet eliminated

Both files live on an SMB share (a mapped drive to a NAS). The zero-read-syscall
signature argues strongly against an SMB interaction — a stalled network read
would not burn CPU — but a local-disk reproduction has not yet been completed.
**A retest against a local copy is in progress and this report should not be
filed until that result is in.** If the hang does not reproduce locally, the
whole framing changes and this becomes an SMB-interaction report instead.

## 7. Workaround

`--limit N` below the boundary completes normally and yields a correct RPU. For
the practical question ("does this title carry FEL?") a 1000-frame limit answers
both files in **3–20 seconds** and both correctly report `Profile: 7 (FEL)`,
versus never completing at all. Validated against 22 titles whose profile came
from a completed full pass: 22/22 agreed.

## 8. What would help most

If the maintainer can suggest a build with parse-loop tracing, or wants the RPU
extracted from the surrounding frame range (`--limit 69577` minus the last good
`--limit 68018` window is ~1,559 frames), that range can be produced and
attached — a small sample rather than a 74 GB file.
