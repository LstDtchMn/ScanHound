# Peer review request — RSS miss accounting

**Round:** 1 (new finding, not a continuation)
**Branch:** `agent/rss-miss-accounting`
**Base:** `main` @ `d909b44`
**Date:** 2026-08-06

---

## Read this first: what I need from you

I am asking you to attack a **favourable** conclusion that **I** produced, about a
row **I** have been trying to close for 15 days. That is the whole reason this
round exists.

Concretely, I claim the RSS shadow window's failure was an accounting artifact
and that the feature never lost coverage. If that claim is wrong, the cost is
that ScanHound silently stops noticing releases it should grab. Please try to
break it.

**Review the code and the data on this branch, not this document.** If you find
yourself reviewing my summary, stop and read the files. I have listed exact
paths below. One earlier round reviewed a summary instead of the artifacts and
reached a conclusion neither of us could use.

**Specific things I want challenged, in priority order:**

1. **Is `rss_requests > 0` the right validity criterion?** I argue a miss claim
   requires the feed set to be from *this* cycle's fetch. Is there a case where
   `rss_requests > 0` and the comparison is still invalid? A partial fetch
   (one feed of two) is the obvious candidate — I deliberately keep those.
2. **Does this narrow your 2026-07-21 rule, or reverse it?** I claim narrow.
   See "Relationship to your prior audit" below. If it reads as a reversal to
   you, say so plainly.
3. **Is grading via `feed_only` sound?** A miss is called resolved only when its
   URL later appears in a `feed_only` set. Is that actually proof the feed
   acquired it, or could a URL enter `feed_only` some other way?
4. **Am I wrong that the 89 excluded records are artifacts?** They are real
   listing rows in real relevant states. I claim the *comparison* was invalid,
   not that the releases were imaginary. Push on that distinction.

## An error I made tonight, disclosed because it bears on my reliability here

While investigating this I told Jesse that 41 cycles showed "a 100-item feed and
a 34-item listing sharing zero URLs — structurally impossible." **That was
wrong.** The 34 was `listing_requests` (pages fetched), not items; actual
`listing_count` on those cycles is 0–8, and zero overlap is unremarkable at that
size (median overlap across the 258 good cycles is 3). I corrected it after
pulling more data. The conclusion in this document does not rest on that claim —
it rests on `rss_requests = 0` — but you should weigh my analysis accordingly.

---

## The two defects

### Defect 1 — `backend/hdencode_shadow.py`, `compare_shadow()`

```python
outcome='success' if normal_feeds_complete else 'incomplete_feeds'
if misses: outcome='relevant_miss'
```

Two independent bugs on two lines.

**1a. Misses are derived from an invalid comparison.** `misses` comes from
`listing_only = listing_urls - rss`. The caller supplies
`rss_urls=rss_cycle.get("candidate_urls")`, and that list originates in
`backend/hdencode_rss_service.py:176`:

```python
candidate_urls = self.db.list_hdencode_current_feed_urls()
```

which reads `hdencode_candidate_feeds` joined to `hdencode_feed_state`
(`backend/database.py:2004`) — the **persisted membership of the last CHANGED
feed snapshot**, out of the database. It is not this cycle's fetch. So when the
feed did not fetch, `rss` is a stale set, `listing_only` is inflated by
everything the feed had merely not collected yet, and every relevant row in it
is booked as a miss.

**1b. The invalidity label is erased.** `if misses:` runs unconditionally, so a
cycle correctly labelled `incomplete_feeds` is relabelled `relevant_miss` — by
the very misses that only exist because it was incomplete.

### Defect 2 — `backend/database.py`, `get_hdencode_shadow_summary()`

Cycle count, duration and request-reduction all filter for eligibility. The miss
sum did not:

```python
misses=self._query(
    "SELECT SUM(relevant_miss_count) AS relevant_misses "
    "FROM hdencode_shadow_cycles",   # every row
    one=True,default=None)
```

