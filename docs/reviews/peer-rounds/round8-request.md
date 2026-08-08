# ScanHound — Round 8 review request

**Branch:** `agent/listing-membership-authority`
**Base:** `main@e60db6015c97fc5bcd3fd1ca53511bc086471f89`
**Previous head you reviewed:** `d3b0e9190063bf355df0f3fa3a90632cb7b0a907` (REQUEST CHANGES)

Your round-7 closure list, item by item, plus two defects I found while verifying it
that you did not raise, plus a false claim of mine that your MEDIUM 5 corrected.

Read the code, not this summary. Where I describe a fix, the question I want answered
is whether the fix is *wired to the consumer that matters*, because that — not the
logic itself — is what has failed in six consecutive rounds.

---

## First: you corrected a false factual claim of mine, and I want that on the record

You wrote that my comment claiming direct file hosts "already bypass `scrape_links`
entirely" is not true. **You are right, and it is worse than a stray comment.** I
asserted it twice — in the `fa56c39` commit message and in the round-7 package — and
used it to justify that the affirmative classifier was safe for dispatch. The actual
flow is `download_item()` → `scrape_links()` first, and only `if not links:` does the
`links = [url]` direct-host fallback run.

So the consequence I had argued was impossible was live: a pasted Rapidgator URL went
through HDEncode reveal-page logic, clicked for a control that cannot exist on it,
then reported `layout_changed` or a reveal stall — **attributing the failure to
HDEncode's source health**, and on the throttle path putting the whole source into
cooldown, because of a URL that has nothing to do with HDEncode.

I have no defence for this one. I did not read the caller.

---

## 1. HIGH — reset all authority state at `run_scan()` entry

**Fixed** in `backend/scanner_service.py`. The reset now sits at run entry beside
`items` / `_last_crawl_seen_urls` / `_last_crawl_request_count`, and the outer handler
sets **both** `_last_crawl_status` and `_last_crawl_termination` to `scan_error`.

### I found a second, likelier path to this defect that you did not name

Your counterexample was a pre-crawl **exception**. But `_run_scan_async` also
**returns normally** before the crawl, at `scanner_service.py:512`:

```python
if not sources:
    if (... and not source_enabled(self.config, "hdencode_enabled", ...)):
        self._log("HDEncode is disabled in Settings; no requests were made.", "warning")
    else:
        self._log("No sources selected!", "error")
    return
```

No exception, no error path — an ordinary configuration state. A user unticking a
checkbox in Settings could publish the previous run's `complete` authority over an
emptied seen-set, i.e. mass false acquisition. That path is more likely to be hit in
normal use than the exception one.

Both are covered by resetting at entry, and both have tests.

**Attack this:** is there any *other* consumer that reads crawl authority and could
observe the window between the two resets, or any path that sets termination without
setting status (or vice versa)? I have made the "set one of a pair" mistake once here
already.

## 2. HIGH — listing authority required in the readiness window

**Fixed** in `get_hdencode_shadow_summary()`:

```sql
WHERE outcome IN ('success','relevant_miss')
  AND normal_feeds_complete=1
  AND (listing_complete IS NULL OR listing_complete=1)
  AND rss_requests>0
  AND listing_requests>0
```

Legacy compatibility is **explicit as you required**: NULL is admitted because those
cycles predate the column and are governed by the aggregate rule everywhere else; a
cycle recorded since the column exists must be an explicit `1`.

**Attack this:** the resolver validates `listing_complete` by identity against
`(0,1,True,False)` precisely because the column is an unconstrained INTEGER and
`bool(2)` is True. This SQL predicate does **not** — a stored `2` or `'garbage'`
fails `=1` and is therefore excluded, which is the safe direction, but a stored
`'1'` (text) would also be excluded and would silently shrink the window while the
resolver accepted it. Is that divergence acceptable, or should the two agree?

## 3. HIGH — DownloadService identity is config-aware

**Fixed.** One helper, `DownloadService._source_kind_of()`, reads `base_url` once and
is used at all three sites (coordinator, off-switch/dispatch, health ownership). No
production call site passes a URL alone any more.

The test asserts the property that actually broke — **agreement between the two
production classifiers** — rather than pinning each side to a literal, since pinning
each side separately is the shape that let them drift apart.

## 4. MEDIUM — exhaustive five-kind dispatch, no HDEncode default

**Fixed.** `scrape_links()` now routes all five kinds, and the two new ones are
handled **before the browser starts**, since neither has a source page to read:

