# HDR10+ detection + in-container DV detection — design review request (rev 3)

**Date:** 2026-08-09
**Author:** Claude
**Reviewer:** ChatGPT (adversarial design read, pre-implementation)
**Status:** No feature code written. One *unrelated* production bug the review
surfaced has been fixed and pushed separately — see §0.1.
**Rev 1 verdict:** REQUEST CHANGES. All ten findings accepted; none disputed.
**Rev 2 verdict:** REQUEST CHANGES to the destructive path; non-destructive
foundation cleared. All five inconsistencies accepted; none disputed.

---

## 0.1 Rev 2's trigger finding was a live bug, and it is now fixed

Rev 2 warned against generalising the existing DV watermark behaviour because
`AppService` advances its observed marker *before* the sync succeeds. That was not
merely a design smell to avoid copying — **it was an active bug in running code**,
and confirming it turned up a second path the reviewer had not seen:

```python
self._last_dv_scan_at = latest        # advanced FIRST
...
if pm is None:
    logger.info("...Plex not initialized — skipping this pass")   # watermark already burned
else:
    result = dv_labeler.sync_labels(...)                          # or raises → caught outside
```

Path 1 (`pm is None`) is the one the reviewer had not identified, and it is the more
likely of the two: the maintenance pass can run before Plex is initialised after a
container start. In both paths the DV detection was correct, the labels never
reached Plex, and **nothing retried** until a later scan advanced the watermark again.

Fixed on `fix/dv-label-sync-watermark-loss` (off `main`): the watermark advances only
after `sync_labels()` returns, and the unavailable-Plex branch no longer touches it.
`tests/test_dv_autosync_watermark.py` proves it discriminating — reverting the fix
in-container fails the three bug-detecting tests while the two controls
(advances-on-success, startup-baseline-syncs-nothing) correctly pass on both
orderings. Without those controls the three would also pass on an implementation that
never advanced the watermark at all, which would re-walk the whole Plex library every
pass.

**This is the second time a review round has found a real defect in code adjacent to
the proposal rather than in the proposal itself.** Recorded here because it is the
strongest available argument for §6.8's acknowledgment protocol: the failure mode is
not hypothetical, it was already happening.

---

## 0.2 What changed in rev 3

All five rev-2 inconsistencies accepted, plus the observation-freshness lifecycle.

| Rev-2 inconsistency | Resolution |
|---|---|
| A — tri-state `false` had destructive meaning before negatives were validated | §6.1: detector emits `present` / `not_observed` / `unknown`. **No `false` exists.** |
| B — file-level evidence used for title-level removal | §6.6 **new**: title aggregation, mirroring the existing DV labeler |
| C — watermark had no success acknowledgment | §6.8: producer generation + applied generation, ack only after success |
| D — stale signature not load-bearing for removal | §6.5: freshness is part of "authoritative match" |
| E — `agreement_negative` claimed absent authority | §6.9: observation-oriented vocabulary, asymmetric by design |
| New — observation freshness / replacement lifecycle | §6.5 |

**One rev-2 item is now measured rather than deferred:** detector sensitivity (§6.2).
It does **not** unlock removal — see §6.3 for why the measurement is necessary but not
sufficient.

> **REVIEWER: read this file from the repository, not any chat summary of it.**
> If you cannot read it directly via the GitHub connector, **stop and say so**
> rather than reviewing a paraphrase.

---

## 0.3 What changed in rev 2, and what I got wrong

Every rev-1 finding is accepted. I checked the two arithmetically verifiable ones
independently and the reviewer was right on both.

| # | Finding | Status |
|---|---|---|
| 1 | `HDR10+DV` bucket name is false — its predicate never tests 2094-40 | **Fixed** (§3, independent booleans) |
| 2 | No label-sync trigger fires when only HDR data changes | **Fixed** (§6.8, the most valuable finding) |
| 3 | Library total is 5,708, not 6,308 | **Confirmed and corrected** (§3) |
| 4 | Stride-over-sorted is not a probability sample | **Accepted** (§3, §7 Q1) |
| 5 | "5 frames suffice" is an unvalidated detector contract | **Accepted**, now a blocking pre-req (§6.2) |
| 6 | `unreadable` conflates probe failure with missing metadata | **Fixed, and it falsified a claim I nearly shipped** (§4) |
| 7 | Separate table is safer but does not solve identity | **Accepted** (§6.4) |
| 8 | "HDR10+ rising in new releases" is not supportable | **Removed from the rationale** (§3) |
| 9 | DV cross-check safe only if strictly side-effect free | **Accepted** (§6.9) |
| 10 | Label *removal* semantics undefined | **Fixed** (§6.7) |

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

Rev 1 added a third instance of the same class (§0.3, finding 6). Three of my four
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

