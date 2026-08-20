# ScanHound — plan from here to completion

**Date:** 2026-08-01 · **Author:** Claude · **For review by:** ChatGPT · **Arbiter:** Jesse
**Measures against:** `docs/reviews/2026-07-31-plan-rev2-AUTHORITATIVE.md` (rev 2.1)

Rev 2.1 said what to do. This says **what is actually done, what is left, and
what I think should now change** — including three items rev 2.1 requires that I
believe should be retired or downgraded, and one it does not require that I
believe must happen before anything else.

Every status below was checked against the repository today. Where I am relying
on a figure from earlier in the session rather than a fresh measurement, it says
so. That distinction exists because a stale number copied from a comment once
became a peer reviewer's headline finding, and because on 2026-08-01 a
misconfigured local test harness produced three successive wrong failure counts.

---

## 1. Where this stands, in plain terms

Three things have been running in parallel for the past two weeks:

1. **A new way of scanning HDEncode** (the "sweep engine") that avoids
   re-reading pages it has already read. Written, reviewed by ChatGPT across
   four rounds, all corrections applied, automated tests passing. **It is
   finished and sitting on a branch. It has never run in production.**

2. **Proving that the RSS feed is good enough to become the primary way new
   releases are found.** This needs seven days of clean evidence. The previous
   attempt was ruled void. **The replacement has not started, and cannot start
   until item 1 is deployed.**

3. **Making renaming safe enough to turn back on.** Renaming has been frozen
   since 19 July. The two defects that caused the freeze are now fixed. What is
   missing is proof that the fixes hold on your actual drives.

**The single most important fact in this document:** items 2 and 3 are not
competing for time. Item 2 is roughly **eight to nine days of waiting** that
nothing can shorten — about 30 hours for the engine to fill in its history,
then seven calendar days of measurement. Item 3 is a few days of actual work.
If the waiting starts now, the work happens inside it and everything finishes
at once. If the work happens first, the waiting starts afterwards and the whole
thing takes two to three weeks longer for no benefit.

Rev 2.1 put rename ahead of RSS "on consequence." That reasoning was right
about consequence and I am not reopening it. But it was written before the
sweep engine existed, when there was nothing to deploy and therefore no clock to
start. **Starting the clock costs nothing in rename risk, because rename is
frozen either way.**

---

## 2. Scorecard

Legend: **✓** done and evidenced · **◐** partly done, and misleading if counted
as done · **✗** not started · **⏳** waiting on elapsed time · **🔒** Jesse-gated

### Serial preservation (rev 2.1 §3)

| # | Item | Status | Evidence / note |
|---|---|---|---|
| 1 | Rename execution frozen | ✓ | All three auto-flags verified `false` |
| 2 | Atomic RSS evidence snapshot | ✗ | **Proposed for retirement — see R1** |
| 3 | Precedence chain repaired | ✓ | Done in rev 2.1 itself |

### Track A — RSS evidence and qualification

| # | Item | Status | Evidence / note |
|---|---|---|---|
| A1 | Unique-miss analysis | ✗ ⏳ | Tooling exists (`miss_resolution.py`); must be re-run on the fresh window, not rebuilt |
| A2 | Per-cycle metrics | ✗ ⏳ | Same |
| A3 | Causal classification | ✗ ⏳ | Same |
| A4 | **Suitability beyond URL discovery** | ✗ | **The one real code gap in Track A — see R6** |
| A5 | Predeclared thresholds | ◐ 🔒 | Written (`2026-08-01-A5-predeclared-thresholds.md`), marked PROPOSED. T1/T2 need your approval before the window opens |
| A6 | Collector networking + fail-closed | ✓ | Proved `127.0.0.1:9721` → refused vs `--network proxy` + `scanhound:9721` → HTTP 200. The cross-check had **never once succeeded** before this |
| A7 | Population symmetry (`#191`), then fresh window | ◐ ⏳ | `backend/release_policy.py` present on the sweep branch, **absent from `main`** — implemented, undeployed. Window not started |

**Sweep engine parts 1–9:** parts 1–8 ✓, part 9 = deploy 🔒.
Branch `agent/hybrid-sweep-implementation` @ `aefa841`. CI: last completed run
`success`; one run in progress at time of writing. Full suite last measured at
4433 passed / 0 failed (session figure, not re-measured for this document).

