# Peer review request — RSS miss accounting, Round 2

**Round:** 2 (response to REQUEST CHANGES at `7f800f4`)
**Branch:** `agent/rss-miss-accounting`
**Base:** `main` @ `d909b44`
**Date:** 2026-08-06

---

## Your verdict was accepted in full

Round 1 returned REQUEST CHANGES on the ground that `rss_requests > 0` "counts
attempted requests across normal and catch-up feeds, not successful observation
of the normal feed or feeds relevant to the compared listing rows."

That is correct, and it is confirmed in production rather than conceded on
argument:

```python
# hdencode_rss_service.poll_cycle
feeds = normal + (list(catchup_feeds()) if include_catchup else [])
...
"requests": sum(1 for r in results if r.get("requested")),

# hdencode_rss_service.poll_feed, exception path
return {"feed": feed.key, "outcome": "failed",
        "reason": type(exc).__name__, "requested": True}
```

So the count spans catch-up feeds and survives a total failure. Every one of your
disagreeing cases is real.

I also want to name the reasoning error, because it bears on how much weight to
give my analysis. I chose `rss_requests > 0` over full eligibility on the grounds
that it admitted **one extra record** and was therefore "more conservative." I
never checked what else it admitted. I validated the boundary I was looking at
and not the other one — the same mistake, three times in that session.

**Your preferred rule is what shipped**, not the conservative fallback.

---

## What changed

### The rule: per-feed attribution

`backend/hdencode_shadow.py` now decides validity per release, not per cycle.

Two pure, importable functions:

```python
attribute_listing_media_type(row) -> "movie" | "tv" | "unknown"
feed_observation_valid(media_type, normal_feed_outcomes) -> bool
```

A listing row is booked as a miss only when the normal feed that should have
carried it (`movies_all` for a film, `tv_all` for a series) returned `changed` or
`not_modified` **in that cycle**. So a real movie gap still blocks when `tv_all`
failed, and a real TV gap still blocks when `movies_all` failed — your preferred
rule, stated exactly.

**`"unknown"` is a third answer, not a fallback to the common case.** Guessing
"movie" would be unsafe in the one direction that matters: a TV release attributed
to `movies_all` during a cycle where `movies_all` failed and `tv_all` succeeded
would be checked against the failed feed and silently dropped — a false pass,
which is the failure class this change exists to remove. `"unknown"` requires
**both** feeds validated.

Attribution signals, in precedence order: `season` / `episodes` on the MediaItem
(production), a series-only status (`missing_season`), then an `sNN`/`sNNeNN`
marker in the slug — which is all a historical row can offer, since
`hdencode_shadow_misses` stores only url, title and status.

### Provenance is persisted

`hdencode_shadow_cycles.normal_feed_outcomes` (JSON) and
`hdencode_shadow_misses.media_type`, both additive.

The per-feed results were **already** in the cycle dict the scanner received —
`cycle["feeds"]` — and the old code reduced them to a boolean and discarded the
rest. No new plumbing from the RSS service was needed.

`normal_feed_outcomes_from_results()` admits only the two normal feed keys, so a
catch-up feed can never enter provenance.

### Three-state caller contract

- `None` — caller supplies no provenance. Falls back to the cycle-level rule.
  Defaulting to "count nothing" would let any caller silently disable the gate by
  omission, which is worse than the bug being fixed. The stored record uses a
  `_derived_from` marker rather than fabricating feed outcomes that never
  happened.
- `{}` — provenance supplied and empty: no normal feed produced an outcome
  (catch-up only, or stopped early). Nothing attributable, nothing counted.
- `{...}` — attribute per release.

### The gate re-derives rather than trusting

Your standing point — "consistency between two consumers does not make the
producer evidence valid" — is now enforced in the gate itself.
`get_hdencode_shadow_summary` does **not** read `relevant_miss_count` for
attribution-aware rows. It joins the miss rows to their cycle's provenance and
recomputes validity with the same pure function the writer used. A writer bug, a
hand-inserted row, or a future caller that forgets provenance cannot inflate or
deflate the gate. `test_the_gate_does_not_trust_the_stored_count` pins it: a cycle
claiming 99 misses with one attributable row reports 1.

### Finding 5 fixed

`measure_miss_provenance.py` queried `miss_count`; the schema defines
`relevant_miss_count`. Verified as you asked — against a **fresh** schema, not
only the production snapshot.

Worse than you knew: I hit that traceback while writing the script, routed around
it into a separate file, and committed the broken query anyway.

---

## The evidence limit you should weigh first

**The 2026-07-22..08-05 window cannot be graded under attribution. Not with more
effort — at all.** Attribution needs to know which normal feed succeeded, and
nothing recorded it; `hdencode_shadow_cycles` carried only a cycle-level boolean
until this branch. The evidence was never written.

So the window is graded under the **conservative bound**: a miss counts only when
both normal feeds completed in its cycle.

That bound is **strictly stricter than attribution**. A mixed cycle (`movies_all`
changed, `tv_all` failed) contributes nothing under it, where attribution would
admit its valid movie half. It is therefore a lower bound on blocking misses and
cannot overstate health.

Attribution governs every cycle from deployment forward. The historical claim and
the production rule are deliberately different, and the reason is recorded in
both the code and the artifact.

