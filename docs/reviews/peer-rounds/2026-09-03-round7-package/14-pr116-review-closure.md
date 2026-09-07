# Closure — independent review of #116–#118 (2026-09-07)

Reviewer disposition was **REQUEST CHANGES**: six HIGH, four MEDIUM. **Every finding was real.** I reproduced each one against a real `DatabaseManager` before changing anything, and each is now fixed with a regression test proven to fail without the fix.

Branch `feat/rss-canary-authority-and-evidence` @ `95cffae`. #117 and #118 are folded into it — one branch, one PR, one review — which also answers the topology finding.

## The topology correction, and one of mine

The reviewer is right that #117 and #118 were siblings, not a linear stack. They were both based on #116; my summary to the owner said "stacked in order", which was wrong. They are now merged into #116 and closed.

## Findings

| # | finding | disposition |
|---|---|---|
| HIGH 1 | real evidence raised `TypeError` instead of an authority decision | **FIXED** |
| HIGH 2 | activation replay read a membership key nothing writes | **FIXED** |
| HIGH 3 | no legitimate first-promotion path | **FIXED** |
| HIGH 4 | activation did not enforce the approved qualification contract | **FIXED** |
| HIGH 5 | runtime coverage unscoped; source provenance never populated | **FIXED** |
| HIGH 6 | a failed durable write still refreshed canary success | **FIXED** |
| MEDIUM 1 | ledger recorded two of its four kinds | **FIXED** |
| MEDIUM 2 | guard omitted `background_scan_sources` | **FIXED** |
| MEDIUM 3 | #117 hid the activation reasons it claimed to expose | **FIXED** |
| MEDIUM 4 | authority and scanner used different normalized settings | **FIXED** |

### HIGH 1 — the crash

Confirmed exactly as reported. `get_shadow_cycle_url_sets` returns `completed_at` as SQLite stored it, a `str`; it reached `classify_coverage`, which compares with `<=` against parsed datetimes. My own probe on a real database:

```
cycle 'at' type   : str
canary_evidence   : RAISED TypeError: '<=' not supported between 'str' and 'datetime.datetime'
evaluate_runtime  : RAISED TypeError  (the function documents that it never raises)
```

Timestamps now pass through `parse_utc` at the boundary. **A second defect in the same three lines**, not in the report: the newest canary was selected by comparing those strings, so a `+09:00` row sorts after a later `+00:00` one and the newest canary becomes the wrong cycle — turning a proven gap into "pending". The comparison is on parsed instants, and a cycle whose own timestamp cannot be read is `coverage_unassessable`, not a silent skip.

### HIGH 2 — the third consumer

Confirmed. `canary_activation_evidence` still read the configured spelling. On a real database with two recorded cycles the replay saw zero and answered `visibility_window_unknown`.

This is the boundary defect I had already found and fixed in two places on 2026-09-07 and missed in the third. The lesson generalises further than I applied it.

### HIGH 3 — the deadlock

Confirmed by construction: `record_canary_attempt` ran only when `canary_run`, which required the **effective** mode to already be `rss_primary`.

The dense qualification crawl already *is* a canary observation — full depth, no early stop, membership under the same keys — so it now records the attempt. Deliberately a **separate flag** from `canary_run`: that one also marks the comparison row `rss_primary_canary` and books canary cost, and a qualification cycle is a shadow comparison paid for as qualification overhead. The same grading applies, so a qualifying crawl that observes nothing still fails.

### HIGH 4 — the gate that measured nothing

Confirmed. `if not cfg.get(EPOCH_KEY)` is a truthiness test; `"not-a-timestamp"` passed it, and so did four cycles spanning three minutes.

Implemented as rev 2 §7 specifies: 14 consecutive observed clean days of eligible cycles, no gap over six hours, a longer gap resetting the clock to the first eligible cycle after it. It publishes what it measured — clean days, worst gap, whether an outage reset the clock — because an owner refused a promotion needs to know whether they are two days short or were reset last night.

