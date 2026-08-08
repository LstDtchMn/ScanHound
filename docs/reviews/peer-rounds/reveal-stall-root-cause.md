# Reveal-stall root cause — request for an independent check

**Written 2026-08-08. Branch:** `agent/listing-membership-authority`

I am asking for a check on an ANALYSIS, not a diff. The operator asked me to validate
an assumption the code has been acting on for days, and in the course of doing so I
stated three different conclusions, at least two of which were wrong. I want the
reasoning attacked before any more code is written on top of it.

## The assumption under test

`download_service.py` concludes, when the HDEncode link-reveal control has not
finished verifying after 60 seconds:

```
[HDEncode] The reveal control never finished verifying (title: '...').
           The source is rate-limiting; cooling down.
```

and `hdencode_coordinator.observe_reveal_stall()` then pauses the **whole source** for
1h → 2h → 4h (escalating, ±10% jitter).

**"The source is rate-limiting" is a hardcoded conclusion, not a measurement.** All the
code observes is "the widget did not finish within my 60-second ceiling". The response
to that inference is very expensive: hours of total source silence, and — because the
batch auto-resume budget is one-shot per batch lifetime — permanent stranding.

**45 downloads are currently stranded this way.**

## What triggered the re-examination

The operator reported that loading a release page in his own browser returns links
immediately. That is a discriminating observation nobody had made, and it is the kind
of five-second test I should have run days before building on the assumption.

## Evidence gathered

### 1. The source serves the exact URL that stalled

The release whose 60.6-second stall paused the queue at 23:02:37Z, re-scraped from
inside the production container using ScanHound's own `DownloadService`:

```
elapsed: 5.4s
links  : 1  (rapidgator.net/file/58233ef3.../Jimmy.Carr...mkv)
reveal tier observed: links-control
diagnostic: none
```

A second stranded item (`adventure-time-minisodes-s02`) also succeeded, 8.7s,
`links-control`, 1 link.

So the source is not refusing this container, this network, or these URLs.

### 2. The stall distribution is perfectly bimodal

Every `reveal-control tier=` observation in the log:

```
tier=links-control   0.1 – 0.9s     22 observations
tier=not-ready      60.0 – 60.6s     8 observations
```

**There is no middle.** Nothing at 5s, 10s, 30s. The widget either resolves almost
instantly or never resolves and hits the ceiling exactly. Whatever the mechanism is, it
is binary, not degraded-service.

### 3. The sequence that actually matters — and it supports the ORIGINAL assumption

Aligning the log (local, EDT) with the database (UTC, +4h):

```
03:25:58Z – 04:06:08Z   21 consecutive successes, 0.1 – 0.9s each
04:07:18Z               STALL, 60.1s  -> source paused -> budget spent -> stranded
```

**Twenty-one successful reveals in forty-two minutes, then a stall.** That is what
cumulative rate limiting looks like. My two successful scrapes were SINGLE requests
after hours of idling, so they cannot refute it.

Earlier stalls fit the same shape — three at 18:31/18:41/18:51Z are ten minutes apart,
which is the retry cadence, each retry hitting the same closed door.

### 4. The cause is systematically erased from the database

`download_queue_items.last_reason_code` shows:

```
source_temporarily_blocked   44
layout_changed                7
reveal_verification_stalled   0   <-- none, ever
```

Yet the log proves stalls happen repeatedly. The reason: a stall pauses the source, and
the stalled item's NEXT attempt records `source_temporarily_blocked`, **overwriting its
own cause with its own consequence**. Only `last_*` columns exist, so the trigger is
unrecoverable from the durable record.

I asserted "zero reveal stalls have ever occurred in production" on the strength of
that query. It was wrong, and the query could not have supported it.

## Where I actually stand — three positions, stated in order

1. First I said the 45 were throttled by HDEncode. **Unsupported.**
2. Then I said the source was healthy and the items were merely abandoned by a spent
   retry budget, and that no reveal stall had ever occurred. **The stalls are real;
   that claim rested on a column that overwrites the cause.**
3. Then I said the root cause was a poisoned cached browser session. **Also not
   established** — my fresh-process scrapes succeeded during a period when the
   long-running app was ALSO succeeding (a `links-control` at 01:03:09Z), so they do
   not discriminate between "fresh browser works" and "the source simply wasn't
   limiting at that moment".

What IS established: the source serves these URLs in ~5s on demand; the stall is
binary at exactly the 60s ceiling; the "rate-limiting" label is an inference the code
presents as fact; and the durable record destroys the evidence needed to tell these
apart.

## The competing hypotheses, none yet eliminated

| # | hypothesis | supported by | would be refuted by |
|---|---|---|---|
| A | Cumulative rate limiting on reveals (~20 per window) | the 21-then-stall sequence; recovery after hours of idling | a fresh process making 25+ rapid reveals without stalling |
| B | Poisoned/stale cached Chrome session in the long-running process | fresh-process scrapes succeed; `self.cached_driver` is reused indefinitely | a fresh driver stalling during an active stall window |
| C | The 60s ceiling is simply too short for a legitimately slow widget | the ceiling is documented in-code as a temporary placeholder | raising it and observing stalls that resolve at 90–120s |
| D | Concurrency — a scan and a download contending for the single selenium slot | stalls cluster at retry cadence | stall timestamps not correlating with scan activity |

Hypotheses A and B predict opposite fixes. A says slow down and the current cooldown is
directionally right (though 4h and source-wide are unjustified quantities). B says
recycle the browser and the cooldown is actively harmful. **Acting on the wrong one
makes things worse**, which is why I want this checked before I write more code.

## The experiment that would settle it

From a fresh process inside the container, perform N sequential reveals (N ≈ 25) on
distinct stranded URLs, logging tier and elapsed for each.

- Stall appearing around request ~20 → **hypothesis A**, cumulative limiting.
- 25 clean successes → A is refuted for this rate, and B/C/D move up.

It costs ~25 requests to the source and needs operator approval, since deliberately
probing for a rate limit is exactly the behaviour the cooldown exists to prevent.

A cheaper first step: instrument the stall path to record, at the moment of the stall,
the driver's age and how many reveals that driver has already served. That
distinguishes A from B passively, with no extra requests — and it fixes the
observability gap in §4 either way.

## What I am asking

1. Is the 21-then-stall sequence strong enough to accept hypothesis A, or is 21 a
   coincidence in a sample of 8 stalls?
2. Is there evidence in the log excerpts that discriminates A from B that I have not
   used?
3. Regardless of cause: is a **source-wide** pause the right response to a
   **per-item** reveal failure? Currently one stalled release converts every queued
   item into "no request was made", and one-shot auto-resume then strands them all.
   That amplification looks wrong to me independent of which hypothesis holds.
4. Should the 60s ceiling and the 1h/2h/4h escalation be treated as evidence-based at
   all? I can find no measurement behind either number.
5. Should the durable record keep a stall's own cause instead of overwriting it with
   the pause it caused? I believe this is the most valuable single change here, because
   without it the next occurrence will be just as unanalysable.
