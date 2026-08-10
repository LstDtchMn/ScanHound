# Handoff: Turnstile classification + recovery semantics

**Session date:** 2026-08-09
**For:** the main ScanHound development chat
**Branches pushed:** `agent/turnstile-classification`, `fix/policy-tests-wall-clock`
**Nothing merged. Nothing deployed. No production data changed.**

---

## 1. What this session was asked to do

A previous session found that HDEncode's "Verifying… Please wait" reveal stall is
a **Cloudflare Turnstile challenge failing with error 600010**, and wrote it up in
`docs/reviews/peer-rounds/hdencode-turnstile-root-cause.md`. ChatGPT reviewed
that finding and returned eight required changes plus one diagnostic that was
still open.

This session did the diagnostic first, then implemented all eight.

**Explicitly out of scope, and not done:** anything that attempts to pass, solve,
or evade the challenge. Nothing changes how the browser presents itself. The work
is *correct classification and correct recovery semantics*, so a person is
involved instead of a pointless retry loop.

---

## 2. The diagnostic, and how it changed the conclusion

Run against the live parked release
`hdencode.org/being-erica-s02-1080p-nf-web-dl-dd5-1-x264-ntb-18-3-gb/`, capturing
only the current navigation, from inside the `scanhound` container using a
separate throwaway-profile Chromium with ScanHound's exact flags. The production
browser session was never touched.

| Question | Result |
|---|---|
| Requests to `challenges.cloudflare.com` | 4 (api.js + 3 challenge-platform documents) |
| `Network.loadingFailed` on any Turnstile resource | **0** |
| HTTP status of the Turnstile script | **200**, `application/javascript` |
| HTTP status of the challenge frames | **200** × 3, `text/html` |
| CSP on the displayed document | **none** — no header, no meta tag |
| `input[name="cf-turnstile-response"]` | **present, value empty**, inside the form posting `#unlocked` |
| `window.turnstile` | `object` — the API loaded and initialised |
| Console | `[Cloudflare Turnstile] Error: 600010.` at t+2.6s, t+13.6s, t+25.7s |

**Everything Turnstile needs loaded and communicated normally. Only the verdict
failed.** By the review's own criterion that is the case where automation
rejection is the dominant explanation, and an integration defect is ruled out —
so the remedy is classification, not a change to how the widget is embedded.

Two open questions from the review are now closed:

* **The `postMessage` warning is incidental, not causal.** It appeared in the
  first capture and **not** in later captures that still produced 600010.
* **Cloudflare's more specific codes never appeared** — no 200500 (iframe load
  error), no 200100 (clock/cache), no 110600 (timeout). Consistent with healthy
  resources and a failed verdict.

### The finding that narrowed the conclusion: the gate is INTERMITTENT

Twenty minutes later, same URL, same container, same flags:

```
load 1..6:  widget=no   reveal='View links'   disabled=False   600errs=0
```

Six consecutive loads presented **no Turnstile widget at all**, with the reveal
control ready and enabled.

So the accurate statement is:

> Turnstile is active on the reveal flow **when hdencode chooses to present it**,
> and when presented it fails to complete in ScanHound's session, producing a
> 600\* generic challenge failure.

`navigator.webdriver` is `True` and chromedriver adds `--enable-automation` and
`--test-type=webdriver`. That is **consistent** with a challenge refusing to
complete. It is **not proven** to be the trigger and is not claimed as one.

### This broke two of the three planned detection signals

The review proposed keying on `.cf-turnstile`, a `challenges.cloudflare.com`
iframe, or the console error. Measurement showed:

* **No `.cf-turnstile` container and no `data-sitekey`.** hdencode renders the
  widget programmatically into `#turnstile-container-<hash>`.
* **No queryable challenge `<iframe>`.** The widget runs in **invisible** mode —
  it builds a frame, fails, tears it down, and retries about every 11 seconds —
  so a DOM read usually lands between attempts.
* There are **no shadow roots**, so nothing was hiding there either.

Had detection relied on Cloudflare's documented markup it would have found
nothing and shipped looking finished. The two signals that actually fire here are
**the unsolved response field** and **the console error**. The container and
iframe checks are kept because they are correct where they apply.

---

## 3. The blocking defect, reproduced on unmodified `main`

