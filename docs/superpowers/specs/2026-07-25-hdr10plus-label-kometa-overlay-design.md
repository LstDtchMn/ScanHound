# HDR10+ Plex label + Kometa overlay — design

**Date:** 2026-07-25 (revised same day after peer review)
**Status:** design. No product code, tests, config, Dockerfile, or Kometa YAML
changed by this document. Not authorized for implementation, merge, deploy, or
enablement.
**Depends on:** `docs/reviews/2026-07-25-dv-scan-bindmount-findings.md`

## Goal

Badge HDR10+ titles in Plex the way DV FEL/MEL/P8/P5 are badged today, without
disturbing the existing DV labelling, which is load-bearing for Kometa.

## 1. The constraint that shapes everything

`backend/rename/dv_labeler.py` manages a **closed, mutually exclusive** set:

```python
MANAGED = {"DV FEL", "DV MEL", "DV P8", "DV P5"}
```

`reconcile_movie()` picks exactly one `desired` label and removes every other
member of `MANAGED` from that movie:

```python
existing_managed = _existing_labels(movie) & MANAGED
if desired and desired not in existing_managed:
    added.append(desired)
if not additive_only or layer is not None:
    for stale in existing_managed - ({desired} if desired else set()):
        removed.append(stale)
```

That is correct for DV layer, which is genuinely one-of-N.

**HDR10+ is orthogonal, not another member.** A file can carry Dolby Vision
Profile 8 *and* HDR10+ simultaneously — `Bring Her Back` is exactly that
(`dv_profile=8`, `hdr10plus_state=present`). HDR10+ can also coexist with plain
HDR10 and with other DV presentations. Adding `"HDR10+"` to `MANAGED` would
make the two compete: reconciling a P8+HDR10+ title would add one label and
strip the other, then flap on every subsequent sync.

**Decision: HDR10+ gets its own managed set and its own reconciliation pass.
`MANAGED` is never modified.** The module docstring's existing warning
reinforces the discipline — it records that a `'DV '` prefix wildcard once
deleted user labels like `'DV Cut'`.

### Share the engine, not the state

Do not duplicate the whole reconciliation implementation. Prefer **separate
public passes over one generic internal Plex reconciliation engine**.

Shared: Plex library enumeration, normalized-path extraction, dry-run
formatting, progress callbacks, write throttling, add/remove error handling,
write summaries.

Kept separate per axis: row source, evidence reducer, managed set, desired-label
calculation, removal authority, scheduling gates.

## 2. Evidence semantics: positive and negative are not symmetric

This is the safety core of the design.

A cheap frame-sampled `ffprobe` probe reads only the first frames of a stream.
It can prove HDR10+ is **present**. It cannot prove HDR10+ is **absent** —
absence in a 12-frame sample is absence of evidence, not evidence of absence.

| observation | HDR10+ evidence |
|---|---|
| sampled frame carries SMPTE ST 2094-40 / HDR10+ side data | authoritative `present` |
| sampled frames carry no such side data | `unknown` |
| validated full-stream detector reports no HDR10+ | authoritative `absent` |
| detector failure, timeout, malformed output, unreadable source, cancellation, missing tool | `unknown` |

Only a validated full-stream detector, or another explicitly trusted
full-stream source, may write authoritative `absent`.

This matters because `absent` authorizes a **destructive Plex label removal**. A
1-frame or 12-frame miss must never remove an existing HDR10+ badge.

Note this constrains the cheap-probe redesign in the findings document: that
redesign may populate `present` cheaply, but `absent` still requires a
full-stream pass or must remain `unknown`.

## 3. Data source and authority

```sql
media_inventory (
    path TEXT PRIMARY KEY,
    rating_key TEXT,
    hdr10plus_state TEXT NOT NULL DEFAULT 'unknown'
        CHECK(hdr10plus_state IN ('present', 'absent', 'unknown')),
    scan_state TEXT NOT NULL DEFAULT 'unscanned'
        CHECK(scan_state IN ('unscanned','current','stale','failed','source_changed')),
    ...
)
```

