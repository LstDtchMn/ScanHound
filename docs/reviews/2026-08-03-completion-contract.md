> ## SUPERSEDED 2026-08-04 by `2026-08-04-completion-contract-rev2.md`
> Round 10 (Q7) ruled that appended amendments are not an amended
> contract; rev 2 integrates every correction into the tables. This file
> is retained as the record of rev 1 + its amendment history.

# ScanHound — Completion Contract, rev 1

**Date:** 2026-08-03 (overnight) · **Author:** Claude · **Reviewer:** ChatGPT (round pending) · **Arbiter:** Jesse
**Status:** DRAFT — awaiting Jesse's morning decision batch, then peer review.

**What this is.** The standing review finding was that "complete" is undefined
(Track D had five items and no exit; other tracks had routes but no binary
finish line). This contract defines **done** for every open track: each item has
a binary, evidence-checkable exit criterion and an owner. Nothing counts as done
without a cited artifact — a test run URL, log line, commit, or committed
verdict. Exit codes are not artifacts.

**Precedence.** The phased route in
`2026-08-02-plan-to-completion-rev2.md` is UNCHANGED and controls sequencing.
The rev 2.1 design (`2026-07-31-plan-rev2-AUTHORITATIVE.md`, branch
`agent/review-2026-07-31-decisions`) controls the RSS/rename design. This
contract controls **completion status only**: whether an item is done, and what
evidence that requires. It covers three tracks the plan rev 2 route does not:
the two shutdown/thread-leak branches, the security-closeout specifics, and the
ops leftovers. `2026-08-01-plan-to-completion.md` was already superseded by
rev 2 and stays superseded.

**Verified baseline (2026-08-02 late evening, all measured, not recalled):**

* `main` = `7cc5275`; production container runs code hash-identical to
  `7cc5275`, image built 2026-08-01 00:36 UTC (reflog + blob-hash proof).
  This means PR #37's security fixes (S1 API-docs exposure, S2 symlink
  containment, `41d0193`) **are deployed** — the commit's "NOT DEPLOYED" note
  is stale.
* `agent/hybrid-sweep-implementation` = `b1825f1`, CI green on head and the two
  commits before it; three local full-suite runs read and green
  (4660/4664/4668 passed, 0 failed, 4 xfailed = the DetailScraper-gap markers).
* `claude/nice-meitner-2b717b` = `09f6433` + tonight's CI-enable commit
  `d0202da`; `claude/nostalgic-brattain-946f4f` = `f9ad4f2` + `8799255`.
  Neither branch had ever had a CI run before tonight; first runs in flight.
* Auto-rename / auto-grab off. Rename freeze stands on missing real-storage
  evidence (B5/B6), not on a live defect (fixes `70dca70`, `44ea7ba` on main).

---

## Track 1 — RSS-primary promotion

The critical path. The review's **seven-item gate** (numbering from the
`b1825f1` commit message, which is the latest statement) blocks Phase 0
completion; the qualification clock cannot start before Phase 0 closes.

| ID | Exit criterion (binary) | Evidence artifact | Owner |
|---|---|---|---|
| R-1 | Gate item 1 — media type resolved by authority end-to-end | DONE at `b1825f1` per commit message + CI green; needs round-9 verdict to confirm closure | ChatGPT verdict |
| R-2 | Gate item 2 — canonical-URL identity inventory + measured corpus: every producer, persistence point, and consumer of URL identity enumerated; identity-form mismatches measured against the real DB | committed inventory doc with query outputs and a positive control | Claude, ChatGPT verdict |
| R-3 | Gate item 3 — DetailScraper seam extracted/measured/unified: the 4 strict xfails in `tests/test_active_listing_path_gap.py` flip to ordinary passes | commit + CI green showing xfail count 4→0 | Claude, ChatGPT verdict |
| R-4 | Gate item 4 — derived-state invalidation/versioning: persisted classifications carry parser/canonicaliser version and are invalidated on version change | commit + tests + verdict | Claude, ChatGPT verdict |
| R-5 | Gate item 5 — consumer-boundary contract suite (end-to-end decision tests replace structural parity assertions) | commit + CI green + verdict | Claude, ChatGPT verdict |
| R-6 | Gate item 6 — promotion gate hardened AND WIRED at both capability boundaries (`backend/promotion_gate.py` is currently pure and imported only by its test — measured 2026-08-02) | commit showing call sites + tests + verdict | Claude, ChatGPT verdict |
| R-7 | T1/T2a/T2b thresholds + Phase A/B contract approved (A5 doc is still marked PROPOSED) | Jesse's recorded approval | 🔒 Jesse |
| R-8 | Sweep candidate merged to main | merge SHA on main | 🔒 Jesse |
| R-9 | Built once under an immutable tag; digest + config fingerprint recorded (`docker buildx imagetools inspect` for registry truth) | digest recorded in the window log | 🔒 Jesse builds, Claude records |
| R-10 | Diagnostic-only rollback DB copy, labelled NOT ADMISSIBLE | file + checksum | Claude |
| R-11 | Deploy exactly that digest | container Image sha == recorded digest | 🔒 Jesse |
| R-12 | Phase 1 bootstrap (~30 h): three auto-flags false; readiness cross-check succeeds in production; per-source bootstrap complete | captured log lines | Claude |
| R-13 | Phase 2A window: 7 calendar days on the pinned digest, window integrity intact (same digest at open and close, config unchanged) | window log with digest checks both ends | Claude collects, 🔒 Jesse opens/locks |
| R-14 | Phase A graded against the predeclared thresholds, verdict recorded | grader output committed | Claude, ChatGPT verdict |
| R-15 | Phase B run per its own predeclared plan on its own pinned artifact; T2b pass/fail recorded | Phase B report committed | Claude, ChatGPT verdict |
| R-16 | Combined promotion decision recorded — **promote and do-not-promote are both valid completions** | decision doc | 🔒 Jesse |

