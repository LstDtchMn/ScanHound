# HDR10+ detection + in-container DV detection — design review request (rev 2)

**Date:** 2026-08-09
**Author:** Claude
**Reviewer:** ChatGPT (adversarial design read, pre-implementation)
**Status:** No code written yet. Measurement report + design proposal.
**Rev 1 verdict:** REQUEST CHANGES. All ten findings accepted; none disputed.

> **REVIEWER: read this file from the repository, not any chat summary of it.**
> If you cannot read it directly via the GitHub connector, **stop and say so**
> rather than reviewing a paraphrase.

---

## 0. What changed in rev 2, and what I got wrong

Every rev-1 finding is accepted. I checked the two arithmetically verifiable ones
independently and the reviewer was right on both.

| # | Finding | Status |
|---|---|---|
| 1 | `HDR10+DV` bucket name is false — its predicate never tests 2094-40 | **Fixed** (§3, independent booleans) |
| 2 | No label-sync trigger fires when only HDR data changes | **Fixed** (§6.4, the most valuable finding) |
| 3 | Library total is 5,708, not 6,308 | **Confirmed and corrected** (§3) |
| 4 | Stride-over-sorted is not a probability sample | **Accepted** (§3, §7 Q1) |
| 5 | "5 frames suffice" is an unvalidated detector contract | **Accepted**, now a blocking pre-req (§6.2) |
| 6 | `unreadable` conflates probe failure with missing metadata | **Fixed, and it falsified a claim I nearly shipped** (§4) |
| 7 | Separate table is safer but does not solve identity | **Accepted** (§6.3) |
| 8 | "HDR10+ rising in new releases" is not supportable | **Removed from the rationale** (§3) |
| 9 | DV cross-check safe only if strictly side-effect free | **Accepted** (§6.5) |
| 10 | Label *removal* semantics undefined | **Fixed** (§6.6) |

**Finding 3, verified.** `34+446+2216+323+583+490+352+432+384+448 = 5,708`. My 6,308
was arithmetic error, off by exactly 600. Recomputing the reviewer's weighted estimate
from the listed strata independently gives **404.8 expected positives / 5,708 = 7.1%** —
matching their figure exactly. **The 10.6% is withdrawn.**

**Finding 6 was worse than the reviewer knew, and it matters.** Acting on rev 1's model I
ran a diagnostic over the same cohort, and it reported four files — including
`Avatar (2009).mkv` — as `VERDICT: LIKELY CORRUPT`. That was **false**. Probing Avatar
directly:

```
codec_name=hevc  width=3840  height=2160  pix_fmt=yuv420p10le
color_space=bt2020nc  color_transfer=smpte2084
ffprobe exit code: 0
```

A healthy 4K HDR10 file, read in 0.2 s. The "corrupt" verdicts came from my own
harness treating an absent/empty `color_transfer` as a failed probe, exactly as the
reviewer said. **All corruption verdicts are retracted; there is no evidence of any
corrupt file.** This is the strongest argument for the reviewer's required result model:
the flawed model did not merely mis-tally — it manufactured an alarming false claim about
the user's library that was one step from being reported to him.

---

## 1. Why this exists

1. **DV detection had no scheduled task.** A manual script, last run 2026-07-25;
   Plex DV labels and the Kometa badges keyed on them were 14 days stale. Nothing was
   broken — all roots reachable, `dovi_tool.exe` present, config valid. Addressed by
   `scripts/run-dv-scan.ps1` + `scripts/install-dv-scan-task.ps1` in this branch.
2. **HDR10+ does not exist as a feature.** It appears only as text matched in *release
   names* (`hdencode_candidate_service.py`, `matching.py`, `rename/conflicts.py`).
   Nothing inspects a file. No table, no column, no label, no badge.

## 2. A methodology error I made, stated up front

**I first recommended NOT building HDR10+, from a sample that found 0 of 20.** That
sample took `sorted(rglob("*.mkv"))[:5]` from **4** of **10** libraries — the first five
titles alphabetically. Jesse declined the recommendation and asked for a wider sample;
the corrected sweep found HDR10+ in **8 of 10 libraries**.