So a window could decline to count a cycle toward its own length while still
being condemned by that same cycle's misses.

The same asymmetry existed in the grader
(`docs/feature-pack-review/qualification/scripts/miss_resolution.py`): it filters
cycles for usability before trusting them as **observations**, then drew misses
with an unfiltered `JOIN`.

---

## Relationship to your prior audit — please check this closely

Commit `f5e3c6e` (2026-07-21, "Apply RSS readiness/recovery corrections (ChatGPT
adversarial audit)") added:

```python
def test_relevant_miss_blocks_even_when_cycle_is_incomplete(tmp_path):
    _insert_cycle(db, uuid="incomplete-miss", ...,
                  normal=0, rss=1, listing=1, misses=1, outcome="relevant_miss")
    assert summary["relevant_misses"] == 1
```

Your rule: **a degraded cycle must not be able to hide a real gap.** I agree, and
I am not touching it. That test is **byte-identical to `main`** on this branch and
**still passes** — verified, not assumed.

My first attempt *did* reverse it (I filtered on full eligibility, which broke
your test). I measured three candidates before choosing:

| Filter | Cycles | Misses graded | Grading | Gate |
|---|---|---|---|---|
| A — status quo (every row) | 301 | 150 | G149 Y0 R0 P0 **A1** | STOPS |
| B — `rss_requests > 0` **(chosen)** | 260 | **61** | **G61** Y0 R0 P0 A0 | passes |
| C — full eligibility | 259 | 60 | G60 Y0 R0 P0 A0 | passes |

B is **more conservative than C** and preserves your rule. The row C would drop
that B keeps:

```
2026-07-28T01:32:02  nfc=0 rss_req=2 rss_count=100 dup=0
  ...uhd-blu-ray-remux-dv-hdr-hevc-flac-2-0-cinephiles-50-2-gb
  grades RESOLVED at 1.25h
```

Keeping it costs nothing (it is green). Dropping it would have cost the
protection your audit added. **89 of the 90 contested records are zero-fetch; only
this one is a partial fetch.** That asymmetry is why B is defensible and C is not.

Reproduce with
`docs/feature-pack-review/qualification/scripts/compare_filter_options.py`.

---

## The measurements

All from the live database at run time. Reproduce with
`docs/feature-pack-review/qualification/scripts/rss_status_report.py`.

**Window** — 300 cycles recorded, 258 eligible, span 14.94 days.
Requirement is 20 cycles / 7 days, so 12.9× and 2.1×. Cadence median 71 min.

**Benefit** — 566 feed requests vs 3,805 equivalent listing requests =
**85.12% reduction**, 3,239 requests never made.

**Miss provenance** — 150 recorded. **90 from cycles with an invalid comparison,
89 of those from cycles with `rss_requests = 0`.** Of the 41 zero-fetch cycles,
all 41 report `rss_count = 100` (the stale snapshot) and `duplicate_count = 0`;
34 have `listing_only_count == listing_count`, i.e. the entire listing counted as
missing.

