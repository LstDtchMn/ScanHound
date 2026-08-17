# 2B — Duplicate / best-version poster labelling

**Status:** SPEC, not built. Written 2026-08-17 after 2A (normalized-path
collision fix) merged, which was its prerequisite.

**Ask (owner):** the poster should indicate a duplicate exists, and the cover
should carry the **best** version's tags. He keeps multiple versions
deliberately — this is a feature, not cleanup.

Every number below was measured against the live database, not carried from the
earlier handoff. **Where they disagree, this document is right and the handoff's
§2B is wrong** — see §2.

---

## 1. What is actually there

```
plex_cache movie rows                       16,332
distinct movies                             15,250
MULTI-VERSION movies                         1,032     983 x2 · 48 x3 · 1 x4
```

Within a multi-version movie, what differs:

```
size          975  (94%)
DV (boolean)  265  (25%)
HDR           225  (21%)
resolution     61  ( 5%)
```

`plex_cache` holds `res, size, dovi, hdr, media_id, file_path` **per version**,
so ranking is local — no Plex round trip per poster.

**`size` is stored in GIGABYTES, not bytes** (median 22.44, max 156.51). Worth
stating because reading it as bytes makes every version look like 0.0 GB, which
is exactly the wrong-instrument trap this project keeps paying for.

**There is no duration column, so true bitrate cannot be computed locally.**
Size is the available proxy and is sound *within* one movie, where runtime is
equal — but see §2 for why it must not be the primary key.

## 2. The handoff's premise is wrong where it matters most

The handoff said *"bitrate differs in 95% — that is the discriminator"*. True as
arithmetic, and wrong as a ranking rule, because it fails precisely on the
movies the feature exists for:

```
movies where DV differs between versions            265
  biggest version IS the DV one                     206
  biggest version is NOT the DV one                  59   <-- size ranks these wrong
```

```
Avatar            4K/66.7GB          vs  4K/60.1GB DV
The Dark Knight   4K/54.3GB HDR      vs  4K/53.8GB DV     (0.5 GB apart)
Dredd             4K/50.9GB HDR      vs  4K/50.9GB DV     (IDENTICAL size)
Companion         4K/46.6GB HDR      vs  4K/17.3GB DV
```

Dredd settles it: both versions are 50.9 GB, so size cannot rank them at all and
a size-first rule would silently drop the DV badge.

Further:

```
movies whose versions are within 10% on size        203
  ...of which DV or HDR still differs               102   <-- size cannot rank these either
```

**Owner decision (2026-08-17): format first, size as tie-break.** Rationale: you
press play on the DV copy, not on whichever file is 6 GB larger.

## 3. Two questions the measurement closed

**Does resolution conflict with format?** No.

```
movies where resolution differs                      61
  ...where the HIGHEST-res version has the weaker format:  0
```

Resolution and format never disagree in this library, so their relative order is
unobservable today. Put resolution first anyway — it is the intuitive reading
("4K beats 1080p") and it costs nothing, since it changes no current outcome.

**Do DV LAYERS differ between versions?** Yes, in 29 movies — so a bare
`dovi` boolean is too coarse:

```
The Crimson Rivers   4K/53GB FEL   vs  4K/70GB MEL     <-- SMALLER file, RICHER format
Black Phone 2        4K/20GB P8    vs  4K/70GB FEL
Ballerina            4K/69GB FEL   vs  4K/13GB P5
Alien: Romulus       4K/21GB P8 · 4K/50GB unknown · 4K/54GB FEL
```

The layer comes from `dv_scan` joined on the normalized path — the join 2A
fixed. **Coverage is sparse and the spec must not pretend otherwise:**

```
multi-version movies with ANY version matched to dv_scan     429
  with >=2 versions carrying a KNOWN layer                    35
  whose layers DIFFER between versions                        29
```

So layer ranking applies to a small, high-value minority; everything else falls
back to the boolean.

## 4. The ranking rule

For the versions of ONE movie, take the first that separates them:

```
1. resolution         4K > 1080p > 720p > unknown
2. DV layer, if KNOWN for both        fel > mel > profile8 > profile5
3. DV present > HDR present > neither (the boolean fallback)
4. size, larger wins
5. otherwise: NO CLEAR BEST -- see below
```

Reuse `dv_labeler._LAYER_RANK` for step 2 rather than declaring a second
ordering; two rankings that can drift apart is how the DV work already went
wrong once.

**`unknown` is not a layer.** A failed scan must never outrank a real finding,
and must never be treated as absence of DV — the same rule 2A enforces. A
version whose layer is `unknown` falls through to step 3 on its boolean.

**`conflict` likewise.** If 2A reports a conflicting layer for a file, that file
cannot be ranked on layer; fall through.