Before any behaviour was changed, both defects were reproduced against
`origin/main` in a container using only main's own symbols:

```
decide(verification_required, interactive_challenge, expired) -> 'authorised'
action_for('authorised')                                      -> 'none'

_log_page_diagnostics(not-ready + real Turnstile markup + 600010 console)
  -> 'reveal_verification_stalled'   retryable=True
```

`DEFERRED_STATES` contains `verification_required` and `RECOGNISED_REASONS`
contains `interactive_challenge`, so a challenge row fell through to the time
checks and an expired cooldown authorised it. Because `action_for(AUTHORISED)` is
`ACTION_NONE`, every operator tool then reported *"nothing to do; the scheduler
will pick it up"* about a row whose entire meaning is that the scheduler cannot.

**A clock released a hold only a person can release.** Correcting the Turnstile
label *without* fixing this would have been actively worse: 22 parked items would
have been fed back into the failing challenge automatically.

---

## 4. What changed

### `backend/queue_recovery_policy.py`
* New decision `VERIFICATION_HOLD` and new action `ACTION_ATTENTION_REQUIRED`.
* Ordered **after** the safety hold and **before** ownership, budget and time — so
  a disabled batch cannot present as a configuration problem when the real answer
  is "a challenge is blocking it".
* `SharedFacts.challenge_open` holds siblings, who carry an ordinary
  `source_deferred` reason and would otherwise be authorised on cooldown expiry.
* Advice text promises nothing: not that the wait ends, not that a retry works.
* `action_for()` still raises on an unmapped decision.
* **One authority.** No second pre-filter was added to `_maybe_auto_resume()` —
  two places deciding one thing caused the round-12 and round-13 bugs.

### `backend/download_outcome.py`
* New pure function `turnstile_challenge_evidence(html, console_entries,
  unlock_target)` returning evidence markers.
* Evidence: an **unsolved** `cf-turnstile-response` field **tied to the reveal
  form**, a `.cf-turnstile` container, a turnstile challenge iframe, or a
  **navigation-scoped** console error in the **600 family** (matched as a family,
  not pinned to 600010).
* Explicitly **not** evidence: a dormant `api.js` script tag (hdencode ships it on
  every release page, healthy ones included), the word "cloudflare", the
  "Verifying… Please wait" wording, "TV Shows", or a **populated** response token —
  that last is a challenge that *succeeded*.
* The unlock-target predicate is **injected**, not reimplemented, so there is one
  copy of that rule.
* Failure title → **"Manual attention required"**.

### `backend/download_service.py`
* Detection is a **conjunction**: `reveal_tier == "not-ready"` **AND** active
  Turnstile evidence.
* Emits `INTERACTIVE_CHALLENGE` with `cause_code="turnstile_challenge_failed"`,
  `retryable=False`, `affected_scope="source"`, `action_code="verification_required"`.
* Calls `observe_challenge()`, not `observe_reveal_stall()`.
* New `_reset_console_log` / `_drain_console_log`. The reset runs immediately
  before every `driver.get`, so a previous page's error cannot classify the next;
  drains **append**, so the error arriving ~2s after load survives the pre-click
  read.

### `backend/browser_adapter.py`
* `goog:loggingPrefs` now requests `browser` logs as well as `performance`. An
  invisible Turnstile reports its failure to the console and nowhere else.

### `backend/scrape_outcome.py`
* `INTERACTIVE_CHALLENGE` added to `_SIGNAL_BEARING_CODES`, so evidence persists
  to the row instead of living only in a log that rotates.
* Message rewritten to *"Automated verification did not complete…"*.

### `backend/download_queue.py` + `backend/database.py`
* New nullable column `download_queue_batches.challenge_episode_id`.
* `_pause_for_source` opens an episode on a challenge (`COALESCE`, so a later
  non-challenge pause cannot erase it).
* `_challenge_episode_open` is **source-scoped**, not batch-scoped, so a second
  batch cannot march its items into the same door.
* `_close_challenge_episodes` fires only on an **affirmative source delivery** —
  the same `is_source_delivery()` predicate that earns retry budget, so a
  pre-scrape duplicate does not qualify.
* `resume_batch` and `retry_ready` ("Retry all") are **refused** while an episode
  is open, with a message saying to probe one item instead. `retry_item` (the
  single-item probe) stays available.

