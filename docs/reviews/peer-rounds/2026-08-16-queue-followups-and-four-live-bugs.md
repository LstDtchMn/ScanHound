# Peer review — queue pacing follow-ups, and four live bugs found while building them

**Repository:** `LstDtchMn/ScanHound`
**Branch:** `feat/queue-review-followups`
**Base:** `main`

Files to read: `backend/download_queue.py`, `backend/database.py`,
`backend/queue_recovery_policy.py`, `tests/test_queue_review_followups.py`,
`tests/test_throttle_lifecycle_integration.py`, `tests/test_repeatable_batch_resume.py`.

---

## 1. What this branch was supposed to be

Three findings left unbuilt from the 2026-08-15 queue-pacing review:

- **F4** — source pacing is per *batch*, so two concurrent batches each get their own
  600s lane and hit HDEncode twice as fast as configured. Batch pacing is demand;
  the source gate should be capacity.
- **F5** — the auto-resume retry budget deadlocks: refunding it requires a source
  *delivery*, getting a delivery requires a *retry*, and once the counter is spent
  with nothing delivered only a human can revive the batch.
- **F10** — scraper template drift is invisible: structural failures
  (`layout_changed`, `reveal_control_absent`) sit in the same bucket as source
  gating, and a broken selector and a hostile source want opposite responses.

All three are built (§3). **That is not the interesting part of this review.**

## 2. Four defects in already-merged, currently-deployed code

Building F4 and F10 required reading `download_queue_attempts`, the telemetry table
added in #79. Doing so surfaced four defects, three of which compound into a single
silent failure. The running container (`scanhound:latest`, built 2026-08-16 07:56Z)
contains all four — verified by grepping `/app/backend/` inside it, not by reading
the repo.

### B1 — every attempt is recorded as a failure

`_execute` opens an attempt row, runs the work, and closes it in a `finally` via
`_close_attempt_if_open`, whose docstring says *"_execute_inner closes with the real
outcome. This only fires when it could not."*

`_execute_inner` never closed anything. There were exactly two `begin/close` call
sites in the module — the open, and the backstop — so **the backstop was the only
closer**. Production agrees; all rows in the table read:

```
terminal_status  reason_code           transport_attempted  count
FAILED           attempt_not_closed    0                    3
```

Three rows, three "failures", none of them real. Every consumer of this table —
stall reporting, source-liveness, drift detection, and the new F4 gate — was
reading a queue in which nothing had ever succeeded and nothing had ever reached
the source.

Fixed with a `_close_attempt` helper called on each of the four terminal paths in
`_execute_inner`. The backstop keeps `only_if_open=True`, so it cannot overwrite a
real outcome.

### B2 — the ambiguous-deferral path raises on every call

#83 added `_defer_item_only` so one item's ambiguous failure could not park its 78
siblings. It writes `queue_reason = 'item_retry'`. The column is
`CHECK(queue_reason IN ('user_batch','interactive_challenge','source_deferred','manual_retry'))`.

`'item_retry'` is not in that list, so every call raises `sqlite3.IntegrityError`
out of `_execute_inner`. The method that exists to prevent 78 permanent failures
had **never once completed**. Changed to `'source_deferred'`.

### B3 — attempts stamped in local time, compared against UTC

`begin_queue_attempt` used `datetime.datetime.now()` — naive **local** time. All six
window queries over the table compare it against sqlite's `datetime('now')`, which
is **UTC**. On this host that is a 4-hour skew, and the separator differs too
(`'T'` vs `' '`), so it does not fail in one consistent direction: `'T' > ' '`, so a
same-day row sorts *after* any same-day UTC cutoff, while a row written 00:00–04:00
UTC sorts before it.

Fixed by stamping UTC in sqlite's own format, with the queue passing its own
injectable clock so the pacing window and the column it reads cannot disagree.

### B4 — the promotion threshold is off by one

`_scope_is_earned` asks whether an ambiguous failure has been seen on
`AMBIGUOUS_PROMOTION_DISTINCT_ITEMS = 2` distinct items. It asks *before* the
current attempt is closed, and `distinct_items_failing` filters on `reason_code` —
which the current attempt does not have yet, because the reason is exactly what is
being decided. The current item was invisible to its own promotion check, so the
constant meant **3 items, not 2**.

### How they compound

`_scope_is_earned` calls `distinct_items_failing(..., within_seconds=3600)`.

- B3: a row written seconds ago never falls inside a 3600s window → answer 0.
- B1: the reason recorded was `attempt_not_closed` anyway → answer 0.
- ⇒ scope was **never** earned for an ambiguous reason.
- ⇒ every reveal stall took the item-local path.
- ⇒ every reveal stall hit **B2** and raised.

Each bug hid the others. The suite did not catch any of it: the five integration
tests that drive this exact sequence were **already failing on `origin/main`**
before this branch — confirmed by running `origin/main` in the same container, same
session.

## 3. The findings as built

**F4.** A `NOT EXISTS` against `download_queue_attempts` in the item-claim query,
keyed on `source`. Only attempts that actually *spend* the source consume the lane
(`transport_attempted = 1`), plus any attempt still `IN_PROGRESS`, which has not yet
reported whether it reached the source. `SOURCE_MIN_INTERVAL_SECONDS = 60` is a
floor configuration cannot lower.

A judgement call worth checking: a `duplicate` is scored as **not** having reached
the source, because `download_service` decides it against our own records and
returns before the scraper runs ("don't scrape or re-send it"). So a batch of
already-grabbed titles does not pace itself against a source it never contacted.

