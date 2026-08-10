# Double-check request: a ~1,260-row bounded-FEL write into the live label path

**Status:** NOTHING HAS BEEN WRITTEN. Jesse has held this pending your review.
Results are sitting read-only in `scratchpad/local_quickcheck.jsonl`.

## What is proposed

A bounded FEL sweep found (rate so far ~46–53%) that roughly 1,260 of 2,740
never-scanned 4K movies carry Dolby Vision FEL. The proposal is to write those
as `dv_layer='fel'` rows into `data/dv_host.db`, which `POST /rename/dv-import`
then pushes into `dv_scan`, which the label sync then turns into **~1,260 new
"DV FEL" labels in Plex, appearing at once** (the sync has not run since
2026-07-25).

Reviewing the branch first is the point — head `1f81d3e`,
`agent/dv-scan-hang-and-starvation`, base `main` `6813260`.

## Context you need

1. **Why bounded FEL is treated as final.** A bounded sample containing a FEL
   frame proves the title contains FEL; nothing later retracts it. The inverse
   does not hold, so `probe_fel_bounded()` returns a bool and only a positive
   short-circuits. Validated 22/22 against completed full passes. Gap stated
   honestly: no mixed `(MEL, FEL)` title appeared in that 22, so the MEL half
   is unvalidated by construction.

2. **These files are OUTSIDE the four configured roots.** They live on local
   drives on the scanning host. Plex refers to them by a *junction* path, not
   the drive letter, verified by volume GUID:

   | scan walked | Plex stores | volume |
   |---|---|---|
   | `A:\` | `C:\4K Drives\4K Gambino\` | `d4fb2889…` |
   | `E:\` | `C:\4K Drives\4K Columbo\` | `8c46128d…` |
   | `I:\` | `C:\4K Drives\4k HDR Arnold\` | `c64c991b…` |
   | `J:\` | `C:\4K Drives\4K Jefferson & Truman BU\` | `04eca07e…` |
   | `Q:` `R:` `U:` | matching junctions | GUIDs match |
   | `G:\` | `G:\` (no junction; Plex uses it directly) | — |

   So every row would be written with its path **rewritten to the Plex form**.
   `dv_paths.DEFAULT_DV_MAPPINGS` currently contains only
   `Y: <-> \\TURTLELANDSRV2\4K HDR Geronimo`.

## The four things to check

**Q1 — Is the FEL-positive-only rule airtight for a MASS write?**
One wrong row is a wrong badge on a real movie. Is there any path by which a
bounded MEL / none / P5 / P8 could reach `dv_scan` through
`probe_fel_bounded` → `detect_layer` → `classify_to_row` → `_upsert` →
`import_dv_host_db`?

**Q2 — Does the path rewrite actually match, or does it only look like it?**
`normalize_path()` unifies separators and casefolds, then applies
longest-prefix rewrites from `DEFAULT_DV_MAPPINGS`. If a row is written as
`C:\4K Drives\4K Gambino\...` and Plex reports exactly that, no mapping entry
is needed at all — is that right, or is there a case/UNC/trailing-segment
detail that makes it fail the way the 2026-07-11 dry-run failed for all 371
Y:-drive files? **This is the failure mode I most want a second pair of eyes
on**, because it fails SILENTLY: unmatched rows produce no badge and look
exactly like "the feature is broken".

**Q3 — Multi-part titles and backup copies.** Some titles exist on both a
primary and a backup drive; the sweep writes ONE row per distinct title.
`pick_layer` aggregates all parts of a title and its rule 2 says a part that is
`unknown` OR HAS NO ROW makes the aggregate `unknown`. Does writing one part's
FEL row while a sibling part has no row give FEL (rule 1 wins) or `unknown`
(rule 2)? Reading the code, rule 1 short-circuits first — please confirm,
because if rule 2 wins the entire write accomplishes nothing.

**Q4 — Blast radius if wrong.** Labels are only ADDED here, never removed
(`desired_label` returns None for none/unknown, and `is_authoritative` gates
removal). Is that enough to make this reversible in practice, or is there a
removal path I have not walked?

## What I am NOT asking

Whether the throughput analysis holds — that is the other document
(`dv-scan-hang-and-starvation.md`) and its own review. This one is only about
whether it is safe to write ~1,260 rows into the live label pipeline.

If you cannot fetch the branch, STOP and say so rather than answering from this
summary.
