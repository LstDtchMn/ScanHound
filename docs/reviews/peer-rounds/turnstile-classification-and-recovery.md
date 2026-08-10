# Turnstile: the resource diagnostic, and correct classification + recovery

**Date:** 2026-08-09
**Author:** Claude
**Branch:** `agent/turnstile-classification` (off `main` at `6813260`)
**Status:** Implemented and tested. **Not merged, not deployed.**
**Answers:** ChatGPT's review of `hdencode-turnstile-root-cause.md` — all eight
required changes, plus the diagnostic that review left open.

> **Out of scope, explicitly:** nothing here attempts to pass, solve, or evade
> the challenge, and nothing changes how the browser presents itself. The work
> is classification and recovery semantics, so a person is involved instead of a
> retry loop.

---

## 0. The diagnostic that was still open — and what it narrows

The review asked for one non-evasive diagnostic **first**, on the grounds that
it might narrow the conclusion. It did, twice.

Captured for the current navigation only, against the live parked URL
`https://hdencode.org/being-erica-s02-1080p-nf-web-dl-dd5-1-x264-ntb-18-3-gb/`:

| Question | Answer |
|---|---|
| Requests to `challenges.cloudflare.com` | 4 (api.js + 3 challenge-platform documents) |
| `Network.loadingFailed` for Turnstile resources | **0** |
| HTTP status, Turnstile script | **200** `application/javascript` |
| HTTP status, challenge frames | **200** × 3, `text/html` |
| CSP on the displayed document | **none** (no header, no meta) |
| `input[name="cf-turnstile-response"]` | **exists, value empty**, in the form posting `#unlocked` |
| `window.turnstile` | `object` — the API loaded and initialised |
| Console | `[Cloudflare Turnstile] Error: 600010.` at t+2.6s, t+13.6s, t+25.7s |

**Every Turnstile resource loaded and communicated normally. Nothing failed to
load, nothing was blocked, and there is no CSP to block it.** By the review's
own criterion, that is the case where automation rejection is the dominant
explanation rather than an integration defect — so the remedy is classification,
not a fix to how we embed the widget.

Two things the review flagged are worth closing explicitly:

* **The `postMessage` warning is not a companion of the failure.** It appeared in
  the first capture and **not** in later ones that still produced 600010. It is
  incidental. It was never promoted to root cause and now has evidence against it.
* **Cloudflare's more specific codes did not appear.** No 200500 (iframe load
  error), no 200100 (clock/cache), no 110600 (timeout) — consistent with the
  resources being healthy and only the verdict failing.

### The finding that narrows the conclusion: the gate is INTERMITTENT

Twenty minutes after the capture above, the same URL in the same container, same
flags, fresh profile:

```
load 1..6: widget=no  reveal='View links'  disabled=False  600errs=0
```

**Six consecutive loads presented no Turnstile widget at all**, with the reveal
control ready and enabled. So this is precise, and the earlier phrasing is not:

> Turnstile is active on the reveal flow **when hdencode chooses to present it**,
> and when presented it fails to complete in ScanHound's session, producing a
> 600\* generic challenge failure.

`navigator.webdriver` is `True` and chromedriver adds `--enable-automation` and
`--test-type=webdriver`. That is consistent with a challenge refusing to
complete. **It is not proven to be the trigger** and is not claimed as one.

The intermittency is also the reason detection had to be a conjunction — see §2.

---

## 1. The blocking defect, reproduced on unmodified `main`

Run against `origin/main` in a container, using only main's own symbols:

```
decide(verification_required, interactive_challenge, expired) -> 'authorised'
action_for('authorised')                                      -> 'none'
```

`DEFERRED_STATES` contains `verification_required` and `RECOGNISED_REASONS`
contains `interactive_challenge`, so the row fell through to the time checks and
an expired cooldown authorised it. Since `action_for(AUTHORISED)` is
`ACTION_NONE`, every operator tool reported *"nothing to do; the scheduler will
pick it up"* about a row whose whole meaning is that the scheduler cannot.

**A clock released a hold only a person can release.** And correcting the label
without fixing this would have made it far worse: 22 items would have been fed
back into the challenge automatically.

The same run reproduces the classification half:

```
_log_page_diagnostics(not-ready + live Turnstile markup + 600010 console)
  -> 'reveal_verification_stalled'  retryable=True
```

---

## 2. What changed, against the eight required items

**1. Classification.** An active Turnstile failure is now
`reason_code=interactive_challenge` with `cause_code=turnstile_challenge_failed`.
No new top-level reason. `INTERACTIVE_CHALLENGE` was **added to
`_SIGNAL_BEARING_CODES`** so the evidence markers persist to the row rather than
living only in a log that rotates.

**2. Source-wide containment retained.** `INTERACTIVE_CHALLENGE` stays in
`_SOURCE_WIDE_REASONS`; `affected_scope` stays `"source"`; `_pause_for_source`
still routes trigger → `verification_required`, siblings → `waiting_source`,
batch → `paused_source`. Nothing was narrowed.

**3. Fallback retained.** Not-ready **without** active challenge evidence is
still `REVEAL_VERIFICATION_STALLED`, still source-wide, still retryable.