The DV pass reads `dv_scan` via `db.get_dv_scans(source="scan")`; the HDR10+
pass reads `media_inventory`. Both key on a filesystem path and normalize
through `backend/rename/dv_paths.normalize_path()`, so existing path-matching
and `mappings` plumbing is reused unchanged.

**Only `scan_state == "current"` is authoritative.**

```python
authoritative = row["scan_state"] == "current"
```

Every other value — `unscanned`, `stale`, `failed`, `source_changed`, a missing
row, an unmatched path, a malformed value, or any future state not yet
recognized — is treated as `unknown`. **Fail closed on unrecognized states**, so
adding a state to the schema can never silently authorize label removal.

### Cross-axis coupling (unresolved, must be resolved before unattended runs)

`scan_state` is a single overall verdict, which is too coarse. Per the findings
document, a DV extraction timeout currently fails the whole inventory item even
when the base probe and HDR10+ detection both succeeded. Under the rule above,
that DV failure silently suppresses good HDR10+ evidence.

Preferred correction is per-axis evidence: persist base probe results, HDR10+
evidence/state, and DV evidence/state independently, and derive overall UI
status from the components. Illustrative future fields:

```
hdr10plus_state, hdr10plus_method, hdr10plus_error, hdr10plus_scanned_at
dv_state, dv_layer, dv_method, dv_error, dv_scanned_at
```

A schema change is **not** authorized here. If the first implementation avoids
one, it must instead define the exact `probe_json` evidence that makes HDR10+
authoritative despite an unrelated DV failure, and prove the source mtime and
size were unchanged. The fully decoupled schema is cleaner and is the
recommendation.

## 4. Multipart aggregation

A Plex movie may have several media parts or editions. Reduction must be
conservative, and must consider the **total Plex part set** — not merely the
rows that happened to match.

```
if any authoritative matched part is present:
    movie = present
elif every Plex part matched an authoritative current row
     and all matched rows are absent:
    movie = absent
else:
    movie = unknown
```

| part evidence | movie state |
|---|---|
| present + absent | present |
| present + unknown | present |
| absent + absent, all parts covered | absent |
| absent + unknown | unknown |
| absent + unmatched Plex part | unknown |
| no matched parts | unknown |
| conflicting duplicate rows | never `absent` unless all authoritative evidence agrees |

Where several inventory rows normalize to the same path, `present` dominates;
any residual uncertainty yields `unknown`; `absent` is accepted only when all
authoritative evidence agrees.

## 5. Never age `unknown` into `absent`

Long-running uncertainty is not evidence of absence. **No time-based decay from
`unknown` to `absent`.**

This is not hypothetical, and the real case was worse than a decay rule's
worst assumption. On 2026-07-26 **all nine** NAS shares were found unmounted
inside the container while the Scheduled Task that mounts them reported
success (findings §11). Every title on those shares — the large majority of the
4K library — would have read as unreadable, and a decay rule would have
mass-deleted their badges. The mount defect is fixed, but the class of failure
it represents (a whole storage backend silently absent, reported healthy) is
exactly what `unknown` must absorb without destroying evidence.

Old `unknown` rows instead feed retry candidates, stale-age reporting,
mount-health diagnostics, coverage alerts, and manual review. Removing labels
because media was intentionally retired is a **separate administrative
workflow** with its own dry run.

## 6. Label ownership

Ship a **fixed label, `HDR10+`, with no configuration in v1.**

An earlier draft proposed both `MANAGED_HDR10PLUS = {"HDR10+"}` and a
configurable `hdr10plus_label`, mirroring `dv_label_vocab`. That combination
creates ownership and migration problems: if the configured value changes, the
previously written label is orphaned and no longer managed, and the Kometa
config silently stops matching.

If configurability is added later it requires an explicit migration operation
that knows both the prior managed label and the requested new one, previews
affected Plex items, confirms additions and removals, refuses to manage
arbitrary labels merely because they appeared in historical config, and updates
the Kometa configuration correspondingly.