**The gate also carries an exemption, added after the first full-suite run.** Five
tests in `test_verification_hold.py` — a file this branch does not touch — went red,
because they call `retry_item()` and then claim. The gate was refusing a human's
"Retry now" for up to 60s, which in production is worse than slow: the API accepts
the retry, the worker silently declines to claim it, and the UI has already reported
success. Rows with `queue_reason = 'manual_retry'` are therefore exempt — pacing
throttles the machine, not the operator.

That exemption keys on a string that **two automatic paths also write**
(`recover_interrupted`, `_recover_expired_claim`). They are harmless only because
both also set `state = 'failed'`, which `_claim_due` never selects. That is
load-bearing and entirely implicit, so there is now a test that reads those two
functions and fails if either stops parking the row as `failed`. Question 5 asks
whether that is good enough or whether it wants a dedicated column.

**F5.** A quiet-window exit from `BUDGET_SPENT` in `queue_recovery_policy.decide()`,
via `_quiet_long_enough()`, which fails **closed** on every uncertainty — absent
timestamp, absent window, non-positive window, unparsable value. The counter is
**not** refunded, and `_resume_batch` stamps `updated_at` on every attempt, which
restarts the quiet period. So an exhausted batch gets at most one attempt per 6h
window, forever, instead of none, forever. `VERIFICATION_HOLD` returns *above* this,
so no timer can release a hold.

Worth flagging: my first version put the window only in the discovery query. Every
unit test passed. The batch was then discovered and immediately refused by
`_resume_batch`, which re-reads the budget and is the single authority — so the
change shipped doing nothing. Only a test driving the real sweep caught it.

**F10.** `scraper_drift_report()` counts DISTINCT items with
`transport_attempted = 1` on `_STRUCTURAL_REASONS`, threshold 3, surfaced in
`queue_stall_report()` under its own `scraper_drift` key.

## 4. Verification

- **Full suite: branch 0 failed / 5014 passed. `origin/main` baseline: 5 failed /
  4971 passed**, all five in `test_throttle_lifecycle_integration.py` — the ones
  §2 explains. Both runs in the same container, same session, whole tree copied in.
- **31 mutants, 31 caught.** Applied by line number where the anchor was not unique.
  One mutant initially "survived" because I had anchored it on a matching string in
  the wrong function — a dict literal in `_recover_expired_claim` rather than the
  SQL in `retry_item`. Unique anchor, wrong occurrence.
- The harness produced **two false survivors** first: two mutants that shorten a
  file by the same number of bytes within one second are indistinguishable to
  Python's `(mtime, size)` `.pyc` check, so the second silently ran the first's
  bytecode. Purging `__pycache__` between mutants fixed it. Reported because a
  mutation score is worthless if the harness can lie in the safe direction.
- One test fixture was also wrong: the reveal-stall outcome carried
  `transport_attempted=False`, where `download_service` sets `True`. The scope
  classifier counts only `transport_attempted=1` as evidence about a source, so the
  fixture could never accumulate the evidence the tests then asserted on.
- Full-suite results and the `origin/main` baseline are in the PR body.

## 5. Questions

1. **The `duplicate` judgement.** I score a cache-resolved duplicate as *not*
   spending the source lane, on the strength of `download_service`'s own comment
   that it returns before scraping. Is there a path where a `duplicate` verdict is
   reached only *after* a page fetch? If so the lane is under-charged.

2. **`IN_PROGRESS` holds the lane.** An unfinished attempt blocks claiming because
   the conservative reading is that it reached the source. A wedged worker therefore
   stops all claiming for that source until its lease expires. Right trade against
   the alternative — a wedged worker's siblings stampeding the source?

3. **F5's self-limiting mechanism rests on `_resume_batch` writing `updated_at`
   unconditionally.** I verified it does today. That is an implicit coupling that
   nothing declares. Should the window read a dedicated `last_auto_resume_at`
   instead, or is the coupling acceptable with a comment?

4. **B3's legacy rows.** Three production rows remain in the old local-time shape.
   They carry `attempt_not_closed`, match no structural or source reason, and age
   out of every window within 24h — so I left them. Convert, delete, or leave?

5. **The manual-retry exemption's key.** It keys on `queue_reason = 'manual_retry'`,
   a value two automatic recovery paths also write. They cannot reach the gate today
   only because they park the row as `failed`, and a test now pins that. Is an
   asserted invariant acceptable here, or does this want an explicit column
   (`pacing_exempt`) so the intent is declared rather than inferred? I chose the pin
   because both automatic paths recover at most one item and a migration on this
   branch adds deployment risk of its own, but I do not have strong conviction.

6. **Did the gate need a floor at all?** `SOURCE_MIN_INTERVAL_SECONDS = 60` cannot
   be lowered by configuration. The review asked for pacing to be *global* rather
   than *per-batch*; the un-overridable floor is my addition, and it is what made
   those five verification-hold tests fail. Is the floor defensible, or should the
   configured interval simply be honoured, including zero as an explicit opt-out?

7. **The pattern.** B1, B2 and B4 are the same shape: a component was written and
   tested, and *nothing verified its consumer*. The attempt row was tested; that
   `_execute_inner` closes it was not. `_defer_item_only` was tested through its
   effect on a row it never managed to write. Is there a structural check — beyond
   "write a test that drives the real sequence" — that would catch this class before
   deployment rather than nine days after?
