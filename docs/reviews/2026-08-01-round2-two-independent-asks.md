# Review round 2 — two independent items

**These are deliberately separate.** You said you would treat the rename-safety
work independently of the Part 9 gate rather than mixing the reviews. I agree,
so this document keeps them apart: different branches, different questions,
different consequences if you say no. Please answer them separately — a single
blended verdict is the outcome I am trying to avoid.

| | Item A | Item B |
|---|---|---|
| Branch | `agent/hybrid-sweep-implementation` | `agent/rename-safety-gate` |
| Head | `7125d6b` | `0b0a398` |
| Base | current `main` | current `main` (independent of A) |
| Blocks Part 9? | **Yes** | No |
| Status | complete | 2 of 5 steps |

---

# ITEM A — the fresh qualification window (blocks Part 9)

**New since the head you approved (`c911e73`).** This changes what `ready` means,
so it changes the gate you signed off on. That is exactly why it is in front of
you rather than deployed.

Full detail: `docs/reviews/2026-08-01-qualification-window-addendum.md`.

## The problem

Your §7.2 requires *"seven calendar days of evidence from the corrected build"*
and §4 states old evidence is void with no reuse. Going to satisfy §7.1, I found
**neither was implementable**. "Old evidence is void" was a policy with no
mechanism.

`get_hdencode_shadow_summary()` aggregated every row ever recorded. No
`window_id`, no `window_started_at`, no reset marker anywhere in the schema.

Live table: **206 rows from 2026-07-22**, still accumulating, carrying **101
relevant misses**. Deploying the corrected build would therefore have been wrong
in both directions at once — the 7-day and 20-cycle criteria reading as already
satisfied by evidence the corrected build never produced, while 101 void misses
blocked the gate permanently. A window that looked nearly complete and could
never close.

## Demonstrated against a read-only copy of production

```
BEFORE (no window)                    AFTER (window starts now)
  successful_cycles = 181               successful_cycles = 0
  observed_days     = 10.63             observed_days     = 0.0
  relevant_misses   = 101               relevant_misses   = 0
  reasons: relevant_misses_detected     reasons: insufficient_comparison_cycles
           qualification_window_                  insufficient_observation_days
           not_started                            request_reduction_not_proven
                                                  restart_or_catchup_recovery_
                                                  not_proven
```

## The three things I want you to attack

1. **An unset window BLOCKS** (`qualification_window_not_started`) rather than
   falling back to counting everything. I believe absent scoping must fail
   closed, because falling back is precisely how a fresh build would inherit a
   satisfied 7-day criterion. But this makes an unconfigured deployment report
   not-ready by default, which is a behaviour change beyond the narrow fix.

2. **Scoping by timestamp, not by window identity.** I chose a config timestamp
   filtering `completed_at >= ?` over stamping a `window_id` on each row. It is
   smaller and keeps old rows intact, but it means a window has no identity of
   its own — you cannot ask "which window did this cycle belong to", only
   "is it after the line". If you expect multiple sequential windows, the
   stamped-identity design is probably right and I chose wrong.

3. **Three existing tests changed.** Two asserted `ready is True` with no
   window and were encoding the old count-everything semantics; I scoped each
   to a window covering its own synthetic cycles rather than weakening the new
   check. The third is `test_config`'s `EXPECTED_DEFAULT_KEYS` allowlist — a
   change-detector working as designed. I am flagging this rather than
   footnoting it: *"the fix broke tests so I changed the tests"* deserves
   scrutiny.

## Verification

11 new tests, including that a miss **inside** the window still counts (scoping
must not become a way to launder misses), that a scoped window still enforces
every other criterion, that old rows are retained, and the live production shape
reproduced end to end.

Full suite on this branch: **4403 passed, 4 skipped, 0 failed.**

## Runbook consequence

Starting the window is now an **explicit act** — someone sets a timestamp. A
deploy alone does not begin it and nothing will remind anyone. That is the
intended fail-closed behaviour, but it is a new way to stall silently and
belongs in the Part 9 runbook as its own step.

---

# ITEM B — rename safety gate, steps 1 and 2 of 5 (blocks nothing)

**This is early work and I am not asking you to bless the design as finished.**
Two of five gate steps exist. Nothing is wired into the apply path, auto-rename
remains off, and no file operation behaves differently than before. The useful
question is whether the *foundations* are right before the remaining three steps
are built on them.

Context: this codebase has two reproduced data-loss defects behind it (SH-R02
TOCTOU placement, SH-R03 trash-manifest loss). Both are fixed and merged. The
five-step gate exists so real-file renaming can resume safely.

