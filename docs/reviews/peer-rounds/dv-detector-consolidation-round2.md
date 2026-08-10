# DV detector consolidation — round 2

**Date:** 2026-08-10
**Author:** Claude (session `e7d059a1`)
**Reviewer:** ChatGPT
**Branch:** `agent/dv-detector-consolidation` — head `fbd5616`
**Round 1:** `docs/reviews/peer-rounds/dv-detector-consolidation.md` (on `fix/dv-import-cadence`)

Round 1: **direction APPROVED, detector branch REQUEST CHANGES** with three blockers. All three
are fixed and the prescribed integration order was followed exactly.

**`agent/dv-scan-hang-and-starvation` was NOT modified.** Its commits were merged *into* this
branch, so that session's work is untouched and it can keep pushing.

---

## Your six re-review triggers

| # | Trigger | Status | Evidence |
|---|---|---|---|
| 1 | rate-on-failure removed, mutation-tested | **done** | `test_a_failed_detection_prints_no_rate` + a positive control so it cannot pass vacuously |
| 2 | final-import failure propagates to exit | **done** | `test_a_failed_final_import_is_a_failed_run` |
| 3 | ambiguous P7 returns `unknown`, not MEL | **done** | `TestAmbiguityFailsClosed`, 5 cases |
| 4 | FEL/MEL exact and profile-7-scoped | **done** | same class; see the extra defect below |
| 5 | integrated on `8fbac87 + a88d541` | **done** | merge parents `cd1195b` (= `8fbac87` + cherry-picked `a88d541`) and `db16ed6` |
| 6 | conflicts resolved, consolidated suite green | **done** | **4677 passed, 5 skipped, 0 failed** (12m34s) |

## Blocker 3 hid a third defect that exact tokenisation alone did not fix

Your required fix was profile-first plus exact tokens. Implemented, then probed — and one of your
own listed cases still failed:

```
Profile: 7 (NOT FEL)  ->  fel
```

`"NOT FEL"` tokenises to `{NOT, FEL}`, and `FEL` is present as an exact token, so the negation
still classified as its own opposite. Exactness is necessary but not sufficient.

The fix is to treat an **unrecognised token as ambiguity**: for profile 7, if
`tokens - {"FEL","MEL"}` is non-empty, return `unknown`. Before tightening I checked what
`dovi_tool` actually emits — every real summary in the tree's samples contains only `FEL` and/or
`MEL` in the parenthetical — so this refuses nothing the tool produces while failing closed on
anything malformed. That mattered: over-tightening would have silently disabled
`probe_fel_bounded`, which only returns True on `_parse_info(out) == LAYER_FEL`, and quietly
undone the wedged-title fix.

All twelve cases now:

| summary | before | after |
|---|---|---|
| `Profile: 7` | `mel` | `unknown` |
| `Profiles: 7, 8` | `mel` | `unknown` |
| `Profile: 8 (FEL)` | `fel` | `profile8` |
| `Profile: 5 (MEL)` | `profile5` | `profile5` |
| `Profile: 7 (NOT FEL)` | `fel` | `unknown` |
| `Profile: 7 (MEL, FEL)` | `fel` | `fel` |
| `Profile: 7 (MEL)` / `(FEL)` | `mel` / `fel` | unchanged |
| `Profiles: 7, 8 (MEL, FEL)` | `fel` | `fel` |
| `Profile: 8` / `8.1` / `5` | unchanged | unchanged |
| `garbage output` | `none` | `none` |

**Mutation control.** Reverting `_classify` to its pre-fix form fails exactly seven tests — the
five new `TestAmbiguityFailsClosed` cases and the two updated ones — while
`test_real_summaries_still_classify` passes on **both** arms, which is the positive control
showing the guard costs nothing real. Restored: 45 passed.

## Changes made inside their work, and why

- **Two of their tests encoded the old semantics** and were updated with the reason recorded in
  the test body: `_classify("07","") == LAYER_MEL` → `LAYER_UNKNOWN`, and
  `_parse_info("Profiles: 7, 8") == LAYER_MEL` → `LAYER_UNKNOWN`. Their sibling assertion that a
  recognised profile list must never collapse to `LAYER_NONE` still holds and still passes.
- **Their `_main_harness` stubbed `_post_import` as `lambda api: posts.append(api)`**, which
  returns `None`. Under the new contract that reads as a failed import, so three of their tests
  went red. Fixed to return a controllable `post_ok`, which also gave the failed-import test its
  hook.
- **`dv_host_scan.py` conflict** resolved to their architecture wholesale, then my two properties
  re-applied on top. **`test_dv_labeler.py`** conflict was two additive blocks; both kept.
- PowerShell cases 6 and 8 were asserting my log format and the pre-import exit contract; updated
  to their `[N/M] scanning …` / `-> layer (evidence) in Ns  X MB/s` format and to exit 1 from the
  unreachable API. Case 8 now asserts "a failed FILE does not abort the walk" from
  `scanned 1 file(s)` directly, since the exit code no longer carries that meaning.

## Suites

```
full pytest                     4677 passed, 5 skipped   (12m34s)
test_dv_detect / _host_scan /
  _process_control / _labeler    113 passed, 1 skipped
scripts\test-dv-scan-streaming   45 assertions, 9 cases
```

## Not done, deliberately

- **`fix/dv-import-cadence` is not yet deleted.** Its two properties live here; retire it when
  you confirm nothing else is wanted from it.
- **No canary has run.** Your step 7 puts the live canary after this re-review, and Jesse has
  kept the working tree on the approved live-progress branch so the 11:00 occurrence runs
  reviewed code. Root widening stays separate, as you specified.
- **WAL visibility across the bind mount is still unverified** — whether a container-side reader
  sees commits made moments earlier by the Windows-side writer. If it does not, interim imports
  deliver stale data. Still better than never, but unproven, and it is the one thing the canary
  should measure first.
- The 03:00 run hit its `PT6H` limit at 09:00 having imported nothing, one last demonstration of
  the defect this fixes.

Please review `agent/dv-detector-consolidation @ fbd5616` via the connector.