## Track 2 — Rename safety (B-series, per plan rev 2)

| ID | Exit criterion | Evidence artifact | Owner |
|---|---|---|---|
| N-1 | B1 historical-limitations note committed (no rate inference from pre-ledger rows) | doc | Claude |
| N-2 | B3 old-fail/new-pass + fault-injection suite runs in CI, with a discrimination (mutation) check | CI run URL | Claude |
| N-3 | B5 capability probe: per-volume matrix (renameat2 / hardlink / errno / collision / fsync behaviour), scratch-only, detection by errno never by `UnsupportedFilesystemSafetyError` | probe output committed | 🔒 Jesse authorises, Claude executes |
| N-4 | B5 copy-only rehearsal on real storage, hashes verified | rehearsal log + hash table | 🔒 Jesse authorises, Claude executes |
| N-5 | B6 restart/reconciliation invariants under real container interruption (separate pinned rename-test container only) | test log | 🔒 Jesse authorises, Claude executes |
| N-6 | B7 one sacrificial backed-up real file; source/destination/hash/ledger/DB/Plex/restart all verified | run log, all seven checks | 🔒 Jesse authorises, Claude executes |
| N-7 | Rollout-or-continued-freeze decision recorded — **both are valid completions** | decision doc | 🔒 Jesse |

## Track 3 — Shutdown / thread-leak (the two claude/* branches)

Measured state 2026-08-02: **nice-meitner** (`09f6433`) — Phases 1, 2, 3 steps
1–2 done with evidence docs; Phase 3 steps 3–7 (metadata future cancellation,
deadline propagation through tmdb_client/rt_scraper, staged commit, executor
completion, bounded join) and Phase 4 unstarted; the metadata-enricher
mid-item-cancellation defect has **no fix on the branch**.
**nostalgic-brattain** (`f9ad4f2`) — shutdown join, executor-cleanup correction,
SMTP bounding (30 s default, 300 s clamp), desktop notifications off-by-default
+ 409 test route; its round-1 verdict defines a **nine-item merge gate**, of
which the three lifecycle P0s (application-wide deadline, generation fencing,
`begin_lifespan()` safety) are deliberately deferred. The branches overlap on
five backend files and will add/add-conflict on `tests/tools/threadleak.py`.
The RSS-cancellation de-flake is **absent** from brattain.

| ID | Exit criterion | Evidence artifact | Owner |
|---|---|---|---|
| S-1 | First machine-attested CI runs green on both branch heads | run URLs for `d0202da` + `8799255` (in flight tonight) | Claude |
| S-2 | A written reconciliation of the two branches: which lands first, what rebases, how the 5-file overlap + threadleak.py add/add conflict resolve, where the flake fix lands | reconciliation doc + ChatGPT verdict | Claude proposes, 🔒 Jesse decides order |
| S-3 | The nine-item merge gate from the round-1 verdict: each item closed, or explicitly deferred with Jesse's sign-off recorded | per-item evidence in a peer-round doc | Claude, ChatGPT verdict, 🔒 Jesse for deferrals |
| S-4 | Original flake demonstrated fixed: the de-flake commit present on whatever merges, plus N=10 consecutive full-suite runs, zero flake occurrences | run logs | Claude |
| S-5 | Thread-leak enforcement mode exists (`--threadleak-fail`, nonzero exit, with an injected-leak self-test) or is explicitly deferred by Jesse | commit + self-test, or recorded deferral | Claude / 🔒 Jesse |

## Track 4 — Security closeout (Track C)