### No clear best

When two versions tie all the way through, the movie has no best version. Say so
rather than picking arbitrarily: tag the duplicate marker, and carry the UNION
of formats, which is today's behaviour and fails safe. This is the honest answer
for the ~101 movies differing only by <10% of size.

## 5. What goes on the poster

Two separate things, and they should not be conflated:

* **A duplicate marker** — this movie has more than one version. 1,032 movies.
* **The best version's tags** — DV FEL / DV MEL / DV8 / DV5 / DV7 / DV / HDR10,
  from the winner of §4, not the union.

**On current data this REMOVES NOTHING. Measured, after the question was raised
and before it was believed:**

```
multi-version movies                                          1,032
  would LOSE the HDR10 badge under §4 ranking                      0
  would LOSE a DV badge under §4 ranking                           0
```

The reasoning, since a zero is worth explaining rather than trusting. Today's
rule is `MAX(hdr) GROUP BY rating_key`: a movie carries HDR10 if ANY version is
HDR, even where another version is SDR. Under §4, HDR outranks SDR at step 3, so
the HDR version wins and keeps the badge. The only way to lose it is a
higher-RESOLUTION SDR version beating a lower-res HDR one at step 1 — and §3
measured zero movies where the highest-res version has the weaker format.

So best-version tagging currently only ever ADDS or KEEPS badges.

**That is a property of today's data, not a guarantee of the rule.** Import one
4K SDR remux of a film you own in 1080p HDR and the case appears immediately.
The implementation must therefore still treat badge removal as destructive and
carry the DV labeler's `may_remove` discipline — an unknown or unscanned version
must never be read as "not HDR" and used to strip a correct badge. Build it as
if removals happen; verify by measuring that they currently do not.

## 5b. The duplicate marker — DECIDED, and buildable NOW

**Owner decision 2026-08-17: show the COUNT, not a binary marker.**

Kometa overlays in `docs/kometa/dv_badges.yml` are label-gated with fixed text:

```yaml
overlays:
  DV FEL:
    overlay: { name: text(DV FEL), ... }
    plex_search: { all: { label: DV FEL } }
```

So a count needs one label per value. The library needs three, plus a guard:

```
2 Versions    983 movies
3 Versions     48
4 Versions      1
5+ Versions     0   <- catch-all, so a movie can NEVER go silently unbadged
```

The `5+` bucket is not speculative padding. Without it, importing a fifth
version produces a count with no label and no overlay, and the poster silently
loses its badge — a failure that looks exactly like "this movie has one version".

**This half does NOT depend on §6.1.** Counting rows per `rating_key` is
reliable; only the BEST-VERSION half needs to identify individual versions. The
marker can ship first.

### It must NOT join `MANAGED`

`reconcile_movie` computes `removed = existing_managed - desired_set`, and
`desired_set` comes from `desired_labels(layer)`, which knows only DV. Adding
`2 Versions` to `MANAGED` would therefore make **the DV sync strip it on every
run** — the identical trap `RETIRED_LABELS` exists to document.

Version labels need their own closed set and their own reconcile pass, sharing
the DV labeler's discipline but not its vocabulary:

* removal only against an authoritative count (a Plex read that succeeded);
* a failed or empty `plex_cache` read must never be read as "one version" and
  used to strip a badge — absence of evidence again;
* the closed set is `{2,3,4,5+} Versions`, so a user's own labels are untouched.

### Poster placement

Every existing badge draws at `horizontal_align: left, vertical_align: top` with
the same offsets, and the file explicitly warns that adding another there stacks
overlapping labels. The version badge needs its own corner — and 1,032 of these
movies already carry a DV or HDR10 badge in that top-left position, so this is
the common case, not the edge case.

## 6. Open questions before building

1. **Match at version level, never by `rating_key` alone** (round-1 review
   guidance, and it stands): a 1080p SDR and a 4K DV copy share a movie id and
   need different tags. What is the version-level key — `media_id`? **6 of 1,032
   multi-version movies have non-distinct `media_id`s**, so it is not reliably
   unique and that must be resolved first.
2. Does the duplicate marker belong in the managed label set (so it is removed
   when a duplicate goes away), or is it a Kometa overlay driven by a count?
   `MANAGED` is a closed set for a reason — adding to it hands the labeler
   permission to remove it.
3. `RETIRED_LABELS` precedent: if the HDR10 semantics change, old labels strand
   unless the migration is explicit.

## 7. Not yet verified

* That `media_id` is the right version key (see §6.1).
* Whether Kometa can render a "duplicate exists" marker from a label alone, or
  needs a separate overlay definition. `docs/kometa/dv_badges.yml` is the
  precedent to read first.
* Nothing here has been tested against a dry-run sync.