An earlier attempt was worse: `for f in $(find ...)` word-split filenames containing
spaces, every ffprobe call failed, and all 101 fragments fell to the `else` branch and
were reported as SDR. **The tell was the count** — I asked for ≤20 and got 101. Both
results were discarded.

Rev 1 added a third instance of the same class (§0, finding 6). Three of my four
measurement errors this session were *classification* bugs that produced confident wrong
answers rather than visible failures.

---

## 3. Measurement

**Method.** `ffprobe` 5.1.9 (already in the `scanhound` container), two calls per file:

```
ffprobe -v error -select_streams v:0 -show_entries stream=color_transfer -of json <file>
ffprobe -v error -select_streams v:0 -read_intervals "%+#5" -show_frames \
        -show_entries frame=side_data_list -of json <file>
```

**Sample:** 10 libraries x 16 files by even stride = **160 files**. The harness printed
`sampled 160 (expected <=160)` — the count check that caught the word-splitting bug.

**Rev-2 classification: independent booleans, no exclusive technology buckets.** Rev 1
used mutually exclusive buckets with HDR10+ winning, which made `HDR10+DV` mean "PQ + DV
with *no* HDR10+ hit" — the opposite of its name.

| Observation | Definition |
|---|---|
| `hdr10plus_present` | any sampled frame side_data_type contains `2094-40` |
| `dv_present` | any sampled frame side_data_type contains `Dolby Vision` |
| `pq_transfer` | `color_transfer == smpte2084` |
| `probe_status` | `success` \| `timeout` \| `nonzero_exit` \| `parse_error` |
| `color_transfer` | optional string; **absent is not an error** |

**Results as measured** (rev-1 buckets, retained verbatim for auditability — do not read
`HDR10+DV` as "has HDR10+"):

| Rev-1 bucket | Count | What it actually means |
|---|---|---|
| `HDR10+` | 17 | 2094-40 present (may ALSO have DV) |
| `HDR10+DV` | 78 | PQ + DV, **no** 2094-40 in 5 sampled frames |
| `HDR10` | 30 | PQ, no DV, no 2094-40 |
| `SDR/other` | 26 | not PQ |
| `unreadable` | 9 | **misnomer** — `color_transfer` empty; ≥1 proven healthy |

Per library (N = library total; 16 sampled each):

```
movies-4k            (34)   HDR10+ 3, [PQ+DV] 7, HDR10 2, SDR 4
a-4k-gambino         (446)  [PQ+DV] 14, SDR 2
e-4k-hdr-columbo     (2216) empty-ct 5, SDR 7, [PQ+DV] 2, HDR10 2
i-4k-hdr-arnold      (323)  HDR10+ 1, [PQ+DV] 11, HDR10 4
j-4k-jefferson-*-bu  (583)  HDR10+ 3, HDR10 9, SDR 3, empty-ct 1
nas-4k-hdr-geronimo  (490)  HDR10+ 3, [PQ+DV] 9, SDR 3, HDR10 1
nas-4k-magellan      (352)  HDR10+ 2, [PQ+DV] 10, SDR 4
q-4k-quantum         (432)  HDR10+ 3, HDR10 9, [PQ+DV] 1, SDR 1, empty-ct 2
r-4k-rickover        (384)  HDR10+ 1, [PQ+DV] 10, HDR10 2, SDR 2, empty-ct 1
u-4k-ulysses-*-bu    (448)  HDR10+ 1, [PQ+DV] 14, HDR10 1
                     -----
                     5,708 files listed
```

### 3.1 No prevalence percentage is claimed

Adopting the reviewer's wording verbatim:

> The corrected sample found 17 HDR10+ files across 8/10 libraries. The sample was
> designed for library coverage, not a file-weighted prevalence estimate, so no
> library-wide percentage is claimed yet.

**Withdrawn:** the 10.6% figure; the "~500-600 movies would get a badge" extrapolation;
and the claim that HDR10+ is rising in new releases (`movies-4k` 3/16, n=16, one library,
era confounded with library membership — a hypothesis, not evidence).

**7.1%** is recorded only as the arithmetic consequence of the listed strata, and is
descriptive, not inferential: within-library selection was deterministic, not random.

