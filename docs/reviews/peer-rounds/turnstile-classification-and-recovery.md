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
episode is open, with a message saying what to do instead; the single-item probe
(`retry_item`) stays available. `scripts/migrate_challenge_episode.py` moves
existing rows in **one transaction** (dry-run by default) and deliberately leaves
cooldowns intact — nulling them would make the siblings `NO_AUTHORISATION`
forever and block the legitimate release after a probe succeeds.

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

---

## 4. What is NOT claimed

* **Not** that `navigator.webdriver` or the automation flags caused the
  rejection. 600\* is a generic family and the trigger is not isolated.
* **Not** that the challenge will be presented next time. It is intermittent and
  was absent for six consecutive loads while this was being written.
* **Not** that cross-batch containment is total. The episode holds every batch of
  a source from **auto-resuming**, but a batch reaching the source by another
  path still relies on the coordinator's process-wide cooldown, which is
  in-memory and resets on container restart. That is pre-existing and unchanged.
* **Not** that the 60s reveal window should move. Unchanged, per the finding.