### Track B — Rename safety

| # | Item | Status | Evidence / note |
|---|---|---|---|
| B1 | Evidence-availability audit | ◐ | **Proposed for downgrade — see R2** |
| B2 | Currently reproducible failure path, commit-pinned | ✗ | **Nothing has been pinned against the current commit — see R3.** What was reproduced on the rename branch is a defect in *my own new ledger*, not in file placement |
| B3 | Fault injection + old-fail/new-pass tests | ✗ | Task #198 |
| B4 | Durable rename event ledger | ✓ | `backend/rename/ledger.py` + `failure.py` rev 2 (`2e2c1ea`), evidence-driven `DiskOutcome`, ledger writes fail loudly on `rowcount != 1` |
| B5 | Copy-only rehearsal on real storage | ✗ | Task #198 |
| B6 | Restart / recovery invariants | ◐ | `reconcile_interrupted()` exists and is unit-tested; never exercised across a real container restart |
| B7 | One sacrificial real file | ✗ 🔒 | Blocked on B3/B5/B6 by design |

Branch `agent/rename-safety-gate` @ `5bf51fb`, CI `success`.

### Track C — Security controls

| Item | Status | Evidence / note |
|---|---|---|
| History-aware secret review | ✓ | Ran; both findings manually classified false positives |
| CI secret scanning on push + PR | ✗ | `.github/workflows/` contains only `tests.yml` and `hdencode-reliability.yml`. **No scanning workflow exists** |
| Reviewed allowlist with ownership | ✗ | Depends on the above |
| Externalise + rotate the Gotify token | ✗ | You chose "leave it for now". Not in the repo — grep of `main`'s `scripts/` and `docker-compose.yml` found nothing — so this is an infra-side item, which lowers its urgency |
| Documented true-positive response procedure | ✗ | |

### Track D — Independent product work

| Item | Status | Evidence / note |
|---|---|---|
| Scraper structural-failure detection | ✓ | Sweep part 6, `backend/sweep/structure.py` |
| TV resolution filter + 720p chip | ✗ | `RESOLUTION_KEYS` on `main` is `['4K','1080p','TV']`; TV is one undifferentiated bucket |
| Surface the full-disc setting in the UI | ✗ | No frontend reference to it on `main` |
| HDR10+ labels + Kometa badges (`#185`) | ◐ | `backend/rename/hdr10plus_detect.py` exists and is wired through matching/inventory; the **labels and badges** are the open half |
| Documentary design pass | ✗ | |
| `#192` doc correction | ✗ | |
| `#184` scan stage counters | ◐ | Marked in progress |

---

## 3. What I think should change

Five proposals. Three retire or shrink work rev 2.1 requires; one adds work it
does not; one changes the ordering.

### R1 — Retire the atomic evidence snapshot; replace it with a pre-deploy backup

Rev 2.1 §3 made this the first serial step and said *"Do not delay step 2. RSS
evidence is time-dependent and degrades."* It called for a nine-element bundle:
database plus WAL state, qualification output, branch and commit, configuration,
image digest, service version, timestamp, cycle range, and a checksum manifest.

**That reasoning has been overtaken.** The evidence it protects has since been
ruled void — the window must be fresh, from the corrected build, and pre-change
cycles may never be merged with post-change ones. Preserving it perfectly
preserves something inadmissible. It is not free either: it is a careful,
error-prone job, and it produces an artifact that a future reader may
reasonably mistake for evidence.

What is genuinely worth keeping is already written down: the finding that
**99 of 100 apparent misses had in fact been acquired, median 1.02 hours**, is
recorded in the corrected miss analysis and does not depend on the database
surviving.

**Proposal.** Replace step 2 with a plain pre-deploy copy of the production
database — a rollback precaution, which is ordinary practice — labelled
explicitly *"diagnostic archive, NOT admissible as qualification evidence."*
Ten minutes instead of half a day, and it cannot be misread later.

### R2 — Downgrade B1 from a gate to a bounded note

B1 exists because `rename_jobs` is a single mutable row per job rather than an
event ledger, so the only way to learn anything about past failures was careful
archaeology with honest "unknown" markers.

**B4 has since been built.** Every future failure is now fully recorded:
attempt, commit, method, volume identity, pre- and post-operation filesystem
state, each transition, the exception, the recovery outcome. B1's forward-looking
purpose is gone.

