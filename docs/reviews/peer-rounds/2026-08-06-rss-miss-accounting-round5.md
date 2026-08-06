# Peer review request — RSS miss accounting, Round 5

**Round:** 5 (response to REQUEST CHANGES at `15c47a3`)
**Branch:** `agent/rss-miss-accounting`
**Base:** `main` @ `d909b44`
**Date:** 2026-08-06

---

## The HIGH is closed, and it was the same hole one branch over

Your construction was exact. Round 4 closed the fail-open on ordinary per-feed
provenance and left the identical defect in the `_derived_from` path:

```
total rows       = 1
stored count     = 1
supported rows   = 0
unsupported rows = 1
integrity        = []          → reports zero misses, no failure
```

Because reconciliation compared stored against **total** rather than
**supported**, it stayed silent. Worse, the branch carried a comment claiming
*"Counted as unsupported so the reconciliation below sees it."* It never looked at
that bucket. My comment described a mechanism that does not exist.

Now reports `miss_row_unsupported_by_derived_completeness` and blocks.

## Marker schema is now exact

You were right that "shape validated" overstated it. `bool("false")` is `True`,
so a string marker could pass the consistency check and reach the silent path.
Required now:

```
keys == {"_derived_from", "normal_feeds_complete"}
_derived_from == "cycle_level_completeness"
isinstance(normal_feeds_complete, bool)
marker completeness == cycle completeness
```

New findings: `derived_marker_schema`, `derived_marker_not_a_boolean`.

## The backstop you asked for

Every nonzero `unsupported`/`corrupt` bucket must now produce at least one
integrity finding, else the gate blocks with `unreported_unsupported_rows` naming
the cycle.

I want to be plain about why this matters more than the individual fix. **Twice a
branch incremented a diagnostic bucket and fell off the end, and both times one of
my own tests certified the silence as correct.** Your suggestion converts that
from something I have to remember into something the code enforces. It is the
most valuable item in the Round 4 delta.

## Tests

Ten added: the five cases you enumerated — derived-incomplete with a row, missing
`normal_feeds_complete`, extra keys, pseudo-booleans (parametrised over `"false"`,
`"true"`, `0`, `1`, `""`, `"0"`), derived-incomplete with stored zero — plus a
positive control on the legitimate complete-derived shape and a backstop test.

One earlier test needed adjusting rather than the code: it supplied
`{"_derived_from": "something_invented"}` with no completeness key, so the new
exact-schema check fires first. It now supplies the full schema with a bad value,
isolating the condition it names.

## Evidence wording narrowed (F5)

I called those 22 rows "exactly the false-pass shape." You were right that this
overstates it — the claim also needs no episodes evidence, no series-only status,
and no season token in the URL, none of which the script measures per row. The
script now records what the counts do and do not establish, and names the
overstatement so it cannot be quoted back as fact.

## Verification

- Full suite: **3 failed, 4358 passed, 4 skipped** (696 s).
- Baseline `main` @ `d909b44`: the **same 3** fail — no frontend build output, no
  selenium, no notification backend.
- `test_hdencode_readiness_integrity.py` alone: **40 passed**.

**On CI (item 6):** I cannot assert this. The Round 4 runs were cancelled before
exposing test steps, and you explicitly forbid reusing the earlier
`Service Unavailable` diagnosis. This push should trigger a fresh run — please
confirm from the run itself, not from this document.

## Optional items not done, listed rather than skipped

- a committed `replay_measurement.py` verifier
- describing URL hashes as pseudonymous/linkable rather than redacted
- `listing_media_type` as a data-model follow-up

Say if any should be in scope for round 6 rather than deferred.

## What I would most like attacked

1. **Is the backstop's finding-matching sound?** It checks whether any existing
   finding mentions the cycle. That is a string match, and a cycle id appearing
   incidentally in another finding could mask a genuinely unreported bucket. A
   structural association between bucket and finding would be stronger, and I
   chose the simpler form.
2. **Does the exact-key schema break any legitimate writer?** `compare_shadow` is
   the only producer today and emits exactly those two keys, but a future caller
   adding a third field would now be classified corrupt rather than accepted.
   That is deliberate fail-closed behaviour; confirm it is the behaviour you want.
3. **Is `derived_marker_not_a_boolean` reachable in practice**, given
   `json.dumps` of a Python bool always yields a JSON boolean? It guards against
   hand-edited or externally-written rows, which may be the only realistic source.
