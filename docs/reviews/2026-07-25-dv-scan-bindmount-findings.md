# 4K metadata scan timeouts: unbuffered Matroska reads over a high-latency 9p mount

**Date:** 2026-07-25 (revised same day after peer review)
**Status:** findings + proposals. No product code, tests, config, Dockerfile, or
Kometa YAML changed by this document.
**Prompted by:** "would upgrading dovi_tool or ffmpeg help?"

## Summary

The 4K metadata inventory's 30-minute `_EXTRACT_TIMEOUT` failures on Dolby
Vision titles are caused by **the interaction of two things**, not by either
alone:

1. `dovi_tool`'s Matroska input path is **unbuffered** — verified in source, see
   §2 — so it issues an enormous number of very small reads; and
2. the Docker Desktop host-file-sharing mount carries a **~125–140 µs fixed
   cost per read** for small requests.

The mount supplies the high fixed cost. The parser's I/O pattern multiplies it
into a production failure. Neither is sufficient on its own: the same binary
parses the same content from local disk at over 1 GB/s, and `ffmpeg` reads the
same mounted file at 149–233 MB/s.

Answer to the question asked: **neither available upgrade addresses the
deployed I/O path.** Installed `dovi_tool` 2.3.2 (2.3.3 exists), `hdr10plus_tool`
1.7.2 (already latest), `ffmpeg` 5.1.9. `dovi_tool` 2.3.3 still routes Matroska
input through the same unbuffered path (§2), and ffmpeg is not part of the
deployed `dovi_tool extract-rpu <mkv>` invocation at all. This is not a claim
that no future version could fix it — a one-line upstream buffering change
plausibly would (§6).

GPU and Ollama are unrelated: no pixel decoding or model inference occurs in
this bottleneck.

## 1. The mount, identified

Earlier drafts of this document asserted "gRPC-FUSE" without verifying it. The
actual backend, from `/proc/self/mountinfo` inside the container:

```
/library/plex-source/a-4k-gambino ro,noatime - 9p A:\134
  rw,aname=drvfs;path=A:\;...;cache=5,access=client,msize=65536,trans=fd
```

It is **9p (WSL2 `drvfs`) with `msize=65536`** — a 64 KiB maximum message size.
`A:` is a **local NTFS volume** (`Get-PSDrive` shows an empty `DisplayRoot`, so
not a mapped network drive; `Get-Volume` reports NTFS). The pathology is
therefore not network storage — it is the host-sharing transport.

`msize=65536` is directly load-bearing for §3: a read larger than 64 KiB is
necessarily split into multiple 9p round trips.

Note for contrast, the `nas-*` mounts are a *different* mechanism entirely —
`ext4` on `/dev/sdd`, i.e. paths inside the Docker VM's own disk (see §11).

## 2. Source-level mechanism (verified)

`dovi_tool` delegates HEVC/MKV input to the `hevc_parser` crate
(`Cargo.toml`: `hevc_parser = { version = "0.6.11", features = ["hevc_io"] }`),
which in turn uses `matroska-demuxer = "0.7.0"`.

In `hevc_parser/src/io/processor.rs` (verified against `main`, crate 0.6.11):

```rust
 99:  let file = File::open(input)?;
102:      IoFormat::Matroska => self.process_matroska_file(processor, file),
104:      _ => { let mut reader = Box::new(BufReader::with_capacity(100_000, file)); ... }
221:  fn process_matroska_file(&mut self, processor: &mut dyn IoProcessor, file: File) -> Result<()> {
222:      let mut mkv = MatroskaFile::open(file)?;
```

The **raw HEVC path is buffered** (100 KB `BufReader`). The **Matroska path
hands a raw `File` straight to `MatroskaFile::open`**, with no buffering
between the EBML parser's many small logical reads and the filesystem.

The defensible claim is that this unbuffered Matroska path performs enough tiny
reads and seeks to create the observed pathology. This document does not claim
`dovi_tool` deliberately reads media payload one byte at a time.

This finding also explains something previously treated as coincidence: the
`ffmpeg -f hevc - | dovi_tool extract-rpu -` form was fast **because piping raw
HEVC routes dovi_tool into its buffered branch at line 104**, not because
ffmpeg is inherently faster.

## 3. Per-read cost: what the benchmark does and does not show

Reading the same mounted file at varying block sizes, against container-local
overlay for reference:

