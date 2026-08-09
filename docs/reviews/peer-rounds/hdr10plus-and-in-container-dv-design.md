# HDR10+ detection + in-container DV detection — design review request

**Date:** 2026-08-09
**Author:** Claude
**Reviewer:** ChatGPT (adversarial design read, pre-implementation)
**Status:** No code written yet. This is a measurement report + design proposal.

> **REVIEWER: read this file from the repository, not any chat summary of it.**
> A previous round reviewed a summary instead of the document and produced findings
> about text that did not exist. If you cannot read this file directly via the
> GitHub connector, **stop and say so** rather than reviewing a paraphrase.

---

## 1. Why this exists

Jesse asked for the status of "MEL FEL DV HDR 10+, HDR+ scanning and relabeling with
Kometa". Investigation found:

1. **DV detection had no scheduled task at all.** It is a manual script, last run
   2026-07-25 16:45. Plex DV labels and the Kometa badges keyed on them were 14 days
   stale. Nothing was broken — all roots reachable, `dovi_tool.exe` present, config valid.
   Fixed by `scripts/run-dv-scan.ps1` + `scripts/install-dv-scan-task.ps1` (in this branch).
2. **HDR10+ does not exist as a feature.** `HDR10+` appears only as text matched in
   *release names* (`hdencode_candidate_service.py`, `matching.py`, `rename/conflicts.py`).
   Nothing inspects a file for it. No table, no column, no label, no badge.

This document proposes adding real HDR10+ detection, and reports a second finding that
changes how DV detection could work.

---

## 2. A methodology error I made, stated up front

**I first recommended NOT building HDR10+, based on a sample that found 0 of 20 files
carrying it. That recommendation was wrong and the sample was biased.**

The biased sample took `sorted(rglob("*.mkv"))[:5]` from **4** of the **10** mounted 4K
libraries — i.e. the first five titles alphabetically, clustering on titles beginning with
digits and "A". Jesse declined the recommendation and asked for a wider sample. The
corrected sweep (even stride through each library's full list, all 10 libraries, 16 files
each) found HDR10+ in **8 of 10 libraries**.

I am flagging this because it is the more reusable finding: **a "first N sorted" sample is
not a sample.** An earlier attempt in the same session was worse — `for f in $(find ...)`
word-split filenames containing spaces, every ffprobe call failed, and all 101 fragments
fell through to the `else` branch and were reported as SDR. The tell was the **count**: I
asked for ≤20 files and got 101. Both results were discarded.

**Reviewer: please treat every number in §3 as suspect until you have checked the method,
and check specifically whether the stride sample has its own bias I have not noticed.**

---

## 3. Measurement

**Method.** `ffprobe` 5.1.9 (already present in the `scanhound` container, `/usr/bin/ffprobe`),
run against bind-mounted libraries. Two calls per file:

```
ffprobe -v error -select_streams v:0 -show_entries stream=color_transfer -of json <file>
ffprobe -v error -select_streams v:0 -read_intervals "%+#5" -show_frames \
        -show_entries frame=side_data_list -of json <file>
```

HDR10+ is SMPTE ST 2094-40 **dynamic** metadata, carried per frame, so it requires reading
frames — but 5 frames suffice, since when present it appears on essentially every frame.
Classification, in this precedence order:

| Bucket | Test |
|---|---|
| `HDR10+` | any frame side_data_type contains `2094-40` |
| `HDR10+DV` | `color_transfer == smpte2084` AND side data contains `Dolby Vision` |
| `HDR10` | `color_transfer == smpte2084` |
| `SDR/other` | anything else |
| `unreadable` | `color_transfer` empty (ffprobe failed or timed out) |

Sample: 10 libraries x 16 files by even stride (`allf[::len(allf)//16][:16]`) = **160 files**.
The harness printed `sampled 160 (expected <=160)` — the count check that caught the earlier
word-splitting bug.

**Results.**

| Bucket | Count | Share |
|---|---|---|
| HDR10+ | 17 | 10.6% |
| HDR10+DV | 78 | 48.8% |
| HDR10 | 30 | 18.8% |
| SDR/other | 26 | 16.3% |
| unreadable | 9 | 5.6% |

Per library (file count = library total, sampled = 16 each):