| kind | route |
|---|---|
| `hdencode` | HDEncode scraper (was the `default:` branch) |
| `ddlbase` | DDLBase |
| `adithd` | Adit-HD |
| `direct_file` | no scrape; new `DIRECT_LINK_NO_SOURCE_PAGE` diagnostic, `affects_source_health=False`, `transport_attempted=False` |
| `other` | new `UNSUPPORTED_SOURCE` diagnostic |

There is deliberately **no `else`**: an unhandled kind raises `AssertionError` rather
than reaching the HDEncode implementation, which is how `other` came to mean
`hdencode` in the first place.

`direct_file` returns **no links on purpose** so that `download_item`'s own
supported-host fallback hands the URL to the downloader (it clears the diagnostic when
it does). The diagnostic only survives for a direct host we identify but cannot hand
off — a real outcome that was previously mislabelled as an HDEncode failure.

**Attack this:** is returning an empty result with a non-`None` diagnostic the right
contract for a non-failure, given `download_item` distinguishes them only by
`if not links:`? An alternative is a dedicated success-with-passthrough result. I chose
the smaller change; tell me if it is the wrong call.

## 5. MEDIUM — unresolved listing-only candidates

**Fixed.** Candidacy is now `detail_failed ∩ listing_only`, tracked in a dict keyed by
URL across cycles in `completed_at` order, and clearable.

The asymmetry is deliberate and I want it attacked specifically: **creating** a
candidate blocks, so it is conservative and an untrusted cycle may still raise one.
**Clearing** is permissive, so a cycle whose membership is contradicted
(`listing_complete=False`) must not clear anything — otherwise a cycle the resolver
refuses to trust becomes the thing that unblocks readiness, which is HIGH 2's
fail-open shape one layer down. Legacy NULL *is* allowed to clear.

`unattributed_candidate_urls` is now returned alongside the count, because a bare
"3 candidates" cannot be investigated.

**Attack this:** clearing requires the URL to appear in a later cycle's
`listing_only ∪ feed_only` and not in its `detail_failed`. A release that is
**delisted** — never appears again — therefore blocks forever. Is that correct
(unfalsifiable evidence should block) or does it need an age-out?

## 6. Discrimination tests

`tests/test_round7_discrimination.py` (20) and
`tests/test_reveal_success_resets_escalation.py` (6). **Each names the wrong answer it
detects**, and I ran the negative control you should expect me to run: both files
against unmodified `d3b0e91`.

```
test_round7_discrimination.py            post-fix 20 passed  |  pre-fix 18 failed, 2 passed
test_reveal_success_resets_escalation.py post-fix  6 passed  |  pre-fix  3 failed, 3 passed
```

In the reveal file the split is by design and is the point of the file: the three
**coordinator-behaviour** tests pass in both arms, because the method always worked —
only the wiring was missing. The three **wiring** tests fail pre-fix. That is the
distinction three existing test files failed to make, which is how a method with no
caller stayed green through six review rounds.

The 2 that pass in both arms are that way **by design**, and I want you to check that
claim rather than take it:
- `test_legacy_null_listing_complete_still_counts` — a regression guard proving the
  HIGH 2 fix did not invalidate the entire historical window.
- `test_candidate_blocking_still_reaches_readiness` — a consumer-wiring guard, because
  "the field changed shape and nothing read it" is the most repeated defect here.

**One test I had to strengthen after the control run.**
`test_a_contradicted_cycle_cannot_clear_a_candidate` originally used one candidate and
asserted `== 1`, and **passed against the pre-fix code** — because code that never
clears anything cannot clear wrongly. It got the right answer by accident. Rebuilt with
two candidates, one cleared by a trusted cycle and one a contradicted cycle merely
attempts to clear, so the expected count differs from every wrong implementation:

```
correct                      -> 1
pre-fix (sums, never clears) -> 2
clears on contradiction too  -> 0
```

---

## Two more things my own tests caught, which I want checked too

**The reset had TWO success paths and I wired one.** `scrape_links` returns early when
file-host links are already visible on the page, with no reveal at all. I put
`observe_reveal_success()` on the post-click branch only. The wiring test came back
with the links present and the coordinator never called — a diff read would have let
me report it wired. Both paths now reset, on the rule "HDEncode served links" rather
than "the reveal control worked", because a page needing no reveal is still a page
HDEncode is not throttling and leaving the streak inflated there half-fixes the
ratchet. **Attack:** is "served links" the right predicate, or should an
already-visible page be treated as no evidence about the reveal control at all?

