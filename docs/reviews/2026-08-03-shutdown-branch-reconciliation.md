# S-2 — Shutdown-branch reconciliation (merge order + conflict plan)

**Date:** 2026-08-03 · **Author:** Claude · **Reviewer:** ChatGPT (round 10) · **Decider:** 🔒 Jesse
**Inputs (all measured 2026-08-02/03, not recalled):** branch inventories of
`claude/nice-meitner-2b717b` (now `ac463c7`) and
`claude/nostalgic-brattain-946f4f` (now `2f8c898`), both CI-green as of
2026-08-03 morning — the first machine-attested runs in their history.

## Recommended order

1. **`agent/hybrid-sweep-implementation` merges first** (already required: it
   is the RSS-promotion prerequisite, contract R-8, 🔒 Jesse). Relevantly for
   this doc, it carries the RSS-cancellation de-flake (patch-id
   `fba0caf2…`, as `9b059c5`) and the two main-level CI fixes — so after this
   merge, main has the de-flake and a green nightly.
2. **`claude/nostalgic-brattain-946f4f` second.** It is the smaller, coherent
   shutdown-join line: bounded thread join, corrected executor cleanup
   (`2b3c880`), SMTP timeout (30 s default / 300 s clamp / file-only key),
   desktop notifications off-by-default + 409 test route. Its open items are
   either policy-closed (desktop dispatch) or explicitly deferred to the
   *other* branch's line of work (the three lifecycle P0s). Rebase onto
   post-1 main; short round; merge gate = its round-1 verdict's nine items,
   each closed or 🔒-deferred with Jesse's sign-off recorded (contract S-3).
3. **`claude/nice-meitner-2b717b` last, rebased onto post-2 main.** It is the
   long-running line — Phase 3 steps 3–7 (metadata future cancellation,
   deadline propagation, staged commit, executor completion, bounded join)
   and Phase 4 are unstarted — so it stays open longest and should carry the
   rebase burden, not impose it. The three lifecycle P0s (application-wide
   deadline, generation fencing, `begin_lifespan()` safety) land HERE.

## Conflict plan (measured overlaps)

* **`tests/tools/threadleak.py` — guaranteed add/add.** Keep **brattain's**
  copy (blob `de2f9d61`, 61 lines): it is the independently-evolved
  descendant carrying the `hookwrapper=True` teardown fix, which the plugin
  MUST retain. Meitner's copy (`eeb47b6b`, 43 lines, from the de-flake
  cherry-pick `c20745c`) is the older instrument. Brattain's own round-1
  package already records this exact resolution.
* **Five shared backend files** (`api/dependencies.py`,
  `api/routes/background.py`, `api/routes/scanner.py`,
  `api/routes/scheduler.py`, `scanner_service.py`): meitner rebases across
  brattain's versions; expect mechanical conflicts only — brattain touches
  shutdown/notification seams, meitner touches attribution/fence seams in the
  same files.
* **`tests/test_feature_pack_integration.py`:** the de-flake arrives via
  step 1 (hybrid-sweep). Meitner's `c20745c` becomes empty on rebase
  (patch-id identical) and drops out cleanly; brattain never had it and picks
  it up from main.
* **Two shared handoff docs** under `docs/reviews/peer-rounds/`: identical
  ancestry, take either side.

## Verification at each step

Per the contract's evidence rules: rebase → CI green on the rebased head
(both 3.11/3.12 legs + frontend) → the S-4 flake demonstration on whatever
merges (10 consecutive full-suite runs, zero occurrences of the RSS
cancellation flake) → ChatGPT round on the rebased head before 🔒 Jesse
merges. The claude/* CI trigger commits (`d0202da`/`8799255`) must survive
the rebases or be re-applied — without them these branches go dark again.

## Open questions for the round-10 verdict

1. Does brattain's nine-item merge gate allow merging with the three
   lifecycle P0s still open, given they are explicitly assigned to meitner's
   line? (Our reading: yes — the round-3 response calls them "explicitly
   separate later work" — but the reviewer wrote the gate, so the reviewer
   confirms the reading.)
2. `desktop_notifications`: inherited `true` on upgrade is a recorded P1
   (fresh installs get `false`, existing installs keep their saved value).
   Merge as-is, or require the explicit opt-in migration first?
3. The Q18 threadleak detector rework (`--threadleak-fail` enforcement mode)
   — S-5: block brattain's merge on it, or track it as meitner-line work?
