# HDEncode reveal stall — ROOT CAUSE FOUND: Cloudflare Turnstile, error 600010

**Date:** 2026-08-09
**Author:** Claude
**Reviewer:** ChatGPT (adversarial review of the finding and the proposed classification)
**Status:** Cause identified by measurement. **No code changed on the download path.**
**Supersedes:** `hdencode-reveal-stall-investigation.md` (its §3 click theory and its
"no captcha or Cloudflare challenge is involved" claim are both **retracted** below).

> **REVIEWER: read this file from the repository, not any chat summary of it.** If you
> cannot read it directly via the GitHub connector, **stop and say so.**

---

## 0. The finding

ScanHound's Chromium, on the stalled release page, logs to the browser console:

```
WARNING  https://challenges.cloudflare.com/turnstile/v0/api.js  0:17636
         "[Cloudflare Turnstile] Error: 600010."
WARNING  https://challenges.cloudflare.com/turnstile/v0/api.js  0
         Failed to execute 'postMessage' on 'DOMWindow': ...
WARNING  https://challenges.cloudflare.com/turnstile/v0/api.js  0:17636
         "[Cloudflare Turnstile] Error: 600010."
```

**The "Verifying… Please wait" control is a Cloudflare Turnstile challenge**, and it is
failing for the automated browser. It is not a countdown, not a throttle, not a slow
reveal. The user's ordinary browser passes the same challenge on the same URL in under a
second; ScanHound's never passes it at all.

**Reload does not recover it.** Three consecutive loads of the same URL, each observed at
t+3s and t+15s: the control stayed `verifying… please wait` and zero Rapidgator/NitroFlare
links appeared on any of them.

## 1. Two claims I made earlier today that are now RETRACTED

**(a) "No captcha or Cloudflare challenge is involved."** I asserted this from ScanHound's
own diagnostics, which reported no challenge markers, and from the observation that the
page body loaded fully (92,509 bytes, 100 links, complete navigation). Both observations
were true and the conclusion was wrong: the challenge is a third-party iframe widget, and
**ScanHound's challenge detector does not recognise Turnstile.**

**(b) The §3 "we never click the control" theory.** Correct as a code fact — the not-ready
gate does prevent clicking — but wrong as a cause, and already refuted by commit
`f96c08b6` (2026-07-24), which recorded 13 of 14 placeholder clicks producing no links.
ChatGPT found that history; I had not read it. The gate is a deliberate fix and must stay.

## 2. The detector gap — the actual software defect

This is the part worth reviewing, because it is ours and it is longstanding.

ScanHound classifies this outcome as `REVEAL_VERIFICATION_STALLED` and treats it as a
**source-wide throttle**:

- `_SOURCE_WIDE_REASONS` includes it (`backend/download_outcome.py:48`), so
  `is_source_wide_denial` routes it to `_pause_for_source`;
- the failure title read **"HDEncode is throttling"** until earlier today (fixed in
  `056de4ec`, with `test_no_failure_title_asserts_an_unproven_cause` to stop it returning);
- items land in `waiting_source` with automatic retry, so they cycle indefinitely.

Meanwhile the app **already has the correct machinery**: `ScrapeCode.INTERACTIVE_CHALLENGE`,
the `verification_required` item state, and a UI surface at
`frontend/src/lib/components/VerificationRetries.svelte`. A Turnstile challenge is exactly
what that path exists for — a human must complete it.

So the defect is: **an interactive bot challenge is being misclassified as a source
throttle, and therefore routed to silent automatic retry instead of to the human.** That
misclassification is also what sent this investigation down four dead ends: every theory
was built on the word "throttle", which was never measured.

Note the challenge-detection weakness is visible in the diagnostics themselves:
`possible access controls: ["a='TV Shows'", "a='TV Shows'"]` — it was matching navigation
links, not the widget.

## 3. Measurements, and what each eliminated

All against the live stalled URL
`https://hdencode.org/being-erica-s02-1080p-nf-web-dl-dd5-1-x264-ntb-18-3-gb/`.

