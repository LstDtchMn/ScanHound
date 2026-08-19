> **Reviewer note:** this document is CONTEXT — a plain-language account of an

> **Filename note.** This file is named `2026-08-06-...` but was written on
> **2026-08-04**. The name is kept because it was already relayed to the peer
> reviewer and renaming would break that path. Dates in the body below have
> been corrected to the real ones.

> overnight session, written for the repository owner. It is **not** a review
> artifact. The things to review are the code and tests on the three branches
> named in section 5, each of which carries its own relay block under
> `docs/reviews/`. Do not review this summary in place of them.

# Overnight run — what happened while you slept

**2026-08-03 evening → 2026-08-04 morning.** No prompting, as instructed. Nothing merged, nothing
deployed, nothing pushed to `main`. Three branches are ready for your review, each with a
paste-ready block for ChatGPT.

---

## 1. The short version

- **The peer reviewer's round-13 objections are all answered**, and two of them were real holes in
  code I had already called finished.
- **A full-program audit found 15 serious candidate defects.** Two were disproved, nine confirmed,
  **six fixed** — including one that is actively running in production right now.
- **The test-suite scare was a false alarm.** 36 failures overnight were all missing test tools in my
  container. With your CI's exact dependency list: **4788 passed, 0 failed.**
- **Two things I believed about your system were wrong**, and both deep dives corrected me. Details
  in section 4 — those matter more than the code.
- **One item is time-critical and needs you: 34 GB of trash gets permanently deleted in ~3 days**,
  and half of it can't be restored through the app.

---

## 2. The round-13 review closures — branch `agent/hybrid-sweep-rebased` @ `9ff626e`

ChatGPT said the branch wasn't merge-ready. It was right.

**The "empty pipe" problem.** I'd fixed two fields — which episode a multi-episode file ends at, and
whether a release uses the HEVC codec — by making the database able to *store* them. Nothing in the
running program ever *produced* either value. Both now have real producers, with the rules defined
first: an episode range is carried only when the filename actually says so, never invented from a
season pack's file count; HEVC is claimed only on an exact codec token, from one shared vocabulary
both the feed reader and the detail reader use.

**The mirrored-test problem.** A test meant to prove two scanning paths agree contained its own
private copy of the logic — so if the real code drifted, the test stayed green anyway. It's now one
named function that both the real scanner and the test call. Extracting it immediately exposed that
**the "rescan this item" button never ran the media-type logic at all**, so every rescanned item was
silently filed as "type unknown". Fixed and tested.

**One retraction.** I'd claimed the release year from the detail page was authoritative. It isn't —
that belongs to the feed. Recorded in the contract rather than quietly dropped.

Evidence: full suite **4788 passed / 0 failed / exit 0**, and this time the actual output is
*committed to the repo* (round 13 objected that a number in a chat message isn't evidence). The
mutation harness — which deliberately re-breaks each fix to prove the tests catch it — passes 9 of 9.

---

## 3. The audit: what was broken

Nine readers covered the database, scanner, RSS pipeline, API routes, rename/file operations,
DV/HDR labeling, frontend, infrastructure and security. Everything below was confirmed by reading
the actual code, then fixed red-first (test fails before the fix, passes after).

### Fixed

| What was wrong | What you'd have seen |
|---|---|
| **Dolby Vision detection failures were recorded as "this file has no Dolby Vision"** | DV films quietly losing their badge. Confirmed live victim: *Alien: Romulus* was recorded as "no DV" after a timeout on 2026‑07‑22 |
| **A failed detection then STRIPPED the DV labels** in the unattended hourly sync | DV badges disappearing from posters with no explanation |
| A failed detection also **overwrote a known-good answer** in the database | The good result lost permanently, not just skipped |
| The Trakt watchlist import called a function that doesn't exist | "Imported 0 items", always, reported as success |
| A scheduled scan finishing empty **wiped the screen of anyone browsing** | Your list, selection and open panel vanishing for no reason |
| Hydration wrote "year 0" over real release years | Wrong/blank years — and the check that should have caught it read 0 as "no year" |
| A failed bookkeeping write in "keep both" **armed a delete of the file you chose to keep** | Silent loss of the library copy on a later undo |

**The most important thing in this table is the pairing of the first two.** My DV detection fix makes
failures report honestly as "unknown" instead of falsely as "none" — but "unknown" was *exactly* the
value that triggered label stripping. Shipping the detection fix on its own would have made things
worse. They must go together, and they do.

### Confirmed but not yet fixed (fix sketches recorded)

- **A cached file probe freezes the DV layer** at its first value, so a later DV scan never surfaces.
- **A cache-rematch routine runs without the scan lock** and can corrupt an in-flight scan. *(Careful:
  the obvious fix — taking the scan lock — deadlocks, because the background scanner already holds
  it.)*
- **Failed page scrapes are recorded as "scanned"**, so incremental scans skip those releases forever.
- **Interrupted-action recovery clobbers in-flight work** when a second request constructs the service.
- A websocket reconnect can leave **two live sockets**, double-delivering every event.
- The scheduler's trigger is never registered, so it **stamps "last scan" without scanning** (it can't
  actually reach the broken path today, so this is cosmetic for now).

