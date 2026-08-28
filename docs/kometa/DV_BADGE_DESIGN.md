# Dolby Vision badge design — a proposal, not a deployed config

**Status:** design candidate. **Not deployed. Not deployable as written.**
**Deployed reality last observed:** 2026-08-26, by reading the running `kometa`
container.

This file replaces `docs/kometa/dv_badges.yml`. It was renamed out of `.yml`
deliberately: a YAML file sitting in a folder called `kometa/` looks like
something you drop into Kometa, and a warning comment inside it is a weaker
signal than the filename. It had already misled one developer.

---

## What Kometa actually runs

Kometa loads `/config/dv-layer.yml`, referenced from `/config/config.yml` line
79. That is **owner-observed runtime state**, not something this repository
controls or can verify — it lives on the host, outside version control.

| | deployed `/config/dv-layer.yml` | this design |
|---|---|---|
| overlays | `DV FEL`, `DV MEL` | `DV FEL`, `DV MEL`, `DV8`, `DV5`, `HDR10`, 2 retiring |
| rendering | **image** — pre-rendered PNG pills | **text** — `text(...)` |
| anchor | top-**RIGHT**, offsets 15/15, 250×96 | top-**LEFT**, offsets 15/15 |
| images present | `dv-fel.png`, `dv-mel.png` | n/a |

Recorded here so the divergence can be compared without a container. **It will
go stale**, and nothing in this repo can detect that.

## Why the warning is at the top

A developer read the old YAML as a description of what renders today, placed
the version-count badges top-RIGHT to *clear* the DV badge, and shipped them
drawing at exactly the DV badge's coordinates — because the real badge is
top-right too.

`tests/test_version_labeler.py` takes its `DV_TOP, DV_HEIGHT = 15, 96`
constants from the **deployed** file for that reason, with a comment saying the
repo copy could not be trusted. The warning existed in a comment; the
misleading artifact stayed. Now the artifact itself is not mistakable for a
config.

## The open gap — a decision, not a chore

ScanHound applies `DV8`, `DV5` and `HDR10` to Plex today. **Kometa renders
nothing for them**, because the deployed design uses pre-rendered PNGs and only
two exist.

Closing it needs one of:

1. three more 250×96 images for the deployed image-based design; or
2. adopting this text design wholesale, accepting the font-resolution
   variability the image design was chosen to avoid.

That is Jesse's call. Nothing here should be copied into Kometa before it is
made.

## Terminology — this design covers a SUBSET

`backend/rename/dv_labeler.py` defines:

```python
MANAGED = _LAYER_LABELS | {"DV7", "DV", HDR10_LABEL} | RETIRED_LABELS
```

which is nine labels. The four this design badges — `DV FEL`, `DV MEL`, `DV8`,
`DV5` — are the **layer-badge subset**, not the whole managed set. An earlier
test called them "the managed set", which was wrong.

`DV7` and `DV` are deliberately never badged: they are group tags for
**filtering** (collections, smart filters, "all Profile 7"), where one tag
beats enumerating four. Every block below draws at the same corner, so badging
them beside a layer badge would stack overlapping labels on one poster.

`HDR10` is badged here because those titles carry no DV label, so it cannot
collide.

---

## The candidate

Adopting this means moving every DV badge to the top-left and switching from
images to text. Do not copy it piecemeal — the whole design is one anchor.

```yaml
overlays:
  DV FEL:
    overlay:
      name: text(DV FEL)
      horizontal_offset: 15
      horizontal_align: left
      vertical_offset: 15
      vertical_align: top
      font_color: "#FFFFFF"
      back_color: "#00000099"
      back_width: 200
      back_height: 80
      back_radius: 30
    plex_search:
      all:
        label: DV FEL

  DV MEL:
    overlay:
      name: text(DV MEL)
      horizontal_offset: 15
      horizontal_align: left
      vertical_offset: 15
      vertical_align: top
      font_color: "#FFFFFF"
      back_color: "#00000099"
      back_width: 200
      back_height: 80
      back_radius: 30
    plex_search:
      all:
        label: DV MEL

  DV8:
    overlay:
      name: text(DV8)
      horizontal_offset: 15
      horizontal_align: left
      vertical_offset: 15
      vertical_align: top
      font_color: "#FFFFFF"
      back_color: "#00000099"
      back_width: 200
      back_height: 80
      back_radius: 30
    plex_search:
      all:
        label: DV8

  DV5:
    overlay:
      name: text(DV5)
      horizontal_offset: 15
      horizontal_align: left
      vertical_offset: 15
      vertical_align: top
      font_color: "#FFFFFF"
      back_color: "#00000099"
      back_width: 200
      back_height: 80
      back_radius: 30
    plex_search:
      all:
        label: DV5

  HDR10:
    overlay:
      name: text(HDR10)
      horizontal_offset: 15
      horizontal_align: left
      vertical_offset: 15
      vertical_align: top
      font_color: "#FFFFFF"
      back_color: "#00000099"
      back_width: 200
      back_height: 80
      back_radius: 30
    plex_search:
      all:
        label: HDR10

  # TRANSITIONAL, remove once converged. Peer review 2026-08-15 recommended a
  # both-names window for the DV P8 -> DV8 / DV P5 -> DV5 rename:
  #   1. Kometa accepts old AND new names   <-- these two blocks
  #   2. run ScanHound to convergence (it removes the old labels)
  #   3. verify a clean Kometa pass
  #   4. delete these two blocks
  # Without them, ~220 Profile 8/5 posters show no DV badge between ScanHound's
  # first post-rename sync and Kometa's next run. Harmless while both exist: a
  # title carries either the old or the new label, never both, because
  # ScanHound removes the retired name in the same pass it adds the new one.
  DV P8 (retiring):
    overlay:
      name: text(DV8)
      horizontal_offset: 15
      horizontal_align: left
      vertical_offset: 15
      vertical_align: top
      font_color: "#FFFFFF"
      back_color: "#00000099"
      back_width: 200
      back_height: 80
      back_radius: 30
    plex_search:
      all:
        label: DV P8

  DV P5 (retiring):
    overlay:
      name: text(DV5)
      horizontal_offset: 15
      horizontal_align: left
      vertical_offset: 15
      vertical_align: top
      font_color: "#FFFFFF"
      back_color: "#00000099"
      back_width: 200
      back_height: 80
      back_radius: 30
    plex_search:
      all:
        label: DV P5
```

## Ordering, if this is ever adopted

Kometa applies overlays on its own schedule. Run it **after** ScanHound's host
detector walk → `POST /rename/dv-import` → `POST /rename/dv-sync-labels`. See
`scripts/host-detector/README.md` for the full ordering and the rollout gate
that must be cleared before the first real label sync.

The label strings must equal ScanHound's `dv_label_vocab` values exactly, or
nothing badges. `tests/test_kometa_dv_badges.py` checks that every label this
design gates on is one the labeller actually applies.
