# Library Duplicate Sweep ("Media Manager") — design

**Date:** 2026-08-21 · **Status:** design approved by Jesse, NOT implemented
**Origin:** infra session (Docker Apps), handed off to the ScanHound thread so the two don't cross wires.

## Problem

ScanHound compares duplicates only at *download-conflict* time: a rename job hits an occupied
destination, and the Compare modal ranks the incoming file against the library copy. There is no way
to ask "where are the duplicates I already have?" across the library as a whole.

Measured on the live library 2026-08-21 (via Plex's database, read from an offline snapshot copy):

| Measure | Value |
|---|---|
| Real local files Plex indexes | ~87,210 (another 175,486 rows are online extras with no file) |
| Total local library size | ~571 TB (390 TB local drives + NAS shares) |
| Items with >1 local file version | **4,483** |
| Exact byte-size matches | 1,365 GB across 308 items / 490 redundant copies |
| **Identical filename AND size (safest)** | **502 GB across 109 groups** |

Biggest single wins: `Dark Angel (1990).mkv` 54.45 GB x2, `The Firm (1993).mkv` 53.04 GB x2.

Several drives are at capacity (`P:` 2.1 GB free of 26 TB; `I:` 12 GB; `E:`/`H:`/`Y:` under 45 GB),
so recovered space has somewhere it is needed.

## Decisions (Jesse, 2026-08-21)

1. **Propose, never decide.** Every removal is manually selected by the user. The sweep surfaces
   candidates and recommends, exactly like the existing advice-only star. No automatic action, and
   explicitly NO baked-in resolution-tier policy — the user judges each case.
2. **Rank the worklist by space recovered, descending.** The payoff is extremely uneven; the top ~50
   decisions recover more than the remaining 4,000. The user stops whenever they like.
3. **Discover via the Plex HTTP API**, not the database file and not an independent filesystem scan.
   Reasons: it cannot lock or corrupt a database that already has documented write-contention on this
   server; it needs no bind mount into Plex's AppData; it survives Plex schema changes; it works the
   same whether Plex is local or on the NAS. Cost: minutes rather than seconds for ~87k files, and a
   Plex token in ScanHound config.
4. **Removal = move to recoverable trash, then tell Plex to refresh.** Reuses the established
   trash-not-delete semantics and `undo()`; the Plex refresh stops the library listing a copy that no
   longer exists. Note trashed files still occupy disk until the trash is emptied.

## Machinery to reuse (do NOT rebuild)

- `_quality_score` (`backend/rename/conflicts.py`) — returns the comparable tuple
  `(resolution_rank, dv, dv_layer_rank, hdr, source_rank, audio_rank, edition)`;
  resolution ranks `2160p=5, 1440p=4, 1080p=3, 720p=2, 480p=1`. Pure string/field heuristics, no I/O.
- `rank_conflict`, `conflictSummary()` (one-line differing-axes diff), and the Compare modal's
  `specRows().better` per-axis winner highlighting.
- `find_library_duplicate` — same title elsewhere in Plex, matched by imdb or title+year.
- ffprobe spec extraction, and the FEL/MEL gate that runs `dovi_tool` ONLY when the DV layer is the
  sole tiebreaker.
- `replace_library_dup` apply strategy + `conflict_replaced_path` for exact-path undo.

## THE key design tension

`_quality_score` is a **lexicographic tuple with resolution first**, so a 4K copy beats a 1080p copy
on the first element regardless of every other axis. That is correct for its original job (pick one
winner for a download conflict) and WRONG as a library sweep default, where a 4K and a 1080p copy of
the same film commonly coexist on purpose — the 1080p is what plays on devices that would otherwise
transcode, and this server's GPU already peaks near capacity, so forcing transcodes has real cost.

Jesse's resolution: **do not encode a tier policy at all.** Surface the tier plainly, rank within the
group using the existing engine, and let the user decide whether a cross-tier pair is redundant. The
sweep must never present "delete your only 1080p" as a recommendation — but it also must not silently
hide cross-tier pairs, because sometimes they ARE waste.

Implementation note: the sweep needs *grouping* semantics the conflict path lacks. Suggest computing
the tier from `resolution_rank` and displaying groups partitioned by tier, while still ranking with
the unmodified `_quality_score` inside each partition. Avoid editing `_quality_score` itself — it has
regression tests that index into tuple positions (see the docstring's note on `dv_layer_rank`
placement) and is shared with the conflict path.

## False positives found while prototyping the query (must be handled)

Discovered by inspecting real results — each of these appeared in the top 8 by size:

- **Split-part files.** `Is This Thing On (2025).CD1.mkv` and `.CD2.mkv` are 33.97 GB EACH and are
  two halves of one film, not duplicates. Any `CD\d`/`part\d`/`pt\d`/`disc\d` pattern must be excluded
  or flagged. (Only 3 rows / 79 GB in this dataset, but they sort straight to the top.)
- **Extended cuts.** `MASH - S07E04 - Our Finest Hour.ext.mkv` (1.47 GB) vs `...Our Finest Hour.mkv`
  (0.78 GB) — deliberate, not waste.
- **Multi-part episodes grouped under one Plex item**, e.g. a `Part.2` file scored as a sibling.
- **Same size, different name**, e.g. `20th Century Women (2016) [2160p].mkv` vs
  `20th Century Women (2016).mkv`, and `The Virgin Suicides (1999)` vs `(2000)`. Probably genuine
  duplicates from a re-download with different metadata — but they need human eyes, so they belong in
  a lower-confidence bucket, not the safe one.
- **DVD rips** (`VTS_01_0.VOB`, `.BUP`, `.IFO`) — Plex counts every VOB fragment as a separate version;
  one item showed 56 "versions". Only 27 items affected, but they must not be offered for removal.

Recommended confidence buckets, safest first:

1. identical filename + identical byte size, two paths  -> 502 GB, 109 groups
2. identical byte size, different filename              -> +863 GB, needs review
3. same item, different size (the rest of the 4,483)    -> judgement per case

## Out of scope for v1

Auto-apply of any kind · cross-library dedupe (movies vs TV) · re-encoding · anything that empties the
trash · a scheduled/recurring sweep. Get a manual, user-driven pass working first.

## Open questions for the ScanHound thread

- Where does this live in the UI — a new top-level "Media Manager" section, or an extension of the
  existing Renames/Compare surface?
- Does the sweep persist its results (a table of candidate groups + user decisions) so a review can be
  resumed across sessions, or is it recomputed each run? Ranked-by-size with 4,483 items argues for
  persistence.
- Plex API pagination/throttling shape for ~87k items, and where the token is stored.
- Does `find_library_duplicate`'s imdb/title+year matching generalise to a full sweep, or does the
  sweep need its own grouping keyed on Plex's `metadata_item_id`?

## Provenance

Numbers came from `com.plexapp.plugins.library.db` in the offline snapshot
`D:\PlexMaintenance\2026-08-20-pre-P1\` (never the live file). Query validated with a positive control
(found all 3 known-silent files) and a negative control (correctly excluded a known-good sibling).
Supporting CSV: `C:\DockerData\infra-ops\reports\plex-duplicate-twins-2026-08-21.csv` (798 rows).
Two earlier figures in the origin session were wrong and are corrected here: the "62,828 duplicates"
counted online extras with no local file, and "30 TB reclaimable" counted deliberate extended-cut and
resolution pairs. The defensible number is 502 GB.