**Estimand still undefined, and it changes the answer.** Two `-bu` libraries (583 + 448)
are backups that likely duplicate other libraries. *Unique-title* prevalence should
exclude them; *scanner-workload* prevalence should not. §7 Q2 asks which.

---

## 4. The 9 "unreadable" files — retraction

Rev 1 reported 9 unreadable (5.6%) and hypothesised bind-mount latency.

**Both the count's meaning and my follow-up diagnosis were wrong.** `color_transfer`
empty does not mean the probe failed; and re-probing `Avatar (2009).mkv` alone succeeds in
0.2 s with `color_transfer=smpte2084` and exit 0 (§0). So at least one "unreadable" file
is a healthy PQ file that the sweep transiently failed to read a field from — under
sequential load, not because of the file.

**Current honest position:** there is **no evidence of corruption**; the 9 are
unexplained *measurement* outcomes, cause unknown. They are not proven unreadable, not
proven slow, and not proven damaged. Resolving them requires the reviewer's result model
first — separate `probe_status` from missing metadata, retain stderr — then re-running.

---

## 5. Existing pipeline

```
host detector (dovi_tool, Windows, needs Y:)
  -> data/dv_host.db -> POST /rename/dv-import      (api/routes/rename.py:691)
  -> dv_scan rows, source='scan'
  -> label sync (app_service.py:680) -> DV FEL/MEL/P8/P5 LABELS INTO PLEX
  -> Kometa reads Plex labels -> badges
```

No Kometa file handoff. The sync fires only when `MAX(last_seen_at)` rises and is
`additive_only`. Consequence: **no scan -> no label sync**, which is why 14 days of
staleness raised no error.

`dv_scan` has two non-interchangeable producers:

| source | rows | layers | drives labels? |
|---|---|---|---|
| `scan` | 466 | fel 172, mel 160, p8 85, p5 34, none 13, unknown 2 | **yes** |
| `seed` | 3729 | unknown 2286, fel 862, mel 581 | **no** (filtered to `source="scan"`) |

I initially cited "55% unknown" as a detection bug; wrong — the unknowns are the seed's,
and the seed was a filename parse never meant to carry layers.

**Coverage is unmeasured, not zero.** `seed` uses `\\TURTLELANDSRV2\...` and
`C:\4K Drives\...`; `scan` uses `Y:/...`, `//TURTLELANDSRV2/...` **and**
`/library/plex-source/...`. `INTERSECT` on `path` returns 0 **vacuously**.

**Also verified in this branch (`tests/test_dv_labeler.py`, 34 passing):** DV labels
rebuild from `dv_scan` after a total Plex-database loss, because reconciliation matches on
normalized path, not `ratingKey`. Proven discriminating — mutating `build_index` to key on
`rating_key` fails 3 of the 4 new tests; the 4th is the negative control and correctly
still passes.

---

## 6. Proposal (rev 2)

### 6.1 Result model — probe outcome separate from findings

Store `probe_status` (`success`/`timeout`/`nonzero_exit`/`parse_error`), optional
`color_transfer`, and `hdr10plus_present`/`dv_present` as **tri-state**
(true/false/unknown), plus compact stderr on failure. **A failed probe must never persist
as `hdr10plus_present=false`.** §0 is the argument: the conflated model produced a false
corruption claim.

### 6.2 Detector sensitivity — BLOCKING pre-requisite

"5 frames suffice" is withdrawn as a contract. Before any production threshold, measure
false negatives on known positives: first-5 vs first-30/60 vs windows at ~25/50/75%.
Likely production strategy is multi-window with early exit on first positive. Governing
rule: **absence in N sampled frames != proven HDR10+-negative.**

The same caveat applies to `dv_present` (78 observations), weaker only because DV RPU data
is expected throughout.

### 6.3 Logical file identity — BLOCKING pre-requisite

A separate table avoids contaminating the DV contract but **does not solve the join**. Key
on `(logical_root_id, normalized_relative_path)`, never raw absolute path, with
`raw_path`/`normalized_path`/`sig_size`/`sig_mtime` as evidence. `Y:\...`,
`\\TURTLELANDSRV2\...` and `/library/plex-source/...` must map to the same logical root
before any comparison. Table named `video_metadata_scan` (broader than HDR10+).