Measured state 2026-08-02: S1/S2 fixes merged (PR #37) and **deployed** (in the
7/31 image). Manual history-wide gitleaks run done (703 commits, 2 findings,
both classified FP). No CI secret scanning, no committed allowlist, no
true-positive response procedure. Gotify token still plaintext at
`docker-port-watchdog.ps1:37` (Jesse chose "leave for now" — deferred by
decision, not forgotten); a second copy is implied in the WUD compose file.

| ID | Exit criterion | Evidence artifact | Owner |
|---|---|---|---|
| C-1 | Secret scanning runs on every push and PR, with a committed, commented allowlist | workflow file on main + one green run URL + `.gitleaks.toml` | Claude builds, 🔒 Jesse merges |
| C-2 | True-positive response procedure committed (who rotates, who is told, in what order, within what time) | doc in repo | Claude |
| C-3 | Gotify token: externalised + rotated, or the deferral re-confirmed and recorded in this contract's close-out | script diff + rotation, or recorded decision | 🔒 Jesse |
| C-4 | Stale-gist decision executed (superseded thread-leak doc `6a731cd9…`, round-5/6 package `3f9ce65c…` — both secret but link-readable; deletion is irreversible) | gists deleted OR retention recorded | 🔒 Jesse |

## Track 5 — Track D product items (batch exit, per plan rev 2 §5)

Every item ends as exactly one of: **implemented+tested+deployed+accepted /
deferred to a numbered issue with reason / rejected**. Acceptance wording from
rev 2 §5. Note: these are internal finding numbers, NOT GitHub issue numbers
(verified — GitHub resolves none of them).