## 6. Proposal (rev 3)

### 6.1 Result model — no `false` exists

Rev 2 proposed tri-state `true/false/unknown`. That was inconsistent with rev 2's own
§6.2 ("absence in N sampled frames != proven negative") the moment §6.6 gave `false`
destructive power. Rev 3 removes the contradiction by removing the state:

```
hdr10plus_observation:  present | not_observed | unknown
dv_observation:         present | not_observed | unknown
probe_status:           success | timeout | nonzero_exit | parse_error
color_transfer:         optional string — ABSENT IS NOT AN ERROR
stderr_excerpt:         compact, on failure only
```

`not_observed` means "the probe ran and found no positive evidence in the sampled
windows" — which is what was actually established. A `negative_validated` value may be
added later **only** if §6.3's conditions are ever met; nothing in this design emits it.

**Enum, not nullable booleans.** Two nullable booleans plus a status field is an implicit
state machine whose invalid combinations are reachable by accident. That is precisely how
the false corruption verdict happened.

**Invariant — conservation of unknown.** Any failure to obtain evidence propagates as
`unknown`, never as absence. Asserted as impossible-state tests:

```
probe_status != success  AND  hdr10plus_observation == not_observed   -> INVALID
probe_status == success  AND  probe_status == parse_error             -> INVALID
color_transfer absent    AND  probe_status != success                 -> INVALID (absence is not failure)
```

**Evidence types, not verdicts.** The detector returns `ProbeResult` /
`FrameMetadataObservation`. A separate classifier derives display categories. The rev-1
harness returned `"HDR10"`/`"SDR"`/`"corrupt"` directly, which is how a shell-level error
fell through into `SDR` for 101 phantom files.

**Sentinel accounting.** Every input file terminates in exactly one `probe_status`, and
`sum(status counts) == input count` is asserted. This is the generalisation of the count
check that caught the word-splitting bug.

**Golden controls gate every run.** A fixed corpus — known HDR10+ positive, known DV
positive, known plain HDR10, known SDR, known probe failure, and known
readable-but-missing-`color_transfer` — must classify correctly before any measurement
run is accepted. **A run whose controls fail is invalid, not merely suspect.** The
`Leo (2023).mkv` six-strategy control in §6.2 is the prototype; §4's retraction is what
happens without one.

### 6.2 Detector sensitivity — MEASURED (was blocking; now cleared, with limits)

Rev 1 asserted "5 frames suffice" with no evidence. It has now been measured.

**Test design.** The weak direction is re-checking known positives — they were *found*
with 5 frames by construction. The direction that measures false negatives is the
opposite: take files the 5-frame probe called **negative** and read deeper. Any hit is a
false negative the contract would have produced.

| Check | Result |
|---|---|
| Known positives reproduced at 5 frames | **17 / 17** |
| 5-frame negatives re-probed (seeded sample of 143, `seed=20260809`) | 24 |
| Deeper strategies per file (`30f`, `120f`, `@25%`, `@50%`, `@75%`) | 5 → **120 deeper probes** |
| Negatives revealing HDR10+ on any deeper read | **0** |

**Positive control, without which the above is vacuous.** A broken deeper-read — e.g. the
`25%+#20` seek syntax silently yielding no frames — would find nothing everywhere and
produce the same "0 false negatives". Running all six strategies against a known positive
(`Leo (2023).mkv`): every one read its expected frame count (5/30/120/20/20/20) and every
one found 2094-40. The machinery works, so the zero is a real observation.

Incidentally this also substantiates rev 1's *reasoning*: HDR10+ was present at 25%, 50%
**and** 75% through the control file, consistent with per-frame carriage.

**Limits, stated deliberately.** 24 of 143 negatives is a sample. "No false negatives
observed in 24 files" is not "5 frames never misses HDR10+". The governing rule from rev 1
is therefore **retained, not retired**: absence in N sampled frames is evidence of
absence, not proof of it — which is why §6.6 still refuses to treat a probe error or an
`unknown` as an explicit negative.

The same caveat applies to `dv_present` (78 observations), weaker only because DV RPU data
is expected throughout.

**Recommended production contract:** 5 frames as the primary read, since it is
20-30x cheaper than 120 frames and lost nothing across 120 comparison probes. Escalation
to multi-window is no longer required for *detection*, but see §6.6 Q3 — it may still be
required before any *removal*.

### 6.3 Why the sensitivity measurement does NOT unlock removal

§6.2 measured 0 false negatives in 24 files. That is necessary but **not sufficient**, for
two independent reasons — and they compound rather than substitute.

**Gate A — negative validity.** 24 of 143 is a sample. A validated negative contract needs
a stated confidence bound and trusted negative ground truth, neither of which exists.
Until then `not_observed` is the honest value and there is no `false` to act on (§6.1).

