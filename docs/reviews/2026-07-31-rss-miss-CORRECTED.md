# RSS miss analysis — CORRECTED. The misses are ~1-hour lag, not coverage loss.

**Date:** 2026-07-31 · **Author:** Claude · **For review:** ChatGPT · **Arbiter:** Jesse
**Supersedes:** `docs/reviews/2026-07-31-rss-miss-analysis-round3.md` (commit `8267ff1`),
whose central finding was **wrong**.
**Evidence:** frozen bundle `rss-snapshot-20260731-174138` (repo `882fab1`,
image `sha256:a977aa3b`, SHA256 manifest, `integrity_check: ok`). Read-only.

---

## 1. What I got wrong, and how the reviewer caught it

I reported **"0 of 100 missed releases were ever acquired by RSS"** and built a
causal story on it: persistent upstream omission, publication lag excluded,
displacement weakened, taxonomy the leading suspect.

**All of it was an artifact of a broken join.** The two tables store different
URL forms:

```
hdencode_candidates    → https://hdencode.org/some-release/     (trailing slash)
hdencode_shadow_misses → https://hdencode.org/some-release      (no slash)

candidates ending in "/":  2434 of 2434
misses     ending in "/":     0 of 100
```

An exact string join was **guaranteed** to return zero. It measured nothing.

The reviewer identified this from the two canonicalisers — `canonicalize_post_url()`
adds a trailing slash, `hdencode_shadow.canonical_url()` strips it — before I
had looked. Worse: my own document listed "canonicalisation comparison — not
done" in §4 and then drew a conclusion depending on it in §2.3. I named the gap
and reasoned past it.

The reviewer also showed my displacement logic was backwards: a newest-first
window moves *away* from a displaced item, so zero later acquisitions would have
been *consistent* with displacement, not evidence against it.

---

## 2. Corrected method

One shared canonicaliser applied to both sides: force https, lowercase host,
strip `www.`, collapse repeated slashes, strip trailing slash, drop query and
fragment.

**Positive controls run before trusting any number:**

| Control | Expected | Result |
|---|---|---|
| Known-present URL joins | True | ✅ True |
| Its trailing-slash variant joins | True | ✅ True — proves the fix works |
| Unrelated URL joins | False | ✅ False |

---

## 3. Corrected result — the opposite of what I reported

```
missed releases        : 100
MATCHED in candidates  :  99
never acquired         :   1
```

### Time-aware classification (the reviewer's four buckets)

| Bucket | Count |
|---|---|
| A — acquired **before** the miss (window turnover) | 0 |
| **B — acquired AFTER the miss (lag)** | **99** |
| C — same cycle (membership/comparison fault) | 0 |
| D — never acquired | 1 |

### Acquisition lag

```
min 0.9h   median 1.0h   max 2.8h
within 6h : 99 of 99
over 24h  :  0
```

**Every single one resolved inside three hours, with a median of one hour.**

---

## 4. What this means

This is **not a coverage failure**. RSS carries essentially everything the
listing crawler finds — it simply arrives about one polling interval later.
The comparison samples at a moment when the listing has a release and the feed
has not yet published it, records a permanent "miss", and never re-checks.

So the qualification gate has been counting **~1-hour latency as permanent loss**
for 9.6 days, and reporting `ready=False` on that basis.

The single genuinely-never-acquired release is 1% — a real but small residue
that deserves its own look, not a programme.

### Consequences

- **The RSS verdict should be revisited.** A criterion with an accepted latency
  threshold — even a strict one, 6 hours covers 99 of 99 — would likely pass.
  This is close to what the parked criterion work (`#188`/`#189`) was for.
- **`#191` explains none of it**, as predicted, though for a different reason
  than I gave.
- **Taxonomy is dead as a hypothesis.** The 63% TV skew reflects composition,
  not a coverage gap — exactly the denominators point the reviewer made.
- **Finite-window displacement is not supported either**, but by evidence this
  time: displaced items would not arrive an hour later; these did.

---

## 5. Where I need help

1. **Is a lag-tolerant criterion the right fix, or does it hide a real risk?**
   If RSS is always ~1h behind, an auto-grab racing the feed could act on stale
   absence. Does the accepted-latency threshold need to be paired with a
   "confirm before acting" rule?
2. **What should the threshold be?** 6h covers everything observed, but the
   observation is one 9.6-day window on one site. Is 6h defensible, or should it
   be derived from the feed's own publication cadence?
3. **Does bucket A being zero surprise you?** I expected some window turnover.
   Zero suggests the feed depth is comfortably ahead of the poll gap — which
   would also mean the constant `rss_count = 100` is not a shallow-window
   symptom after all.
4. **The 1 never-acquired release** — worth chasing, or accept 1% as noise?
5. **A2 coverage-margin work: still needed?** It was justified by a
   window-depth theory this result undercuts. I would rather not spend the effort
   if the question is now answered.

---

## 6. Process note

Three times this session I asserted something I had not verified — the collector
networking, the missing miss table, and this. All three were caught by review,
not by me.

I am adopting the reviewer's seven-step absence protocol before any future
absence claim, with the two steps I actually skipped emphasised: **reconcile
identity representations across stores**, and **require a positive control
before trusting a zero**.

Nothing was built on the wrong finding. No code, no `#191`, no reprioritisation.
The damage was confined to a document, which this replaces.

---

# ADDENDUM — round-4 review items resolved

Two authorized items completed. Both corrected me further.

## A. It is POLLING lag, not PUBLICATION lag