| Measurement | Result | Eliminates |
|---|---|---|
| User opened the same URL on a phone browser | Links appeared with almost no wait | **Rate limiting / throttling** |
| Same, desktop, recorded: control already reads "View links", one click, links in ~0.5s | Release healthy, no challenge visible to a human | Release-specific fault |
| `elapsed=60.4s found=False not_ready_seen=True` in the production log | Pinned at **our own** ceiling; right-censored | 60s window as *cause* |
| DNS/connect from the container to hdencode.org, rapidgator, CDN, ad domains | All fast (dns 0.10s, connect 0.03s) | **Container network / DNS** |
| Killed the day-old Chromium, moved the 190 MB profile aside | Fresh profile stalls identically; page load dropped 23s -> **0.7s** | **Stale profile** (and explains the 23s) |
| `visibilityState=visible`, `hidden=False`, **rAF 121 ticks/2s**, `setTimeout(1000)=1000ms` | Clock entirely healthy | **Timer/visibility throttling under Xvfb** |
| Waited 12s without scrolling, then scrolled the reveal section into view and waited ~13s | Never advanced; 0 host links | **Scroll / IntersectionObserver gating** |
| `chromium --version` vs `chromedriver --version` | Both `151.0.7922.71` | **Version mismatch** (a known past cause here) |
| 3x load/reload, observed at t+3s and t+15s each | Never advanced; 0 host links | **Transient failure**; reload as a fix |
| Browser console | **Turnstile error 600010** | — **this is the cause** |

One incidental find worth its own note: at t=12s without scrolling, the label changed to
`verification delayed - please hold a moment or reload the page.` — the site's own failure
text. ScanHound never sees this because it only ever samples for a *ready* label.

`navigator.webdriver` is `True` and chromedriver adds `--enable-automation` and
`--test-type=webdriver`. That is consistent with a challenge refusing to complete, but the
Turnstile error is the direct evidence and the webdriver flag is not independently proven
to be the trigger.

## 4. Proposal — classify correctly and involve the human

**Explicitly out of scope: anything that attempts to pass, solve, or evade the Turnstile
challenge.** Not proposed, not desired, and not what this document asks to be reviewed.

1. **Detect Turnstile.** Look for the `challenges.cloudflare.com/turnstile` script, a
   `cf-turnstile` container, or a `cf-turnstile-response` field, and treat its presence
   plus a non-ready reveal control as `INTERACTIVE_CHALLENGE` rather than
   `REVEAL_VERIFICATION_STALLED`.
2. **Route to the human.** `INTERACTIVE_CHALLENGE` already maps to `verification_required`
   in `enqueue_retry`, and already has a UI surface. The item should appear as "needs you",
   not retry silently.
3. **Stop the pointless retry loop.** A challenge that fails on browser characteristics
   will fail identically on retry — measured three times here. Retrying spends the batch's
   budget for nothing and, via `_pause_for_source`, parks every sibling untried.
4. **Preserve the source-wide pause** as containment, but under challenge semantics rather
   than throttle semantics. It is still correct not to hammer a source presenting a
   challenge.
5. **Do not raise the 60s window.** With the cause known, a longer wait only produces the
   site's "verification delayed" message later.

## 5. Questions

1. **Is `INTERACTIVE_CHALLENGE` the right classification**, or does a Turnstile failure
   deserve its own code? ChatGPT's previous round argued `verification_required` should
   mean "actual operator verification is required" — a Turnstile challenge seems to fit
   that definition exactly, but confirm rather than assume.
2. **Detection robustness.** Keying on `challenges.cloudflare.com/turnstile` is narrow and
   will break if the widget is proxied or renamed. What is the least brittle signal — the
   script URL, the DOM container, the hidden response field, or the console error itself?
   And what must NOT be keyed on, given the existing detector already false-positived on
   `a='TV Shows'`?
3. **What should happen to the 22 currently-parked items** once they are reclassified?
   Re-attempting each after the human clears one challenge may itself present 22 more
   challenges. Is there a sane batching model here, or is per-item human action inherent?
4. **Does reclassification create a fail-open?** `_SOURCE_WIDE_REASONS` membership is what
   currently prevents these becoming permanent failures (the comment there records how 78
   items once accumulated). If the reason code changes, does that protection still hold?
5. **Have I mis-eliminated anything in §3?** The profile refutation rests on a single
   post-wipe attempt, and the reload test on three loads in one session. Both feel thin
   relative to how confidently I am now naming a cause.
6. **What else could produce Turnstile 600010 besides automation detection?** I have
   treated the error as self-explanatory. If it can also arise from a missing referrer,
   a blocked subresource, a clock skew, or an iframe/postMessage failure — note the
   `postMessage on 'DOMWindow'` warning immediately after it — then the conclusion may be
   narrower than "we are detected as a bot", and the remedy might be legitimate and
   entirely different.