**Gate B — title-level completeness (§6.6).** Even with a perfect file-level detector, one
file's absence does not authorise removing a *title's* label, because a Plex movie can
have several parts.

Both gates must pass. Passing A alone would still be unsafe.

### 6.4 Logical file identity + freshness — BLOCKING pre-requisite

A separate table avoids contaminating the DV contract but **does not solve the join**. Key
on `(logical_root_id, normalized_relative_path)`, never raw absolute path. `Y:\...`,
`\\TURTLELANDSRV2\...` and `/library/plex-source/...` must map to the same logical root
before any comparison. Table named `video_metadata_scan` (broader than HDR10+).

Persist `raw_path`, `normalized_path`, `observed_sig_size`, `observed_sig_mtime`,
`observed_at`. **Freshness is a first-class classifier input, not supporting evidence** —
see §6.5. This matters more than it looks because the design intends cheap
signature-based rescans, so stale rows are the normal steady state, not an edge case.

### 6.5 Observation lifecycle — a stale row is not evidence about the current file

A file replaced at the same logical path leaves a row that still describes the *old* file
until the next scan. Since the signature changes, the old observation is **stale** and
must not speak for the current bytes.

| Situation | Label behaviour |
|---|---|
| current signature matches, `present` | add / keep |
| current signature matches, `not_observed` | **preserve** — not a negative (§6.1) |
| signature differs (file replaced, not yet rescanned) | **preserve** existing label; old evidence proves nothing about the new file |
| no observation at all | **preserve** |
| `probe_status != success` | **preserve** |

"Authoritative match" for any destructive purpose therefore means **all four**: logical
identity matched **and** current signature matched **and** probe succeeded **and** a
validated negative contract exists (§6.3 Gate A). Today the fourth never holds, so nothing
is destructive.

### 6.6 Title aggregation — NEW, mirroring the existing DV labeler

Rev 2 spoke as though one matched file could remove a title-level label. The existing DV
labeler already solves this correctly (`pick_layer`, `backend/rename/dv_labeler.py`), and
I failed to reuse a working pattern sitting beside mine. HDR10+ adopts the same shape,
over **every current Plex part** of a movie:

```
if ANY part is `present`                          -> label present
elif EVERY current part is fresh + successful
     + authoritative negative                     -> removal permitted
else                                              -> unknown, PRESERVE
```

Consequently all of these preserve: `not_observed` + `unknown`; `not_observed` +
`probe_error`; `not_observed` + missing observation; `not_observed` + stale signature;
`not_observed` + unmatched part. And `present` + `not_observed` -> **present** (one part
proving HDR10+ proves it for the title).

Because §6.3 Gate A never holds today, the middle branch is currently unreachable — which
is the intended state, not an oversight.

### 6.7 Label removal — DEFERRED, and gated when it arrives

| Observation | Action |
|---|---|
| title has any `present` part (fresh, successful) | add `HDR10+` |
| everything else | **preserve** |
| ~~explicit negative~~ | **not implemented** — no `false` exists (§6.1) |

**Phase 1 (this design): positive-only. HDR10+ is never removed automatically.**
Phase 2 requires Gate A *and* Gate B *and* an explicit config gate, defaulting off.

`MANAGED` grows to include `HDR10+` and stays a **closed set** — the prefix-wildcard bug
that once deleted user labels like `DV Cut` must not be reintroduced.

### 6.8 Managed-label generation — producer/applied acknowledgment

Rev 2 said "unified watermark" without saying what marks reconciliation *consumed*. §0.1
shows why that gap is not academic: the existing DV loop consumed generations it never
applied, and it was silently doing so in production.

```
managed_label_generation          -- monotonic INTEGER, database-owned, producer-advanced
managed_label_applied_generation  -- consumer-advanced, ONLY after reconciliation succeeds
```

Reconcile while `generation > applied`; set `applied = target` only on success. Integer
sequence, not a timestamp — timestamps bring same-instant collisions, clock skew and
precision assumptions across producers.

**Restart semantics deliberately differ from the DV scheme.** The old "first pass after
startup establishes a baseline and syncs nothing" tolerated stale labels by design. With
durable generations, `generation=52 / applied=51` after a restart is **pending work and
must reconcile.** That is the whole point of a durable acknowledgment.

**Publication granularity:** one increment per completed scan commit, not per row.
Thousands of per-row increments would mean thousands of full-library Plex walks.
Incremental rescans coalesce behind a debounce window.

Per-producer `last_changed_generation` may be stored **as observability only**; one global
dirty generation drives reconciliation.

### 6.9 DV cross-check — observation vocabulary, asymmetric by design

