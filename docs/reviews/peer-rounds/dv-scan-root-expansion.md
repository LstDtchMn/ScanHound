# DV scan-root expansion — review request (config change, live 2026-08-14)

`data/dv_host.json` is gitignored, so this production change has no diff to
review; this doc IS the review surface. Backup beside the live file
(`dv_host.json.bak-20260814`); revert = restore it.

## The change

`dv_library_roots` went from 4 roots (~730 files, all current) to 9:

    Y:/Movie 1 (14TB)/4K DV          (existing)
    Y:/Movie 2 (8TB)/4K DV           (existing)
    Y:/Movie 3 (2TB)/4K DV           (existing)
    //TURTLELANDSRV2/4K Magellan/DV  (existing)
    C:/4K Drives                     (NEW — local, ~2,616 files incl. subdirs)
    //TURTLELANDSRV2/4K Magellan/4K  (NEW — ~76)
    Y:/Movie 1 (14TB)/4K             (NEW — ~24, sibling of /4K DV, no overlap)
    Y:/Movie 2 (8TB)/4K              (NEW — ~76)
    Y:/Movie 3 (2TB)/4K              (NEW — ~19)

Why: ~2,286 library files had NO DV verdict (measured from the 6/30 seed
baseline); the biggest chunk on local disk. Every new root was
Test-Path-verified from the host BEFORE the edit — the wrapper fails closed on
any unreachable root (exit 11 stops ALL scanning), and that verification caught
that Colombo (the naive 5th candidate) no longer holds movies at all.

## The two decisions worth challenging

1. **`C:/4K Drives` includes `4K Jefferson & Truman BU` — a BACKUP folder**
   (~500+ files that are backup copies of primaries elsewhere). Scanning it:
   costs one-time budget (signature-skip makes rescans cheap), adds backup-path
   rows to dv_host.db and dv_scan. Consumer analysis says harmless (media
   inventory joins by rating_key/path; backup paths match neither) — but it is
   the same duplicate-rows-under-different-paths shape as the seed/UNC dupes.
   Alternative: enumerate the non-BU subdirs instead. I chose the whole root
   for simplicity and because a DV verdict on a backup copy arguably has
   integrity value. Challenge welcome.

2. **Path spellings are identity.** New Geronimo roots use Y:/ (matching the
   live scanner's existing rows), not the seed's UNC spelling — so live rows
   will NOT collide with seed rows, they form new identities alongside them,
   exactly like the existing 371 Y:/-vs-UNC pairs already proven harmless.
   The alternative (UNC spellings matching the seed) would overwrite seed rows
   in place but diverge from the mapped-drive identity the wrapper verifies.

## Not in scope

Colombo: repurposed to TV; its 265 seed titles reconciled against Plex — 257
present, ~4 real absentees after normalization, accepted by Jesse.
