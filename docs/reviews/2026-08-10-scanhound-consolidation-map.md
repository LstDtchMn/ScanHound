# ScanHound branch + session consolidation map

**Date:** 2026-08-10
**Author:** Claude (session `e7d059a1`)
**Purpose:** one page that tells any session — or Jesse — what is outstanding, what already
contains what, and in what order things should land.

---

## 0. The finding worth leading with

**Three separate pairs of sessions independently built the same thing.** This is not a
one-off; it is the systemic cost of running parallel chats on one repo.

| Duplicated work | Sessions | Cost |
|---|---|---|
| Per-file DV logging + interim `dv-import` | this one and `46af8201` | resolved — consolidated, theirs won the architecture |
| The `test_queue_recovery_policy.py` date bomb | two, `a88d541` and `9f28ba4` | **unresolved — the two fixes CONFLICT** |
| Turnstile verification-hold work | `54093368` and `b087aa20` | **unresolved — 17 conflicts between the branches** |

Each pair branched from `main` without knowing about the other. The DV pair was only discovered
because an unaccounted-for `dovi_tool` process turned up in a process list.

**The cheap prevention:** before starting work on a repo area, run
`git for-each-ref --sort=-committerdate refs/remotes/origin` and look at anything touched in the
last few days, and check `list_sessions` for a running session with an adjacent title.

---

## 1. Branch inventory — only what is CURRENT

70+ branches are unmerged, but almost all are stale (100–290 commits behind `main`). These eleven
are 0 behind and touched in the last three days:

| Branch | Ahead | Owner session | Status |
|---|---:|---|---|
| `agent/dv-detector-consolidation` | 38 | this one | **round 3 out** |
| `agent/hdr10plus-design-review` | 21 | this one | **subsumed** ↓ |
| `fix/dv-scan-live-progress` | 21 | this one | **subsumed** ↓ |
| `agent/dv-scan-hang-and-starvation` | 14 | `46af8201` (running) | **subsumed** ↓ |
| `agent/policy-migration-audit` | 1 | — | **subsumed** ↓ |
| `fix/dv-import-cadence` | 24 | this one | **retire unmerged** (properties ported) |
| `fix/queue-policy-test-time-bomb` | 1 | `b295ea14` | content in via cherry-pick `cd1195b` |
| `fix/policy-tests-wall-clock` | 1 | `b087aa20` | **conflicts with the above — pick one** |
| `fix/dv-label-sync-watermark-loss` | 1 | — | **outstanding, live consumer bug** |
| `claude/scanhound-turnstile-verification-hold-z43q0x` | 8 | `54093368` | **outstanding** |
| `agent/turnstile-classification` | 5 | `b087aa20` | **conflicts with the above** |

**`agent/dv-detector-consolidation` already contains four of them** (verified by
`git merge-base --is-ancestor`, not assumed). Merging it lands the approved live-progress wrapper,
the HDR10+ design work, the DV scheduling commits, the other session's detector architecture, and
the policy-migration audit — in one move.

## 1b. Session → branch attribution (every current branch is owned)

| Session | Title | Owns | Covered by |
|---|---|---|---|
| `e7d059a1` | this one | the consolidation + 3 subsumed/retired | Track A |
| `46af8201` | DV scan cannot converge… | `agent/dv-scan-hang-and-starvation` | Track A (subsumed) |
| `54093368` | Turnstile verification hold timer | `claude/scanhound-turnstile-verification-hold-z43q0x` | Track B |
| `b087aa20` | Add a verification hold a timer cannot release | `agent/turnstile-classification`, `fix/policy-tests-wall-clock` | Tracks B and C |
| `b295ea14` | ScanHound completion drive | `fix/queue-policy-test-time-bomb` (`a88d541`) | Track C — content already in Track A |
| `3c040c7c` | Docker setup and app container updates | none current | nothing to consolidate |

**Every current branch is attributed**, so a session owning none of them has no outstanding code
to fold in. Note the date-bomb duplication crosses tracks: `b087aa20` is a turnstile session that
also fixed the policy tests, unaware `b295ea14` had already done it.

## 2. Three independent tracks

They do not interact. **Turnstile vs the DV consolidation: 0 conflicts.** So they can proceed in
parallel and be merged in any order relative to each other.

### Track A — DV detector (this session + `46af8201`)

Consolidated and under review.

```
agent/dv-detector-consolidation @ 14d6b24
  ├── agent/hdr10plus-design-review @ 8fbac87   (live-progress, APPROVED x3, live-verified)
  ├── cherry-pick a88d541                       (date bomb)
  └── agent/dv-scan-hang-and-starvation @ db16ed6 (detector architecture, UNMODIFIED at source)
```

Review history: round 1 REQUEST CHANGES (3 blockers) → round 2 REQUEST CHANGES (1 new HIGH
blocker) → round 3 out. **Not merged, not deployed.** After approval the canary runs on existing
roots, with WAL/bind-mount visibility proved first, and root widening kept as a separate event.

**To reach a stopping point:** session `46af8201` should stop committing to
`agent/dv-scan-hang-and-starvation` — anything added there now diverges from the consolidation.
It has been told. Any in-flight work should land on the consolidation branch instead.

### Track B — Turnstile (`54093368` + `b087aa20`)

**Needs exactly the treatment Track A just had.** Two branches, both from `main`, 17 conflicts
between them, ~2,300 and ~2,900 insertions. Neither has been consolidated and I have not reviewed
either.

Recommended, mirroring what worked: pick the branch with the better architecture as the base,
merge the other *into* it (never rewriting the source branch beneath a live session), resolve
deliberately, then a single peer round over the result.

### Track C — housekeeping (unowned, 1 commit each)

- **The date bomb**, fixed twice and conflicting. `a88d541` is already in the consolidation via
  cherry-pick; `9f28ba4` adds 27 lines to the same file. Someone must pick one and close the other.
- **`fix/dv-label-sync-watermark-loss` (`28a3cb0`)** — a **live** bug: `_run_maintenance_pass`
  assigns `_last_dv_scan_at = latest` *before* the `pm is None` check and before `sync_labels()`,
  so a scan generation is consumed even when no sync ran. This matters more once the DV import
  actually starts running, because generations will arrive far more often. **Merge before or with
  the canary.**

## 3. Suggested order

1. **`fix/dv-label-sync-watermark-loss`** — smallest, live bug, no conflicts.
2. **`agent/dv-detector-consolidation`** once round 3 passes — lands four branches with it.
3. **Canary** on existing roots; prove WAL visibility first; no root widening.
4. **Retire** `fix/dv-import-cadence`, `fix/dv-scan-live-progress`, `agent/hdr10plus-design-review`,
   `agent/dv-scan-hang-and-starvation` — all subsumed or superseded.
5. **Pick one** of the two date-bomb fixes; close the other.
6. **Track B consolidation** — independent, can run in parallel with 1–5.
7. **Root widening**, separately, after the canary is clean.

## 4. What is verified vs assumed here

**Verified:** every ahead/behind count, every `is-ancestor` subsumption claim, every conflict
count (via `git merge-tree`), and that turnstile and DV do not interact.

**Not verified:** the quality of either turnstile branch — I have not reviewed them, and the
recommendation to consolidate them is about branch topology, not about which is better. Also not
verified: WAL/bind-mount visibility, still the largest open unknown in Track A.