RHC-13's **0.50 floor** is enforced at activation. Shadow readiness only ever asked for `> 0`. It is activation-only on purpose: a month of measured saving is not a reason to demote a healthy system, and cost must not outrank safety cadence.

### HIGH 5 — scope and provenance

Both halves confirmed. The runtime read every stored cycle and unioned all historical carriage into one set; the counterexample now in the suite records RSS carriage **before** the promotion and four canaries after it showing the URL listing-only, and the gap is proven rather than excused. The window starts at the promotion; the activation replay is scoped to the epoch.

`rss_present` was written as `bool(row.get("rss_present"))` from crawl rows that never carry that key — a constant. It is populated from each source's **own mapped feed**, per RHC-9. The column was `NOT NULL`, which collapsed "unavailable" into "not carried"; it is nullable and three-valued now.

**One deviation from the design's wording, flagged rather than hidden.** The design says insufficient provenance → `unassessable` → blocker, and `unassessable` is a revocation. I map it to `canary_evidence_insufficient`, a **suspension**. Rationale: a revocation deletes the promotion, and thin provenance on a freshly promoted system would delete it before the first canary resolved anything — the same trap that made me add the suspension in the first place. It is still fail-closed: primary does not run. **If you disagree, say so and I will change it.**

### HIGH 6 — the lost write

Confirmed. The write failure was logged and grading continued from in-memory rows. A canary's success claim is "these pages were observed **and** the evidence is on disk"; a failed membership write is now `error` / `membership_write_failed`.

### MEDIUM 1, 2, 3, 4

All confirmed and fixed: `rss_poll` booked once per cycle outside the comparison path (poll-only cycles were never counted at all), `fallback` and `canary_retry` given real writers; `background_scan_sources` added to the guard, including the scanner's own forced-list rule; activation blockers and named uncrawled sources rendered beside the control, with the readiness card reporting the authority's verdict rather than raw shadow readiness; and the contract normalised once so the hash, the replay, the scheduler and the crawler see the same values.

## Verification

- **16 mutants** on whole-tree copies with a green control, each restoring one reviewed defect. **All killed.**
- Four survived the first run. Three were the same mistake — tests that exercised a helper directly while nothing proved the scanner ever called it — and are now covered by wiring tests that drive `scan_once`. The fourth was **my own mutant being wrong**: a 20-space pattern is a substring of a 24-space line, so it mutated a different branch and "survived" a test that was never about it.
- 143 focused backend tests; 482 frontend tests; `svelte-check` 0 errors; production build passes.
- Whole suite, on a whole-tree copy carrying #110's trash isolation: **5,682 passed, 5 skipped, 1 failed**, 16m20s. The failure is `tests/test_dv_host_scan.py::test_post_rows_direct_success_delivers_key` with `[WinError 10054]` — TST-3, the Windows socket-abort race whose fix is #115 and is not on this branch. Verified rather than assumed: #115's diff changes exactly that test and consumes the request body before responding, and the test passes 6/6 re-runs here. Your own run hit the same file's sandbox-unrelated failures in `test_process_control.py`; this environment does not.
- `C:\.scanhound-trash` absent before and after.
- New file `tests/test_canary_real_database_boundary.py` uses `DatabaseManager` itself, writes through the real writers and reads through the real readers, and constructs none of the values under test.

## What I take from this

The reviewer's closing line is the important one: *green tests do not contradict the findings; the current tests largely exercise pure policy functions or doubles that reuse one key/timestamp shape on both sides.* That is exactly what happened. 5,633 passing tests and 26 killed mutants described my assumptions rather than the system, because the fixtures produced the values they then consumed.

The owner has made "run it against the real thing before calling it verified" a standing rule for my work.

## Not claimed

Nothing here has run against production. No merge, deploy or enablement is requested, and RSS primary stays disabled.