### Disproved — don't chase these

- A Windows drive-letter path escape in the trash delete: **impossible**, the app runs in a Linux
  container where drive letters are meaningless.
- A feed ID collision aborting all RSS ingestion: **can't happen** with the real feed's data shape.

---

## 4. The deep dives — where I was wrong

### Dolby Vision / FEL / MEL / HDR10+ / Kometa

**My standing note said the DV feature was "deployed but never run for real." That was wrong.** It
ran on 2026‑07‑26, your Plex library carries **171 DV FEL, 159 DV MEL, 81 DV P8, 33 DV P5**, and
Kometa has been drawing badges from those labels daily — most recently 2026‑08‑03. Memory corrected.

What's actually wrong there now:

- **The nightly detection job documented in the README was never created.** The inventory has been
  frozen since 2026‑07‑05, and the scan stopped mid-alphabet — **184 files** after "Godzilla Minus
  One" have never been classified. Every 4K DV movie added since early July is unbadged.
- **Kometa only draws badges for FEL and MEL.** The 114 movies labeled P8 or P5 get nothing. The
  badge file in the repo isn't the one Kometa actually uses.
- **The DV panel has no dry-run button** even though the API supports one — every click is a live
  write across 3,831 movies.
- A wrong label isn't cheap to undo: Kometa composites badges *into* your poster images, so fixing
  one needs a redraw pass.

### Renaming

**"Auto-rename is paused" only means the JDownloader hook is off.** The manual path — Process a
folder, then Apply — is fully live and performs a real, source-consuming move on real storage. What's
protecting you today is that no appliable jobs exist, not a code gate.

Worse, the safest guard in the file is dormant *because* of your settings: it downgrades a "move" to a
safe "hardlink" only for unattended applies, and since you require confirmation, every apply counts as
attended.

The good news, verified by hash-comparing the running container against `main`: **both file-operation
defects really are fixed in production**, backed by a differential reproduction run (pre-fix: data
loss in 3 of 4 scenarios; current: safe in all 4).

---

## 5. Waiting on you

**Time-critical:** 34 GB in two trash buckets from 2026‑07‑08 hits the 30-day retention limit around
**2026‑08‑07**. The 21 GB bucket (*Little Women*) has its manifest and can be restored from the Trash
panel. The 13 GB bucket has **no manifest**, so the Restore button can't recover it — it needs a
manual copy. Both sit on the failing X: drive. Doing nothing is a valid choice, but it's silent.

**Three branches ready for ChatGPT** (paste blocks are in each branch under `docs/reviews/`):

| Branch | What it carries |
|---|---|
| `agent/hybrid-sweep-rebased` @ `9ff626e` | Round-13 closures, contract rev 3.2, committed suite artifact |
| `agent/audit-fixes-2026-08` @ `fa56dfd` | DV detection + label stripping, Trakt import, keep-both data loss |
| `agent/category-switch-cache-fix` @ `c7fdac7` | Category switching, empty-selection sentinel, scheduled-scan view wipe |

Still blocked on you from before: the **GitHub billing** fix (unblocks all CI attestation) and the
**elevated mount install** command.
