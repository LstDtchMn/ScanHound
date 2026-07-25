# HDR10+ Plex label + Kometa overlay — design

**Date:** 2026-07-25
**Status:** design, awaiting peer review and Jesse's approval. No code changed.
**Depends on:** `docs/reviews/2026-07-25-dv-scan-bindmount-findings.md`

## Goal

Badge HDR10+ titles in Plex the way DV FEL/MEL/P8/P5 are badged today, without
disturbing the existing DV labelling, which is load-bearing for Kometa.

## The constraint that shapes everything

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
(`dv_profile=8`, `hdr10plus_state=present`). Adding `"HDR10+"` to `MANAGED`
would make the two compete: reconciling a P8+HDR10+ title would add one label
and strip the other, then flap on every subsequent sync.

**Decision: HDR10+ gets its own managed set and its own independent
reconciliation pass. `MANAGED` is not modified.**

The module docstring's existing warning reinforces this — it records that a
`'DV '` prefix wildcard once deleted user labels like `'DV Cut'`. The closed-set
discipline is deliberate and must be preserved per-axis.

## Data source

HDR10+ state already has a home, written by the metadata inventory:

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

This differs from the DV path, which reads `dv_scan` via
`db.get_dv_scans(source="scan")`. The HDR10+ pass reads `media_inventory`
instead. Both key on a filesystem path and normalize through
`backend/rename/dv_paths.normalize_path()`, so the existing path-matching and
`mappings` plumbing is reused unchanged.

Per the findings document, `hdr10plus_state` can be populated by the **cheap
ffprobe probe** (0.2–0.6 s/file, frame side-data
`HDR Dynamic Metadata SMPTE2094-40`) rather than the full `hdr10plus_tool`
stream read. That changes how the column gets filled, not this design — this
spec consumes the column and is agnostic to which probe wrote it.

## Behaviour

New managed set, deliberately a singleton so the same closed-set reconciliation
shape applies:

```python
MANAGED_HDR10PLUS = {"HDR10+"}
```

Reconciliation is a **tri-state**, and the third state is the safety-critical
one:

| `hdr10plus_state` | action |
|---|---|
| `present` | ensure `HDR10+` label is on the movie |
| `absent` | ensure `HDR10+` label is **removed** |
| `unknown` | **leave the movie untouched** |

`unknown` must never strip a label. It is the value for unscanned files,
`scan_state=failed` rows, and — critically — every file under the currently
broken `nas-4k-*` mounts (see findings). Treating `unknown` as `absent` would
mass-delete correct badges across a large fraction of the library the first
time a mount hiccuped.

A row whose `scan_state` is `failed` or `source_changed` is treated as
`unknown` regardless of its stored `hdr10plus_state`, since the stored value
may predate the change that invalidated it.

`additive_only` carries the same meaning as the DV pass: when set, an unmatched
movie is left alone entirely. The scheduled auto-sync passes it; a manual
button does not.

## Kometa overlay

`docs/kometa/dv_badges.yml` places all four DV badges at the **same
coordinates** — `horizontal_align: left`, `vertical_align: top`, offsets
`15/15`, back box `200x80`. That is safe today because the four are mutually
exclusive.

An HDR10+ badge is *not* mutually exclusive with them and would render on top
of the DV badge on any P8+HDR10+ title. It needs its own slot. Proposal: same
left/top corner, dropped below the DV badge by the box height plus the gutter
(`vertical_offset: 110`), so a title with both reads as a stacked pair and a
title with only one still sits in a sensible place.

The label string must match the vocab value exactly or nothing badges — the
existing file already carries that warning and a commit reference for the
strings it was verified against. The new block follows the same convention:

```yaml
  HDR10+:
    overlay:
      name: text(HDR10+)
      horizontal_offset: 15
      horizontal_align: left
      vertical_offset: 110
      vertical_align: top
      font_color: "#FFFFFF"
      back_color: "#00000099"
      back_width: 200
      back_height: 80
      back_radius: 30
    plex_search:
      all:
        label: HDR10+
```

Open question for review: whether to ship this in the existing
`dv_badges.yml` (one file to install, but the filename becomes a misnomer) or a
sibling `hdr_badges.yml` (honest name, second `overlay_files` entry). Leaning
sibling file, since the two axes now have independent data sources and
independent sync passes.

## Configuration

Mirror the DV vocab pattern rather than inventing a new one.
`backend/config.py:525` holds:

```python
"dv_label_vocab": '{"fel": "DV FEL", "mel": "DV MEL", "profile8": "DV P8", "profile5": "DV P5"}',
```

Add alongside it:

```python
"hdr10plus_label": "HDR10+",
```

A plain string, not JSON — the set is a singleton, so the JSON-map shape would
be ceremony. `_vocab_from_config()`'s defensive parsing (fall back to the
default on malformed input) is worth copying in spirit: a bad config value must
degrade to the default, never to an empty label that would match everything.

## Surface

A separate endpoint, parallel to `POST /rename/dv-sync-labels`
(`backend/api/routes/rename.py:703`), which already runs its work on a
background thread and reports through the same progress channel. Reusing that
shape keeps the two passes independently runnable — important because they read
different tables and one can be healthy while the other is stale.

Whether the scheduled maintenance loop should run both passes together is a
question for review; the safe default is to add the HDR10+ pass as opt-in and
leave the DV schedule untouched.

## Explicitly out of scope

- Changing `MANAGED`, `pick_layer`, `desired_label`, or any DV reconciliation
  behaviour.
- Changing how `hdr10plus_state` is *produced*. The cheap-probe redesign is a
  separate change with its own review.
- HDR10+ detection for TV.
- Any deploy, or enabling the pass in production. Both are Jesse's call.

## Risks

1. **Label flap.** If the HDR10+ pass and the DV pass ever share a set, or if
   `unknown` is mistreated as `absent`, badges will oscillate on every sync.
   Mitigated by the separate set and the tri-state rule; should be covered by a
   test that runs two consecutive syncs and asserts the second is a no-op.
2. **Overlay collision.** Verified by inspection of the existing YAML, not by
   rendering. Should be eyeballed in Plex on a known P8+HDR10+ title
   (`Bring Her Back`) before wider rollout.
3. **Stale source.** `media_inventory` is only as current as the last scan. The
   `scan_state` guard above is the mitigation; it means a stale row goes
   untouched rather than wrong.

## Review questions

1. Separate managed set and separate pass — agreed, or is there a reason to
   unify the two axes behind one reconciler?
2. Is `unknown` → leave-untouched the right failure posture, or should a
   long-`unknown` row eventually be treated as `absent`?
3. Sibling `hdr_badges.yml` or extend `dv_badges.yml`?
4. Should the scheduled maintenance loop run the HDR10+ pass, or stay manual
   until the data source is trusted?