### `frontend/src/lib/components/VerificationRetries.svelte`
* State label → "Manual attention required". "Retry now" is described as a probe.
* The backend's refusal message is surfaced verbatim rather than flattened to
  "the source is still paused" — a different condition with a different remedy.

### `scripts/migrate_challenge_episode.py` (new)
* Moves existing parked rows onto episode semantics in **one transaction**.
* **Dry run by default**; `--apply` to write.
* Refuses to run if no row carries challenge-trigger evidence, rather than
  inventing a trigger.
* **Deliberately leaves cooldowns intact** — nulling them would make siblings
  `NO_AUTHORISATION` forever and block the legitimate release after a probe
  succeeds.

---

## 5. Two things found that were not in the brief

**`scripts/queue_recovery_state.py` had an unguarded label map.** It renders
verdicts with `LABELS.get(verdict, verdict)`, so a missing entry does not crash —
it prints the raw string `manual_verification_hold` at the person deciding
whether to intervene. Found by grepping the consumers, not by a failing test.
Fixed, and pinned with an exhaustiveness test, since `ACTION_FOR` had one and
this map did not.

**`test_auto_resume_scopes_source_and_preserves_unknown_outcome` was pinning the
defect.** It set a row to `verification_required` / `interactive_challenge` with
an expired cooldown and asserted it resumed to `ready`. The row was kept as a
challenge row and the assertion corrected, rather than downgrading the row —
the test's real subject is source-scoping, which is better served by a deferred
row that must *not* move sitting beside one that must.

---

## 5a. ChatGPT's review, and round 2

ChatGPT returned **REQUEST CHANGES** with four MEDIUM blocking findings and two
LOW. Each was verified in code before being accepted. **All six were real.**

Its closing sentence is the one worth keeping:

> The authority is correct. The remaining failures are at boundaries where
> consumers reconstruct, infer, or silently drop one of its facts.

| # | Finding | Verdict |
|---|---|---|
| 1 | `scripts/queue_recovery_state.py` builds `SharedFacts` without `challenge_open`, so the diagnostics disagreed with production about every **sibling** row | Confirmed. Fixed with a correlated subquery in the adapter SQL |
| 2 | Episode closure was inferred from child-state emptiness, so probing or cancelling a single-item episode batch closed it before any success | Confirmed. `challenge_episode_id` is now sole authority; added `clear_challenge_episode()` as the explicit operator escape hatch |
| 3 | The migration treated `reveal_verification_stalled` as challenge evidence — the code that specifically means *no* challenge evidence — and gave one episode to every parked batch | Confirmed. Now requires explicitly named trigger item IDs and refuses to guess |
| 4 | The older `captcha_frames or challenge_markers` branch classified before the new conjunction, so a **ready** reveal plus any turnstile iframe became a source-wide hold | Confirmed. Interstitial and embedded-frame evidence are now partitioned |
| 5 | The doc claimed the one-item probe "stays available"; it is disabled during the coordinator's one-hour quiet period | Confirmed. Documentation corrected |
| 6 | Response-field association read `form.action` only, ignoring a submit's `formaction` override | Confirmed. Now shares one effective-target helper with the reveal rule |

Two things worth carrying forward as lessons rather than as fixes:

* **I found the `LABELS` instance of finding 1 myself and then stopped.** Both
  bugs are the same shape — a consumer dropping a fact — in the same file. Having
  found one, the right move was to grep every construction site. There are
  exactly two `SharedFacts(` calls in the codebase.
* **My own discrimination test for finding 4 could not reach the code it was
  discriminating.** It used the response field and the console; the bypass is in
  the iframe branch. It passed while the bug sat open beside it. The replacement
  includes a control asserting `iframe:turnstile` is *still produced*, so the
  test cannot pass by the detector having gone silent.

## 6. Test evidence

Method: `git archive HEAD` of the **whole tree** into a fresh container off
`scanhound:latest`, `pip install pytest pytest-asyncio "httpx<0.28"`, then pytest.
`origin/main` was run the same way in the same session as a baseline.

