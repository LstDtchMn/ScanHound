# Addendum — the fresh qualification window was not implementable

**Raised after** the consolidated review's Part 9 gate decision.
**Status:** fixed on `agent/hybrid-sweep-implementation`. Nothing deployed.

Your §7.2 requires *"at least seven calendar days of evidence **from the
corrected build**"* and your §4 states *"all old qualification evidence is void;
no continuation or reuse is permitted."*

I went to satisfy §7.1 and found that **neither was implementable.** "Old
evidence is void" was a policy with no mechanism behind it.

---

## What was wrong

`get_hdencode_shadow_summary()` aggregated **every row ever recorded** —
`COUNT(*)`, `MIN(completed_at)`, `MAX(completed_at)`, `SUM(...)` over the whole
`hdencode_shadow_cycles` table. There was no window boundary anywhere: no
`window_id`, no `window_started_at`, no reset marker in the schema.

Live table as of 2026-08-01: **206 rows from 2026-07-22 to today**, still
accumulating (the shadow is running), carrying **101 relevant misses**.

So on deploying the corrected build the gate would have been wrong in **both
directions simultaneously**:

* the 7-day and 20-cycle criteria would read as **already satisfied** — earned
  entirely by pre-fix evidence the corrected build never produced;
* `relevant_misses = 101` is a hard stop condition, so the gate would have been
  **permanently blocked** by misses from the void window.

## Demonstrated against the real production database (read-only copy)

```
=== BEFORE (no window) — what a fresh deploy would have reported ===
  successful_cycles    = 181
  observed_days        = 10.631199423715277
  relevant_misses      = 101
  ready                = False
  reasons              = ['relevant_misses_detected',
                          'qualification_window_not_started']

=== AFTER (window starts now) — an honest empty window ===
  successful_cycles    = 0
  observed_days        = 0.0
  relevant_misses      = 0
  ready                = False
  reasons              = ['insufficient_comparison_cycles',
                          'insufficient_observation_days',
                          'request_reduction_not_proven',
                          'restart_or_catchup_recovery_not_proven']
```

The "after" column is what §7.2 actually asks for: a window that must be earned
from scratch, with every criterion honestly unmet.

## The fix

A `hdencode_rss_window_start_at` config value scopes the summary
(`WHERE completed_at >= ?`) and is threaded through all four readiness call
sites. Chosen over archiving/deleting the old rows so the previous window stays
available for forensics — it simply stops counting toward the current one.

**Fail-closed, and this is the part I want you to check:** an unset window is a
**blocking readiness reason** (`qualification_window_not_started`), not a licence
to fall back to counting everything. Absent scoping is exactly how a fresh build
would have inherited a satisfied 7-day criterion, so absent scoping must block.
You can see it firing in the BEFORE output above.

## Three existing tests changed, and why

`test_readiness_counts_comparisons_not_ingest_rows` and
`test_readiness_requires_cycles_days_and_two_healthy_normal_feeds` both asserted
`ready is True` without any window. They predate the concept and were encoding
the old "count everything" semantics.

I scoped each to a window covering its own synthetic cycles rather than
weakening the new check — they are testing comparison-vs-ingest accounting and
feed health respectively, not window semantics. Flagging it explicitly because
"the fix broke two tests so I adjusted the tests" is a claim that deserves
scrutiny rather than a footnote.

The third is `test_config.py::test_default_config_has_no_unexpected_keys`, an
explicit `EXPECTED_DEFAULT_KEYS` allowlist that fires whenever a config key is
added. That is a change-detector working exactly as designed, and the new key
was added to the allowlist. Worth noting as the one guard in this codebase that
caught my change unprompted — it is the same class of protection the CI
recommendation is about.

## Tests

11 new, in `tests/test_rss_qualification_window.py`, including:

* old relevant misses do not poison a new window — **and** a miss *inside* the
  window still counts, so scoping cannot become a way to launder misses;
* `observed_days` measures only the new window;
* old rows are retained, not deleted;
* an unstarted window blocks;
* a scoped window still enforces every other criterion — scoping removes
  inherited evidence, never the bar;
* the live production shape reproduced end to end.

## What this does not change

Not a challenge to your gate decision — the implementation verdict stands. This
is a prerequisite for §7.1/§7.2 being satisfiable at all, found while working
through your own conditions.

Also unchanged: nothing deployed, auto-rename and auto-grab off, Part 9 is
Jesse's gate. The old shadow run is still recording, by his decision — it costs
a few requests an hour and the data is void either way, so more of it changes
nothing, and stopping it risks forgetting to restart before the real window.

**One consequence worth naming:** starting the fresh window is now an explicit
act — setting a timestamp — rather than something that happens implicitly on
deploy. That is the point, but it means a deploy alone does not begin the
window, and nothing will remind anyone. It should be an explicit step in the
Part 9 runbook.