A malformed or empty configuration must never produce a blank label write.

## 7. Policy flags: retire `additive_only` for the new pass

`additive_only` is a misleading name. In the existing DV code it still permits
removing a stale managed label when a positive authoritative match exists —
`if not additive_only or layer is not None:` — so "additive only" does not mean
"no removals".

The generic engine should take explicit policy fields instead:

```python
# burn-in
preserve_unmatched=True,  remove_authoritative_absent=False
# convergence, later
preserve_unmatched=True,  remove_authoritative_absent=True
```

The DV pass's existing behaviour and flag are left untouched.

## 8. Kometa layout

`docs/kometa/dv_badges.yml` places all four DV badges at identical coordinates
(`left`/`top`, offsets `15/15`, back box `200x80`). That is safe only because
the four are mutually exclusive. HDR10+ is not exclusive with them and would
render on top.

A fixed second position (`vertical_offset: 110`) avoids collision but leaves an
HDR10+-only movie displaying its single badge in the second slot, with a gap
above it.

**Kometa queues solve this properly** (verified against the Kometa overlay
documentation): a file may declare a top-level `queues:` block listing ordered
positions; an overlay joins one via a `queue:` attribute; and `weight` decides
order — the highest weight takes the first position, the next takes the second,
and so on. Desired result:

- DV only → first slot
- HDR10+ only → first slot
- DV + HDR10+ → first and second slots

```yaml
queues:
  dynamic_hdr:
    - horizontal_offset: 15
      horizontal_align: left
      vertical_offset: 15
      vertical_align: top
    - horizontal_offset: 15
      horizontal_align: left
      vertical_offset: 110
      vertical_align: top
```

The four DV overlays join `dynamic_hdr` with the higher weight; HDR10+ joins
with a lower weight. The DV overlays remain mutually exclusive between
themselves, so at most one DV badge ever claims the first slot.

**This settles the packaging question, and reverses the earlier draft's
preference for a sibling file.** The Kometa documentation defines `queues` as a
file-level attribute and does not describe sharing a queue across overlay
files, so DV and HDR10+ overlays must live in the **same file** to share
positioning. Recommendation: extend the existing file, and if the DV-specific
name becomes misleading, rename it (e.g. `dynamic_hdr_badges.yml`) as a
deliberate, documented change — accepting that a rename requires a matching
`overlay_files` update in the user's Kometa config. Separate files are
acceptable only if fixed coordinates are deliberately accepted, or if
cross-file queues are proven to work.

Cross-file queue behaviour and exact weight semantics should be confirmed
against the installed Kometa version before implementation.

## 9. Rollout — manual and dry-run first

Do **not** attach the HDR10+ pass to the normal maintenance schedule in the
first implementation.

**Stage 1 — dry run only.** Inspect coverage and present/absent/unknown counts;
verify known-positive and known-negative titles; verify path normalization and
multipart reduction; confirm no DV label is touched.

**Stage 2 — present-only writes.** Add `HDR10+` for authoritative current
`present`. Remove nothing. Keep the dry-run report and monitor discrepancies.

**Stage 3 — authoritative convergence.** Only once detector semantics and mount
coverage are trusted: permit removal for authoritative current `absent`,
preserve every unknown and unmatched title, enforce a removal-count safety
threshold, and expose a dry run before real writes.

**Stage 4 — scheduled.** Gated, never merely timer-driven. Preconditions: the
inventory run completed successfully; current-row coverage is above the approved
threshold; the unknown count has not spiked; mount health is good; the planned
removal count is within expected bounds; no scan is running; no concurrent label
pass is active; and the summary is persisted or logged.

## 10. Surface

A separate endpoint parallel to `POST /rename/dv-sync-labels`
(`backend/api/routes/rename.py:703`), which already runs on a background thread
and reports through the same progress channel. Keeping the passes independently
runnable matters because they read different tables and one can be healthy while
the other is stale.