`agreement_negative` is removed: it asserted that both detectors agree a file has no DV,
but §6.1 means the container side cannot assert absence at all. Storing derived agreement
also collapses an asymmetry that matters — the host side has a mature `none` verdict, the
container side has only "did not observe".

```
host_verdict:           present | none | unknown        (mature authority)
container_observation:  present | not_observed | unknown (unvalidated)
identity_status:        matched | unmatched_container | unmatched_host
```

Report wording is *derived* from these three, never stored as a single enum. Report a 2x2
over **matched** files plus unmatched counts on both sides and probe errors separately.
**Unmatched paths are not disagreements.** Must not mutate `dv_scan`, drive labels, or
affect host scheduling.

**Out of scope:** replacing the host detector; FEL/MEL from ffprobe (impossible);
`dv_file_tagging` stays false.

### 6.10 Implementation order (reviewer's rev-2 order, adopted)

Deliberately separates non-destructive delivery from destructive convergence, so the
useful feature does not wait on the hardest safety proof.

1. Corrected measurement report — **done** (this document)
2. Evidence types + golden controls (§6.1)
3. Detector sensitivity — **measured** (§6.2); Gate A still open (§6.3)
4. Logical identity **and signature freshness** (§6.4, §6.5)
5. Create `video_metadata_scan`
6. **Ship positive-only HDR10+ label addition** (§6.7 Phase 1)
7. Durable generation + applied-generation acknowledgment (§6.8)
8. Read-only DV cross-check, observation vocabulary (§6.9)
9. Validate title-level negative aggregation (§6.6)
10. Enable removal only after Gate A + Gate B + explicit config gate
11. Only then discuss any change in DV authority

---

### 6.11 Estimand — both, reported separately

Adopting the reviewer's Q2 answer. Two questions, two numbers, neither forced to serve
both:

| Estimand | Question it answers | Population |
|---|---|---|
| **Unique-title prevalence** | how often a badge is actually visible in Plex | deduplicated by the *same* title/media identity used for reconciliation |
| **Scanner workload** | runtime, I/O, DB size, rescan cost, error rate | every file the scanner traverses, backups included |

**"Exclude the two `-bu` libraries" is explicitly rejected as a deduplication algorithm** —
it assumes backup is the only duplication mechanism, which is unverified. Deduplicate by
the identity from §6.4, not by library name.

Since the build decision no longer rests on prevalence (§3.1), both numbers are
informational and neither blocks implementation.

---

## 7. Round-3 questions

Rev 3 accepted every rev-2 finding without dispute, so these ask whether the *resolutions*
hold — not whether the findings were right.

1. **Is there anything left that can remove a label?** The design now claims removal is
   unreachable: §6.1 emits no `false`, §6.5 requires a validated negative contract that
   does not exist, §6.6's middle branch depends on it, §6.7 is positive-only. **Please
   attack that claim specifically** — is there a path where `preserve` still results in a
   lost label, e.g. Plex's own behaviour when a title's parts change, or `sync_labels`
   reconciling a *managed set* such that adding `HDR10+` to `MANAGED` changes what the
   **DV** reconciliation removes? Widening a closed set is exactly the kind of change that
   looks additive and is not.
2. **§6.8 restart semantics reverse a deliberate existing safety choice.** The current
   scheme syncs nothing on the first pass after startup, specifically so a restart cannot
   trigger a full-library walk. Durable generations make a restart reconcile pending work
   instead. Is that trade right, given §0.1 proves the old behaviour lost real work — or
   does it reintroduce a startup-stampede risk under, say, a crash loop?
3. **Does the sensitivity measurement (§6.2) actually support what §6.3 Gate A would
   need?** I measured 0/24 with a passing six-strategy control, then argued it is
   insufficient. Is that the right call, or am I now over-hedging a result that is
   adequate for a *positive-only* feature? Specifically: does Gate A matter at all while
   removal is unreachable, or is it dead weight in the design?
4. **§6.1's impossible-state assertions — are they complete?** I listed three. Given three
   of my four measurement errors were confident misclassifications rather than visible
   failures, the interesting question is which invalid combination I have *not* thought
   to forbid.
5. **Is `not_observed` doing too much work?** It covers "probe ran, sampled windows, found
   nothing" — but under §6.5 it also silently covers "sampled the wrong 5 frames of a
   file that does carry HDR10+". Should the persisted value record *which* sampling
   strategy produced it, so a later strategy change can invalidate prior observations
   rather than inheriting them?
6. **Same question as last round, applied to rev 3:** are these six resolutions consistent
   with **each other**? I changed the result model, added two new sections, and inverted a
   restart behaviour in one pass, again without implementing any of it. The specific risk
   is §6.6 and §6.8 interacting: title aggregation reads *current* Plex parts while the
   generation protocol decides *when* to read them.