* **35 new tests pass on the branch.** Both new files fail to even import on main.
* **The load-bearing negative control** — 22 scheduled items, the first meets an
  active Turnstile: 1 `verification_required` trigger, 21 source-held siblings,
  **0 failed**, and **1 transport attempt total**. The harness raises on any
  unscripted attempt, so a sibling attempt is a hard error rather than a soft
  count. Then **12 × 24h advanced past every cooldown → still 1 attempt.**
* **Paired positive control** in the same rig: a reveal stall with no challenge
  evidence *does* auto-resume after the same advance. Without it the negative
  control would pass on a rig that never promotes anything.
* **Migration smoke test:** before, all 22 rows are `authorised` — they would be
  released automatically today; after, all 22 are `manual_verification_hold`.
  Verified through `decide()`, i.e. the consumer, not by checking columns.
* `test_queue_liveness_model.py` — the independent oracle that deliberately does
  not import the policy — is unaffected and still passes.
* **Round 2:** `tests/test_challenge_episode_migration.py` added; the other two
  extended. **14 of the new tests fail on the pre-review head `fcc5a40` and pass
  now** — a control for every one of ChatGPT's six findings.
* Full suite (round 1): **4654 passed** on the branch vs **4620** on main, with
  the only failures the known date bug that also fails on main.

**Note on CI.** ChatGPT observed no GitHub Actions run associated with the
reviewed head and correctly treated the suite counts as author/local evidence,
not independent confirmation. That is accurate. Every run reported here was done
locally by `git archive` of the whole tree into a fresh container off
`scanhound:latest`, with `origin/main` run the same way in the same session as a
baseline. Worth checking whether Actions minutes are the reason before reading
anything into the absence.

---

## 7. Second branch: `fix/policy-tests-wall-clock`

Separate, pre-existing bug, fixed on its own branch off main.

Three tests in `test_queue_recovery_policy.py` began failing at **2026-08-09
12:00 UTC with no code change**. `FUTURE_S` is `NOW + 1 day` = exactly that
instant, and the four `_rig` tests drive the production path, which reads the
wall clock via `download_queue._utcnow`. Once real time passed it, the "future"
sibling became due and the assertions inverted.

This matters more than a red suite: the failure is **indistinguishable from a
real regression in the round-12/13 protections**, which are precisely the
protections anyone reading that output would suspect. The clock is now pinned for
the whole module via an autouse fixture. **Verified against unmodified main: 28
passed, fixture as the only change.**

---

## 8. What remains

1. **Review.** `agent/turnstile-classification` is pushed for the ChatGPT round.
2. **Merge and deploy are Jesse's calls.** Neither has been done.
3. **The migration has not been run, and now needs an argument it did not
   before.** It cannot run until the app is rebuilt (the live DB lacks
   `challenge_episode_id` and the script refuses without it), and it now requires
   `--trigger <item_uuid>` naming a row someone has actually verified met a
   challenge. For the live incident that is
   `9e888af4-2f72-4ff3-8ad0-0d6277ea5b98` (the Being Erica S02 row, the one whose
   console produced 600010) — but **only if the challenge is still what is
   holding it.** Jesse's decision was to **leave the 22 parked items alone**:
   their cooldowns lift around 01:54 and the source was serving the reveal
   control ready, so they will most likely grab normally and no episode is
   needed for this incident at all.
4. **`fix/policy-tests-wall-clock` is pushed** and independent; it can merge on
   its own.
5. **Unrelated, worth someone's attention:** the main working tree at
   `X:\Docker Apps\ScanHound` switched branches four times during this session
   without this session doing it, and carries an uncommitted edit to
   `scripts/run-dv-scan.ps1`. All work here was done in isolated git worktrees
   (`.claude/worktrees/turnstile`, `.claude/worktrees/clockfix`) to avoid
   colliding with whatever that is.

---

## 9. What is NOT claimed

* **Not** that `navigator.webdriver` or the automation flags caused the
  rejection. 600\* is a generic family and the trigger is not isolated.
* **Not** that the challenge will be presented on the next attempt. It is
  intermittent and was absent for six consecutive loads.
* **Not** that cross-batch containment is total. The episode holds every batch of
  a source from **auto-resuming**; a batch reaching the source by another path
  still relies on the coordinator's process-wide cooldown, which is in-memory and
  resets on container restart. Pre-existing and unchanged.
* **Not** that the 60s reveal window should move. Unchanged, per the finding.