| block | mounted MB/s | mounted µs/read | local MB/s | local µs/read |
|---|---|---|---|---|
| 1 B | 0.0070 | 136.1 | — | — |
| 16 B | 0.1127 | 135.4 | — | — |
| 512 B | 3.5330 | 138.2 | — | — |
| 4 KB | 31.9 | 122.5 | 3435.1 | 1.1 |
| 64 KB | 179.1 | 348.9 | 6965.1 | 9.0 |
| 1 MB | 257.6 | 3882.0 | 7284.1 | 137.3 |
| 8 MB | 226.2 | 35366.8 | 4144.6 | 1930.2 |

**Corrected claim.** An earlier draft said latency is "flat regardless of block
size". The table contradicts that above 4 KiB and the wording was wrong. The
accurate statement:

> A roughly 125–140 µs fixed per-read cost dominates requests through at least
> 4 KiB. Larger reads amortize that cost and approach the mount's sequential
> ceiling of roughly 226–258 MB/s.

The rise above 4 KiB is consistent with `msize=65536`: a request larger than
64 KiB becomes `ceil(size / 64 KiB)` round trips. Checking that model against
the data — 1 MiB / 64 KiB = 16 chunks at 3882 µs → 243 µs per chunk;
8 MiB / 64 KiB = 128 chunks at 35367 µs → 276 µs per chunk; a single 64 KiB
read costs 349 µs. Per-chunk cost is roughly constant, which is what the
transport's message ceiling predicts.

The operative conclusion is unchanged: **catastrophic with tiny reads,
acceptable with large buffered reads.**

## 4. Direct syscall evidence

Peer review correctly noted that inferring read granularity from throughput
alone is weak. `/proc/<pid>/io` exposes `syscr`, so `Δrchar / Δsyscr` gives mean
bytes per read directly, with no tracing overhead to distort the result.

`dovi_tool extract-rpu` sampled for 45 s on the mounted 50 GiB P7 title:

```
read syscalls   : 403,254
bytes via read(): 0.54 MB
MEAN READ SIZE  : 1.4 bytes
wall per syscall: 111.6 us
effective rate  : 0.0121 MB/s
```

Same binary on a 2 GiB local clip of the same title:

```
read syscalls   : 1,837,472
bytes via read(): 1,721.32 MB
MEAN READ SIZE  : 982.3 bytes
wall per syscall: 0.82 us
effective rate  : 1,147.3 MB/s
```

**Mean read size is not the same across the two filesystems** (1.4 B vs 982 B),
and this must not be overstated. The most likely explanation is that the two
samples capture different phases: the mounted run never escapes the early
EBML/seek-head parsing phase, where element IDs and sizes are decoded a byte or
two at a time, while the local run completes and its mean is dominated by bulk
cluster reads. The mounted sample is nonetheless the operative one, because
that early phase is where the production run spends its entire 1800 s budget.

Either figure is fatal over this transport: at 111.6 µs per read, even the
local run's 982-byte mean would yield ~8.8 MB/s, i.e. ~1.6 hours for a 50 GiB
title — still far beyond the timeout.

### Interpretation caveats

- `rchar` counts bytes through read-like syscalls regardless of whether they
  came from cache or storage.
- `read_bytes` is block-I/O accounting and does not necessarily represent work
  performed through 9p/FUSE-like layers; `read_bytes = 0` is therefore **not**
  proof that no host-side I/O occurred.
- Zero major faults does not mean the process was never blocked in ordinary
  `read()` calls.

These observations support the diagnosis; the `syscr`-derived mean read size is
what makes it direct rather than inferential.

### Remaining evidence worth collecting

A short `strace -c` or `perf trace` sample (not over the full 50 GiB run, where
tracing overhead would distort timings) to capture the read/pread size
histogram and `lseek` frequency. Randomized trial ordering and repeated
warm-cache trials would further isolate page-cache and readahead confounds,
which can inflate apparent local throughput but cannot rescue a workload whose
every tiny logical read crosses a high-latency boundary.

## 5. Fix A — bounded extraction (fastest to adopt, needs validation)

`dovi_tool extract-rpu` accepts `-l, --limit <limit>  Stop processing input
after N frames`. Measured:

| target | limit | elapsed | verdict |
|---|---|---|---|
| local 2 GiB clip | 1 | **4 ms** | `Profile: 7 (FEL)` |
| local 2 GiB clip | 12 | 5 ms | `Profile: 7 (FEL)` |
| local 2 GiB clip | 128 | 20 ms | `Profile: 7 (FEL)` |
| local 2 GiB clip | none | 1149 ms | `Profile: 7 (FEL)` |
| **mounted 50 GiB title** | **1** | **106 s** | **`Profile: 7 (FEL)`** |
| mounted 50 GiB title | 12 | 105 s | `Profile: 7 (FEL)` |

