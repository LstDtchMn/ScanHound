# Download queue: how it behaves vs how it should

**The complaint, in Jesse's words:** *"days of waiting for downloading to start
from a large batch — it should be a large batch gets queued and a slow and
steady link grabbing and downloading routine begins and continues."*

That is a design question, not a bug report, and it deserves one. What follows
is the observed behaviour with evidence, the mechanism that produced it, and
where I think intent and implementation diverge. I have marked what I could NOT
establish, because the container was restarted today and the logs covering the
gap are gone.

---

## 1. What was observed

| When | What |
|---|---|
| 2026-08-13 23:09-23:23 | Three HDEncode batches queued: 36, 30 and 2 items |
| 2026-08-13 23:33-23:39 | All three moved to `paused_source` |
| | 3 of 68 items had completed |
| 2026-08-13 → 2026-08-15 | **No further progress. ~48 hours.** |
| 2026-08-15 19:51:20 | A reveal succeeded (`reveal-control tier=links-control found=True`) |
| 2026-08-15 19:51:28 | All three batches "recorded real source delivery(ies) ... restoring its retry budget" |
| 2026-08-15 19:51+ | 62 items moved `waiting_source` → `ready`; work resumed |

Net: **68 items queued, 3 delivered in the first 30 minutes, 65 delivered
nothing for two days, then recovery.**

## 2. The mechanism that produced it

Pacing is configured and IS the "slow and steady" shape asked for:

    mode = staggered
    interval_seconds = 600      # one item every 10 minutes

A 36-item batch is therefore intended to take ~6 hours of steady work. Nothing
about the pacing is wrong.

The parking behaviour is separate, and it is batch-scoped:

1. An item's reveal fails in a way that looks like a source challenge or stall.
2. **The whole batch** transitions to `paused_source` — not just that item.
3. A cooldown is set: base `hdencode_reveal_cooldown_minutes` (default 60),
   multiplied by an escalation step on consecutive stalls, plus jitter.
4. `_maybe_auto_resume` (queue watchdog, polls every 2s) may resume the batch
   once the cooldown expires, subject to a retry budget.
5. The budget is REFUNDED when a resume produces real source deliveries, and
   only runs down on resumes that achieved nothing.

Each piece has a defensible rationale, and the code comments show they were all
added in response to real incidents.

## 3. Where behaviour diverges from intent

**A single item's failure parks the entire batch.** One unresolvable reveal —
and reveals fail for many reasons, including ones the code itself cannot
classify — stops 35 unrelated items that might have succeeded. Observed
tonight, minutes after recovery:

    [WARNING] The Young Riders: The link-reveal control was not found on this
    page, and no verification challenge was recognised. ... it may be a login
    or region gate, a pulled release, an error page, an unrecognised block, or
    a different page template.

That item legitimately cannot proceed. Under the current design it is the kind
of event that can park everything behind it.

**The cooldown compounds.** Base 60 minutes, escalating on consecutive stalls.
A source that is intermittently gated — which HDEncode demonstrably is —
produces: pause, wait an hour, resume, hit the gate again, escalate, wait
longer. The batch spends most of its life waiting, and the wait grows precisely
when the source is flakiest.

**Recovery depends on the source healing itself.** The budget refunds on real
deliveries, so the system needs a successful delivery to regain retries. If
nothing is attempting deliveries, nothing proves health, and the batch waits.
Tonight's recovery was triggered by a reveal succeeding at 19:51:20 — not by a
timer expiring.

**What I could NOT establish:** whether anything was attempting reveals during
the 48-hour gap. The container was restarted today (deploy), so logs from 13-15
August are gone. I cannot distinguish:

* the source was gated for ~48 hours and every attempt legitimately failed, from
* nothing was attempting, and the batch sat because no attempt was made.

I raised the restart as the likely trigger earlier in conversation. **I am
withdrawing that** — the log shows the resume followed a successful reveal, and
I have no evidence about attempt frequency during the gap.

## 4. The behaviour that was actually asked for

> a large batch gets queued and a slow and steady link grabbing and downloading
> routine begins and continues

Restated as properties:

1. **Progress is continuous.** A queued batch makes forward progress at its
   configured pace until it is genuinely done.
2. **Failures are item-scoped by default.** One item that cannot be revealed
   should not stop items that can.
3. **Source-wide backoff exists but is bounded.** A genuine challenge should
   slow the source down, not stop the batch indefinitely.
4. **Nothing waits silently.** If a batch has made no progress for hours,
   somebody should be told. Nothing was.

Property 4 failed completely: there are **no notification tables at all**, and
no alert fired in 48 hours. That is the same shape as every other problem found
today — a system that stops and produces no signal.

## 5. Questions for review

1. **Is batch-scoped pausing the right granularity?** Item-scoped deferral with
   a source-level rate limit would let 35 healthy items proceed while one
   problematic item backs off. What breaks if the pause moves from the batch to
   the source, with items individually deferred?
2. **Should an unclassifiable reveal failure ("tier=none, no challenge
   recognised") pause anything at all?** It is explicitly NOT a recognised
   challenge. Treating it as a source stall means an item-specific problem — a
   pulled release, a template change — parks the queue.
3. **Is escalating cooldown correct for an intermittently-gated source?**
   Escalation assumes the source is getting worse. HDEncode appears to gate
   intermittently, so escalation may be maximising wait exactly when a retry
   would have worked.
4. **Should recovery depend on a successful delivery to refund budget?** It
   creates a bootstrap problem: you need a success to earn retries, and retries
   to get a success. What breaks if the budget also refunds on a timer?
5. **What SHOULD alert here?** A batch with no completed item in N hours is a
   concrete, cheap condition. Is that the right signal, or is there a better
   one — and should it use the Gotify path already proven working?
6. **Is 10 minutes between items right?** It was presumably chosen to look
   human. With 36-item batches that is 6 hours minimum. Does the pacing need to
   adapt to batch size, or is fixed spacing the safer property?

## 6. What is NOT in question

The staggered pacing itself, the hold-rather-than-fail decision (items were
preserved intact and in order for 48 hours, and nothing was lost or
double-grabbed), and the refund-on-progress rule are all sound in isolation.
The question is whether their composition produces the continuous progress the
feature is supposed to deliver.

---

## Current state, for reference

    batches: 34 completed, 4 scheduled
    items:   334 completed, 63 ready, 1 claimed, 1 verification_required, 7 cancelled
    pacing:  staggered, 600s
    provenance: 1 link set recorded (the feature populates as new grabs flow)

Deployed today: #72, #73, #74, #75, #76, #77, #78. JDownloader healthy.