**4. A genuine hold in the pure policy.** `VERIFICATION_HOLD` +
`ACTION_ATTENTION_REQUIRED`, ordered directly after the safety hold and **ahead
of** ownership, budget and time — so a disabled batch cannot present as a
configuration problem. It lives in `queue_recovery_policy.decide()`, the one
authority production and diagnostics share; **no second pre-filter was added to
`_maybe_auto_resume()`**, which is what caused rounds 12 and 13. `action_for()`
still raises on an unmapped decision.

**5. A timer never authorises a challenge row.** Asserted directly, including at
+1, +10 and +100 years.

**6. One episode, not 22 replays.** `download_queue_batches.challenge_episode_id`
holds the triggering row and every sibling behind it on one fact. The hold is
**source-scoped**, so a second batch cannot march its own items into the same
door. Only an **affirmative ScanHound-side source delivery** closes it — the same
`is_source_delivery()` predicate that earns retry budget, so a pre-scrape
duplicate does not qualify, and neither does a human succeeding in another
browser. "Retry all" (`resume_batch`, `retry_ready`) is **refused** while an
episode is open, with a message saying what to do instead.

**The single-item probe is the only route through, and it is not available
immediately.** `retry_item()` calls `_assert_hdencode_available()`, and
`observe_challenge()` sets a one-hour source cooldown, during which
`list_retries()` reports `retry_available=False` and the UI disables "Retry now".
So the accurate statement is: **the one-item probe becomes available once the
coordinator's quiet period ends.** That is deliberate containment — probing a
source that just presented a challenge, immediately and repeatedly, is the
behaviour this whole change exists to stop — but the earlier wording said the
probe "stays available", which was wrong, and this document said it.

`scripts/migrate_challenge_episode.py` takes **explicitly named trigger item
IDs** and moves them in **one transaction** (dry-run by default). It deliberately
leaves cooldowns intact — nulling them would make the siblings
`NO_AUTHORISATION` forever and block the legitimate release after a probe
succeeds.

**7. Wording.** Title → *"Manual attention required"*. Message → *"Automated
verification did not complete…"*. The advice text promises nothing: not that the
wait ends, not that a retry works, and not that the user can complete the
verification — they cannot reach the Xvfb Chromium session at all. The UI labels
"Retry now" a probe.

**8. Detection is a conjunction:** `reveal_tier == "not-ready"` **AND** active
Turnstile evidence. Evidence is an **unsolved** `cf-turnstile-response` field
**tied to the reveal form**, a `.cf-turnstile` container, a
`challenges.cloudflare.com` turnstile iframe, or a **navigation-scoped** console
error in the **600 family** (matched as a family, not as 600010). Console logs
are drained and discarded immediately before each `driver.get`, so a previous
page's error cannot classify the next; within one navigation drains append, so
the error that arrives ~2s after load is not lost by the pre-click read.

Deliberately **not** evidence: a dormant `api.js` script tag (hdencode ships it
on every release page, healthy ones included), the word "cloudflare", the
"Verifying… Please wait" wording, "TV Shows", or a populated response token —
that last one is a challenge that *succeeded*.

### What the measurements changed about the plan

The review suggested `.cf-turnstile` or a challenge iframe as detection signals.
**On this site neither exists:**

* there is no `.cf-turnstile` container and no `data-sitekey` — the widget is
  rendered programmatically into `#turnstile-container-<hash>`;
* there is no queryable challenge `<iframe>` — the widget runs **invisible**,
  building a frame, failing, and tearing it down about every 11 seconds, so a
  DOM read usually lands between attempts.

Both checks are kept because they are correct where they apply, but the two
signals that actually fire here are **the response field** and **the console
error**. Had detection relied on the documented Cloudflare markup, it would have
found nothing and this would have shipped looking finished.

---

## 3. Tests

`tests/test_turnstile_classification.py` (16) and
`tests/test_challenge_episode_containment.py` (19).

**The load-bearing negative control** — 22 scheduled items, the first meets an
active Turnstile:

* 1 `verification_required` trigger, 21 source-held siblings, **0 failed**;
* **1** transport attempt total — the harness raises on an unscripted attempt, so
  a sibling attempt is a hard error, not a soft count;
* then **12 × 24h advanced past every cooldown** → still exactly 1 attempt.

Paired with a **positive control** in the same rig: a reveal stall carrying no
challenge evidence *does* auto-resume after the same advance. Without it the
negative control would pass on a rig that never promotes anything.

Also covered: the conjunction in **both** directions (evidence on a *ready*
reveal is not a challenge); the form tie (a captcha on the comment form is not
evidence about the reveal, and the same field inside the reveal form is);
navigation scoping in both directions; probe-is-one-item; and a duplicate
completion **not** closing an episode.

Round 2 added `tests/test_challenge_episode_migration.py` and extended the other
two. **14 of the new tests fail on the pre-review head `fcc5a40` and pass now**,
which is the control for every one of ChatGPT's six findings — including the two
that only a discrimination test could distinguish (`iframe:turnstile` is still
produced, so "ready reveal is not a challenge" cannot pass by the detector
having gone silent).

