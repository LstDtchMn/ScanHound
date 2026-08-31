# Round 4 — boundary lock, parity fixtures, category separation

**Branch:** `agent/hybrid-sweep-implementation` · **Head:** `71bfebf`
**Previously reviewed head:** `e241ec3`
**Full suite:** 4433 passed, 4 skipped, 0 failed

All four round-3 items addressed. Two places where I went beyond what you
specified, both flagged below — those are the ones worth your attention.

---

## 1. Boundary lock — the Part 9 blocker

Implemented as durable safety state, per your design:

| behaviour | result |
|---|---|
| correct the boundary before any evidence | allowed — that is setup |
| change it once ≥1 cycle exists inside | **`ValueError`, naming the cycle count** |
| explicit new window (`supersede=True`) | allowed, records `previous_window_start_at` |
| restate the same boundary | no-op |
| malformed or future boundary | refused outright |
| configuration disagrees with persisted | `qualification_window_boundary_changed` |
| configuration **cleared** while a window is live | also a mismatch, also blocks |
| persisted vs configured | **persisted wins** |

Stored with `created_at`, `build_ref` and `operator_note`. Superseded rows are
retained, so the sequence of windows stays auditable.

The test that matters most states the threat directly: a relevant miss is
recorded inside the window, readiness reports it, and an attempt to move the
boundary past it raises. That is the attack your review named — turning a failed
window into a passing one with one edit — and it is now impossible without an
explicit supersede.

### DEPARTURE 1 — I removed the boundary from the collector entirely

You specified persistence plus a configuration cross-check. I went further: the
collector no longer passes the boundary at all, and the evidence script reads it
straight from the database.

Reasoning: I had previously wired the collector to pass a value read from
`window-start-at.txt`. Once the boundary became durable state, that file was a
second source of truth *and an edit surface* — precisely what the lock exists to
close. A flag or file handed to the evidence script would let someone move the
line without touching the locked record.

Consequence: both implementations now read the **same** persisted row, which is
also what makes the parity fixtures meaningful. If you wanted the collector to
retain an independent declaration as a cross-check, say so — I judged the extra
edit surface to be worth more than the redundancy, but that is a judgement.

## 2. Parity fixtures

`tests/test_readiness_parity.py`. One synthetic database, both implementations,
agreement asserted on every gate-consumed field: `successful_cycles`,
`relevant_misses`, `recovery_cycles`, `request_reduction_pct`,
`window_start_at`.

Cases, as you listed them: no window · old misses only · a miss inside the
current window · malformed boundary · future boundary · unhealthy feeds ·
insufficient duration · a full passing window. Plus one more — that the mirror
picks up the boundary with no flag, proving the durable-state path end to end.

They are black-box and deliberately do **not** refactor the two into one. The
file's docstring records why this exists: a defect fixed on one side while the
other kept the old behaviour, where the *other* is what the alert path reads.

## 3. Category separation

Three conditions, three distinct codes, and the alert text now names which:

```
relevant_misses_detected      current-window RSS miss  -> qualification
historical_evidence_not_...   previous window          -> diagnostic only
shadow_miss_count_mismatch    row-level corruption     -> integrity, global
```

The collector's push now reads `RELEVANT RSS MISS xN IN THE CURRENT WINDOW`.
The notification is often all anyone sees, and a row-level integrity fault
demands a different response from an RSS miss.

The mismatch check remains deliberately **unscoped** — corruption is corruption
wherever it occurs — with a comment recording that this is intentional, not an
oversight the next reader should "fix".

## 4. Three of my own tests broke, all legitimately

* two supplied a configuration boundary without persisting one, which no longer
  works by design;
* one used hardcoded dates that were past when written and are now **future**,
  so the fail-closed future check correctly refused them.

That third one is worth naming as a real defect in my tests rather than a
chore: a literal timestamp in a suite that refuses future boundaries is a test
that silently rots. Both files now derive every timestamp relative to the
current time.

### DEPARTURE 2 — no window is started

The lock means a window must be started explicitly. **I have not started one**,
and `window-start-at.txt` does not exist, so the gate currently reports
`qualification_window_not_started` and is correctly blocked.

Starting it is a deliberate operator act tied to the corrected deployment
instant, so it belongs to Jesse at deploy time, not to me now. I mention it
because "the gate is blocked" is the expected state, not a fault.

---

## What I did not do

* `expected_typical` remains unwired; volume-anomaly detection stays off and
  unclaimed.
* No CI. Every figure here is locally produced and unattested — the same
  limitation that let a misconfigured harness produce three wrong failure counts
  earlier today.
* Item B (rename safety) untouched. Your five-point revision is logged and
  happens before steps 3–5, not alongside them.

## Standing state

Nothing deployed. `auto_rename_enabled`, `auto_grab_enabled` and
`hdencode_rss_auto_grab_enabled` verified `false` against the live config.
Part 9 remains Jesse's gate.