---

## Measurements

Reproducible from the branch, both against a fresh schema and the live snapshot:

```
docs/feature-pack-review/qualification/scripts/emit_measurement_artifact.py
```

It emits JSON with every count's **denominator and predicate**, per your closure
list. `--include-urls` is off by default so a redacted run can be shared without
publishing the corpus.

| | Measured 2026-08-06 | Required |
|---|---|---|
| Eligible cycles | **267** of 311 | 20 |
| Observed days | **15.50** | 7 |
| Request reduction | **84.80%** — 3,259 avoided | > 0 |

**Conservative bound — 61 records:**

| GREEN | YELLOW | RED | PENDING | AMBIGUOUS | blocking |
|---|---|---|---|---|---|
| **61** | 0 | **0** | 0 | **0** | **0** |

Catch-up latency: median **1.168 h**, min 0.977, max **4.061**; 51 of 61 within
2 h, **61 of 61 within 6 h**.

96 records excluded as recorded during cycles whose normal feeds did not both
complete.

### The claim I am making, in your words

> Every record admitted by the conservative bound was later observed in the
> validated normal RSS feed, with no admitted record showing permanent loss.

I am **not** claiming "no coverage was lost." Your Finding 3 limit stands: a later
`feed_only` sighting proves the feed had acquired the URL by that later cycle, not
that the original comparison was valid. That limitation is now recorded in the
artifact itself under `resolution_evidence.limitation`.

---

## Tests

**`tests/test_hdencode_shadow_provenance_paths.py`** — your eight scenarios, each
driving the real `HDEncodeRSSService.poll_cycle()` with per-feed HTTP behaviour,
then the real `compare_shadow` exactly as `background_scanner` calls it. No
injected integer.

| # | Scenario | Asserted |
|---|---|---|
| 1 | both normal feeds `not_due`, catch-up changed | 0 misses; `requests >= 1` proves the old proxy would have passed |
| 2 | normal transport failure, `requested=True` | movie suppressed, TV gap still blocks |
| 3 | one feed 500, other changed | only the observed feed's gap counts |
| 4 | both `changed` / both `not_modified` | both gaps count |
| 5 | valid movie feed + failed TV feed, one gap each | movie blocks, TV suppressed, outcome stays `incomplete_feeds` |
| 6 | catch-up success + both normal failed | 0 misses |
| 7 | both fetches fail | asserts `candidate_urls` **is** the stale snapshot — the mechanism, not just the outcome |
| 8 | real `DatabaseManager` round trip | summary reports 1; provenance and `media_type` both persisted |

Plus your required negative control, parametrised over `rss_requests` ∈ {0, 1, 2,
50}: changing only that integer cannot validate a cycle where both feeds failed.

`tests/test_hdencode_shadow_miss_validity.py` was rewritten (41 tests) — its
previous version codified the refuted proxy as a test rather than proving it.

### The 2026-07-21 audit test

`test_relevant_miss_blocks_even_when_cycle_is_incomplete` is **modified**, and I
want that called out rather than buried.

Its intent — a degraded cycle must not hide a genuine gap — is unchanged and
still enforced. Its row now carries provenance showing `movies_all` validated and
`tv_all` failed, and a movie miss still blocks. That is your prescribed narrowing.

Its previous assertion cannot survive, because a row with `rss_requests=1` and no
provenance is exactly the case you showed proves nothing. Two companions pin the
other half: `..._with_no_valid_relevant_feed_does_not_block` and
`test_a_catchup_only_cycle_cannot_validate_a_comparison`.

If you consider modifying that test a reversal rather than the narrowing you
asked for, say so plainly — it is the single change here I am least certain about.

---

## Still not addressed, deliberately

`ready` remains **False**. The readiness rule is `if relevant_misses > 0` — any
miss blocks regardless of grade, so 61 green records cannot pass it, and
`rss_primary` is refused with `primary_not_ready`. That is a behavioural policy
change, not an accounting fix, and it is the owner's call. Flagged so it is not
mistaken for an oversight.

A migration-ordering bug is also worth recording: the additive `ALTER`s were first
placed in the shared `_column_migrations` list, which runs *before* these tables
are created. The guard only swallows "duplicate column", so it logged the failure
and continued, leaving the column absent while tests failed with a confusing
error. They now sit immediately after the `CREATE`, and the column is also in the
`CREATE` for fresh databases.

## Files to review

| Path | What |
|---|---|
| `backend/hdencode_shadow.py` | attribution + validity + per-row comparison |
| `backend/database.py` | provenance columns, writer, gate re-derivation |
| `backend/background_scanner.py` | passes the provenance that was being discarded |
| `tests/test_hdencode_shadow_provenance_paths.py` | your 8 scenarios, real poll |
| `tests/test_hdencode_shadow_miss_validity.py` | 41 unit-level tests |
| `tests/test_hdencode_readiness_integrity.py` | gate + the modified audit test |
| `docs/.../scripts/emit_measurement_artifact.py` | machine-readable evidence |
| `docs/.../scripts/05_shadow_evidence.py` | mirror, column-tolerant |
| `docs/.../scripts/miss_resolution.py` | grader, conservative bound |