---

## 4. Round 2 — ChatGPT's REQUEST CHANGES, and what each fix was

All six findings were verified in code before being accepted. All six were real.
Fourteen new tests fail on the pre-review head `fcc5a40` and pass now, so every
fix is pinned by a control rather than by assertion.

**F1 — the operator adapter dropped `challenge_open` (MEDIUM).** Production
passed it into `SharedFacts`; `scripts/queue_recovery_state.py` did not, so the
dataclass default `False` applied and the two disagreed about every **sibling**
row — production held it, the diagnostics said "due now". The trigger row hid
the disagreement because its own `queue_reason` is enough on its own.

This is the fourth appearance of one failure class: **the authority is right and
a consumer drops one of its facts before calling it.** I had already found and
fixed the `LABELS` instance of it in the same file — and stopped there instead of
grepping every `SharedFacts(` construction. There are exactly two.

Fixed with a correlated subquery in `JOINED_DEFERRED_SQL`, so the fact is
computed in the same statement the adapter already runs, with a matching source
scope. Pinned end to end through the real SQL, plus the negative control that the
same rows become `AUTHORISED` once the episode closes.

**F2 — episode closure had two authorities (MEDIUM).** `_challenge_episode_open`
also required that the episode's batch still held a deferred row. ChatGPT's
counterexample is exact: an episode whose batch holds a **single** trigger closes
itself the moment an operator probes it, because the probe moves that row to
`ready`. The episode evaporated during the probe window — the one interval it
exists to cover. Cancelling the trigger did it more directly.

`challenge_episode_id` is now the sole authority. Closure has exactly two
explicit causes: an affirmative source delivery, or `clear_challenge_episode()`,
a new operator action. That escape hatch is what makes the stricter rule safe —
without it an episode could outlive everything it held and strand the source.

**F3 — the migration invented evidence (MEDIUM).** It treated
`reveal_verification_stalled` as a challenge trigger. That code means precisely
*the classifier found no active challenge evidence*, so promoting it fabricated
the fact the whole change exists to demand — and then wrote the fabrication into
the row as `cause_code = turnstile_challenge_failed`, a confident claim about a
page nobody looked at. It also assigned one episode to **every** parked batch for
the source.

Historical rows cannot be reclassified, because the evidence is gone. The script
now **requires explicitly named trigger item IDs**, refuses to run without them,
refuses an id that is not a parked row for the source rather than skipping it,
and holds only the batches containing the named triggers plus any explicitly
named with `--hold-batch`. `cause_code` is now
`operator_identified_challenge` — what actually happened. Seven automated tests
replace the manual smoke run.

**F4 — the conjunction had an older bypass (MEDIUM).** `_log_page_diagnostics`
classified on `header_challenge or captcha_frames or challenge_markers` *before*
reaching the new branch, and `strong_challenge_markers` returns
`iframe:turnstile` for any turnstile frame anywhere. So a page whose reveal read
"View links" and which carried a Turnstile frame became a source-wide manual
hold. **My own "evidence without a stalled reveal" test used the response field
and console — neither of which reaches that branch — so it passed while the
bypass sat open beside it.** A discrimination test that cannot reach the code it
discriminates is not a control.

Evidence is now partitioned. An **interstitial** (a `cf-mitigated` header, a
challenge title, visible challenge text) replaces the page and still classifies
on its own. An **embedded frame** rejoins at the reveal conjunction, where it
must coincide with a not-ready control. Kept generic rather than Turnstile-only,
so reCAPTCHA and hCaptcha frames retain the coverage they had — tested.

**F5 — the probe availability claim was wrong (LOW).** This document said the
one-item probe "stays available". It is disabled during the coordinator's
one-hour quiet period. The containment is deliberate; the sentence was not
accurate. Corrected in §2.

**F6 — the response field ignored `formaction` (LOW).** The reveal-control rule
has honoured a submit's `formaction` override since 2026-07-24; the field check
reused the URL predicate but not the effective-target rule. Both directions were
wrong: a form whose action looked safe while its submit posted `#unlocked` was
missed, and the reverse counted. Now shares one `_form_posts_unlock` helper, with
mirror tests.

### Residual, stated rather than fixed

An embedded **non-Turnstile** captcha frame belonging to some other page feature
will still classify on a not-ready reveal, because a generic frame carries no
form association to test. The Turnstile path additionally requires the form tie.
The not-ready conjunct bounds the blast radius, and this is strictly narrower
than the pre-review behaviour, but it is not zero.

---

## 5. What is NOT claimed

* **Not** that `navigator.webdriver` or the automation flags caused the
  rejection. 600\* is a generic family and the trigger is not isolated.
* **Not** that the challenge will be presented next time. It is intermittent and
  was absent for six consecutive loads while this was being written.
* **Not** that cross-batch containment is total. The episode holds every batch of
  a source from **auto-resuming**, but a batch reaching the source by another
  path still relies on the coordinator's process-wide cooldown, which is
  in-memory and resets on container restart. That is pre-existing and unchanged.
* **Not** that the 60s reveal window should move. Unchanged, per the finding.