Bounded extraction turns a never-completing operation into 106 s **with no copy
and no scratch space**. `limit=1` and `limit=12` cost the same, so the residual
106 s is fixed setup (header/seek-head traversal over 9p), not per-frame work.

**Not yet a production rule.** This is one file, and it is a FEL. Before this
can authorize a FEL/MEL verdict it must be validated against the existing
trusted corpus — the 458 host-detector results in `dv_scan`, across FEL, MEL,
P8 and P5, including any titles with historical corrections or known
disagreement. Required comparison per title: `--limit 1 / 12 / 32 / 128` versus
the trusted `dv_scan` layer.

Candidate policy, **only after** that corpus comparison:

- one consistent usable early RPU classification → accept;
- no usable RPU in the sampled frames → `unknown`, do not guess;
- mixed early subtypes → escalate to full scan;
- dual-track input → special handling (§9);
- disagreement with the trusted host result → audit queue, do not overwrite;
- uncertain result → never overwrite trusted evidence.

## 6. Fix B — buffer the Matroska input (best, unproven)

Given §2, the highest-value engineering fix is to interpose buffering before
the EBML parser's small logical reads reach the mount. `MatroskaFile::open`
requires `Read + Seek`, and `BufReader<File>` satisfies both, so this is
plausibly a one-line change at `processor.rs:222` — the same treatment line 104
already gives the raw HEVC path.

Record as **unproven but high priority**. If it works it eliminates the copy,
the scratch requirement, the staging cleanup, and the write amplification of
Fix C simultaneously.

Required benchmark matrix before adopting:

- 667 MB clip and full 50 GiB title;
- local overlay and 9p mount;
- current unbuffered binary and buffered experimental binary;
- elapsed time; `syscr` count and mean read size; output RPU hash; final
  FEL/MEL verdict.

The RPU hash must be identical to the unbuffered build's. Adopting this means
building or vendoring a patched `hevc_parser`, which is a larger supply-chain
commitment than Fix A or C and should be weighed as such.

## 7. Fix C — local scratch copy (validated fallback)

Copy to container-local disk in large blocks, then run `dovi_tool` unchanged:

```
copy (dd bs=8M):  463 s  (116 MB/s)
dovi_tool:         76 s  (710 MB/s, local)
TOTAL:            539 s
verdict:          Profile 7 (FEL), 171,235 frames
```

Versus the previous 1800 s timeout and `scan_state=failed`. The overlay has
864 GB free.

`dd ... /tmp/local.mkv` is **not** a production implementation. A real one
requires: a scan-owned scratch directory with a unique destination filename per
item; a free-space preflight of source size plus margin; one staged DV parse at
a time (current scan concurrency is one, so no scratch pool is needed for v1 —
stage, parse, delete, repeat); a large buffered, cancellation-aware copy;
deletion in a `finally`; startup cleanup of abandoned ScanHound-owned scratch
files only, never unrelated `/tmp` content; a source signature check before and
after staging; preserved read-only source behaviour; and distinct error codes
for insufficient scratch space, copy failure, source unreadable, parse timeout,
parse failure, and cancellation.

Ranking: Fix B is best if it works; Fix A is the cheapest immediate improvement
and needs corpus validation; Fix C is the validated fallback that changes no
demuxing behaviour.

## 8. What the FFmpeg pipe does and does not prove

`ffmpeg -c:v copy -f hevc - | dovi_tool extract-rpu -` produced a
**byte-identical RPU** (same MD5, 651,178 bytes, 2881 frames, both
`Profile: 7 (FEL)`) on the Alien Romulus clip.

That result is real but narrow. The clip has a **single** video track with BL
and EL interleaved, so `-c:v copy` necessarily captured everything. It says
nothing about the dual-track case, and per §2 its speed is explained by the
buffered code path rather than by ffmpeg's demuxer being better.

`dv_detect.py`'s warning that the pipe "can drop EL NALs and misreport a FEL as
MEL" is **not obsolete**.

## 9. Dual-track Profile 7 is unproven in both directions

An earlier draft implied that direct `dovi_tool extract-rpu file.mkv` is safe
for dual-track P7 and only the pipe is risky. That is not established. The
Matroska path selects a video track and processes frames for that track, so
**both** of these require explicit validation on dual-track content:

- direct `dovi_tool extract-rpu file.mkv`;
- `ffmpeg -map ... -c copy -f hevc - | dovi_tool extract-rpu -`.

The correct claim about Fix C is narrow:

> Scratch copying introduces no new demuxing behaviour relative to the existing
> direct-file path.

