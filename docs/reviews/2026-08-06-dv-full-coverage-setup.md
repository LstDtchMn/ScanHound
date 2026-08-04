# Dolby Vision full-coverage scan — setup steps

**Decided 2026-08-06:** scan **every directory in the Plex `Movies (4K HDR)` library**, not just the
folders named "4K DV", and let the **nightly job** work through the backlog rather than running a
marathon. This document is the ordered checklist, with the one blocking dependency first.

## Why this is needed

The detector's configured roots are four folders literally named `4K DV` / `DV`, so it has only ever
seen 463 files. Measured against Plex's own file paths on 2026-08-06:

| Root | Files in the 4K library | Of which Plex flags Dolby Vision |
|---|---:|---:|
| `C:\4K Drives` | 3,199 | 1,659 |
| `\\TURTLELANDSRV2\4K HDR Geronimo` | 492 | 362 |
| `\\TURTLELANDSRV2\4K Magellan` | 354 | 272 |
| `G:\Movies 1` | 299 | 172 |
| **Total** | **4,344** | **2,465** |

Scanning everything (not only Plex's DV-flagged set) is deliberate: Plex's flag is Plex's own
detection, and the point of this tool is to decide FEL vs MEL for itself.

## STEP 0 — BLOCKING. Do not scan before this is done.

`scripts/host-detector/dv_host_scan.py` imports `backend.rename.dv_detect` **from the working tree**
(`sys.path.insert` to the repo root, dv_host_scan.py:27-30). The working tree at
`X:\Docker Apps\ScanHound` is currently on `agent/hybrid-sweep-implementation`, whose `dv_detect.py`
still has the pre-fix ordering at line 153.

Running the scan before the fix lands would record **every read failure as an authoritative "no
Dolby Vision"** — the exact defect confirmed on Alien: Romulus — but now across 4,344 files instead
of 463, and each false verdict then feeds the labeler.

So: merge `agent/audit-fixes-2026-08` (or check it out in that working tree) **first**, and confirm
with:

```bash
grep -n "_NO_RPU_MESSAGES" "X:/Docker Apps/ScanHound/backend/rename/dv_detect.py"
```

That constant exists only in the fixed version. **No output means do not start the scan.**
(An earlier draft of this file checked for `explicit_no_dv`, an intermediate name that the round-2
review replaced — checking for it now would give a false negative.)

## STEP 1 — clear the ~15 stale rows (required part of shipping, per the 2026-08-06 decision)

13 rows read `none` and 2 read `unknown` in `dv_scan` (source='scan'). Some are false negatives
produced by the old behaviour. They must be cleared so the corrected detector re-decides them rather
than skipping them on their stored signature:

```sql
DELETE FROM dv_scan WHERE source='scan' AND dv_layer IN ('none','unknown');
```

Run against `/dbvol/crawler.db` inside the container. The host detector will re-scan those files on
its next pass because the row (and its signature) is gone.

Selected **by value, not by a remembered count of 15** — peer review's point, and the right one: the
count is a snapshot, and a row written under the old detector cannot be told apart from a good one
by anything except its value. Deleting every `none`/`unknown` scan row is a deliberate superset of
"rows written before the fix": it re-scans a handful of genuinely-no-DV files, which costs minutes
and removes the need to trust a timestamp boundary. Nothing else is touched — positive findings
(fel/mel/p8/p5) stay, so no correct label is disturbed.

## STEP 2 — widen the scan roots (Settings → Renaming → DV detection)

Replace `dv_library_roots` with the four top-level roots, semicolon-separated:

```
C:/4K Drives;//TURTLELANDSRV2/4K HDR Geronimo;//TURTLELANDSRV2/4K Magellan;G:/Movies 1
```

**UNC rather than the `Y:` mapped drive, on purpose, for two reasons:** a scheduled task frequently
cannot see per-user drive mappings, and `backend/rename/dv_paths.py` records that Plex serves these
files under the UNC form — so scanner paths and Plex paths then agree directly.

Saving in Settings re-exports `data/dv_host.json`, which is what the host script reads.

## STEP 3 — register the nightly task (needs an elevated PowerShell)

Per `scripts/host-detector/README.md`. Claude cannot do this: `RunLevel=Highest` requires elevation.
Today `Get-ScheduledTask` shows only `ScanHound Qualification Evidence` and
`ScanHound-MountNASShares` — the DV task documented in the README was never created, which is why
the inventory has been frozen since 2026-07-05.

## What to expect

Measured from the July run's own timestamps: **191 files on its best day**, ~13 files in its best
hour. At that rate the full 4,344-file library takes roughly **three weeks of nightly passes**, and
the ~2,465 DV titles are covered sooner since they are spread across all four roots. Already-scanned
files are skipped on their stored signature, so every night makes forward progress and nothing is
repeated.

Nothing here touches the failing X: mirror — all four roots are on C:, G: and the NAS.

## Known follow-up, not blocking

Every file gets a full `dovi_tool extract-rpu` pass, including the ~1,879 that are not Dolby Vision
at all. A cheap pre-filter (a fast probe for DV presence before the expensive extract) would cut the
run substantially. Worth doing only if the three-week figure turns out to matter.