| ID | Item | Measured state 2026-08-02 | Owner |
|---|---|---|---|
| D-1 | TV resolution filter + 720p chip | not started | Claude, Jesse accepts |
| D-2 | Full-disc setting in UI | not started (escape hatch `hdencode_skip_full_disc` exists backend-side; keep-vs-remove is O-3) | Claude, Jesse accepts |
| D-3 | HDR10+ labels + Kometa badges (#185) | half-done: `hdr10plus_detect.py` wired; labels/badges absent | Claude, Jesse accepts |
| D-4 | Documentary design pass | not started; exit = a recorded design decision, not necessarily code | Claude, Jesse accepts |
| D-5 | #192 RSS-spec docs correction | not started | Claude |
| D-6 | #184 scan counters (branch `agent/scan-failure-visibility` @ `9ec6665`, 8 commits, recording-only) | implemented but 25 commits behind main; needs rebase, then merge decision | Claude rebases, 🔒 Jesse merges |

## Track 6 — Ops leftovers

| ID | Exit criterion | Evidence artifact | Owner |
|---|---|---|---|
| O-1 | Missing-MANIFEST.json mystery resolved and rollback restored. **Recharacterised 2026-08-02:** current installer DOES write it (line 685, since `8d16fa8`), yet the 7/30 21:50 install left every other artifact and no manifest — cause unknown; without it `rollback-mount-task.ps1` hard-throws, so rollback is fully BLOCKED, not "degraded" | supervised elevated re-run showing MANIFEST.json on disk + verify script passing that check | Claude preps, 🔒 Jesse runs elevated |
| O-2 | Installer normalisation gap dispositioned: assert-don't-repair is a DELIBERATE design (recursing elevated ACLs over unvalidated content follows planted reparse points); the real defect is ordering — the assert runs AFTER task registration, so a bad pre-existing artifact aborts with the task already registered. Fix the ordering or record acceptance; sweep the stale 777ff0f-era script copies | commit + verify run, or recorded acceptance | Claude preps, 🔒 Jesse runs elevated |
| O-3 | Escape-hatch decision (`hdencode_skip_full_disc`: keep-with-warning vs remove; ChatGPT recommended removal, standing offer to overrule) | recorded decision | 🔒 Jesse |
| O-4 | BACKLOG.md TL-032 updated: approved-commit pin `740308c9` is four commits stale vs installed `eb31f9a` | backlog edit | Claude |
| O-5 | Catch-up doc + memory reflect verified reality | edits (done 2026-08-02: production identity, CI states, test outputs, security-deploy status) | Claude |

## Programme completion

Plan rev 2 §5's definition stands, extended: **programme complete = R-16 and
N-7 decisions recorded + Tracks 3–6 every item dispositioned + rollback and
operating instructions match the deployed system.** A documented
"do-not-promote" or "stay-frozen" outcome completes a track exactly as a
promotion or rollout does.

---

## Ordered plan (dependencies; qualification window scheduled earliest)

The window is the critical path: **~30 h bootstrap + 7 calendar days, nothing
compresses it.** Everything else fits inside or around it.

1. **Now → window open (critical path):** R-2 (inventory, started tonight,
   read-only) → R-3/R-4/R-5/R-6 (engineering, peer-reviewed round by round) →
   round-9 verdict closing the seven-item gate → R-7 Jesse approves thresholds
   → R-8 merge → R-9 build+pin → R-11 deploy → R-12 bootstrap → R-13 window
   opens. Jesse's part: one popup batch + merge + one build/deploy command.
2. **Inside the window (no deploys, same digest):** N-1, N-2 (CI), C-1, C-2,
   D-1..D-5 development (not deployed), S-2/S-3 reconciliation + merge-gate
   work, Phase B bridge build (not deployed). B5/B6 (N-3..N-5) in the separate
   pinned rename-test container, on Jesse's authorisation.
3. **Window close:** R-14 grade → Phase B (R-15) → R-16 decision.
4. **After promotion decision:** merge order per S-2 for the shutdown branches;
   deploy batches Track D + security + shutdown in ONE pinned rebuild (or more,
   Jesse's call); N-6/N-7 rename rollout decision.
5. **Any time (independent):** O-1/O-2 (elevated, Jesse-run), C-3/C-4, O-3, O-4.

## Assumptions made overnight (each is a morning popup)

1. This contract operationalizes plan rev 2 rather than replacing it.
2. The seven-item review gate is treated as blocking R-7..R-11 (Phase 0 exit).
3. Overnight work stays read-only/docs/CI-enablement; no production-behaviour
   code without a peer round.
4. "Do-not-promote" and "stay-frozen" are valid completions.
5. CI enablement commits on the two claude branches (`d0202da`, `8799255`) were
   in-scope git work, not a guardrail action.
6. R-2 (canonical-URL inventory) proceeds tonight since it blocks Phase A and
   is purely observational.

---

## Round-9 amendments (2026-08-03, applied from the amended verdict)

1. **Anti-proxy rule, contract-wide:** no criterion is satisfied by committing
   a document that describes the work, by deleting or narrowing a test, by
   checking only endpoint states, or by an undefined deferral. The evidence
   artifact must demonstrate the behaviour itself.
2. **R-2 reworded:** the inventory DOCUMENT does not complete R-2. R-2 closes
   only when the inventory's §5 criteria PLUS the §6 round-9 additions land:
   committed executable queries + controls + snapshot provenance,
   machine-readable outputs with fixed denominators (including the 1 residual
   miss and the 35 out-of-population exclusions), per-join "100%" definitions,
   consumer-boundary contract tests for every bridge, and a migration policy
   for persisted A/B keys.
3. **Track 5 gains the missing columns:** exit criterion and evidence artifact
   per item — D-1: filter provably classifies 720p/1080p/4K on a labelled
   corpus, test + UI screenshot; D-2: UI toggle round-trips to the SAME shared
   rule both paths call, test proving one predicate object; D-3: HDR10+ label
   present in Plex + Kometa overlay renders, API query + screenshot; D-4:
   committed design decision doc; D-5: corrected spec diff + a check that the
   corrected criterion matches deployed behaviour; D-6: rebased branch, CI
   green, counters demonstrated against injected failure AND cancellation.
4. **CI attestation is part of every evidence package:** run URLs, not
   commit-message claims (7681a87 = actions/runs/30811406913, success).

---

## Status snapshot — 2026-08-03 end of autonomous session

Legend: ✅ done+evidence · 🔨 engineering done, verdict pending · 📋 proposal
in round 10 · ⬜ open · 🔒 waiting on Jesse.

**Track 1:** R-1 ✅ (round 9) · R-2 🔨 (f0ed051/ee9567f/3c54d9a + refuter pass;
§5.5 re-measure is post-deploy by nature) · R-3 🔨 (a57c7ef/f172d1f, three
declared deltas) · R-4/R-5/R-6 📋 · R-7 🔒 approved-in-principle · R-8..R-16 ⬜.
**Track 2:** all ⬜ (scheduled inside the window). **Track 3:** S-1 ✅ (both
claude heads CI-green) · S-2 🔨 (order approved pending round 10) · S-3/S-4/
S-5 ⬜. **Track 4:** C-1+C-2 🔨 staged on agent/security-track-c @ 85ad01f 🔒
merge · C-3/C-4 🔒. **Track 5:** D-6 🔨 rebased clean as
agent/scan-failure-visibility-rebased @ ce3f10b 🔒 merge · D-3 half · rest ⬜.
**Track 6:** O-1 🔨 supervised wrapper delivered 🔒 Jesse runs · O-2 ⬜ (wait
for O-1's evidence) · O-3 🔒 · O-4/O-5 ✅.

**Cross-cutting blocker:** GitHub Actions billing failure — four heads
(f172d1f, 3409543, 85ad01f, ce3f10b) await machine attestation.
