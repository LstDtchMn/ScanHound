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
argued. Snapshot kept in `docs/reviews/peer-rounds/dv-evidence-2026-08-10/label_snapshot.json` with ratingKey,
title and current label state.

---

---

## Round 2: reviewer verdict and the two follow-ups it asked for

Reviewed at head `c8360b3`. Verdict: **GO for the current 694-positive set** —
689 matched targets, 5 explained non-targets, 0 expected removals, 0 expected
replacements, 689 pure additions. Approval is explicitly for the **gate design
and the current staged population**, not carried forward to the final set by
assumption.

Two follow-ups were requested and both are now closed.

**1. The five residuals must never dissolve into an undifferentiated
"unmatched" count** (reviewer's preferred option: exclude them from the
Plex-application set while retaining them in a separate report). Implemented —
staging now emits two artifacts, so the accounting reconciles by construction:

```
rows intended for Plex effect : 689
rollback snapshot population  : 689
reconciles                    : True
explained no-Plex-target rows : 5   (enumerated, not counted)
    Hamilton.2020.2160p.UHD.Blu-ray.Remux.DV.HDR.HEVC.TrueHD.Atmos.7.1-CiNEPHiLES.mkv
    Notting Hill (1999).mkv
    The Return of the Pink Panther (1975).mkv
    Day.of.the.Dead.1985.2160p.UHD.Blu-ray.Remux.DV.HDR.HEVC.FLAC.1.0-CiNEPHiLES.mkv
    Bowfinger.1999.2160p.UHD.Blu-ray.Remux.DV.HDR.HEVC.DTS-HD.MA.5.1-CiNEPHiLES.mkv
```

An unmatched row cannot add or remove a Plex badge, so these are a coverage
observation rather than a destructive-write risk — but they are named, not
summarised.

**2. Confirm the live apply path does not explicitly send
`additive_only: false`**, which would override the new API default. Checked in
the client rather than assumed:

```
frontend/src/lib/api/client.ts:635
  dvSyncLabels: (dryRun = false) =>
    request('/rename/dv-sync-labels', {
      method: 'POST',
      body: JSON.stringify({ dry_run: dryRun })   <- additive_only never sent
    })
```

A repo-wide search for `additive_only` across `.ts/.tsx/.js/.svelte/.html`
returns nothing outside the backend, and the function's only caller
(`frontend/src/routes/renames/+page.svelte:145`) passes `dryRun` alone. So the
UI button inherits the safe default. The final dry-run response remains the
authoritative proof of the live path.

## Round 3: sign-off, and the caveat it left

Head `7260499` reviewed. **All four gates PASS.** Execution remains HOLD for one
reason only: the probe is incomplete, so the final population will differ from
the reviewed one.

The reviewer left one caveat, and it was a fair hit. The script printed

```
staged rows UNMATCHED: N  <- stop condition if unexplained
```

and then **did not stop**. The explained/unexplained distinction lived in human
review, not in code — so a genuinely new mismatch (a systematic rewrite failure
of the 2026-07-11 Y:-drive kind) would have printed among the examples and been
waved past by exactly the reader most likely to skim it.

Now encoded. `EXPLAINED_NO_PLEX_TARGET` maps each known non-target to a reason,
every unmatched row is printed **with its reason**, and any row without one
exits 1:

```
Hamilton.2020…-CiNEPHiLES.mkv          raw release name; Plex never matched it
Day.of.the.Dead.1985…-CiNEPHiLES.mkv   raw release name; Plex never matched it
Bowfinger.1999…-CiNEPHiLES.mkv         raw release name; Plex never matched it
Notting Hill (1999).mkv                on disk, absent from the Plex library
The Return of the Pink Panther (1975)  on disk, absent from the Plex library
zero UNEXPLAINED mismatches -> gate 2 invariant holds
```

Mutation-checked: removing one entry from the allowlist makes the run exit 1;
restoring it returns exit 0. So the gate is the invariant the reviewer asked
for — **zero unexplained mismatches, not a coverage percentage** — and adding a
new exception is now a deliberate act rather than an omission.

## FINAL SET — probe complete, all four gates re-run

The bounded probe finished: **2,738 movies, 716 FEL (26%), 0 errors, 109
minutes**. All four gates re-run against the frozen final artifact, per the
sign-off condition. The earlier 694/689 figures are left above deliberately so
the signed-off population stays visible.

```
GATE 1  staged rows            716      histogram {'fel': 716}
        not-proven-FEL staged    0      (2,022 negatives, none staged)
GATE 2  Plex paths indexed   86,041     positive control PASS
        matched                711
        unmatched                5      zero UNEXPLAINED -> invariant holds
GATE 4  snapshotted            711      currently labelled: 0
        replacements             0      removals: 0
ACCOUNTING  Plex-effect rows   711  ==  rollback population 711
SCRIPT EXIT CODE                 0
```

**The five unmatched are the same five**, still individually explained — no new
mismatch appeared as the population grew from 694 to 716, which is the outcome
that would have indicated a systematic rewrite failure. Expected mutation set:

```
+711 DV FEL
   0 replacements
   0 removals
```

Against the reviewer's ten final-run acceptance criteria: 1–6 and 8–9 are
satisfied by the run above. **7 (apply path additive_only=True) and 10 (nothing
changes between snapshot and execution) are deployment-time conditions**, not
staging ones — criterion 7 in particular cannot be satisfied until this branch
is deployed, because the running container still carries the old destructive
default. The ordering that resolves this is section 8 of
`dv-scan-deploy-checklist.md`: deploy, verify the deployed default, re-snapshot,
dry-run, then apply.

## What is still outstanding

The probe is not finished: 694 positives from **1,977 of 2,740** titles. The
final set will be larger, and **all four gates will be re-run on the complete
set** before Jesse is asked to approve. This document will be updated with
those final numbers rather than replaced, so the figures you signed off on stay
visible.

**The question back to you:** given gate 4 shows 689 pure additions with zero
replacements, and gate 2 shows 99.3% matched with the remainder individually
explained, do you consider the four gates satisfied?
