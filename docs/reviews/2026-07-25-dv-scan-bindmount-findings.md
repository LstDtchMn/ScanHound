# 4K metadata scan: root cause is the bind mount, not the tools

**Date:** 2026-07-25
**Status:** findings + proposal, awaiting peer review. No code changed.
**Prompted by:** "would upgrading dovi_tool or ffmpeg help?"

## Summary

The 4K metadata inventory's 30-minute `_EXTRACT_TIMEOUT` failures are **not**
caused by slow disks, an old `dovi_tool`, an old `ffmpeg`, or a lack of GPU
acceleration. They are caused by `dovi_tool` reading across a Docker Desktop
for Windows bind mount, which charges a **flat ~136 µs per read syscall
regardless of block size**. `dovi_tool` reads at roughly byte granularity, so
it pays that toll tens of millions of times.

Answer to the question asked: **upgrading either tool would change nothing.**
Installed `dovi_tool` 2.3.2 (2.3.3 exists, a patch); `hdr10plus_tool` 1.7.2 is
already latest; `ffmpeg` 5.1.9. Neither tool is the constraint.

## Evidence

All measurements taken inside the running `scanhound` container, 2026-07-25.

### 1. Same binary, same content, two locations

A real 667 MB Dolby Vision clip (`ffmpeg -t 120 -c copy` cut from Alien
Romulus), parsed with `dovi_tool extract-rpu`:

| Source | Elapsed |
|---|---|
| container-local disk (`/tmp`, overlay) | **1 second** |
| bind mount (`A:/` → `/library/plex-source/a-4k-gambino`) | **1 MB per 100 s** |

Sampling `/proc/<pid>/io` and `/proc/<pid>/stat` for the mounted run:

```
   t      rchar  read_bytes    majflt  cpu_s    rpu_out
  10         0M          0M         0    0.7      0.00M
  50         1M          0M         0    3.0      0.00M
 100         1M          0M         0    6.2      0.00M
```

Zero block-device reads and zero major faults — it is not blocked on disk
hardware. It is making an enormous number of tiny, high-latency calls.

### 2. The mount's cost is per-syscall, not per-byte

Reading the same file at varying block sizes:

| block | mounted MB/s | mounted µs/read | local MB/s | local µs/read |
|---|---|---|---|---|
| 1 B | 0.0070 | 136.1 | — | — |
| 16 B | 0.1127 | 135.4 | — | — |
| 512 B | 3.5330 | 138.2 | — | — |
| 4 KB | 31.9 | 122.5 | 3435.1 | 1.1 |
| 64 KB | 179.1 | 348.9 | 6965.1 | 9.0 |
| 1 MB | 257.6 | 3882.0 | 7284.1 | 137.3 |
| 8 MB | 226.2 | 35366.8 | 4144.6 | 1930.2 |

Latency per read is **flat at ~136 µs** from 1 byte to 4 KB. Throughput is
therefore purely a function of how much a tool asks for per call.
`dovi_tool`'s observed 0.01 MB/s matches the 1-byte row (0.0070 MB/s).
`ffmpeg` reads in large buffered blocks and is unaffected — it demuxes the
same mounted file at 149–233 MB/s.

Mount definitions are Windows drive letters in `docker-compose.yml`
(`A:/:/library/plex-source/a-4k-gambino:ro` and siblings), served over Docker
Desktop's gRPC-FUSE layer.

### 3. Methodological correction

An earlier version of this benchmark piped `head -c N | ffmpeg -i -`, which
forces ffmpeg into non-seekable stdin mode and understated it by ~4x (47 MB/s
vs 233 MB/s). It also did not measure what production runs at all:
`backend/rename/dv_detect.py` invokes `dovi_tool extract-rpu "<file>"`
directly, letting `dovi_tool` demux the Matroska container itself. All numbers
above are from the corrected, file-fed form.

## Proposed fix (validated end-to-end)

Copy the file to container-local disk in large blocks, then run `dovi_tool`
**unchanged**:

```sh
dd if="$SRC" of=/tmp/local.mkv bs=8M
dovi_tool extract-rpu /tmp/local.mkv -o rpu.bin
```

Measured on Alien Romulus (50 GiB, the file that previously hit the 1800 s
timeout and recorded `scan_state=failed` / `dv_incomplete`):

```
copy:       463 s  (116 MB/s)
dovi_tool:   76 s  (710 MB/s, local)
TOTAL:      539 s  (9 min)
verdict:    Profile 7 (FEL), 171,235 frames
```

30-minute timeout and failure → 9 minutes and a correct FEL verdict. Requires
scratch space equal to the file size; the overlay has 864 GB free. The copy is
now the bottleneck, at the mount's large-block ceiling.

### Rejected alternative: the ffmpeg pipe

`ffmpeg -c:v copy -f hevc - | dovi_tool extract-rpu -` produced a
**byte-identical RPU** (same MD5, 651,178 bytes, 2881 frames, both
`Profile 7 (FEL)`) on the Alien Romulus clip, and would avoid the copy
entirely.

**Not recommended on this evidence.** That clip has a *single* video track
with BL+EL interleaved, so `-c:v copy` necessarily captured everything.
`dv_detect.py`'s comment warns the pipe "can drop EL NALs and misreport a FEL
as MEL" — the hazard is the **dual-track** P7 case, where the enhancement
layer is a separate video stream that default stream selection would discard.
No dual-track sample was available to test, so the warning is neither
confirmed nor refuted. The `dd` approach keeps the demuxer byte-for-byte
identical to today's behaviour and carries no such risk.

## Scope correction

A prior estimate in this workstream put a full-library inventory at ~10 days.
That figure was an artifact of this defect, not a real cost. It should not be
used for planning.

Separately, the cheap-probe result stands and is unaffected: `ffprobe` returns
the DOVI configuration record (`dv_profile`, `el_present_flag`,
`bl_present_flag`, `rpu_present_flag`, `dv_bl_signal_compatibility_id`) in
0.1–0.2 s and HDR10+ frame side-data
(`HDR Dynamic Metadata SMPTE2094-40`) in 0.2–0.6 s — *even over the bad
mount*, because it reads in large blocks. Verified against the slow path's
answers: `present` for Bring Her Back, `absent` for Casper.

`ffprobe` cannot distinguish FEL from MEL: a known-FEL file's config record
(`prof=7 el=1 bl=1 rpu=1 compat=6`) is identical to another P7's. That
distinction lives in the enhancement-layer content and needs the RPU.

## Unrelated bug found while investigating

`/library/plex-source/nas-4k-hdr-geronimo` and `.../nas-4k-magellan` are
**empty inside the container** — the host paths do not resolve, so Docker
created bare directories. Every file beneath them returns `ffprobe rc=1` and
is recorded as `scan_state=failed` with a `dv_incomplete` reason,
indistinguishable from a real timeout. This is a compose/mount defect and is
independent of everything above. Suggest a distinct error code for
unreadable-path so the two failure modes stop being conflated.

## Open questions for review

1. Is `dd bs=8M` + local parse the right shape, or should the inventory keep a
   bounded local scratch pool and stream-and-delete per title?
2. Is the first ~12 frames a *reliable* HDR10+ signal, or can HDR10+ metadata
   begin later in a stream and be missed by the cheap probe?
3. Is there a cheap field that splits FEL from MEL that this missed?
4. Should the inventory re-derive FEL/MEL at all, given `dv_scan` already
   holds it for 458 titles from the host-side detector and already drives the
   Plex labels and Kometa overlays?
