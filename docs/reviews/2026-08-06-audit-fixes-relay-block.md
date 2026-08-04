# Audit-fixes relay block (Jesse: paste the fenced block to ChatGPT)

**Round 2.** The five blockers from the 2026-08-06 review are closed at `9438d94`;
this block replaces the round-1 text.

```
Peer review ROUND 2 -- audit fixes. All five blockers closed. Read the
artifacts, not any summary; if you find yourself reviewing a summary,
STOP.

Repository: LstDtchMn/ScanHound
Branch: agent/audit-fixes-2026-08 (resolve its tip; commits after the one
  below are documentation only)
Last CODE commit -- this is what to review:
  9438d94b51df53475721d0b4832be34dc6a69bba
Code commits, oldest first: 440682d, 93f7060, fa56dfd, 03f0569, d0edea8,
  9438d94
Base: main @ 7adb17b
Prior reviewed head: 5387df2

Every blocker was re-verified against the code before changing
anything, and you were right on all five -- including about a test of
mine that pinned the wrong behaviour.

1. GENERIC "not found" REMOVED. Confirmed: the bare substring also
   matched "input file not found" / "video track not found" / "NAL
   unit not found", and a file can vanish between the isfile() check
   and the subprocess -- the exact mount failure this module exists to
   classify honestly. Absence is now asserted only by RPU-SPECIFIC
   messages (_NO_RPU_MESSAGES: "no rpu", "rpu not found", "no dolby
   vision rpu"). Your four generic strings are committed as negative
   controls asserting 'unknown'. A clean exit with an empty RPU still
   means 'none'.

2. 'unknown' IS NOW NON-DESTRUCTIVE IN EVERY MODE. You were right that
   my "negative control" pinned behaviour contradicting this module's
   own invariant. reconcile_movie now decides removal per case:
   'unknown' never removes, in any mode; an authoritative layer
   (including 'none') removes in any mode; an UNMATCHED title keeps the
   pre-existing policy (full reconcile removes, additive_only does
   not). That test is REVERSED, with two controls proving the guard did
   not break what it must not: authoritative 'none' still removes, and
   an unmatched title still removes under full reconcile.

3. MULTIPART AGGREGATION HAS AN EXPLICIT CONTRACT. Reproduced your
   finding by execution before fixing: pick_layer(['none','unknown'])
   returned 'none' or 'unknown' depending purely on part ORDER, and one
   'none' part plus one unscanned part returned 'none'. Now: a positive
   finding wins by rank; ANY missing or unknown part makes the
   aggregate 'unknown'; only all-parts-authoritative-'none' is 'none'.
   All four of your cases are tested, both orders included, plus an
   end-to-end assertion that a mixed title keeps its badge.

4. THE PAUSE MOVED TO THE DESTRUCTIVE BOUNDARY. Confirmed the bypass
   you named: process_package() calls apply(automatic=True) itself when
   a job matches and confirmation is off, never touching queue_apply --
   so the pause was false for precisely the UNATTENDED case. The
   authoritative check is now the first thing apply() does, before any
   filesystem mutation; queue_apply keeps its copy as the early,
   friendly response for the HTTP routes. Tests cover direct apply(),
   the process_package path with confirmation disabled, queued
   single/bulk/confident, undo still available, keep-Plex still
   available, and everything working again after re-enabling.

   NOTE, since your Q3 reviewed the older head: the toggle is no longer
   auto_rename_enabled. It is a dedicated rename_manual_apply_enabled
   (default True), plumbed through the config model, the SettingsUpdate
   API model, the frontend type and a labelled Settings toggle -- so
   pausing automation no longer forces giving up manual renaming, which
   is the split you described as the longer-term shape. Three
   pre-existing guard tests caught real gaps in that change (the
   expected-keys set, the UI-editable-keys check proving it would have
   422'd, and svelte-check on the frontend type).

5. THE ONE-TIME RESCAN IS A REQUIRED SHIPPING STEP, not a follow-up --
   the repo owner decided that explicitly. It is STEP 1 of
   docs/reviews/2026-08-06-dv-full-coverage-setup.md, which also
   carries the blocking dependency you would want flagged: the host
   scanner imports dv_detect FROM THE WORKING TREE, so scanning before
   this branch lands would rewrite the same false verdicts at
   4,344-file scale. Taking your point about the boundary, the step
   selects by VALUE ('none'/'unknown' under source='scan') rather than
   by a remembered count of 15.

Evidence: red-first -- 11 tests fail on the unfixed tree (11 failed /
218 passed), including both rename tests, which proves the bypass was
real rather than theoretical. Green at 9438d94: 571 passed / 0 failed /
exit 0 across dv_detect, dv_labeler, dv_scan_db, dv_import,
dv_host_scan, all four rename suites, the watchlist route and config.
CI remains billing-blocked (declared).

Your instruction to keep the DV producer and consumer commits together
is recorded in the branch and in this block: do not cherry-pick 440682d
without 93f7060 and 9438d94.

Verdicts requested:
Q1 Do all five blockers close?
Q2 The multipart contract: is "any missing or unknown part -> unknown"
   the right aggregate, or should a missing part be distinguished from
   a failed one?
Q3 Merge order given the overlap you identified with hybrid-sweep
   (backend/database.py, backend/rename/service.py,
   tests/test_rename_service.py) -- audit-fixes first, then rebase
   hybrid, or the reverse?
```
