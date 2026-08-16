# The reveal verification may be a per-SESSION warm-up, not a per-item gate

**New evidence from the owner, observed manually in his own browser
2026-08-16.** It reframes `reveal_verification_stalled`, which three rounds of
review have circled without a mechanism.

## The observation

> "When I initially went and opened a movie page the link button showed
> 'Verifying Links' or something similar, the text on the button then changed
> to 'View Links' after a couple seconds. After that initial behavior, when I
> visited other items the button just showed 'View Links' and did not appear to
> go through the first step."

So for a human: the verification runs **once**, on the first page of a session,
takes **~2 seconds**, and every page afterwards skips it.

## What the code does

`_find_reveal_control` already models this exactly:

    HDEncode serves the unlock form in a not-ready state whose submit reads
    "Verifying... Please wait" and only later swaps to "View links".

It polls for the label to become a positive links label, with
`_REVEAL_CLICKABLE_TIMEOUT = 60`. `tier="not-ready"` means the control was
present but had not finished verifying when that 60s window expired.

The strictness is load-bearing and well-evidenced: the placeholder posts the
SAME endpoint, so accepting "the single safe submit" produced **0 link
retrievals in 14 attempts** (2026-07-24). Requiring the positive label makes a
re-worded placeholder fail closed rather than be clicked.

## Why the observation matters

**60 seconds is thirty times the human duration.** So `not-ready` is not a
too-short timeout. It means the swap that takes a human ~2s did not happen at
all for our browser within a minute.

The driver is cached and reused (`self.cached_driver`, quit only when dead),
and `hdencode_browser_profile_mode = persistent`, so a session SHOULD carry
across items. That is consistent with the production shape:

    warm session   f33d9076   11 items in 1.6 hours
    cold/stalled   eaeb62a5    1 item  in 2 days
                   bf9bae0a    2 items in 2 days

and with batch 9dbb1dd7 reaching 14/36 in bursts rather than uniformly.

## Working hypothesis

The verification is a per-session warm-up. When our session is warm, items fly.
When it is cold — first item after a driver restart, profile reset, cookie
expiry, or container restart — that item must complete the warm-up, and
sometimes it never does within 60s.

If true, the failure is concentrated almost entirely on the FIRST item of a
cold session, and the current design turns that single item's problem into a
whole-batch park (now fixed separately in #83, but the underlying stall
remains).

## What this suggests, and what I have NOT established

A targeted change would be an explicit **session warm-up before a batch**: hit
one cheap page, complete the verification once, confirm a subsequent page shows
the ready label, and only then start the run. A warm-up that fails is a much
better signal than an item failing, because it is unambiguous about scope.

I have NOT established:

* whether our browser actually completes the warm-up ever, or is treated
  differently from a human browser (automation detection);
* whether the stalls really do cluster on the first item of a session -- the
  attempt records added in #80 would show this, but they have only one row so
  far;
* whether the 2s human duration holds for a cold profile with no cookies, which
  is the state our browser is in after a container restart;
* whether "cookies persist via the profile" is the same thing as "the session
  is warm" for this site -- it may key on something else.

## Questions

1. Is the per-session warm-up model consistent with the code and evidence
   above, or is there a reading that fits better?
2. If the warm-up is real, is an explicit pre-batch warm-up the right shape, or
   should the FIRST item of a cold session simply get a longer window than the
   rest?
3. `not-ready` after 60s when a human sees 2s is a large gap. Does that alone
   argue that the swap depends on something our browser is not doing --
   and if so, is that worth pursuing before any timing change?
4. What is the cheapest way to test the hypothesis without changing behaviour?
   My instinct is to record, per attempt, whether the driver was freshly
   created -- the attempt table added in #80 could carry that flag.
5. Prior work concluded this was a Cloudflare Turnstile gate
   (`600010`, invisible widget, intermittent). Does the owner's observation
   contradict that, refine it, or describe a second and separate step?

## Context

- Deployed and live: attempt records (#80/#82), three stall alerts, the
  outer-cycle heartbeat (#79), failure-scope-by-evidence (#83).
- The verification hold was cleared manually tonight; downloads resumed at
  57.5 MB/s and the queue is moving again (36 ready, 1 claimed).
- Batch pacing is `staggered, 600s`. Overall delivery is healthy: 604
  downloads, 650/654 packages extracted, 36 batches at 98% item completion.
