# Turnstile active-stall capture — the owed live evidence

**Date:** 2026-08-10 04:19 UTC
**How:** `scripts/turnstile_watch.py`, read-only and non-evasive (no click, no solve, no
queue/profile/DB mutation), running detached in the production container. It polled the
parked reveal pages every 10 min; on cycle 20 the reveal for
`.../being-erica-s02-1080p-nf-web-dl-dd5-1-x264-ntb-18-3-gb/` was **actively stalled** and it
captured the full evidence, then exited.
**Raw record:** `turnstile-active-stall-capture-2026-08-10.json` (same directory).

This closes the one item both review rounds left owed: the console-600 detection leg had
never been observed against a real active stall, only against the healthy page.

## What was captured, during an ACTIVE stall

- **Reveal control not-ready:** the submit label read `verifying… please wait` (vs
  `View links` when healthy). `reveal_tier = "not-ready"`.
- **Turnstile response field present, unsolved:** `input[name="cf-turnstile-response"]`
  exists with **no value**. On the healthy page this field was *absent* — so the DOM leg
  discriminates healthy from stalled.
- **Two console failures:** `[Cloudflare Turnstile] Error: 600010.` from
  `challenges.cloudflare.com/turnstile/v0/api.js`, at t≈8s and t≈18s after load.
- **The Turnstile resources loaded and communicated normally:** every request to
  `challenges.cloudflare.com` returned **HTTP 200** — `turnstile/v0/.../api.js`, plus two
  `cdn-cgi/challenge-platform/.../turnstile/.../invisible` requests. No
  `Network.loadingFailed`, no `blockedReason`, no CSP rejection.
- The widget is an **invisible** Turnstile (the `challenge-platform` URL says so); it renders
  no visible `challenges.cloudflare.com` iframe, which is why the response-field and console
  legs — not the iframe leg — are what fire here.

## What it establishes, at the right precision

1. **The detector's conjunction fires correctly on a real stall.** `reveal_tier == not-ready`
   AND active Turnstile evidence (`cf-turnstile-response` field + navigation-scoped 600*
   console line) both hold → the code classifies this `INTERACTIVE_CHALLENGE` with
   `cause_code=turnstile_challenge_failed`, routes it to the human, and arms the hold. The
   console-600 leg is now proven against a live failure, not just modelled.

2. **A benign integration defect is ruled out for this capture.** Cloudflare documents
   `600*` as a generic challenge-failure family, and the earlier writeup was careful not to
   name a cause. This capture narrows it: the Turnstile script and challenge-platform
   requests all returned 200 and there were no failed/blocked subrequests, so the
   alternatives the round-1 review flagged — a blocked resource, a CSP rejection, an
   iframe/frame that fails to load (Cloudflare's 200500/200100-style causes) — are not what
   happened here. The resources work; the challenge *verdict* fails.

3. **Automation rejection is therefore the dominant remaining explanation** — consistent
   with, but still not independently proven by, `navigator.webdriver` being true (chromedriver
   also adds `--enable-automation`/`--test-type=webdriver`). Note this does not contradict the
   healthy-page probe where the reveal succeeded while `navigator.webdriver` was also true:
   the challenge is **intermittent**, exactly as the batch-history log showed (three reveals
   succeed, then the door shuts). The right precision stays: *Turnstile is active on the
   reveal flow and intermittently fails to complete in ScanHound's session, producing a
   600010 challenge failure; its resources load normally, so the failure is in the challenge
   verdict rather than the integration.*

## What it does NOT establish

- It does not prove the *exact* trigger inside the 600* family; automation rejection is the
  best explanation, not an isolated one. We are deliberately not trying to make the challenge
  pass — the whole design routes it to the human instead.
- One capture. It is strong corroboration of the detector and the cause direction, not a
  claim about base rates or every stall.