The reviewer was right that `first_seen_at` is an ingestion timestamp and that I
had assumed a publication mechanism without measuring it. Measured:

```
pub_date AFTER the miss  (upstream had not published) :  0
pub_date BEFORE the miss (published, we had not polled): 99
pub_date offset: min -1.1h   median -0.2h   max -0.0h
```

**HDEncode had already published all 99** — a median of roughly twelve minutes
before the listing comparison ran. ScanHound then ingested them about an hour
later.

Corrected mechanism: **RSS observation/polling lag.** The upstream feed is not
behind; our poll schedule is. This is a scheduling problem, materially more
tractable than either coverage loss or upstream latency.

Wording accepted throughout: "ScanHound had not yet observed the release through
the qualified RSS feeds at the time of the listing comparison."

## B. The unmatched release is RED, not noise

```
url        : tambi-2026-2160p-sonyliv-web-dl-dd5-1-atmos-h-265-cptn5dw-8-7-gb
miss cycle : 2026-07-30T02:26:28Z
snapshot   : 2026-07-31T21:09:38Z
age at close: 42.7 hours  ->  RED (> 24h boundary)
```

I called this "1% noise." **Withdrawn.** At 42.7 hours it is nearly twice the red
threshold with ample subsequent cycles — a genuine persistent miss requiring
diagnosis, not a rounding error. The reviewer was right to refuse the framing.

Lead worth following: it is a **SonyLIV 2160p** release, an unusual source. That
is a taxonomy signal, and it is the one identity for which taxonomy omission
remains live.

## Wording corrections adopted

- "This is not a coverage failure" -> the dominant cause of recorded misses is
  bounded RSS **observation** lag; one identity remains RED pending diagnosis.
- "RSS carries essentially everything" -> RSS acquired 99 of 100 normalized
  actionable miss identities in this frozen window.
- "Taxonomy is dead" -> not supported for the 99; **still live for the one**.
- "Finite-window displacement is not supported" -> not the cause of the 99;
  window sufficiency under outage and burst remains open for focused A2.
- "1% noise" -> withdrawn entirely.

## Still outstanding (authorized, not started)

1. Feed attribution for the 99 via `hdencode_candidate_feeds` — which feed
   acquired each, and whether any were catch-up-only.
2. Focused A2: coverage margin, outage and burst resilience.
3. Lag-aware readiness gate: pending / green / yellow / red / ambiguous state
   model replacing `cumulative_misses > 0 => not ready`.
4. Diagnose the RED release against raw feed evidence.
5. Commit the read-only analysis script and canonicaliser for reproducibility —
   the reviewer's point that two opposite conclusions came from the same
   evidence, so prose is not sufficient.

## Accepted without reservation

- No per-grab listing confirmation is needed. My auto-grab concern was
  unfounded; discovery lag does not create stale-absence action. Auto-grab keeps
  its own separate safety gate and **remains off**.
- 6h green / 24h red retained — predeclared before this result, so not a post-hoc
  fit — and to be revalidated over a clean window including a restart.
- Bucket A being zero supports first-observation lag but proves nothing about
  feed-depth headroom.

---

# ADDENDUM 2 — the RED release diagnosed

`https://hdencode.org/pallichattambi-2026-2160p-sonyliv-web-dl-dd5-1-atmos-h-265-cptn5dw-8-7-gb`

| Check | Result |
|---|---|
| Alternate URL / identity variants in candidates | **0** |
| Feed-membership rows in ANY feed | **0** |
| Feeds that exist and could have carried it | `movies_all`, `movies_2160p` (12 feeds total) |

Not an identity mismatch, not a catch-up-only acquisition, not a timing artifact.
**HDEncode never placed this release in any RSS feed.** It appeared on the
listing pages only.

Classification: **persistent upstream omission** — ChatGPT's bucket 7, and the
only true instance among the 100.

## Why this matters for the gate design

The proposed readiness criterion requires **zero RED**. This finding says zero
RED may be **unachievable**: roughly 1 in 100 releases never enters the feeds at
all, through no fault of ScanHound's polling, canonicalisation or coverage.

That is a genuine product decision, not a bug to fix:

- **Zero RED** means RSS-primary can never qualify while upstream omits anything.
- **A small RED allowance** (e.g. "under 2% of actionable discoveries, none of
  them a title the user bookmarked") makes qualification possible but concedes
  that RSS-primary will silently miss the occasional release.
- **A hybrid** — RSS for discovery, with a low-frequency listing sweep purely to
  catch upstream omissions — keeps most of the ~89% request saving while closing
  the gap. This is the option I would put in front of Jesse.

Sample size caveat, stated plainly: **one release out of 100 is a 1% point
estimate from a single 9.6-day window.** It establishes that upstream omission
is non-zero. It does not establish the rate. A clean post-change window should
measure it against the full actionable listing population before any threshold
is fixed.

---

# DECISIONS — Jesse, 2026-07-31

**1. Criterion design: HYBRID.** RSS for discovery, plus an infrequent listing
sweep purely to catch upstream omissions. Chosen over zero-RED (likely
unachievable given ~1-in-100 upstream omission) and over a bare RED allowance
(which would silently miss releases with nothing watching).

The sample-size caveat stands and must be respected when the gate is built:
1 in 100 is a point estimate from one 9.6-day window. It establishes that
upstream omission is non-zero, not what the rate is.

**2. Sequencing: finish Track A before Track B.** Narrowed A2 resilience work,
then the lag-aware gate designed around the hybrid. Note this is a deliberate
departure from rev 2.1's "rename outranks RSS after preservation" priority —
Jesse's call, made with that priority stated.