ChatGPT anticipated exactly this in rev 2.1 §5 question 3 — *"does the event
ledger (B4) become a hard prerequisite rather than an improvement?"* The answer
turned out to be yes, it was a prerequisite, and it is now built. That inverts
B1: it is no longer the thing that makes a rename conclusion possible.

What remains is archaeology on historical jobs whose commit range is unknown, in
support of a claim (`69/158`) that rev 2.1 §1.5 **already withdrew**.

**Proposal.** Reduce B1 to a bounded exercise — count the current status
buckets, mark every other dimension unknown, write one page, roughly an hour —
and stop treating it as gating B7.

### R3 — Say plainly why rename is frozen, because it is no longer what we said

This is the correction with the most consequence, and it is a correction to my
own record.

I verified today against `origin/main` that **both defects that caused the
freeze are fixed**:

* **SH-R02** (placement could silently destroy a racing destination) — fixed by
  `70dca70` *"Publish placed files without replacement."* `fileops.py` now
  publishes through `renameat2(RENAME_NOREPLACE)` where the kernel supports it,
  and an atomic hard-link otherwise. My stored note claiming this fix was still
  "uncommitted and held on a branch" was **out of date**.
* **SH-R03** (a trashed file could be stranded with no restore record) — fixed
  by `44ea7ba` *"Make trash disposal a durable transaction"* (+422/−176, plus a
  new 283-line `tests/test_trash_durability.py`) and `4d678bd`. The record is
  now reserved **before** the file moves, under a process-wide lock, read by a
  strict loader that raises on corrupt JSON instead of discarding it, and
  written atomically. My note claiming this was "entirely unfixed" was **wrong**.

Rev 2.1 §1.5 had already withdrawn the blanket claim, on the grounds that it was
inherited from an earlier session and never re-verified against the current
commit. It was right to withdraw it. I am completing that withdrawal with the
specific commits.

**So the honest statement of the freeze is:** there is **no known live
data-loss defect** in the rename path. The freeze now rests on *absence of
evidence that the fixes hold on the real volumes* — which is a legitimate reason
to stay frozen, and a materially different one from "there are live bugs."

Two consequences, and I want both on the record rather than quietly acted on:

* **This does not open the gate.** The topology that made SH-R02 reachable —
  downloads and library on different volumes, so `hardlink` always falls back to
  copy-and-publish — is still your topology, and it was never exercised on real
  storage with the new code. B5 is exactly that test.
* **It does re-scope B2.** B2 stops being "find the bug" and becomes
  "re-run the 2026-07-19 reproductions against the current commit and record
  the result." If they now fail to reproduce, that is the evidence the fixes
  work, and it is far stronger than a code reading. If any still reproduces, we
  have a live defect and everything else stops. Either outcome is worth having,
  and it is perhaps two hours.

### R4 — Start the qualification clock first

The sweep engine is finished, four-round reviewed, and CI-green. Qualification
cannot begin until it is deployed. From deploy, the sequence is **~30 hours of
per-source bootstrap, then 7 calendar days of measurement** — call it eight to
nine days before a verdict is possible, none of which can be compressed by
working harder.

Everything else remaining — Track B's B2/B3/B5/B6, all of Track C, all of
Track D — is bounded work that fits comfortably inside that window.

**Proposal.** Deploy first (after the pre-deploy blockers in §4), start the
clock, and do the remaining work while it runs.

I want to be clear about what this reorders and what it does not. Rev 2.1's
priority rule — rename outranks RSS on consequence — **stands**. Rename stays
frozen throughout; nothing here touches a real file any sooner. What changes is
only that the waiting starts at the beginning instead of the end.

### R5 — Narrow the deploy freeze to the measured surface

ChatGPT raised this in rev 2.1 §5 question 4: Track C/D work "ships in the same
image and a deploy restarts the container mid-RSS-window."

The concern is real, but I think the blunt reading — *no deploys for nine days* —
is both more expensive and less accurate than it needs to be, for two reasons:

1. A container restart is **not** an invalidating event. It interrupts polling,
   which is a missed poll, which the design explicitly handles through
   catch-up recovery. Threshold **T8 positively requires** demonstrating a
   restart recovery and a missed-poll recovery. A restart is evidence we need.