**My own positive control could pass on a coin flip.** `observe_reveal_stall` applies
±10% jitter. I asserted `second > first` as the escalation control *with the jitter
live* — which a completely flat curve would still satisfy roughly half the time. The
production signature already accepts an injectable `rng`, so escalation is now pinned
exactly at `3600 → 7200 → 14400` and drops back to `3600` after a success. Worth
naming because it is the same failure as the stubbed-collaborator control: a control
that can pass by accident is not a control.

**And a bad test the dispatch fix exposed.** `TestScrapeLinksHDEncode` scraped
`hdencode.com` — a host that appears **nowhere** in production code — reaching the
HDEncode scraper only through the default fall-through you told me to remove. Two of
its three cases did not even fail when it went away, because they assert
`result == []` and the new `unsupported_source` outcome is also empty: they had been
passing for a different reason than the one they were written for. Only the case
asserting links are *found* broke, which is the only reason the wrong host surfaced.
Corrected to `hdencode.org`.

## A defect the new tests found that no review round did

`test_every_scrape_code_has_a_message_and_a_failure_title` failed immediately — not on
my two new codes, but on **`REVEAL_VERIFICATION_STALLED`**, which I added earlier this
session *specifically so a source throttle would stop being reported as a broken
scraper*, and then left out of `_FAILURE_TITLES`. `.get(reason, "Download Failed")`
rendered it as **"Download Failed"** above a message reading "nothing is wrong with
this release."

That is the exact code behind the 45 items currently parked in cooldown. The fix I
shipped was undone in the UI by an omission in a hand-maintained map that nothing
asserted was complete. Titled `"HDEncode is throttling"`, and the exhaustiveness test
now covers both maps.

---

## Suite

Whole-tree, in a clean container from `scanhound:latest` with `pytest`,
`pytest-asyncio` and `httpx<0.28` installed (the image ships none of them), and with
the **same command run against unmodified `d3b0e91`** in the same session, because a
count without a baseline is not evidence.

```
baseline  d3b0e91  : 4536 passed, 4 skipped, 0 failed   (matches your exact-head CI)
round-8 head       : 4562 passed, 4 skipped, 0 failed
delta              : +26, reconciling exactly against the two new files (20 + 6)
```

The baseline matching your independently-run CI at 4536/4 is the part that makes the
4562 mean anything. I have published a wrong baseline before -- "3 standing
environment failures on main" that turned out to be a property of my container, not of
the codebase -- so the control is run in the same session, in the same image, with the
same command, every time now.

## What I am asking of round 8

1. Are the six fixes wired to the consumers that matter, or have I again built the
   right thing and attached it to nothing? Note that **two of this round's own
   findings were exactly that failure** — HIGH 3 was a parameter nothing passed, and
   the reveal reset was a method nothing called — so treat "is it reached from
   production?" as the primary question for every item, not a formality.
2. The four explicit attacks above — text affinity in the SQL predicate, the
   empty-result-plus-diagnostic contract, the delisted-release blocking forever, and
   whether any authority consumer can observe a partial reset.
3. Are the 2 both-arms-passing tests genuinely regression/wiring guards, or weak
   assertions I have rationalised?
4. Anything still unwired that I have not listed — see below for the one I consider
   most serious.

---

## Still unwired, stated precisely (this is the 5th instance of the same pattern)

**`observe_reveal_success()` has no production caller.** Verified: defined at
`hdencode_coordinator.py:558`, referenced only from three test files and from comments
in `download_queue.py` noting its absence. Its own docstring names the consequence:

> Without this the streak would ratchet up forever and every later stall would draw
> the maximum cooldown regardless of how healthy the source had been in between.

**Scoped accurately, because I would otherwise overstate it.** `_reveal_stall_streak`
is initialised to `0` in `__init__` (line 154), incremented at 531–532, reset only at
566, and read at 575. There is **no persistence and no restore path**. So the streak
ratchets to the escalation ceiling and stays there **for the container's lifetime**,
and a container restart is the only thing that clears it. Not permanent — but the
reset mechanism is an accident of process lifetime rather than evidence of source
health, which is the wrong design for a throttle dial.

I could not read the live streak to quantify it: the in-memory value belongs to the
uvicorn process, `docker exec python -c` constructs a *different* coordinator object
(I printed zeros from one before noticing), and the real `/sources` route returns 401.
I did not go looking for credentials to satisfy a diagnostic.

Two smaller ones: `transport_attempted` is written unconditionally by `_complete`, and
per-host identity (`filehost:<domain>`) remains deliberately unimplemented and
documented as such in `source_identity.py`.
