# Round 3 — the no-window fix, and a line of yours I changed

**Branch:** `agent/hybrid-sweep-implementation` · **Head:** `f4faf03`
**Previously reviewed head:** `7125d6b`
**Full suite:** 4413 passed, 4 skipped, 0 failed

Your blocking defect is fixed. Chasing it turned up a second implementation you
could not have known about, and led me to modify a line **you deliberately
specified** in July. That last part is the real reason this is in front of you.

---

## 1. Your blocking defect — confirmed exactly, fixed

You were right, and I verified it against the live database before changing
anything:

```
readiness with no window:
   relevant_misses = 102
   reasons         = ['relevant_misses_detected', 'qualification_window_not_started']

what the COLLECTOR does with that:
   stop conditions = ['RELEVANT RSS MISS x102']
   -> MANDATORY STOP fires
   -> Gotify priority 8 alert
   -> "Per the runbook: stop and roll back. Do not continue the window."
   -> exit code 3, Scheduled Task shows FAILED
```

A rollback instruction, from the void window, before the new one started.

Fixed as you specified: the no-window path returns an empty current-window
summary and moves the history to `historical_evidence_not_counted`, a key
nothing gates on.

This is the same error shape as the two defects you caught in round 1 — I
verified the field I was reasoning about (`ready`, `reasons`) and never asked
what consumes the others. Third time this session.

## 2. There are TWO readiness implementations, and you reviewed the wrong one

Your report quoted the collector's `if misses:` logic, so you were reading the
collector — but you attributed those misses to the app-side
`get_hdencode_rss_readiness`. They do not come from there.

`docs/feature-pack-review/qualification/scripts/05_shadow_evidence.py` is an
independent DB-derived mirror of the same logic. **Its** output is
`summary["readiness"]`, which is what the collector's stop logic reads. The
app-side function only supplies `app_readiness`, the cross-check.

**Fixing `database.py` alone would have passed every test I would have written
and still fired the false alert in production.** Both are now fixed:

```
app side       relevant_misses 0 · cycles 0 · days 0.0 · COLLECTOR STOP: NONE
mirror script  relevant_misses 0 · cycles 0 · days 0.0 · COLLECTOR STOP: NONE
(102 historical misses preserved under the diagnostic key in both)
```

Worth stating as a standing hazard: **any future change to gate or readiness
semantics must be applied to both.** Neither references the other.

## 3. THE ONE I MOST WANT YOU TO CHECK — I changed a line you specified

Commit `f5e3c6e`, 2026-07-21, "Apply RSS readiness/recovery corrections
(ChatGPT adversarial audit)", **added** this to the mirror script:

```python
all_misses = con.execute(
    "SELECT COALESCE(SUM(relevant_miss_count),0) "
    "FROM hdencode_shadow_cycles"
).fetchone()[0]
```

Deliberately unscoped, and deliberately **not** subject to the eligibility
filter applied to the cycle totals — because a relevant miss is a mandatory
stop even when the cycle was otherwise incomplete. That was correct when there
was only ever one window.

Introducing windows invalidates the assumption, so I changed it:

```python
"FROM hdencode_shadow_cycles WHERE 1=1" + window_scope, window_params
```

**What I preserved:** the eligibility filter is still not applied, so a miss in
an incomplete cycle still counts. **What I changed:** the window boundary now
applies.

My reading is that your intent was "no miss may be excused by cycle quality",
not "no miss may ever be excluded by anything", and a window boundary is a
different axis from cycle eligibility. But that is me interpreting a decision
you made, in a place where being wrong means a real miss goes uncounted. If you
meant the query to be absolutely unscoped, say so and I will find another way to
keep the void window's misses out of the current gate.

The same reasoning was applied to `database.py`'s miss query, which carries a
comment recording it.

## 4. Timestamp safeguards, as you required

Both layers now parse the boundary and re-emit it in the stored ISO `+00:00`
form before it reaches SQL. `completed_at` is TEXT, so a `...Z` or bare-date
value would compare lexicographically against a different shape and silently
select the wrong rows.

Fail-closed on malformed values, **and on future ones** — a future boundary
excludes every cycle forever, so the window could never accumulate evidence and
would be indistinguishable from a stalled collector until someone read the
timestamp.

Not yet done from your list: protection against changing the boundary once valid
cycles have accumulated. Today it is a file anyone can edit. Say whether you
want that enforced in code or handled as a runbook rule.

## 5. Two of my own tests failed on this change

They asserted the intermediate, still-defective behaviour — that unscoped
readiness returns the inherited totals. I rewrote them to draw the actual
distinction: the raw summary helper still aggregates when unscoped, which is its
job as a query helper, but readiness never surfaces that in gate-consumed
fields. Flagging it because "my fix broke my tests so I changed my tests" should
not pass without being said out loud.

## Tests

21 in the window file, 10 new this round, including your required case:

* gate-consumed fields are zero with no window;
* **the exact collector stop logic, quoted from the collector**, produces no
  stop;
* the only reason is `qualification_window_not_started`;
* history preserved under the explicitly named key;
* a real miss inside a **started** window still stops the collector — the
  suppression must apply only to the no-window case;
* `...Z` and naive timestamps normalise; malformed and future values fail
  closed.

## Standing state

Nothing deployed. `auto_rename_enabled`, `auto_grab_enabled`,
`hdencode_rss_auto_grab_enabled` all verified `false`. `window-start-at.txt`
does not exist, so the gate is correctly blocked right now.

Item B (rename safety) is untouched by this round — your five-point revision is
logged and will be done before steps 3–5, not alongside them.

Still open from your consolidated review: no CI on the agent branches, and
`expected_typical` unwired so volume-anomaly detection stays off and unclaimed.
