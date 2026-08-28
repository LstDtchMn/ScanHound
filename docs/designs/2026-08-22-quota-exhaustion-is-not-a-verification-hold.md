# Quota exhaustion is not a verification hold

**Status: DESIGN ONLY. Nothing implemented. Needs Jesse's decision and, given it
touches the authority model, the peer reviewer's ruling.**

## The problem

HDEncode allows ~20 distinct link reveals per visitor per UTC day, resetting at
midnight UTC. Verified: five complete days in `download_package_links`, every
one exactly 20 distinct URLs, never 21, with the reset visible at the
2026-08-15 → 08-16 boundary. See the `scanhound-hdencode-daily-reveal-quota`
memory.

ScanHound burns all 20 inside the first hour, then spends 22 hours retrying
against a wall. The current recovery schedule escalates 1h → 2h → 4h, so it
fires roughly 22 times a day against a clock it does not know about. **Every one
of those retries is futile by construction**, and each one arms a source-wide
hold that parks dozens of untried siblings.

The obvious fix — "resume at 00:05 UTC" — is not available as written, because:

```python
# backend/queue_recovery_policy.py:65
VERIFICATION_HOLD = "verification_hold"    # deliberate; a timer must NEVER release it
```

That invariant was established across several review rounds and is correct for
the reason it was written: **a timer cannot solve a challenge.** Letting a clock
release a hold whose cause was an unsolved human-verification step is precisely
the defect rounds 15–19 removed.

## The distinction the current model is missing

A hold conflates two causes that have opposite remedies:

| Cause | What clears it | Timer appropriate? |
|---|---|---|
| A genuine interactive challenge | A person, or an affirmative reveal | **No** — a timer cannot solve it |
| The daily quota being spent | Midnight UTC | **The clock IS the remedy** |

Quota exhaustion needs no human. It needs tomorrow. Treating it as a
verification hold both misdescribes it and makes it un-clearable except by an
operator clicking a button that was never needed.

## Why you cannot classify this from the failure alone

The enforcement is a staircase, and only the middle step is distinctive:

1. reveals 1–20 — normal
2. reveal 21 — the click is ACCEPTED and the server returns a full ~90KB page
   with the links **stripped**. Logged `no_file_host_links`. Distinctive.
3. reveal 22+ — Turnstile `600010`, auto-retrying every ~10s.
   **Indistinguishable from a genuine challenge at the point of observation.**

So a classifier keyed on the failure signature would work for step 2 and fail
for step 3 — and step 3 is where almost all the volume is. This is the shape of
[[classification-bugs-produce-confident-wrong-answers]]: a rule that is right on
the rare case and confidently wrong on the common one.

## Proposal: derive the quota state, do not infer it from failures

The count of distinct reveals today is **already recorded** and is queryable:

```sql
SELECT COUNT(DISTINCT url) FROM download_package_links
WHERE substr(recorded_at, 1, 10) = <today UTC>;
```

That is derived, durable, queryable state — not a best-effort event. It answers
"are we walled?" without guessing from a failure signature.

Sketch:

- A `source_quota` fact: `(source, utc_day, distinct_reveals, observed_ceiling)`.
- `observed_ceiling` is **measured, not hardcoded**. It should be the running
  maximum ever seen on a complete day, so the system corrects itself upward if
  the real limit is 25, or if a paid account raises it. Hardcoding 20 would make
  the system permanently unable to notice it was wrong.
- A new decision outcome `SOURCE_QUOTA_EXHAUSTED`, distinct from
  `VERIFICATION_HOLD`. It clears when the derived count for the *current* UTC day
  is below the ceiling — that is, it clears **on re-derived evidence**, not on a
  timer firing. The distinction is not cosmetic: if the count does not reset at
  midnight, the state correctly does not clear, and we learn the model is wrong.
- Scheduling: when quota-exhausted, the next attempt time is the next UTC
  midnight plus a small offset, rather than the 1h/2h/4h escalation. That
  replaces ~22 futile attempts with one useful one.

**The invariant survives intact.** A timer still never releases a verification
hold. Quota exhaustion is simply not a verification hold, and its release is
gated on a fact being re-read, not on a clock elapsing.

## What must be true before this ships

1. **The ceiling must be measured, never assumed.** Five days is a thin base.
   If any day exceeds the ceiling, the model is refuted and this design is void.
2. **Step 3 must not be classified as quota unless the derived count says so.**
   A `600010` on reveal 3 of the day is a genuine challenge and must still arm a
   verification hold. Getting this backwards would let a timer release a real
   challenge — the exact defect the invariant exists to prevent.
3. **A test that proves the failure direction**, not just the success one: a
   `600010` while the day's count is LOW must still produce
   `VERIFICATION_HOLD`. Without that assertion the change is indistinguishable
   from removing the invariant.
4. It changes nothing about throughput. 82 items were ingested on 08-22 against
   a 20/day drain. This fix stops wasting the 20; it does not raise it.

## The open question this does not answer

What the counter is keyed to is still unresolved. Not cookies or profile (a
wiped throwaway profile also stalled). Not IP alone (Jesse's own browser works
on the same IP at the same moment). The remaining candidate is IP plus
automation signature — `navigator.webdriver=true`, no WebGL context at all,
`document.hasFocus()` permanently false, four fonts — which is **inferred, not
proven**.

That matters here only insofar as raising the ceiling is a separate project. The
design above is correct regardless of the answer, because it stops the futile
retries either way.

## The decisive test, which is Jesse's to run

Reveal links on ~21 different HDEncode pages in his own browser in one day.

- He walls too → a flat site rule, no code change raises it, and a registered
  account is the only lever.
- He never walls → ScanHound is in a lower-trust bucket, and the fingerprint
  work becomes worth doing.
