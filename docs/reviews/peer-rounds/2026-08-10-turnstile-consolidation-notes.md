# Turnstile consolidation notes — for whoever folds the two branches

**Date:** 2026-08-10
**Author:** Claude (session on this branch)
**Status:** joint decision reached; **nothing merged** (Jesse's call). This note travels with
the branch so the consolidation does not depend on a chat message surviving.

## The situation

Two sessions independently built the same Turnstile verification hold, both forked from
`main` (`6813260`):

- **This branch** `claude/scanhound-turnstile-verification-hold-z43q0x` (head `c05186c`, 9
  commits) — 2 ChatGPT review rounds, full suite 4672/0 mutation-verified, plus the only live
  active-stall capture.
- **`agent/turnstile-classification`** (session `b087aa20`, head `2103d86`, 5 commits) — 1
  review round, no mutation verification, no live capture.

They conflict on **10 files / 17 hunks** (`git merge-tree`). Designs converged: same
`VERIFICATION_HOLD` decision above the ownership/time checks, same "resume is a probe" model,
same interstitial/embedded classification partition.

## Joint decision (both sessions agreed; b087aa20 argued against their own branch)

**Use THIS branch as the base; fold `agent/turnstile-classification` onto it, then one combined
peer round.** Deciding factor was correctness, not review count: this branch releases the hold
from **both** `_complete` and `_fail` (`source_reveal_succeeded`), so a good reveal followed by
a failed JDownloader hand-off still clears the hold; b087aa20's released only from `_complete` —
a real gap they found and verified in their own code.

## TWO REAL GAPS IN THIS BRANCH the consolidation must fix (both verified in this code)

1. **`resume_batch` (manual, `automated=False`) bypasses the hold.** Its branch selects all
   deferred rows and promotes them without calling `decide()`, so a manual batch resume fans a
   held batch back into the challenge. Round-2 finding 2 guarded `retry_ready` only — this is the
   unguarded second door. `agent/turnstile-classification` raises from **both** `resume_batch`
   and `retry_ready`; take its `resume_batch` guard. (Consider keeping this branch's
   `retry_ready` exclude-and-report `{scheduled, held}` behaviour for the bulk-button UX rather
   than a hard raise — reconcile, don't take either wholesale.)
2. **No operator escape hatch.** The only thing that clears `verification_hold_source` is a
   reveal success (`_release_verification_hold`), so a permanently-challenged source **deadlocks**
   — no successful grab is possible because the hold blocks it, and the only exit is
   `cancel_item`/`cancel_batch`. Take b087aa20's `clear_challenge_episode()` (~15 lines): an
   explicit operator abandon.

## Also port from `agent/turnstile-classification`

- Its **22-item negative-control harness** that RAISES on any unscripted `download_item` plus a
  paired positive control proving the rig CAN promote when nothing is held. This branch's control
  asserts `call_count == 1` (softer) and lacks the positive control (a rig that never promotes
  would pass silently).
- Diff its `_form_posts_unlock` (response-field form association: submit `formaction` overrides
  `form.action`) against this branch's reveal-form logic.
- Keep **both** migrations: this branch's inline v9 (keyed on a real `verification_required` +
  `interactive_challenge` trigger row) for automatic future correctness, and b087aa20's standalone
  `scripts/migrate_challenge_episode.py` (explicit named trigger IDs, refuses to infer) for a
  named-incident force-hold.

## The combined round MUST add this test (silent-regression risk)

A **body-only "just a moment" interstitial** — no `<title>`, iframe the only evidence — must
still classify `INTERACTIVE_CHALLENGE`. b087aa20's first partition attempt over-narrowed and
demoted such interstitials to `LAYOUT_CHANGED`; it was caught by the full suite, not review.
This branch's `top_level_challenge = header_challenge or bool(interstitial_markers)` uses the
non-`iframe:` markers, which come from `<title>`/visible-body — a body-only interstitial with no
title has only iframe evidence, so if the partition is resolved by taking either side wholesale
the regression can return silently. This branch has title-based interstitial coverage in
`test_scrape_outcomes.py` but NOT the body-only-no-title case. Add it.

## Open items neither branch closes (state as non-claims in the combined doc)

- Cross-batch containment beyond auto-resume leans on the coordinator's **in-memory** cooldown,
  which resets on container restart.
- A **non-Turnstile captcha frame** on a not-ready reveal classifies source-wide in this branch
  too (`captcha_frames` gated on not-ready, no form association tested).
- This branch's v9 migration is a **no-op on current live data**: the parked rows are
  `reveal_verification_stalled` (they predate the classifier), so no
  `verification_required` + `interactive_challenge` trigger row exists to migrate. Verify the
  migration finds a trigger at all against the real DB before relying on it.

## Method notes (dead ends, so they are not re-derived)

- **Reloading does NOT catch the intermittent stall.** Both sessions hit this — 3–6 consecutive
  reloads all read healthy. Only time-spaced polling works: `scripts/turnstile_watch.py`, 10-min
  cadence, caught it on cycle 20 (~3h). Read-only, exits on first catch.
- **Cloudflare's documented markup finds nothing here.** hdencode renders Turnstile
  programmatically in **invisible** mode, tearing the frame down and rebuilding ~every 11s, so
  `.cf-turnstile`, `data-sitekey`, and a queryable challenge iframe are usually absent. The two
  signals that fire reliably are the **unsolved `cf-turnstile-response` field** and the
  **console 600\*** line. Keep the container/iframe legs (correct where they apply) but do not
  rely on them.

## Date-bomb branches

`a88d541` on this branch (= `fix/queue-policy-test-time-bomb`, already in the DV consolidation)
is the KEPT date-bomb fix. `fix/policy-tests-wall-clock` (b087aa20's separate fix for the same
bomb) is redundant — close it once this branch is the turnstile base.