**Grading** (Jesse's 2026-07-24 rule: ≤6h GREEN, 6–24h YELLOW, >24h/never RED):

| Population | GREEN | YELLOW | RED | PENDING | AMBIGUOUS |
|---|---|---|---|---|---|
| All 150 | 149 | 0 | **0** | 0 | 1 |
| Fetched cycles only (61) | **61** | 0 | **0** | 0 | **0** |

Catch-up latency across all 149 resolved: **median 1.10 h, max 4.06 h**, 135 of
149 within 2 h, **149 of 149 within 6 h**.

**Resilience** — 21 restart-recovery and 8 catch-up cycles, 28 inside the
eligible set. Not an undisturbed window.

**Feed health** — `movies_all` and `tv_all` both HTTP 200, 0 consecutive
failures.

### The known limit, stated plainly

`duplicate_count` is stored as a **count, not a list**. So when a URL leaves
`listing_only` without later appearing in `feed_only`, there is no way to
distinguish "the feed acquired it" from "the listing dropped it while the feed
still lacked it". That case is graded AMBIGUOUS and blocks closure — it is not
counted as success. Under population A exactly one record sits there; it is from
a zero-fetch cycle, so it does not appear in B. **If you think that record should
still block, that is a coherent position and I want to hear it.**

---

## Verification performed

**Negative controls, each fix independently reverted:**

| Reverted | Result |
|---|---|
| `hdencode_shadow.py` only | 4 failed, 15 passed |
| `database.py` only | 3 failed, 3 passed |
| neither | **25 passed** |

**Tests added** — `tests/test_hdencode_shadow_miss_validity.py` (19) plus 3 in
`tests/test_hdencode_readiness_integrity.py`. They pin **both** directions: a
zero-fetch cycle must record nothing, and a cycle that fetched must still report,
including a parametrised case over all four states seen live (119 `missing`, 15
`upgrade`, 13 `missing_season`, 3 `dv_upgrade`).

**Gate-condition tests** —
`docs/feature-pack-review/qualification-evidence/test_gate_conditions.py`, 21
assertions over the two pure gate functions. Discrimination check: **11 of 21
fail** against the pre-change rule.

**Full suite** — this head: **12 failed, 4270 passed, 4 skipped** (597.91s).
Baseline on `main` @ `d909b44`, identical container method and whole tree:
**12 failed, 4248 passed, 4 skipped** (592.85s). The failure sets are
**identical test-for-test** (`diff` empty), so this change introduces no new
failures; the +22 are the tests added here.

**Correction to the commit message.** It states that all 12 pre-existing failures
are an artifact of the harness copying `backend/tests/docs` without `scripts/`.
That is true of **9** of them (all in `test_dv_host_scan.py`) — re-running with
`scripts/` present leaves **3 failed, 241 passed**. The remaining 3
(`test_dv_settings`, `test_notifications`, `test_source_hdencode`) are a
different environment gap: absent frontend build files, no selenium, no
notification backend in the container. Both arms show all 12 identically, so
attribution is unaffected — but the commit message overstated the explanation and
cannot be amended without a force-push.

---

## Unrelated blocker found while verifying — not part of this change

The collector's readiness cross-check began returning **HTTP 401** at
2026-08-06T01:24Z. Root cause: the session token in `auth-token.txt` was created
2026-07-07T00:27:04 and `SESSION_TTL_DAYS = 30` expired it at
**2026-08-06T00:27:04Z**, 58 minutes earlier. My 23:06Z run succeeded because it
predated expiry. Nothing to do with this change; needs a fresh token, which is
Jesse's to issue. Noted so it is not mistaken for a regression here.

Also visible: `auth_sessions` holds 3 expired rows with no cleanup.

---

## Files to review

| Path | What |
|---|---|
| `backend/hdencode_shadow.py` | Defect 1, both halves |
| `backend/database.py` (`get_hdencode_shadow_summary`) | Defect 2 |
| `tests/test_hdencode_shadow_miss_validity.py` | new, both directions |
| `tests/test_hdencode_readiness_integrity.py` | 3 added; your `f5e3c6e` test untouched |
| `docs/.../qualification/scripts/miss_resolution.py` | grader sourcing |
| `docs/.../qualification-evidence/collect_shadow_evidence.py` | graded stop condition |
| `docs/.../qualification/scripts/compare_filter_options.py` | the A/B/C measurement |
| `docs/.../qualification/scripts/rss_status_report.py` | every statistic above |
| `docs/.../qualification/scripts/measure_miss_provenance.py` | the 90/60 split |

## What I am NOT asking for

I am not asking whether the RSS row should close. That is Jesse's call and he has
explicitly held it open pending this round. I am asking whether the accounting
change is correct and whether the evidence supports "no coverage was lost".
