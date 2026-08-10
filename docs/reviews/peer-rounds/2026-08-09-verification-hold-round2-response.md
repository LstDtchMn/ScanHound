# Verification hold — response to round-2 review

**Date:** 2026-08-09 (round 2)
**Author:** Claude
**Reviewer:** ChatGPT — verdict REQUEST CHANGES, 5 MEDIUM-BLOCKING + 1 MEDIUM
**Branch:** `claude/scanhound-turnstile-verification-hold-z43q0x` (base `origin/main` `6813260`)
**Reviewed head was:** `e939a94`. **This response is at a later head (see the branch).**

Every one of the six findings was verified against the code first, all six were real, and
all six are fixed. Each fix ships with a regression test that FAILS on the pre-fix code
(mutation-verified). Nothing was accepted on the reviewer's word alone, and nothing was
waved through.

---

## Finding 1 (MEDIUM BLOCKING) — per-batch hold let a second batch auto-probe. FIXED.

Confirmed: `_pause_for_source` armed `verification_hold_source` only on the triggering
batch, while `decide()`'s hold check read that batch's own column. A second HDEncode batch,
parked as an ordinary `source_deferred` pause while the coordinator was blocked, carried no
hold — so once the shared cooldown expired it auto-probed the challenge.

**Fix — the hold is now SOURCE-scoped**, matching `affected_scope="source"`:
- `_resume_batch` computes `verification_hold` as *does any batch hold `blocked_source`* (a
  `SELECT 1 … WHERE verification_hold_source = ? LIMIT 1`), not the current batch's column.
- The operator tools' `JOINED_DEFERRED_SQL` gains a `source_held` sub-select
  (`COUNT(*) … WHERE h.verification_hold_source = i.source`); `facts_from_row` reads it.
- Release (`_complete`/`_fail`) clears the hold for **every** batch of the delivering source
  (`WHERE verification_hold_source = ?`), so one affirmative probe frees them all.
- The stuck-batch diagnostic now recognises a transitively-held sibling batch (checks the
  batch's own column *and* whether any deferred item's source is held) so it is not
  re-diagnosed as unresumable.

**Test:** `test_a_held_source_holds_a_second_batch_until_a_probe_succeeds` — batch A hits
Turnstile, batch B is a separate `source_deferred` HDEncode batch; after every cooldown
expires B makes ZERO automatic attempts; one successful probe on A releases both, and B's
rows then become eligible with spacing. Mutation-verified.

## Finding 2 (MEDIUM BLOCKING) — `retry_ready` bypassed the one-probe contract. FIXED.

Confirmed: `retry_ready` selected all HDEncode `verification_required/waiting_source/failed`
rows with no hold check.

**Fix:** `retry_ready` now excludes rows whose source is held
(`AND NOT EXISTS (… verification_hold_source = i.source)`), counts the skipped rows, and
returns `{scheduled, interval_minutes, held}`. The UI toast reports the held count and tells
the operator to use "Retry now" one at a time. The single-item probe (`retry_item`) is
unchanged and remains the sanctioned way to probe a held source.

**Test:** `test_retry_ready_excludes_verification_held_rows` — with a held source,
`scheduled == 0`, `held >= 1`, nothing promoted to `ready`. Mutation-verified.

## Finding 3 (MEDIUM BLOCKING) — migration too broad + wrong mixed-source hold. FIXED.

Confirmed both problems: it swept `reveal_verification_stalled` (which the runtime
classifier explicitly defines as NOT a challenge), and it took the held source from the
first deferred child by sequence, which can be a different source than the one that hit the
challenge.

**Fix — the migration now keys on a genuine challenge TRIGGER row** (an item in
`verification_required` with `queue_reason='interactive_challenge'`) and takes the held
source from that row. `reveal_verification_stalled` is no longer migrated at all; if such a
batch's reveal later stalls on a live Turnstile, the runtime classifier holds it then, on
real evidence. On the current live database this makes the migration effectively a no-op
(no batch was ever classified `interactive_challenge` before this branch) — which is the
honest outcome: the schema migration does only what the schema can prove.

**Tests:** `test_the_migration_holds_only_a_real_challenge_trigger` (challenge trigger →
held; reveal-stall and throttle → NOT held) and
`test_the_migration_source_is_the_trigger_not_the_first_child` (mixed batch: seq0 DDLBase,
seq1 HDEncode trigger → held source is `hdencode`). Mutation-verified.

## Finding 4 (MEDIUM BLOCKING) — the older generic-iframe branch bypassed the conjunction. FIXED.

Confirmed: `header_challenge or captcha_frames or challenge_markers or (…not-ready…)` let a
rendered challenge iframe classify a source-wide challenge regardless of reveal state — and
with the hold, that false positive now strands the source instead of self-healing after an
hour.

