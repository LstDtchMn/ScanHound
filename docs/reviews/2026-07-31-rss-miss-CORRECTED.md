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
