# Turnstile fold — response to the review round

**Date:** 2026-08-10
**Reviewers:** ChatGPT (heads `2453172` and `f330831`) + b087aa20 (peer, head `2453172`/`f330831`)
**Branch:** `agent/turnstile-consolidation`. **Head after this response: in the relay message.**
**Nothing merged** (Jesse's call).

Both reviewers independently agreed on the top findings. Each was verified against the code
first — all were real — and each fix ships with a regression test that FAILS on the pre-fix code
(mutation-verified).

## F1 (HIGH) — `resume_batch` check/use race. FIXED.

Confirmed: `resume_batch` checked `_source_is_held` in one transaction; `_resume_batch`'s
non-automated branch promoted rows in a second, with no re-check — a worker could arm the hold
between them.

**Fix:** the AUTHORITATIVE hold check now runs inside `_resume_batch`'s promotion transaction,
immediately before the non-automated row selection, and raises (rolling the transaction back)
before any row is promoted. `resume_batch`'s outer check is retained only as a fast message. The
inaccurate "one predicate" comment on `_source_is_held` is corrected — `retry_ready` and the
automated `decide()` path each phrase the equivalent membership test in their own single
transaction (safe there; the comment now says so).

**Test:** `test_resume_batch_is_hold_safe_against_a_check_use_race` calls `_resume_batch` directly
(simulating the hold armed after the outer check) → raises, promotes nothing. Mutation-verified.

## F2 (HIGH) — Turnstile response-field discrimination was dropped. FIXED.

Confirmed: `turnstile_challenge_evidence` counted any `cf-turnstile-response` field, including a
populated (solved) token, and never checked the field's owning form — so a Turnstile widget on
the page's comment/report form was reveal evidence. My "base already has this via
`_resolves_to_unlock_target`" was wrong: that validates the CLICK target, not evidence
attribution.

**Fix:** ported the other branch's semantics. A populated token is ignored (a solved challenge is
not failure evidence). For an empty token, the owning form is resolved by nesting OR a `form="id"`
attribute, and its EFFECTIVE submit target (respecting `formaction`-overrides-`action`, via a
ported `_form_posts_unlock`) must be this page's unlock endpoint — the caller injects that
predicate (`_resolves_to_unlock_target` against the page URL), so there is one copy of the rule.

**Tests:** solved token → not evidence; comment-form field → not evidence; unlock-form field →
evidence; `form="id"` ownership honoured; `formaction` override decides ownership. The
solved-token skip and the ownership gate are both mutation-verified.

## F3 (HIGH) — the structural interstitial guard could still hold a working page. FIXED.

Confirmed: `interstitial_shape` required `not candidates` (lexical labels) but not `host_links`.
The reviewers' reachable counterexample: a post-click page for a DIFFERENT requested host exposes
a real Rapidgator link labelled just "Rapidgator" (no lexical keyword → `candidates` empty) while
the invisible Turnstile iframe is transiently present and `reveal_tier` is None → a working page
armed a source-wide hold.

**Fix:** `interstitial_shape` now also requires `not host_links` (actual
Rapidgator/Nitroflare/1fichier/DDownload URLs) — the stronger "this page is working" signal.

**Test:** `test_a_control_less_page_with_a_host_link_is_not_an_interstitial`. Mutation-verified.
The carried non-claim stands: a persistent GENERIC captcha frame (reCAPTCHA/hCaptcha) on the
qualifying control-less shape is still source-wide; keying the shape strictly on Turnstile is a
possible future tightening, noted not done.

## F4 (MEDIUM) — the standalone migration lost tests + had reporting/validation gaps. FIXED.

- **Atomic validation:** the trigger `UPDATE` now repeats the `state IN (...) AND source = ?`
  predicate inside the write transaction and asserts `rowcount == 1`, raising (rollback) otherwise
  — a row that moved between validation and the write can no longer be forced back to
  `verification_required`.
- **Invocation-scoped report:** it reports the batches THIS run held (`len(batches)`), not every
  batch already carrying the source hold.
- **`--hold-batch` membership:** each `--hold-batch` must contain a deferred row for the source,
  so a DDLBase-only batch can't be stamped with an HDEncode hold.

**Tests:** requires a named trigger (exit 2); dry-run writes nothing then `--apply` holds; a
typo'd trigger writes nothing (exit 1); a `--hold-batch` with no source row is refused (exit 1).
The hold-batch membership check is mutation-verified. (Note: the fail-fast subprocess harness was
not copied literally; these exercise `main()` in-process, which covers the same promises.)

## F5 (LOW) — clear route lacked a test + operator surface. ADDRESSED.

- **Route test added** (`test_clear_verification_hold_route`): 503 when the queue is unavailable;
  delegation + response otherwise.
- The clear **response now reports** `remaining_triggers` and `next_action` (probe one item), so
  the operator knows the trigger is still held and why.
- **Auth confirmed:** `POST /download/verification-hold/clear` is gated by the app-wide
  `auth_middleware` (the `download` segment is protected), identical to resume/retry/cancel. No
  per-route change needed.
- **UI:** the route is API-only for now; wiring a UI control is a separate deployment choice.

## Corrections accepted, not changed

- **Do not auto-release the trigger on clear** — both reviewers agree; the trigger has the direct
  evidence, siblings are held by association, and the probe is one click. Kept separate.
- **409 for `resume_batch`** is correct (not a silent downgrade). Kept.
- **The route path is `/download/verification-hold/clear`** (singular prefix) — docs corrected.
- **Cross-batch containment beyond auto-resume** (new/scheduled work for a durably-held source
  after a restart resets the coordinator's in-memory cooldown) remains an explicit open gap, true
  of both original branches. Not closed here.

## Evidence

- Affected suites: 88 passed (`test_verification_hold` + `test_scrape_outcomes`), 141 across the
  classifier-touching suites.
- Full suite: result in the relay message (expected green).
- Fold-review mutations discriminate: resume race, host_links guard, solved-token skip,
  form-ownership gate, migration hold-batch membership — each FAILS on the restored defect.
