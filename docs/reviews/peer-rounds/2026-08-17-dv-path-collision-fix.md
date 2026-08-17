# Review round — normalized-path collision fix (dv_labeler)

**Date:** 2026-08-17
**Scope:** `backend/rename/dv_labeler.py`, `tests/test_dv_path_collision.py`
**Asking for:** a check on the resolution rule, and on one live measurement that
**contradicts** what the 2026-08-17 handoff says about this bug.

---

## 1. The bug

Several `dv_scan` rows can normalize onto one key — the same file recorded under
a drive letter and its UNC share, under different separators or case, or stored
twice under two spellings. All three indexes in `dv_labeler` were built with a
last-write-wins assignment in a loop:

```python
for r in rows:
    p = normalize_path(r.get("path"), mappings)
    if p:
        idx[p] = r.get("dv_layer")        # last row wins, silently
        norm_to_path[p] = r.get("path")
```

`get_dv_scans()` orders `last_seen_at DESC`, so the **oldest** row wins.

## 2. The rule implemented

Depends only on the SET of layers observed, so it is permutation-invariant:

```
exactly one distinct authoritative layer (+ any failures) -> that layer
no authoritative layer at all                             -> the failure value
two or more DIFFERENT authoritative layers                -> CONFLICT
```

`none` is authoritative (the detector ran, found no DV). `unknown`/NULL is a
failed detection and is never evidence.

Per the spec, **not** used to arbitrate: `last_seen_at` (a failed scan preserves
the old layer while advancing the timestamp, so it dates the observation, not
the layer) and `_LAYER_RANK` (it ranks the parts of one title, not two
observations of one file).

All three call sites — `build_index`, `build_index_and_paths`, and the
`seed_index` comprehension — now share `_index_by_normalized_path`.

## 3. Design decision I want checked

**A conflict is emitted as `unknown`, and the key STAYS in the index.**

The alternative — omitting the conflicting key — looks equivalent and is not.
Traced through the consumer:

| | key present, `unknown` | key omitted |
|---|---|---|
| `pick_layer`, multi-part title | `unknown` | `unknown` |
| `pick_layer`, **single-part title** | `unknown` | `None` ("not our title") |
| `reconcile_movie` `may_remove` | **False** (layer is `LAYER_DETECTION_FAILED`) | `authoritative or not additive_only` → **True under a full reconcile** |

So omitting the key would let a contradiction **strip managed labels** from a
single-part title whenever someone ran a non-additive reconcile. Keeping it as
`unknown` routes the case through the existing failure path, where
`may_remove=False` and `matched=False`, and `matched` is what gates the
`rating_key` back-write in `sync_labels`. Both spec requirements — a conflict
must neither remove a label nor back-write a `rating_key` — then hold without
any new branch.

`tests/test_dv_path_collision.py::test_conflict_never_removes_a_label_under_full_reconcile`
is the test that distinguishes the two designs; it is the only one that does.

## 4. Live measurement — this contradicts the handoff

The handoff §2A says: *"311 normalized keys ... All 311 currently resolve to the
real layer — accidentally."*

**That is not what the live data shows.** Measured by calling the deployed
`build_index_and_paths` from `/app` and the candidate
`_index_by_normalized_path` on the *same* row list, in the same process, DB
opened `mode=ro`:

```
rows (source='scan')                       6,937
normalized keys                            4,725
colliding keys (>1 row)                    2,212
true conflicts (>=2 authoritative layers)      0
keys whose LAYER changes                     335   <-- all 'unknown' -> authoritative
    'unknown' -> 'none'                      163
    'unknown' -> 'profile8'                   60
    'unknown' -> 'mel'                        49
    'unknown' -> 'fel'                        49
    'unknown' -> 'profile5'                   14
keys whose back-annotation PATH changes    1,177
```

So the old code is not "accidentally right" on these — it is **currently wrong**
on 335 keys, resolving them to `unknown` when a real layer exists. 172 of those
are layers that produce a badge.

**Do these correspond to real Plex titles?** Joined against `plex_cache`
(`file_path` is Plex's own reported path, per `rename/service.py:2056`) using
the same `normalize_path`:

```
all 335 changed keys present in plex_cache:  335/335, library 'Movies (4K HDR)', is_tv 0
172 badge-gaining keys present:              172/172
control: unchanged keys sampled 2,000        777 present  (join works; not vacuous)
```

The 100% vs 39% asymmetry is *expected* and is the mechanism, not an artefact:
being in Plex is what produces the second path spelling in the first place.
**Flagging this as inferred, not measured.**

**Are they visibly missing the badge today? UNKNOWN — and I could not
establish it.** Removal is blocked for `unknown`, so a title badged before its
duplicate row appeared would still carry the badge, and the count of titles
*currently* unbadged could be anywhere from 0 to 172.

I tried to estimate it from `dv_scan.rating_key`, on the theory that
`sync_labels` back-writes it only when `matched=True` (which requires an
authoritative layer, and labels are added in that same pass), so its presence
would evidence a past successful label pass. **That theory is wrong and the
estimate is withdrawn:** `plex_metadata_scan.py:301` also calls
`upsert_dv_scan(..., source="scan", rating_key=item.get("rating_key"))`, so a
`rating_key` on a `source='scan'` row can come from the metadata scanner and
says nothing about labelling. For the record, the numbers were 134 of 172 with a
`rating_key` against a 703-of-733 baseline; they do not mean what I first said
they meant.

**The only thing that answers this is asking Plex what labels those 172 titles
carry.** Not done — it is a live Plex read, and it is the obvious next step.
Nothing in the fix depends on the answer; it changes only how the fix is
*described*.

### Side finding

`unknown` counted as **rows** is 5,584; as **distinct files** after collapsing
duplicate spellings it is **3,566**. The DV detector monitor reports the row
count. (My independent row count came out 5,584 against the monitor's 5,583,
measured minutes apart while the detector was running — same population.) This
does not explain `otherErr=1814`, which remains uninvestigated.

## 5. Test evidence

`tests/test_dv_path_collision.py` — 12 tests, every case asserted in **both**
row orders, because the old code passes one order of each pair.

Mutation-tested: reinstating last-write-wins inside `_collapse_observations`
(inserted by line number after asserting the anchor was unique) makes **10 of
12 fail**. The 2 that survive are the positive control (the two spellings really
do collide) and the no-collision regression test — both are supposed to be blind
to this bug.

Full suite run in a throwaway container from `scanhound:latest` with the source
tree copied in, against a same-session `HEAD` baseline built the same way.

## 6. What I would like checked

1. Is emitting `unknown` for a conflict right, versus a distinct sentinel? A
   distinct value would require touching `is_authoritative`, `desired_labels`,
   `pick_layer` and `reconcile_movie`'s `may_remove` — four places to get wrong
   — for no behavioural gain I can see.
2. `min()` on the raw path among rows carrying the winning layer: chosen for
   order-independence. It changes which of two duplicate spellings gets the
   `rating_key` on 1,177 keys. Both spellings are the same file, and
   `annotate_dv_scan_rating_key` is `UPDATE ... WHERE path = ?` so it still
   matches a real row — but is there a consumer of `norm_to_path` I have missed?
3. Does the §4 contradiction hold? It is the number that changes what this fix
   is *for*, and the handoff's §4 lists four retractions from exactly this kind
   of measurement.