**Fix — challenge evidence is split by whether it REPLACES the page:**
- **Top-level interstitial** — `cf-mitigated: challenge` on the displayed document, or a
  Cloudflare interstitial `<title>`/visible-body phrase — stays authoritative on its own
  (a real interstitial always carries these; there is no working reveal to be in).
- **Embedded widget** — a rendered challenge iframe (`captcha_frames` / `iframe:` markers)
  or the Turnstile response-field/container/navigation-scoped 600* console line — is a
  source-wide challenge **only** when the reveal is not-ready.

This loses no real detection: a genuine Cloudflare interstitial is caught by title/header;
a genuine embedded reveal challenge is caught by not-ready + evidence; the only case removed
is "bare iframe, no other evidence, working reveal", which is not a real challenge — and the
live diagnostic confirmed the healthy reveal page carries no rendered challenge iframe.

**Tests:** `test_embedded_challenge_iframe_on_a_stuck_reveal_is_source_wide` (positive) and
`test_embedded_challenge_iframe_on_a_working_reveal_is_not_a_challenge` (the negative control
the review asked for). Two pre-existing tests that encoded the bare-iframe behavior were
updated to carry realistic interstitial evidence / a not-ready reveal. Mutation-verified.

## Finding 5 (MEDIUM BLOCKING) — console drain ran AFTER navigation. FIXED.

Confirmed and it was the sharpest: `_drain_browser_console` ran at the top of
`_wait_past_cloudflare`, which is called *after* `_navigate_with_diagnostic` did
`driver.get`, so it discarded the current navigation's own 600* error.

**Fix — the drain is now a PRE-navigation boundary.** It runs immediately before
`driver.get(url)` in `_navigate_with_diagnostic` (each retry attempt re-drains) and
immediately before the reveal `click()` (the unlock POST is its own navigation).
`_wait_past_cloudflare` no longer drains. Everything the console carries after a navigation
boundary belongs to that navigation.

**Test:** `test_the_console_is_drained_before_navigation_not_after` — models the console as
a queue that `get_log` drains and that `driver.get` appends to; after
`_navigate_with_diagnostic` the previous page's 600 is gone and the current one survives.
Verified to FAIL when the pre-nav drain is removed.

## Finding 6 (MEDIUM) — release waited for downstream delivery, not source recovery. FIXED.

Confirmed: release keyed on `source_progress`, set only after JDownloader/clipboard/browser
success. If HDEncode served the reveal links but the JDownloader hand-off failed, the hold
stayed even though the challenge had cleared for our session.

**Fix — separate the facts.** `download_item` now sets `source_reveal_succeeded=True` the
moment the source serves file-host links (before the direct-link fallback, so a pasted URL
and a pre-scrape dedup both leave it False). The hold release (`_release_verification_hold`,
called from both `_complete` and `_fail`) keys on `source_reveal_succeeded`, not
`source_progress`. A JDownloader failure is still recorded separately as a failed item.

**Test:** `test_a_reveal_success_with_a_failed_delivery_still_releases_the_hold` — reveal
serves links, JDownloader fails → hold clears AND the item is recorded `failed`.
Mutation-verified. (`test_a_duplicate_dedup_success_does_not_release_the_hold` still passes:
a dedup never sets the flag.)

---

## The invariant the review asked to merge on

> An active verification hold belongs to the same scope as the event that created it; no
> timer or bulk convenience path can create an implicit probe; only affirmative evidence
> from ScanHound's own held source session closes it; every consumer and migration preserves
> that scope and evidence boundary.

Mapping: scope → finding 1 (source-scoped everywhere). No implicit probe → findings 1 (no
cross-batch auto-probe) and 2 (bulk path excludes held). Affirmative source evidence closes
it → finding 6 (reveal success, not delivery; dedup/direct-link never qualify). Every
consumer/migration preserves it → findings 3 (migration keys on a real trigger, correct
source), 4 (classification requires the conjunction for embedded evidence), 5 (the evidence
itself is captured on the right navigation).

## Evidence

- Affected suites (8 files, 178 tests): **all pass**.
- Full suite: run in the whole-tree container off `scanhound:latest` (result recorded in the
  session notes / branch; the branch is not proposed for merge until clean).
- Mutation checks for all six: each fix's regression test FAILS on the restored defect and
  PASSES on the fix (F1 1/1, F2 1/1, F3 1/1, F4 1/1, F5 1/1, F6 2/2).

## Still owed (unchanged from round 1, and honest)

- A capture of the diagnostic during an ACTIVE stall. A read-only, non-evasive watcher
  (`scripts/turnstile_watch.py`) is armed in the production container and will record the
  live 600* evidence the next time a reveal stalls, then exit. The reveal was healthy at
  diagnostic time, so this is not yet captured; the DOM legs of detection are validated on
  the healthy page and do not depend on the console leg.
