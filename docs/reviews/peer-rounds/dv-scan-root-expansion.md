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

---

## Review outcome (2026-08-14, same night)

**Decision (1) REVERSED per review — and the reviewer was right twice over:**

1. `plex_cache` holds **1,040 media parts under the BU directories** (the
   container mounts the Ulysses/Gagarin BU drive as a Plex library source), so
   BU scan rows would have been ACTIONABLE by the labeler, not inert. The
   "matches neither consumer" claim in this doc was wrong.
2. There are TWO BU folders (`4K Jefferson & Truman BU` AND
   `4K Ulysses & Yuri Gagarin BU`) — the whole-root config included both.

**Deeper finding: the original edit NEVER TOOK EFFECT.** `dv_library_roots` is
owned by the APP config (`/data/.config/scanhound/config.json`);
`app_service.py` exports `dv_host.json` from it, so the direct file edit was
regenerated away. Verify-the-write-not-the-survivor, again. The 7 PM run
scanned the old 4 roots; no BU scan ever happened.

**Applied correctly through the owner** (PUT /settings; qualification token
use explicitly authorized by Jesse for this one change): 13 roots — the
original 4, Magellan/4K, the three Y:/…/4K siblings, and FIVE enumerated
non-BU C:/4K Drives subdirs (`4K Columbo`, `4K Gambino`, `4K Quantum`,
`4K Rickover`, `4k HDR Arnold` — the last three plus Columbo were unknown to
the seed entirely). Verified: app reports 13; app-regenerated dv_host.json
carries 13; every root Test-Path OK; `dv_file_tagging=false` confirmed live.

**Decision (2)** (Y:/ spellings): approved as reviewed; the doc's
"protects seed rows" rationale is corrected per the review — the durable seed
evidence lives in `dv_seed_baseline`; the real reasons are namespace
consistency with the live scanner and the wrapper's verified mapping.

---

## Decision (1) REVERSED again, on evidence the review did not have (2026-08-15)

The reviewer required excluding `4K Jefferson & Truman BU`, and I widened that
to both BU folders. **Both exclusions were wrong, and so was the premise both
of us reasoned from: that "BU" meant backup.** Jesse challenged it and asked
for a Plex cross-check. Plex disagrees with the folder names:

| C:\4K Drives subdir | Plex parts | titles found NOWHERE else |
|---|---|---|
| 4K Columbo | 574 | 269 |
| 4K Gambino | 446 | 125 |
| 4K Quantum | 433 | 87 |
| 4K Rickover | 383 | 63 |
| 4k HDR Arnold | 323 | 49 |
| **4K Jefferson & Truman BU** | 586 | **95** |
| **4K Ulysses & Yuri Gagarin BU** | 454 | **58** |

All seven are Plex library sources, and the two BU drives hold **153 titles
that exist on no other drive**. Their internal layout is live-content shaped
(`Rickover BU 2 (8TB)/DV/`, `BU1 8TB/4K DV/`). The names are legacy.

Excluding them was therefore not conservative, it was harmful: those 153 titles
could never be labeled, and — per the confirmed `pick_layer` rule-2 finding —
any Plex movie merging a BU copy with another copy is pinned to `unknown`
forever, which blocks BOTH stale-label removal and the planned HDR10-only tag.
The risk the reviewer actually worried about (mkvpropedit writing into backup
files) is nil: `dv_file_tagging=false`, verified live again today.

(Title matching is normalized text, so a few counts either way may be off; the
magnitude is not in doubt.)

**Applied:** the five enumerated `C:/4K Drives/<subdir>` entries are replaced by
the single parent `C:/4K Drives` — 9 roots total. A hand-maintained subfolder
list went stale within one day *twice*; the parent root cannot. Verified at the
app (9 roots), at the regenerated `dv_host.json`, and by Test-Path on each.
`dv_library_roots` is a SEMICOLON-SEPARATED STRING (the detector splits on `;`
and newlines) — not a JSON array, which is worth knowing before editing it.

**Standing lesson for this file:** twice now the roots were set from folder
NAMES. The authoritative question is what Plex actually serves, and that is one
query away.