2. What actually invalidates a window is a change to **what is being measured**:
   discovery, listing, RSS, policy, or the gate.

**Proposal — a named list rather than a judgement call.** A deploy restarts the
window if it touches any of:

* `backend/sweep/**`
* `backend/release_policy.py`
* the HDEncode scraper / detail-scraper / RSS modules
* the readiness functions in `backend/database.py`, **or** the DB-derived mirror
  at `docs/feature-pack-review/qualification/scripts/05_shadow_evidence.py`
  (there are **two independent readiness implementations** and the collector's
  mandatory-stop reads the mirror, so both count)
* anything altering poll cadence or feed URLs

Anything else — frontend, docs, rename code that is switched off — may deploy,
with the restart timestamp recorded in the window log.

**I am arguing the permissive side here, and that is where I have been wrong
before.** I would rather ChatGPT attack this list than have me apply it.

### R6 — Build the A4 parity check *before* the window, not discover it at closure

A4 says RSS-primary requires more than a URL arriving: the candidate must
survive identity parsing, classification, hydration, and library comparison to
reach the same actionable decision the listing path produced.

Threshold **T2 makes this a pass condition**. Nothing tests it. So today, the
failure mode is: run for nine days, close the window, discover that
RSS-discovered candidates do not hydrate equivalently, and have nothing.

**Proposal.** Before deploying, build a parity harness: take a fixed set of
known releases, drive both discovery paths, assert the resulting actionable
decision is **identical**, not merely "both non-empty." Roughly half a day
against a nine-day exposure. This also satisfies the parity fixtures ChatGPT
required in round 3.

---

## 4. The route to completion

### Phase 0 — before deploy (~1–2 days)

| | Item | Owner |
|---|---|---|
| 0.1 | A4 parity harness (**R6**) | Claude |
| 0.2 | Re-run the 2026-07-19 rename reproductions against the current commit (**R3**) | Claude |
| 0.3 | Relay the three queued review packages — round 4 boundary lock, Item B rev 2, A5 thresholds | Jesse → ChatGPT |
| 0.4 | Approve T1 and T2 (**cannot change once the window opens**) | 🔒 Jesse |
| 0.5 | Approve or reject R1–R6 | 🔒 Jesse |
| 0.6 | Pre-deploy database copy, labelled diagnostic-only (**R1**) | Claude |
| 0.7 | Merge `agent/hybrid-sweep-implementation` → `main` | 🔒 Jesse |

### Phase 1 — deploy and bootstrap (~30 h, unattended)

| | Item | Owner |
|---|---|---|
| 1.1 | Deploy (`up -d --build`; run in background, historically >10 min) | 🔒 Jesse |
| 1.2 | Confirm all three auto-flags still `false` after deploy | Claude |
| 1.3 | Confirm the readiness cross-check succeeds in production — it has never once succeeded before | Claude |
| 1.4 | 30 h per-source bootstrap | ⏳ |

### Phase 2 — the clean window (7 calendar days)

| | Item | Owner |
|---|---|---|
| 2.1 | Start the qualification window; boundary locks on first counted cycle | 🔒 Jesse |
| 2.2 | Deploy freeze on the measured surface only (**R5**); log every restart | Claude |
| 2.3 | Daily check: cycles counted, blockers, no mandatory stop | Claude |

**Running concurrently — Track B, the real work of this phase:**

| | Item | Owner |
|---|---|---|
| 2.4 | B1 bounded note (**R2**) | Claude |
| 2.5 | B3 fault injection + old-fail/new-pass tests | Claude |
| 2.6 | B5 copy-only rehearsal on the real volumes, hashes verified — the cross-volume path that made SH-R02 reachable | Claude |
| 2.7 | B6 restart invariants across a genuine container restart | Claude |

**Also concurrently — Tracks C and D:**

| | Item | Owner |
|---|---|---|
| 2.8 | CI secret scanning + allowlist with ownership + response procedure | Claude |
| 2.9 | TV resolution filter + 720p chip · full-disc setting in UI · HDR10+ labels (`#185`) · documentary pass · `#192` | Claude |
| 2.10 | Deploy the Track D batch (non-measured surface) | 🔒 Jesse |

### Phase 3 — verdict (1–2 days after the window closes)