### 6.4 Label-sync trigger — the rev-1 finding that would have shipped a dead feature

`app_service.py:680` watches only `get_latest_dv_scan_at(source="scan")`. As proposed in
rev 1, HDR10+ would be detected, written, and **never reach Plex** until an unrelated DV
scan advanced the DV watermark. Rev 2 adopts a **unified managed-label watermark**: any
producer of a managed label advances one generation marker, and reconciliation triggers on
that. `max(latest_dv_scan, latest_hdr_scan)` is the minimum acceptable form. Chosen over
per-source watermarks because more file-derived labels are likely later, and over
"scanner schedules sync directly" because that couples producer to consumer.

### 6.5 DV cross-check — read-only, mechanically side-effect free

Record `container_dv_present`, `host_dv_present`, `identity_match_status`,
`comparison_status` (`agreement_positive`/`agreement_negative`/`container_only`/
`host_only`/`unmatched_container_path`/`unmatched_host_path`/`probe_error`). Report a 2x2
table over *matched* files plus unmatched counts on both sides and probe errors.
**Unmatched paths are not disagreements.** Must not mutate `dv_scan`, drive labels, or
affect host scheduling.

### 6.6 Label removal semantics

| Observation | Action |
|---|---|
| matched, `probe_status=success`, `hdr10plus_present=true` | add `HDR10+` |
| matched, `probe_status=success`, `hdr10plus_present=false` | remove stale managed `HDR10+` |
| no matching observation | **preserve** (additive_only) |
| `probe_status != success`, or `unknown` | **preserve** — never an explicit negative |

`MANAGED` grows to include `HDR10+` and stays a **closed set** — the prefix-wildcard bug
that once deleted user labels like `DV Cut` must not be reintroduced.

**Out of scope:** replacing the host detector; FEL/MEL from ffprobe (impossible);
`dv_file_tagging` stays false.

### 6.7 Implementation order (reviewer's, adopted)

1. Corrected measurement report (**this document**)
2. Validate detector sensitivity (§6.2)
3. Define logical file identity (§6.3)
4. Create `video_metadata_scan` with the §6.1 result model
5. HDR label reconciliation with §6.6 semantics
6. Unified label-sync watermark (§6.4)
7. DV cross-check, read-only (§6.5)
8. Only then discuss any change in DV authority

---

## 7. Round-2 questions

1. **Is the unified managed-label watermark (§6.4) the right shape**, or does one
   generation marker across all label producers create its own failure — a slow producer
   forcing full-library reconciliation, or one producer's advance masking another's
   staleness? Would per-source watermarks with `max()` be safer despite scaling worse?
2. **Which estimand?** Unique-title prevalence (exclude the two `-bu` libraries, 1,031
   files) or scanner-workload prevalence (include them)? This changes both the number and
   whether a census is even the right frame.
3. **§6.6 row 2 is the only destructive path in the design.** Is "matched + successful
   probe + explicit false -> remove" safe given §6.2 is still unvalidated? A detector
   with unmeasured false-negative behaviour producing an explicit `false` is exactly what
   would strip a correct badge. Should removal be gated on multi-window agreement, or
   deferred entirely until sensitivity is measured?
4. **Three of my four measurement errors were classification bugs** that produced
   confident wrong answers instead of visible failures (§2, §0). Beyond the §6.1 result
   model, is there a structural check that would have caught them — the count assertion
   caught one, and a positive control caught nothing here because I ran none?
5. **Does §6.5's `comparison_status` vocabulary leak authority?** `agreement_negative`
   asserts both detectors agree a file has no DV — but under §6.2 the container side
   cannot prove absence. Should that value be renamed or removed?
6. **Is anything in rev 1 that I marked "accepted" actually still wrong in rev 2?** I
   changed the classification model, the trigger design, the identity design and the
   removal semantics in one pass without implementing any of it. Please check whether the
   fixes are consistent with each other rather than only with the findings.
