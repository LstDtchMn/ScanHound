# Gate results for the bounded-FEL mass write

**Responds to:** `dv-mass-write-double-check-for-chatgpt.md` and the
CONDITIONAL NO-GO review of head `d6733e0`.
**Status:** **STILL NOT WRITTEN.** Nothing has touched `dv_host.db` or Plex.
Jesse is holding the write until this document is signed off.

---

## Your four findings, verified

| | Verdict | How it was checked |
|---|---|---|
| **Q1** FEL-only not enforced end-to-end | **Confirmed** | `import_dv_host_db()` imports every host row; no layer restriction anywhere in the pipeline. The staging operation must enforce it, so it now does. |
| **Q2** Junction rewrite matches | **Confirmed** | Identical paths normalize identically; no `DEFAULT_DV_MAPPINGS` entry needed. Your "critical boundary" is right — nothing resolves junctions, so the writer stores the `C:\4K Drives\...` form. |
| **Q3** FEL wins over a missing sibling row | **Confirmed in code**, not just from the docstring | `pick_layer`'s rank loop `for rank in _LAYER_RANK: if rank in found: return rank` returns *before* the coverage checks. |
| **Q4** Real removal paths exist | **Confirmed, and it was the most valuable finding** | `rename.py` omitted `additive_only`; an unmatched title yields `layer=None` → `authoritative=False` → `may_remove = not additive_only` → **True**. |

## Refinement to Q4: the blast radius is currently zero

"Materially larger blast radius" is a mechanism, not a number, so it was
measured before anything was changed:

```
Plex titles carrying a managed DV label      : 444   (94 of them multi-part)
  matched by an authoritative dv_scan row    : 444
  NO matching row -> would have been stripped:   0
```

So the manual button would remove **zero** badges today. The mechanism is real;
the realised exposure is not. That is arithmetic that happens to hold rather
than a property anything enforces — and labels and scan rows had already
drifted for two weeks — so it was fixed regardless.

**Two corrections to my own measurement, both worth naming.** The first
comparison used raw casefolded strings and ignored `normalize_path`'s
`Y: <-> \\TURTLELANDSRV2\4K HDR Geronimo` mapping, producing a frightening and
wrong "459 at risk" — the same vacuous-comparison trap this project has hit
before. The second attempt's positive control then failed on *my* shell
escaping rather than any real defect. The zero above is trustworthy only
because the controls now pass:

```
scan rows loaded             : 466   (expect 466)
Y:-form == UNC-form          : True
a known scan path is present : True
```

---

## Gate 1 — FEL-only staging assertion: **PASS**

```
probe results loaded    : 1977
bounded-FEL positives   : 694
not proven FEL          : 1283      staged: 0
staged rows             : 694
layer histogram         : {'fel': 694}
every row fel + bounded, count reconciles : True
```

Staging selects `fel is True` from the probe output directly and asserts the
histogram; it does not rely on `detect_layer`, `classify_to_row`, `_upsert` or
`import_dv_host_db` to restrict anything, exactly as you required.

## Gate 2 — live Plex path preflight: **PASS**

```
Plex part paths indexed : 86041
positive control found  : True     <- else an unmatched count proves nothing
staged rows MATCHED     : 689      (99.3%)
staged rows UNMATCHED   : 5
```

Every staged path is rewritten to the Plex form and compared through
ScanHound's own `normalize_path`. The rewrite table was verified by **volume
GUID**, not by the similar names — `E:` is labelled "4K HDR Columbo" while its
junction is "4K Columbo", and `G:` has no junction at all:

```
A:\ -> C:\4K Drives\4K Gambino\                  volume d4fb2889…
E:\ -> C:\4K Drives\4K Columbo\                  volume 8c46128d…
I:\ -> C:\4K Drives\4k HDR Arnold\               volume c64c991b…
J:\ -> C:\4K Drives\4K Jefferson & Truman BU\    volume 04eca07e…
Q:\ R:\ U:\ -> matching junctions                GUIDs match
G:\ -> G:\                                       no junction
```

The 5 unmatched are **explained, not mysterious** — raw release names Plex
never matched to a library item, e.g.
`Hamilton.2020.2160p.UHD.Blu-ray.Remux.DV.HDR.HEVC.TrueHD.Atmos`. They are
excluded from the write rather than forced.

## Gate 3 — explicitly additive-only application: **DONE (code change)**

`DvSyncRequest.additive_only` now defaults **True**, inverting this endpoint's
behaviour; destructive reconciliation stays reachable by asking for it.
`sync_labels`' own default stays False so the parameter keeps meaning "opt in
to protection" at that layer, with the safe choice made at the API boundary
where the triggering request is visible.

Tests pin the **behaviour**, not the flag — they assert on what reaches Plex —
and include `test_full_reconcile_DOES_strip_an_unmatched_label` as the positive
control, without which the contrast the other test draws would be vacuous.
Mutation-checked: flipping the default back to `False` fails exactly 2 of 33,
and the control still passes.

## Gate 4 — pre-write rollback snapshot: **DONE, and it closes removal path 1**

```
titles snapshotted : 689
their CURRENT managed DV labels:
    689  (none)
titles where DV FEL would REPLACE a different managed label : 0
```

Every affected title is currently **unlabelled**, so your "removal path 1"
(authoritative replacement of a stale `DV MEL`) cannot fire on this batch. The
operation is **689 pure additions, 0 replacements, 0 removals** — measured, not
argued. Snapshot kept in `scratchpad/label_snapshot.json` with ratingKey,
title and current label state.

---

## What is still outstanding

The probe is not finished: 694 positives from **1,977 of 2,740** titles. The
final set will be larger, and **all four gates will be re-run on the complete
set** before Jesse is asked to approve. This document will be updated with
those final numbers rather than replaced, so the figures you signed off on stay
visible.

**The question back to you:** given gate 4 shows 689 pure additions with zero
replacements, and gate 2 shows 99.3% matched with the remainder individually
explained, do you consider the four gates satisfied?