## Explicitly out of scope

- Changing `MANAGED`, `pick_layer`, `desired_label`, or any DV reconciliation
  behaviour.
- Changing how `hdr10plus_state` is *produced* — the cheap-probe redesign is a
  separate change with its own review, constrained by §2.
- Any schema migration (called out in §3 as an implementation decision).
- HDR10+ detection for TV.
- Any deploy, or enabling the pass in production.

## Risks

1. **Label flap** if the axes ever share a set, or if `unknown` is mistreated as
   `absent`. Mitigated by §1, §2 and §5, and by the idempotence tests below.
2. **False removal** from cheap-probe `absent`. Mitigated by §2; the strongest
   guard is that only a full-stream detector may assert `absent`.
3. **Overlay collision / layout regression**, verified so far only by reading
   the YAML and the Kometa docs, not by rendering. Eyeball a known P8+HDR10+
   title (`Bring Her Back`) before wider rollout.
4. **Stale source.** `media_inventory` is only as current as the last scan; the
   `scan_state` guard means a stale row goes untouched rather than wrong.
5. **Cross-axis suppression.** Until §3 is resolved, a DV failure hides good
   HDR10+ evidence — conservative, but it will under-badge.

## Test matrix

### DV / HDR10+ independence
1. P8 + HDR10+ ends with both labels.
2. DV pass then HDR10+ pass preserves both.
3. HDR10+ pass then DV pass preserves both.
4. Running both a second time produces zero writes.
5. DV sync never adds or removes `HDR10+`.
6. HDR10+ sync never adds or removes any DV label.

### Tri-state behaviour
7. Current `present` adds `HDR10+`.
8. Current authoritative `absent` removes `HDR10+` in convergence mode.
9. Current `absent` does not remove in present-only mode.
10-16. `unknown`, `unscanned`, `stale`, `failed`, `source_changed`, missing row,
and unmatched Plex path each leave labels untouched. (`failed` leaves them
untouched unless per-axis evidence authority is explicitly implemented per §3.)
17. A broken NAS mount leaves labels untouched.

### Multipart and duplicate paths
18. present + absent → present.
19. present + unknown → present.
20. two current absent parts, full coverage → absent.
21. absent + unknown → unknown.
22. absent matched part + unmatched Plex part → unknown.
23. Conflicting normalized rows can never produce `absent`.
24. Duplicate authoritative present rows remain present.
25. Duplicate authoritative absent rows remain absent only when all agree.

### Config and ownership
26. Fixed `HDR10+` is the only managed HDR10+ label in v1.
27. Unrelated user labels are never touched.
28. A future label migration cannot orphan the prior label.
29. Empty or malformed label configuration cannot produce a blank label write.

### Scheduling and safety
30. Burn-in mode adds but never removes.
31. A scheduled run refuses or skips when inventory health gates fail.
32. A removal threshold aborts excessive changes.
33. Dry-run and real-run reducers produce identical proposed actions.
34. Concurrent DV and HDR10+ sync execution is serialized or otherwise safe.
35. A Plex write failure is recorded without falsely counting success.
36. A second run retries only unresolved writes and does not flap successful
    items.

## Conclusions

1. HDR10+ must remain outside the mutually exclusive DV managed set.
2. Separate public passes over one shared generic internal engine.
3. A quick frame probe may prove `present`; it may not prove `absent`.
4. Only current, authoritative evidence may authorize removal.
5. `unknown` always leaves labels untouched.
6. `unknown` never decays automatically to `absent`.
7. Multipart reduction is explicitly conservative and needs the full Plex part
   set.
8. Use the fixed `HDR10+` label in v1; no configuration.
9. Prefer queue-based Kometa positioning; queues are file-scoped, so the DV and
   HDR10+ overlays should share one file.
10. Keep the pass manual and dry-run first.
11. Begin with present-only additions.
12. Enable removal and scheduling only after detector and coverage validation.
13. Per-axis evidence authority must be resolved before unattended operation.
