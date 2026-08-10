# Turnstile fold — review request

**Date:** 2026-08-10
**Repository:** `LstDtchMn/ScanHound` (private; use the GitHub connector)
**Branch:** `agent/turnstile-consolidation`
**Base of the fold:** `claude/scanhound-turnstile-verification-hold-z43q0x` @ `c05186c` (the agreed
base; already had 2 ChatGPT rounds + a live active-stall capture)
**Folded in:** `agent/turnstile-classification` @ `6b0b1a5` (the other independent build)
**Head SHA:** in the relay message accompanying this request.
**Working tree:** clean. **Nothing merged, nothing deployed** (Jesse's call).

> **REVIEWER: read from the repository via the connector, not any chat summary. If you cannot
> read the branch, stop and say so.**

## What this is

Two sessions independently built the SAME Turnstile verification hold (a download hold a timer
cannot release). They agreed on a base — this one — because its hold-release fires from both the
success and failure paths (`source_reveal_succeeded`), whereas the other released only on
`_complete`. This branch **folds the other's four genuinely-better pieces onto the base**. The
base is well-reviewed; **please focus this round on the FOLD** — the newly added/changed code and
the combination's seams — not a re-review of the base.

The two branches used **divergent data models**: this base scopes the hold with a
`verification_hold_source` string column on the batch; the other used an opaque
`challenge_episode_id`. So the fold is a **deliberate port adapted to the source-column model**,
not a wholesale merge (which would have dropped the very pieces being ported and imported tests
written against the other model).

## The fold, port by port (all in the head commit)

1. **`clear_verification_hold(source)` + `POST /downloads/verification-hold/clear`**
   (`backend/download_queue.py`, `backend/api/routes/downloads.py`). The operator escape hatch the
   base lacked: the hold clears only on an affirmative source reveal, so a permanently-challenged
   source would deadlock (the only other exit being to cancel the items). Adapted from the other
   branch's `clear_challenge_episode`. It is NOT wired to any timer or `_maybe_auto_resume`. The
   other branch had the method but no route; this adds one.
2. **`resume_batch` guard** (`backend/download_queue.py`). The base guarded the bulk `retry_ready`
   path but NOT the manual `resume_batch`, whose non-automated branch promoted every deferred row
   **without calling `decide()`** — an unguarded second door. It now refuses (surfaced as HTTP 409)
   while a hold is open, pointing at the single-item probe. `retry_ready` keeps its
   exclude-and-report `{scheduled, held}` UX (the agreed reconciliation, not either side wholesale).
   A new `_source_is_held(conn, source)` is the one predicate all paths share.
3. **Stricter test harness** (`tests/test_verification_hold.py`). The 22-item negative control now
   drives the queue to quiescence (`_drive_to_quiescence`) so a wrongly-promoted held sibling
   becomes a real transport attempt, not a soft state check a never-executed queue would pass.
   Added the PAIRED POSITIVE control (`test_the_rig_can_promote_when_nothing_is_held`) proving the
   rig can promote when nothing is held — the base lacked it.
4. **`scripts/migrate_challenge_episode.py`** — the auditable named-incident force-hold (explicit
   `--trigger` item IDs, refuses to infer, dry-run by default), adapted to `verification_hold_source`
   and kept alongside the inline v9 migration.

## The one behaviour fix (not a pure port) — please scrutinise

**Body-only interstitial regression.** Both branches' interstitial/embedded partitions shared a
defect: a genuine Cloudflare interstitial that renders its phrase in the BODY (no `<title>`, no
captured `cf-mitigated` header) with a challenge iframe was demoted from `INTERACTIVE_CHALLENGE`
to `LAYOUT_CHANGED`.

**This section was ALREADY through one peer round** (`agent/turnstile-classification`'s author).
My first fix keyed on the challenge iframe + a body phrase; they showed it false-positives,
because invisible Turnstile renders a TRANSIENT iframe (~11s build/teardown) on otherwise-working
pages and release pages carry "access denied"/"just a moment" as related-release NAMES — so a
working page could be held on a source-wide challenge. **Fixed** to their recommendation: guard on
the STRUCTURAL property a page-replacing interstitial has and a working page never does —
**no reveal control found** (`reveal_tier` None/"none") **AND no access/download/link controls**
(`candidates` empty), with a rendered challenge iframe (`backend/download_service.py`, the
`interstitial_shape` term; the body-phrase logic was removed from
`backend/download_outcome.strong_challenge_markers`). Phrase- and lifecycle-independent. Tests:
the positive case (bare interstitial → still a challenge) and their counterexample (working page,
ready reveal, transient iframe, "Access Denied" related title, controls present → NOT a challenge)
are both in `tests/test_scrape_outcomes.py`, and both are mutation-checked. Please sanity-check the
structural guard for any remaining gap.

`_form_posts_unlock`: the fold instructions listed it as a possible port; the base already
implements that rule (`_resolves_to_unlock_target` + the `formaction`-overrides-`form.action`
logic in `_reveal_candidate`), so nothing was ported.

## Evidence

- Full suite (whole tree, container off `scanhound:latest`, `git archive HEAD` → fresh dir,
  `pip install pytest pytest-asyncio "httpx<0.28"`): **result in the relay message** (expected
  green; affected suites already 301 passed).
- Fold mutation checks — each new behaviour's test FAILS on the restored defect and PASSES on the
  fix: `resume_batch` guard 1/1, `clear_verification_hold` 2/2, body-only interstitial 1/1,
  positive control 1/1.
- The base's evidence stands: `docs/reviews/peer-rounds/turnstile-active-stall-capture-2026-08-10.{json,md}`
  (live 600010 + all `challenges.cloudflare.com` requests HTTP 200), the round-2 response, and the
  fold instructions (`turnstile-fold-instructions.md`).

## Open / unverified (carried, non-claims)

- The v9 inline migration is a **no-op on current live data**: parked rows are
  `reveal_verification_stalled`, predating the classifier, so no
  `verification_required` + `interactive_challenge` trigger row exists to migrate. The standalone
  script is the path for a verified named incident.
- **Cross-batch containment beyond auto-resume leans on the coordinator's in-memory cooldown**,
  which resets on container restart (true of both original branches).
- A **non-Turnstile captcha frame on a not-ready reveal classifies source-wide** (a generic frame
  carries no form association to test).

## Questions for this round

1. **The port fidelity.** Did adapting the other branch's episode-id semantics onto the
   `verification_hold_source` column preserve them? In particular: `clear_verification_hold` clears
   the source hold but leaves the trigger row held by its own `queue_reason` (so siblings recover
   but the trigger still needs a probe) — is that the right operator semantics, or should the
   escape hatch also release the trigger?
2. **The `resume_batch` guard.** Is refusing (409) correct, versus silently downgrading? Any
   remaining path that promotes a held row without `_source_is_held`/`decide()`?
3. **The interstitial structural guard** (already through one peer round — see above). Is
   "rendered challenge iframe AND no reveal control found AND no access/download/link controls" the
   right signal for a page-replacing interstitial? Any real interstitial it would still miss (e.g.
   one that renders some link-like control), or any working page that could present as control-less
   with a challenge iframe?
4. **The stricter harness.** Is the positive control meaningful — does it actually prove the rig
   promotes, so the negative control is not vacuous?
5. **Did the fold drop anything** from either branch that mattered?