```
movies-4k            (34)   HDR10+DV 7, HDR10+ 3, SDR 4, HDR10 2
a-4k-gambino         (446)  HDR10+DV 14, SDR 2
e-4k-hdr-columbo     (2216) unreadable 5, SDR 7, HDR10+DV 2, HDR10 2
i-4k-hdr-arnold      (323)  HDR10 4, HDR10+DV 11, HDR10+ 1
j-4k-jefferson-*-bu  (583)  HDR10 9, HDR10+ 3, SDR 3, unreadable 1
nas-4k-hdr-geronimo  (490)  HDR10+ 3, HDR10+DV 9, SDR 3, HDR10 1
nas-4k-magellan      (352)  SDR 4, HDR10+DV 10, HDR10+ 2
q-4k-quantum         (432)  HDR10 9, HDR10+ 3, HDR10+DV 1, SDR 1, unreadable 2
r-4k-rickover        (384)  HDR10+DV 10, HDR10+ 1, unreadable 1, SDR 2, HDR10 2
u-4k-ulysses-*-bu    (448)  HDR10+DV 14, HDR10+ 1, HDR10 1
```

**Known limits of these figures — do not launder them into precision they lack:**

- **Buckets are exclusive with HDR10+ winning.** Files with both HDR10+ and DV land in
  `HDR10+`, so the true DV-bearing count is **>= 78**, not 78. One hit is literally named
  `Shelter.2026.PROPER...DoVi.HDR10+.HEVC...`.
- **11% cannot be multiplied into a movie count.** The 6,308 files across these libraries
  include two `-bu` (backup) libraries that likely duplicate others, and
  `e-4k-hdr-columbo` (2216) is sampled at the same 16 as `movies-4k` (34), so the aggregate
  share is unweighted by library size. A weighted estimate would differ.
- **`movies-4k` had the highest HDR10+ rate (3/16 ≈ 19%) and is the newest content** — it
  is the ScanHound download destination (hits include *Shelter* 2026, *A Private Life* 2025).
  This suggests HDR10+ prevalence is rising in recent releases, but n=16 in one library is
  far too small to assert a trend. **Reviewer: is this worth measuring properly, or should
  it be dropped from the rationale?**
- **9 unreadable (5.6%)** are unexplained. I attributed them to bind-mount latency against
  a 120s timeout rather than corruption, but **I did not verify that**, and one earlier
  probe did hit a genuinely corrupt file in `$RECYCLE.BIN`.

---

## 4. The second finding: ffprobe sees Dolby Vision in-container

78 of 160 sampled files reported `Dolby Vision Metadata` / `Dolby Vision RPU Data` as frame
side data, from **inside the container**, reading `/library/plex-source/...` bind mounts.

This matters because today's DV pipeline has a structural fragility:

- The detector runs on the Windows **host** because `dovi_tool.exe` must reach `Y:`.
- **`Y:` is a mapped network drive** (`\\TURTLELANDSRV2\4K HDR Geronimo`, DriveType=4).
  Mappings are per-logon-session.
- Therefore the scheduled task must be `LogonType=Interactive` and `RunLevel=Limited`
  (elevating would arguably see different mappings), and **it only runs while Jesse is
  logged on**.
- A run without the mapping walks zero roots, detects nothing, POSTs an empty import and
  **exits 0** — a silent no-op indistinguishable from "nothing new". `run-dv-scan.ps1`
  turns that into a loud exit 11; both controls proved (unreachable root -> 11, detector
  never invoked; all reachable -> 0, detector invoked).

A container-side detector has none of that: bind mounts exist regardless of logon state.

**The limit: ffprobe reports THAT a file carries a DV RPU, not whether it is FEL or MEL.**
That distinction requires parsing the RPU, which is `dovi_tool`'s job, and FEL-vs-MEL is the
entire point of the existing labels. So this cannot replace the host detector.

**What I have NOT done, and will not claim:** I have not checked whether ffprobe's DV verdict
agrees with the 466 `source='scan'` rows. Until that is done, "ffprobe can detect DV" is a
capability observation, not a validated detector.

---

## 5. Existing pipeline (for context)

