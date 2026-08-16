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

---

## 6. Round 2 — what your review led to (head `35fc91b`)

**Your blocker was right, and fixing it properly found more than it reported.**
`LAYOUT_CHANGED` and `REVEAL_CONTROL_ABSENT` were the two you named, but **nine of
the fourteen** `ScrapeDiagnostic` construction sites in `download_service` never
declared `transport_attempted` at all. So I did not patch the two call sites.
Transport is now a **declared property of each `ScrapeCode`** (`_TRANSPORT_BY_CODE`),
resolved through a total `effective_transport_attempted` that never returns `None`;
an explicit constructor value still wins. `test_every_scrape_code_declares_transport`
turns an omission into a build error rather than a silent `False`. And there is now
a producer → queue → attempt → `scraper_drift_report()` test, which is the seam every
earlier F10 test started halfway along.

**Your B3 finding was one of THREE dead predicates, not one.** Following it into
`queue_stall_report` found the same defect at the outermost gate:

| predicate | consequence |
|---|---|
| `scheduled_for <= datetime('now')` | `due_now` has read **0 for every same-day item** since this report was written. It gates the whole starvation branch. |
| `? < datetime('now',?) AND NOT EXISTS(... started_at > ?)` | the one you found; both halves cross-format |
| `MAX(started_at)` | lexical, so a pre-fix legacy row outranks every same-day real one as "most recent" — and that value decides whether the source is declared dead |

So `executor_starved` was dead behind **three independent gates**. It has never
once been capable of firing, including during the 2026-08-13 starvation it was
built to catch. All three now meet inside `julianday()`.

**Also fixed, with tests:**

- *(my own defect, found by an independent sweep, not by you)* the `manual_retry`
  pacing exemption was opened by the BULK paths — `retry_ready()` and a manual
  `_resume_batch()` stamp the same marker on every row, so one tap on "Retry all
  ready" sent N items at the source that was already refusing. Now exempt only
  when exactly **one** manual row is due.
- **Q2:** an `IN_PROGRESS` attempt holds the lane while its **claim lease** is
  live, not for the pacing interval.
- **`_defer_item_only`:** confirmed on every sub-claim including the double
  increment — `_claim_due` does increment `attempt_count`. Now requires
  `state='claimed' AND claimed_by=?`, checks rowcount, releases the claim, and
  does not re-increment.
- **F10 distinct:** confirmed. Threshold now counts globally distinct items.

**Q4 — the legacy rows: not done, deliberately.** You recommended deleting them. I
have left them in place: the `julianday()` fixes make them harmless where they were
previously actively misleading, and deleting production rows is the owner's call,
not mine. Flagging rather than silently declining.

**Q3 and the restructuring.** Jesse has asked for the full architectural programme,
not the instance fixes. Recommendation #3 is done — one declared vocabulary,
enforced by a test — and it is the one that would have prevented this round's
blocker. #1, #2, #4, #5, #6 and F5's dedicated `last_exhausted_probe_at` are
staged for subsequent rounds; this branch is not claiming them.

**Full suite: 5035 passed, 0 failed.** `origin/main` baseline in the same
container: 5 failed, 4971 passed.

### Question for round 2

Your #5 argued the fix must be architectural because the pattern reproduced inside
the branch fixing it. Agreed. But note *how* this round's blocker was found: not by
the structure, but by an external reader checking the producer of a value the
consumer depended on. Of the six recommendations, **which single one would have
caught THIS blocker without a reviewer?** I believe it is #3 as generalised above —
make the fact a declared property of the type so omission is impossible — and I
would rather do that one thoroughly across the other implicit-default fields than
do all six shallowly. Push back if you think #5's contract matrix is the stronger
first move.

---

## 7. Round 3 — the last open MEDIUM

**`source_no_progress` fixed.** Verified your reading before changing anything: with
no delivery ever recorded, `COALESCE(last_progress, '1970-01-01')` is older than any
deadline, so the first failed attempt in a fresh history declared the source dead —
and `last_attempt_at` being tested for existence only meant a stale attempt let
`executor_starved` and `source_no_progress` fire together.

Rewritten as a **no-progress episode**, per your three points:

* **start** — the earliest source-spending attempt since the last delivery, or the
  earliest ever if the source has never delivered
* **open** — that start is older than the deadline
* **live** — a source-spending attempt inside the deadline window, i.e. we are still
  *asking*. Without this, "we gave up hours ago" reads as a source fault when it is a
  scheduler one.

Only `transport_attempted = 1` counts, in both halves: a policy deferral never asked
the source anything. `evidence.no_progress_episode_since` is reported so the verdict
is checkable.

Your three suggested tests are all present and pass, plus four more.

### Two process notes, because both are the session's recurring lesson

**My first mutation run had two survivors, and they were the TESTS' fault.** The
episode filter and the recent filter were each independently sufficient to keep
`test_policy_deferrals...` green, so neither was actually constrained. Fixed by
adding the two asymmetric cases where only one filter can save you — a transported
failure followed only by policy skips (tests the *recent* filter), and a policy skip
followed by a real request (tests the *episode* filter). **7 of 7 mutants now caught,
including one that restores your reported defect verbatim.**

**A pre-existing test encoded the defect.**
`test_queue_stall_alerts.py::test_attempts_with_no_delivery_for_too_long` asserted
that ONE attempt five hours ago sets `source_no_progress`. Under the old code both
alarms fired for that fixture — the exact collapse you identified. Rather than
weaken the new rule to keep it green, the test now spans the window (which is the
claim it was really about) and two cases were added for the corrected contract,
including the one where a lone old attempt is a *scheduler* fault. Flagging it
because "an existing test went red" is precisely where a correct fix gets quietly
reverted.

**Also corrected: a lying fixture.** The `_attempt` helper never set
`source_progress` on a success, so a fixture "delivery" was invisible to the logic
that reads it — a test asserting "a delivery closes the episode" failed against
correct code. It now defaults to "a success delivered", which is what production
produces.

### On your answer to the round-2 question

Noted, and it changes the plan: you rank the generalized type-boundary declaration
*ahead* of the contract matrix for this class, because it prevents the bad state
rather than detecting it. Jesse has authorised the full programme; I will do the
remaining passes in that order — extend the declared-semantics treatment to the
other implicit-default fields first, then the seam contract tests, then mutate the
contract. Not on this branch.

**Full suite: 5046 passed, 0 failed.** `origin/main` baseline in the same
container: 5 failed, 4971 passed.

