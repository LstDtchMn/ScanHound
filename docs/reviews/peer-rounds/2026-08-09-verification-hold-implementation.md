# The verification hold — implementation for review

**Date:** 2026-08-09
**Author:** Claude
**Reviewer requested:** ChatGPT (adversarial review of the implementation against the eight agreed changes)
**Branch:** `claude/scanhound-turnstile-verification-hold-z43q0x` (base: `main`; includes the
`fix/queue-policy-test-time-bomb` clock pin as its first commit, which must merge anyway)
**Prior docs:** `hdencode-turnstile-root-cause.md` (the finding), and the review round that
produced the eight required changes.

> **REVIEWER: read the code from the repository, not this summary of it.** If you cannot
> read it directly, stop and say so.

## 0. What this closes

`decide()` in `backend/queue_recovery_policy.py` returned **AUTHORISED** for
`ItemFacts(state="verification_required", queue_reason="interactive_challenge",
cooldown_until=<expired>)` — a timer alone released a human-verification hold. With the
measured cause of the reveal stall being a Cloudflare Turnstile challenge that fails
identically on every automated attempt, reclassification without this fix would have fed
every item back into the challenge on a schedule.

**Explicitly out of scope, unchanged from the finding doc: nothing here attempts to pass,
solve, or evade the challenge.**

## 1. The eight changes, and where each lives

1. **Classification.** An ACTIVE Turnstile failure classifies as
   `reason_code=interactive_challenge` with `cause_code=turnstile_challenge_failed`
   (`TURNSTILE_CAUSE_CODE`, `backend/download_outcome.py`). No new top-level reason.
   `INTERACTIVE_CHALLENGE` was added to `_SIGNAL_BEARING_CODES`
   (`backend/scrape_outcome.py`) so the evidence list survives into `last_message` —
   the mechanism also travels in `last_cause_code`, so it survives log rotation twice over.
2. **Containment preserved.** `affected_scope="source"`; `_pause_for_source` still parks
   the episode (trigger → `verification_required`, same-source siblings →
   `waiting_source`, batch → `paused_source`). The same transaction now also writes
   `verification_hold_source`.
3. **Fallback preserved.** A not-ready reveal **without** active Turnstile evidence still
   classifies `REVEAL_VERIFICATION_STALLED` with unchanged retry semantics
   (`tests/test_reveal_verification_throttle.py` passes unmodified).
4. **The hold lives in the ONE policy.** `VERIFICATION_HOLD` is a decision in
   `queue_recovery_policy.decide()`, consulted by production (`_resume_batch`) and both
   operator tools (via `scripts/queue_recovery_state.py`) — not a pre-filter in
   `_maybe_auto_resume`. Its action is the new `ACTION_ATTENTION_REQUIRED`; `action_for()`
   remains fail-closed; `NEEDS_HUMAN` and `ALL_DECISIONS` extended
   (`test_every_decision_has_an_action` covers the mapping).
5. **No timer release.** `queue_reason == "interactive_challenge"` (or an active group
   hold) returns `VERIFICATION_HOLD` for every cooldown combination — the full 3×3
   temporal matrix is pinned. Placement in `decide()`: after SAFETY and OWNERSHIP (both
   still outrank it), before DISABLED/BUDGET/time (whose advice is wrong for a challenge
   row).
6. **One episode, one probe, affirmative release.** The hold is batch-level
   (`download_queue_batches.verification_hold_source`, schema v9). An explicit
   `retry_item` promotes exactly one probe; a probe that fails re-parks the episode
   without burning siblings; the hold clears ONLY in `_complete`, on a delivery that
   genuinely crossed the source boundary (`source_progress`), source-matched in SQL — a
   dedup "success" does not release. The v9 one-time migration
   (`DatabaseManager._mark_existing_challenge_pauses_held`, gated by `user_version < 9`)
   moves the currently-parked episode under the hold in one UPDATE, without rewriting
   item history; batches with nothing deferred, and ordinary throttle pauses, are left
   alone.
7. **Wording promises nothing.** UI label is now "Manual attention required"
   (`VerificationRetries.svelte`); the section text and the policy advice both state that
   a retry is a single probe and verification cannot be completed inside ScanHound.