It does **not** prove dual-track correctness, because the existing direct-file
path is itself unvalidated there.

A synthetic dual-track sample can be constructed — `dovi_tool demux` exists
("Demuxes single track dual layer Dolby Vision into Base layer and Enhancement
layer files"), so: start from the known single-track P7 FEL source, `demux` to
separate BL and EL elementary streams, remux them as two MKV video tracks,
record stream order and metadata, then compare direct MKV extraction, ffmpeg
default mapping, explicit BL mapping, explicit EL mapping, and any combined
reconstruction against the reference RPU hash and FEL/MEL summary.

Until that is done, production should detect multi-HEVC-track inputs cheaply
and classify them `unknown` or route them to a dedicated detector rather than
emitting a possibly-wrong FEL/MEL verdict.

## 10. Inventory should reuse `dv_scan`, and axes should fail independently

The inventory should not unconditionally re-derive FEL/MEL for every Dolby
Vision title when a signature-current `dv_scan` row already exists. `dv_scan`
already holds trusted results for 458 titles from the host-side detector, and
already drives the Plex labels and Kometa overlays.

Recommended shape:

1. persist cheap inventory evidence independently;
2. reuse `dv_scan` when path, mtime and size are current;
3. perform bounded or deep DV detection only when no trusted row exists, the
   row is stale, the source signature changed, evidence conflicts, or an audit
   was explicitly requested;
4. **do not let a DV timeout invalidate already-valid HDR10+, audio, video or
   subtitle evidence**;
5. preserve detector provenance and scan time.

The current flow treats failed DV extraction as failure of the whole inventory
item. That is an undesirable coupling between independent metadata axes, and
future implementation should separate base technical probe success, HDR10+
evidence state, DV-layer evidence state, and overall source readability. The
HDR10+ design document carries this forward.

## 11. Correction: the broken NAS mounts do *not* report `dv_incomplete`

An earlier draft claimed the empty `nas-4k-*` mounts surface as `dv_incomplete`
failures "indistinguishable from a real timeout", and recommended adding a
distinct unreadable-path error code. **Both claims were wrong**, and the
recommendation was for something already implemented. Actual production rows:

```
status   stage   error_code          n
failed   stat    filesystem_error    21
failed   dovi    dv_incomplete        2
pending  -       -                   55
current  -       -                    5
```

The deployed code already distinguishes the two cleanly: the 21 unreadable NAS
paths fail at `stage=stat` with `error_code=filesystem_error`, while only the
two Alien Romulus copies (the genuine P7 timeouts) carry `dv_incomplete`. The
55 `pending` rows belong to the earlier cancelled pilot
(`2b31cffe`, cancelled 2026-07-22), not to a failure.

The mount defect itself is real and unfixed. Per `/proc/self/mountinfo` the
`nas-*` entries are `ext4` on `/dev/sdd` — that is, `docker-compose.yml` maps
VM-internal paths (`/mnt/nas/nas-4k-hdr-geronimo:/library/plex-source/...:ro`)
that are not mounted inside the VM, so Docker created empty directories. This
is a compose/VM-mount issue, entirely separate from §1–§10, and it is correctly
reported today.

## Conclusions

1. The prior "whole-file I/O bandwidth" diagnosis was wrong.
2. The timeout is caused by unbuffered tiny Matroska reads interacting with
   high-latency Docker Desktop host file sharing (9p `drvfs`, `msize=65536`).
3. The benchmark supports a fixed per-read cost through roughly 4 KiB, not
   across all block sizes.
4. `/proc` block-I/O accounting and fault counts support but do not
   independently prove the mechanism; the `syscr`-derived mean read size does.
5. Further direct syscall evidence (size histogram, `lseek` frequency) is still
   worth collecting.
6. Neither `dovi_tool` 2.3.3 nor an ffmpeg upgrade corrects the deployed path.
7. GPU and Ollama are unrelated.
8. Local scratch copying is a validated fallback (1800 s timeout → 539 s).
9. Bounded `--limit` extraction is the cheapest immediate improvement
   (1800 s timeout → 106 s, no scratch) but is validated on exactly one file
   and must be checked against the 458-title truth set first.
10. Buffered Matroska input is the preferred engineering fix and is now
    source-justified, but remains unproven.
11. Dual-track Profile 7 remains unproven for both direct MKV and ffmpeg-pipe
    approaches.
12. No cheap container-level flag distinguishes FEL from MEL; `ffprobe`'s DOVI
    configuration record is identical across P7 subtypes.
13. Inventory should reuse current `dv_scan` evidence rather than re-derive
    every title.
14. Independent metadata axes should not fail as one unit.