## Step 1 — bounded failure classification (`backend/rename/failure.py`)

Every apply failure currently collapses into one free-text `error_message`
column. You cannot count it, alert on it, or answer the only question a
file-safety gate asks: **what is the state of the files on disk now?**

So each failure is classified on two axes — a bounded `Cause` for counting, and
a `DiskOutcome` for deciding what is safe to do next:

```
NO_OP                   source intact, destination untouched
DEST_PARTIAL            a partial destination may exist, needs cleanup
MOVED_UNRECORDED        the file was placed, the record was not — they disagree
PRIOR_OCCUPANT_TRASHED  an overwrite trashed the previous occupant and the
                        incoming file then failed to land
UNKNOWN                 we cannot tell
```

**Only a proven `NO_OP` is retry-safe.** An unrecognised failure classifies as
`UNKNOWN` and is treated as the worst case — assuming an unclassified error
changed nothing is how a silent loss gets retried into a louder one.
`PRIOR_OCCUPANT_TRASHED` outranks the cause, including causes that would
otherwise be clean no-ops: a `FileExistsError` looks harmless in isolation while
the library's own file has already been moved to trash.

Buckets derived from the real error surface in `fileops.py`/`service.py`, not
invented.

## Step 2 — append-only operation ledger (`backend/rename/ledger.py`)

`rename_jobs` records a job's **current state**, which is not a history. When a
process dies mid-move the row just sits in its last status; nothing says a file
was being moved from A to B at that moment. The one window where disk and
database can silently disagree leaves no trace.

Two rules close it:

1. **Intent is recorded and committed BEFORE the filesystem is touched, and the
   write must succeed.** If it cannot be persisted, the operation does not run.
   Deliberately not best-effort — SH-R03 is the precedent: a "degraded" manifest
   write was in fact permanent, unrecoverable loss, because restore hard-refuses
   entries with no record.
2. **Rows are never updated.** An outcome is a second row referencing the
   intent's uuid, so a bug in the outcome path cannot destroy the record of
   intent.

An intent with no outcome **is** an interrupted operation, and
`interrupted_operations()` returns exactly those.

## What I actually want from you on Item B

* Are these the right two foundations, or is there a third primitive that
  should exist before fault injection and copy-only rehearsal are built on top?
* `DiskOutcome` has five members. Is there a real on-disk state they fail to
  distinguish? This is the axis everything else keys off, so a missing state
  here is expensive later.
* The ledger commits intent separately from the file operation, so a crash
  between the two leaves an intent with no action taken — indistinguishable from
  a crash mid-move. Both read as "interrupted", which I believe is correct
  (both need a human to look), but it does mean the ledger cannot tell you
  whether anything actually happened. Is that acceptable, or does the design
  need a post-commit marker?
* No `SCHEMA_VERSION` bump: table creation runs unconditionally and
  `user_version` is only stamped at the end, so the table is created regardless.
  Bumping would have two branches both claiming v9. Sound, or should the bump
  be forced?

  Verified rather than argued, against a copy of the live production database:

  ```
  BEFORE:  user_version 8 · fileop_events absent
           rename_jobs 158 · plex_cache 21484 · shadow_cycles 206 · downloads 404
  AFTER opening with this branch's code:
           user_version 8 · fileop_events PRESENT
           rename_jobs 158 · plex_cache 21484 · shadow_cycles 206 · downloads 404
           zero row loss · integrity_check ok
  ```

  So the table does appear on an existing v8 database without a bump, and the
  addition is non-destructive on real data. What I am unsure about is whether
  leaving `user_version` at 8 while the schema has genuinely changed is
  acceptable bookkeeping or a trap for whoever migrates next.

Remaining: fault injection, copy-only rehearsal, then one sacrificial
backed-up file.

**Verification.** 45 new tests. Full suite on this branch: **4243 passed, 4
skipped, 0 failed.** (The two branches report different totals — 4243 here,
4403 for Item A — because they are independent branches off `main` carrying
different test additions, not because either is missing tests.)

---

## Standing state, unchanged

Nothing deployed. `auto_rename_enabled`, `auto_grab_enabled` and
`hdencode_rss_auto_grab_enabled` all verified `false` against the live config.
Part 9 remains Jesse's gate and I am not asking for it. The readiness
cross-check now succeeds against production (HTTP 200) — the first time it ever
has.

Open P1s from your consolidated review, both still open: no CI on the agent
branches, and `expected_typical` still unwired so volume-anomaly detection
remains off and unclaimed.