8. **Detection is a conjunction.** `reveal_tier == "not-ready"` AND active evidence:
   `input[name=cf-turnstile-response]`, a `.cf-turnstile` container, a
   `challenges.cloudflare.com` iframe, or a **navigation-scoped** console error in the
   600* family (`turnstile_challenge_evidence` / `is_turnstile_console_failure`). The
   browser console log is drained at every navigation boundary
   (`_drain_browser_console`, called from `_wait_past_cloudflare`, which runs on the
   initial and the post-click navigation), so an old page's error cannot classify the
   next. Chrome's console log is enabled next to the existing performance log
   (`browser_adapter._enable_performance_log`). NOT evidence: dormant script references,
   navigation/control text (the "TV Shows" false positive is pinned), the word
   "cloudflare" in prose, and 600010 as an exact contract (the family matches).

## 2. Evidence

* `tests/test_verification_hold.py` — 41 tests: the policy matrix, detection
  discrimination (positives and the dormant/navigation-text negatives), the
  classification boundary through the real `_log_page_diagnostics`, the consumer
  integration through the real queue engine, the migration, and the operator-tool
  adapter path.
* **The load-bearing negative control:** 22 scheduled items, first hits an active
  Turnstile → 1 transport attempt, 1 `verification_required` trigger, 21 held siblings,
  0 failures; then EVERY cooldown expired and the automatic machinery run to quiescence →
  still 1 transport attempt, no state change.
* **Liveness oracle extended, both halves:** the RULE now names the hold as a legitimate
  no-automatic-transition state; a `challenge` operation was added to the enumerator; and
  the mirror safety test (`test_verification_held_items_are_never_auto_retried`) proves
  `settle()` cannot move a held episode.
* **Mutation checks, all discriminating** (defect restored → FAIL, fix restored → PASS):
  the policy hold branch neutralised → 3 tests fail; the detection conjunction
  neutralised → 2 tests fail; the `_complete` release neutralised → the release test
  fails.
* One pre-existing test updated: `test_auto_resume_scopes_source_and_preserves_
  unknown_outcome` asserted the challenge row promotes to `ready` — it pinned the
  defect. Its sibling/unknown-outcome assertions are unchanged.
* Full suite: run in the standard whole-tree container off `scanhound:latest`
  (result recorded in the session notes; the branch is not proposed for merge until it
  is clean).

## 3. Behaviour changes a reviewer should weigh

* **Single-item challenge retries (`enqueue_retry`) no longer auto-resume.** They were
  born `verification_required`/`interactive_challenge` and previously came back on the
  timer; they now hold until probed. Their creation path is only reachable for
  `interactive_challenge` and `source_temporarily_blocked` outcomes (the latter is
  born `source_deferred` and still auto-resumes), so no non-challenge row is affected.
* **The migration treats the current `reveal_verification_stalled` pauses as the
  challenge episode.** Justified only for the measured episode, which is why it is
  one-time (`user_version` gate) rather than inference that runs on every start. New
  stalls classify at grab time, where actual evidence decides.
* **Release is per-batch.** A successful probe releases its own batch's siblings. A
  second held batch needs its own probe; nothing cross-batch releases on another
  batch's success. Chosen deliberately: cross-batch release via the coordinator would be
  new machinery with its own failure modes, and the live episode is concentrated in one
  place. Say so if you think this is the wrong trade.
* **Manual `resume_batch` ("Retry all") still promotes every deferred row** in a held
  batch — operator override. The first probe failing re-parks the rest, so the blast
  radius is one transport attempt, matching `f96c08b6`'s production measurement. The
  advice text now tells the operator to probe one item instead.

## 4. What I could not verify here

* The live diagnostic the review listed as "do first" (non-evasive capture of Turnstile
  resource loads, CSP headers, iframe attributes, and the exact console sequence on the
  current navigation) has NOT been run in this session — it needs the production
  container. The classification work above is cause-agnostic (it keys on the challenge
  being present and failing, not on WHY it fails), but if that diagnostic shows a benign
  integration defect (e.g. a blocked subresource), the right remedy may make the
  challenge pass again — at which point held items release through the normal probe path.
* Chrome's `goog:loggingPrefs {"browser": "ALL"}` is exercised through a driver double
  here; whether the production chromedriver actually surfaces the Turnstile console
  entries under Xvfb needs one live observation before the console leg of detection is
  trusted (the DOM legs do not depend on it).
