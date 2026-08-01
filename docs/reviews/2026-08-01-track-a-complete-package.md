# Track A complete — RSS diagnosis, coverage margin, and the hybrid design

**Date:** 2026-08-01 · **Author:** Claude · **Reviewer:** ChatGPT · **Arbiter:** Jesse
**Controlling plan:** `docs/reviews/2026-07-31-plan-rev2-AUTHORITATIVE.md` (rev 2.1)
**Evidence:** frozen bundle `rss-snapshot-20260731-174138` — 62 MB, 54 files,
SHA256 manifest, DB via backup API (`integrity_check: ok`), pinned to repo
`882fab1` / image `sha256:a977aa3b`.
**Reproducer:** `scripts/analysis/rss_miss_reconcile.py --db <snapshot>`

Per Jesse's instruction, no review round was requested until Track A was
complete. This is that package. Everything below is read-only analysis; no
semantic change has been made and `#191` is **not** implemented.

---

## 0. A confound I have to declare first

**The production container was rebuilt and restarted mid-window**, after the
snapshot was taken. `main` moved `882fab1 → 7cc5275` (security fixes + the 4K
filter fix + mount scripts), deployed 2026-08-01 ~00:37Z.

Neither change touches RSS discovery. But **you raised exactly this in round 2** —
whether "independent" product work is truly independent "given it ships in the
same image and a deploy restarts the container mid-RSS-window" — and I did not
treat it as a real constraint. It then happened.

Consequences I am declaring rather than discovering later:
- The frozen snapshot predates the deploy and is unaffected. All numbers below
  come from it.
- Cycles after ~00:37Z on 2026-08-01 sit across a restart boundary and should
  **not** be pooled with earlier ones without justification.
- If a clean qualification window is required, it starts **after** this deploy,
  not from 2026-07-22.

**Question 0: does this deploy invalidate the pre-deploy window for
qualification purposes, or only require a boundary marker?**

---

## 1. What Track A established

### 1.1 The misses are polling lag, not coverage loss

| Bucket | Count |
|---|---|
| A — acquired **before** the miss | 0 |
| **B — acquired AFTER the miss** | **99** |
| C — same cycle | 0 |
| D — never acquired | 1 |

Acquisition lag: **min 0.85 h, median 1.02 h, max 2.84 h.** 99 of 99 inside six
hours, none over 24.

### 1.2 It is OUR polling, not upstream publication

```
pub_date AFTER the miss  (upstream had not published) :  0
pub_date BEFORE the miss (published, we had not polled): 99
pub_date offset: min -1.05 h  median -0.16 h  max -0.01 h   (ALL negative)
```

HDEncode had already published every one — a median of ~10 minutes before the
comparison ran. Your correction that `first_seen_at` is an **ingestion**
timestamp was load-bearing; I would have shipped "publication lag" as the
mechanism and been wrong about the cause.

### 1.3 Feed attribution — your decisive test

You wrote: *"if the 99 were acquired by normal feeds one cycle later, the simple
lag conclusion is strong."*

| First acquiring feed | Count |
|---|---|
| `tv_all` (alone or with TV sub-feeds) | 63 |
| `movies_all` (alone or with movie sub-feeds) | 36 |
| **catch-up-only** | **0** |

All 99 via **normal** feeds. The comparison grades the same population
RSS-primary would use; the population-mismatch concern is closed.

### 1.4 The one RED release

`pallichattambi-2026-2160p-sonyliv-web-dl-dd5-1-atmos-h-265-cptn5dw-8-7-gb`

| Check | Result |
|---|---|
| Alternate URL / identity variants | **0** |
| Feed-membership rows in ANY of 12 feeds | **0** |
| Age at snapshot close | **42.7 h → RED** |

Not identity mismatch, not catch-up-only, not timing. HDEncode never placed it
in any feed. **Persistent upstream omission** — your bucket 7, the only true
instance. I had called it "1% noise"; withdrawn.

---

## 2. Coverage margin (focused A2) — NEW

| Feed | Depth | Median poll gap | Worst gap | Margin |
|---|---|---|---|---|
| `movies_all` | 11.3 h | 1.20 h | 5.79 h | **+5.5 h** |
| `tv_all` | **9.8 h** | 1.20 h | 5.79 h | **+4.0 h** |

Both currently safe — feed depth exceeds the worst observed poll gap, so nothing
has aged out. This confirms the lag finding from an independent direction.

**But `candidate_count` was 50 on 343 of 344 polls.** Both normal feeds return a
full window every time. Exactly as you predicted: a full window is not proof of
loss, but it provides **no spare capacity**.

Depth of every feed, for reference:

```
tv_all         9.8    movies_all      11.3   tv_webdl       13.8
tv_1080p      19.1    movies_1080p    24.3   tv_2160p       28.3
movies_2160p  41.0    tv_720p         61.7   movies_720p    71.1
movies_bluray 168.2   movies_remux   184.6   tv_webrip     210.6
```

**`tv_all` at 9.8 h is the binding constraint**, and it carries 63% of the
misses. TV is the exposed side.

**Resilience translation:** an outage under ~9.8 h loses nothing permanently.
Beyond that, TV releases fall below the feed horizon and no local filtering
recovers them — HDEncode will not return them again.

---

## 3. Jesse's decisions

1. **HYBRID criterion** — RSS for discovery plus an infrequent listing sweep
   purely to catch upstream omissions. Chosen over zero-RED (likely unachievable)
   and a bare RED allowance (silently misses releases).
2. **Sweep every 6 hours** — inside the 9.8 h binding constraint with margin, and
   covers any outage shorter than 6 h.
3. **Sweep crawls page 1 of each source only** — the crawl averages 3.5 new items
   per cycle, so page 1 covers a 6-hour window comfortably. Would have caught the
   SonyLIV release.
4. Bands **6 h green / 24 h red**, predeclared before the corrected result.
5. **Finish Track A before Track B** — a deliberate departure from rev 2.1's
   "rename outranks RSS", made with that priority stated to him.

---

## 4. What I want attacked

1. **Question 0 above** — the mid-window deploy. You flagged the risk; I did not
   act on it. How much does it cost us?
2. **Is a 6-hour sweep at page 1 sufficient?** My reasoning: 3.5 items/cycle
   average means page 1 covers 6 hours easily. But that is a *mean* — I have not
   measured the busiest 6-hour publication burst in the window. If a burst
   exceeds one page, the sweep silently under-catches. **Should I measure peak
   publication volume before this is fixed?**
3. **Does the hybrid actually close the RED gap?** The sweep catches upstream
   omissions only if the omitted release appears on the listing pages within the
   sweep's reach. The SonyLIV release did. I have not established that all
   upstream omissions are listing-visible.
4. **Coverage margin is computed from `hdencode_feed_state`'s current
   newest/oldest entries** — a single snapshot per feed, not a per-poll series.
   Is that adequate, or does the margin need to be computed per cycle to catch
   depth *contraction* during busy periods?
5. **Is Track A now complete enough to close?** Remaining known gaps: per-cycle
   depth series, peak-burst measurement, and the lag-aware gate itself (designed
   but not built).

---

## 5. Process note

Three of my conclusions this session were wrong and all three were caught by
review: the collector's networking mechanism, the "missing" miss table, and the
false `0 of 100`. The reproducible script now enforces in code the two rules I
skipped — one shared canonicaliser on both sides, and positive controls that
**exit nonzero** if they fail.

Jesse has since recorded that I should **proactively request review** rather than
wait to be asked, with the strongest trigger being *a causal claim resting on a
single measurement* — the exact shape of all three errors.