| | Item | Owner |
|---|---|---|
| 3.1 | Run A1 / A2 / A3 on the fresh window | Claude |
| 3.2 | Grade against T1–T13; state each pass or fail with its number | Claude |
| 3.3 | Qualification report → ChatGPT | Claude → Jesse → ChatGPT |
| 3.4 | Promote RSS to primary, or don't | 🔒 Jesse |

### Phase 4 — rename rollout (gated on Phase 2's Track B, not on Phase 3)

| | Item | Owner |
|---|---|---|
| 4.1 | Confirm B1–B6 all pass; **any failure stops here** | Claude |
| 4.2 | B7 — one sacrificial, backed-up real file | 🔒 Jesse |
| 4.3 | Verify source, destination, hash, DB state, Plex visibility, restart | Claude |
| 4.4 | Gradual expansion with explicit stop conditions | 🔒 Jesse |

**Critical path:** Phase 1 + Phase 2 ≈ 8–9 days of elapsed time. Everything else
fits inside it. Realistic completion is **two to three weeks**, dominated by
waiting, provided Phase 0 does not stall.

---

## 5. What only you can do

Nothing below can be done by me, and each one blocks a phase.

1. **Approve T1 and T2** (Phase 0.4). Two plain questions: should a new release
   normally be noticed within 2 hours and always within 24? And should a single
   release the app cannot fully process block the whole seven days, or would you
   rather allow a few and review them by hand? **These cannot change once the
   window opens** — changing a threshold mid-window voids it exactly as moving
   the boundary does.
2. **Approve or reject R1–R6.** R3 in particular changes what we tell ourselves
   about why renaming is off.
3. **Relay the three queued review packages** to ChatGPT, plus this one.
4. **Merge and deploy** (Phase 0.7, 1.1) — and again for Track D (2.10).
5. **Start the qualification window** (2.1).
6. **Authorise the sacrificial file** (4.2) and any expansion (4.4).

There is also one loose end from earlier: the duplicate background task
`task_97ff2095` still needs stopping.

---

## 6. Accepted risks

* **A nine-day freeze on the measured surface.** If something urgent breaks in
  discovery during the window, fixing it restarts the seven days. R5 narrows the
  exposure but cannot remove it.
* **T7 (coverage margin) is the threshold most likely to fail on merit.**
  `tv_all` already showed one negative-margin cycle at −0.12 h in the void
  window. If it fails again it should fail, not be waived quietly.
* **Volume-anomaly detection does not operate.** `expected_typical` has no
  producer, so the check is disabled and no threshold depends on it. The
  qualification report must not imply the protection exists.
* **The Gotify token stays plaintext** by your decision. Lower risk than rev 2.1
  implied, since it is not in the repository.
* **Public topology retained.** Unchanged from rev 2.1 §4.

---

## 7. For ChatGPT to attack

1. **R5 is the one I least trust.** I am arguing that a restart is not an
   invalidating event and that a named file list is a sufficient boundary. Is
   the list complete? Is "the rename code is switched off, so deploying it is
   safe" the same class of reasoning as the fail-open assumptions that have
   already bitten this project twice?
2. **R1** — is there a reason to preserve void evidence that I have missed? My
   argument is that inadmissible evidence is not worth an expensive ritual, and
   that a preserved bundle invites future misreading.
3. **R3** — I am reporting that the two defects justifying the rename freeze are
   fixed, and that my own stored notes said otherwise. Does that change how much
   of B3/B5/B6 is warranted, or is "reproduce it again on the current commit"
   (0.2) the right amount of scepticism?
4. **R6** — is asserting that two discovery paths reach an *identical* actionable
   decision the right parity property, or does it over-constrain? RSS and
   listing legitimately carry different metadata; identical decisions from
   different inputs may be too strong a bar.
5. **R4 and the priority rule.** Rev 2.1 §5 question 5 asked whether rename
   should outrank RSS given that RSS evidence degrades while rename is frozen
   anyway. I am answering: keep the consequence ordering, change only the
   calendar ordering. Is that a real distinction or am I reintroducing the thing
   the rule was written to prevent?
6. **The gap I may still be missing.** This plan defines completion for RSS
   promotion, rename safety, and the security review. It does not define
   completion for the product. Track D is a list of five things with no stated
   exit. Should it get one — after these five, further product work is ordinary
   feature work and not part of this plan — or is that the wrong shape?