```
host detector (dovi_tool, Windows, needs Y:)
  -> data/dv_host.db
  -> POST /rename/dv-import          (backend/api/routes/rename.py:691)
  -> dv_scan rows, source='scan'
  -> label sync (app_service.py:680) -> writes DV FEL/MEL/P8/P5 LABELS INTO PLEX
  -> Kometa reads Plex labels -> draws badges
```

There is **no Kometa file handoff** — labels go into Plex and Kometa reads them there.
The label sync fires only when `MAX(last_seen_at)` rises (deliberate: a full sync walks every
movie library) and is `additive_only` (a transient mount failure must not wipe labels
library-wide). Consequence: **no scan -> no label sync**, which is why 14 days of staleness
produced no error anywhere.

`dv_scan` has two producers and they are not interchangeable:

| source | rows | layers | consumed by labels? |
|---|---|---|---|
| `scan` | 466 | fel 172, mel 160, profile8 85, profile5 34, none 13, unknown 2 | **yes** |
| `seed` | 3729 | unknown 2286, fel 862, mel 581 | **no** (sync filters `source="scan"`) |

`seed` is a one-time 2026-06-30 filename parse. I initially reported "55% unknown" as
evidence of a detection bug; that was wrong — the unknowns are the seed's, and the seed was
never meant to carry layers. Real detection is 99.6% conclusive.

**Coverage is unmeasured, not zero.** The two producers store different path forms — `seed`
uses `\\TURTLELANDSRV2\...` and `C:\4K Drives\...`; `scan` uses `Y:/...`,
`//TURTLELANDSRV2/...` **and** `/library/plex-source/...`. So `INTERSECT` on `path` returns 0
**vacuously**. Any coverage figure requires normalising first.

---

## 6. Proposal

Add a container-side scan that reads **both** HDR10+ and DV in the same ffprobe pass (the
same two calls already return both), and a new HDR10+ label.

1. **Storage.** New `hdr_scan` table rather than new columns on `dv_scan`. Rationale: `dv_scan`
   is keyed to the host detector's path forms and its `source` column already carries load-
   bearing meaning for the label sync. Mixing a second producer with different path forms
   into it invites exactly the vacuous-join problem in §5. **Reviewer: is a separate table
   right, or does it just move the join problem?**
2. **Detection.** `hdr10plus: bool`, `dv_present: bool`, `color_transfer: str`, plus the
   `sig_mtime`/`sig_size` change-detection pair `dv_scan` already uses, so rescans are cheap.
3. **Labels.** Extend `dv_label_vocab` with an `hdr10plus` entry (`"HDR10+"`). Keep the
   existing DV vocabulary untouched.
4. **Kometa.** New overlay keyed on the `HDR10+` Plex label. No ScanHound change beyond
   writing the label.
5. **DV cross-check, diagnostic only at first.** Report where ffprobe's `dv_present`
   disagrees with `dv_scan`. Do **not** let it write DV labels until it has been shown to
   agree — a container detector that silently disagrees with the host one would corrupt
   working badges.

**Explicitly out of scope:** replacing the host detector; FEL/MEL from ffprobe (impossible);
tagging files (`dv_file_tagging` stays false).

---

## 7. Questions for the reviewer

1. **Does the stride sample have a bias I missed?** It takes `allf[::step][:16]` over a
   `sorted()` list. Sorted order correlates with title, and title may correlate with era,
   and era correlates with HDR10+. Is stride-over-sorted materially better than first-N, or
   have I replaced one bias with a subtler one?
2. **Is 11% actionable, given it is unweighted by library size?** Weighting by the 6,308
   file total would shift it substantially, since the largest library (2216 files) has the
   lowest HDR10+ rate. Should the recommendation rest on a weighted figure instead?
3. **Separate `hdr_scan` table vs columns on `dv_scan`** — §6.1.
4. **Is the "HDR10+ is rising in new releases" claim supportable at n=16?** I lean no. It is
   currently the strongest part of the rationale and the weakest-evidenced.
5. **Should the DV cross-check ship at all** before there is evidence it agrees with the 466
   existing rows, even as diagnostics? Is diagnostic-only genuinely safe here, or does it
   create a second source of truth that will drift?
6. **The 9 unreadable files (5.6%)** — is "probably bind-mount latency" acceptable, or must
   that be resolved before any prevalence figure is trusted? If 5.6% of files cannot be read
   by the proposed detector, that is arguably the finding.
