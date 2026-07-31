# RSS miss analysis — evidence preserved, first findings

**Date:** 2026-07-31 · **Author:** Claude · **Reviewer:** ChatGPT · **Arbiter:** Jesse
**Executes:** rev 2.1 serial step 2 (evidence preservation) and Track A step A1.
**Controlling plan:** `2026-07-31-plan-rev2-AUTHORITATIVE.md`

Read-only throughout. Nothing enabled, nothing deployed, no semantic change
made. The `#191` symmetry fix has **not** been implemented — deliberately, since
rev 2.1 requires evidence preservation and analysis to precede it.

---

## 1. What was done

### 1.1 Evidence bundle (serial step 2)

`C:\DockerData\infra-ops\evidence\rss-snapshot-20260731-174138\` — 62 MB, 54
files, private, outside any git repo.

| Captured | Method / value |
|---|---|
| Database | SQLite **backup API**, not a file copy, so WAL state is included. `PRAGMA integrity_check` = **ok** |
| Qualification artifacts | 46 evidence JSONs + full window log |
| Repo provenance | branch `main`, commit `882fab1d58c446cf5a450df589bf2d3701755b28` |
| Runtime provenance | image `scanhound:latest`, id `sha256:a977aa3b…`, container started `2026-07-31T02:51:50Z` |
| Config | live `config.json` snapshot |
| Integrity | `SHA256SUMS.txt` over all 54 files |

All analysis below runs against this frozen copy, not the live database.

### 1.2 A correction I have to disclose

I previously told Jesse the per-miss detail "is not in the database I can
reach," and rev 2.1 was written on that basis.

**That was wrong.** The table `hdencode_shadow_misses` exists in
`/dbvol/crawler.db`. My earlier search ran against `/data/scanhound.db` — a
0-byte stray file — and I never re-ran it after locating the real database. The
analysis I called blocked was available the whole time.

---

## 2. Findings

### 2.1 Observations versus distinct releases — the caution was right, the number is not

ChatGPT's round-1 correction ("97 observations ≠ 97 titles") was methodologically
correct and I was wrong to say "titles." Having now measured it:

```
miss rows (observations) : 100
DISTINCT canonical_url   : 100
most-repeated release    : 1 occurrence
```

**Every miss is a unique release. Nothing is double-counted.** The recurrence
hypothesis is dead. The two numbers coincide, so the cautious reading did not
deflate the problem — it left it intact.

(100, not 97: the window advanced during the work. It is still accumulating.)

### 2.2 Composition

| Resolution | TV | Movie | Total |
|---|---|---|---|
| 2160p / 4K | 12 | 29 | 41 |
| 1080p | 37 | 6 | 43 |
| 720p | 14 | 0 | 14 |
| unknown | 0 | 2 | 2 |
| **Total** | **63 (63%)** | **37 (37%)** | **100** |

Status: 79 `missing`, 12 `missing_season`, 8 `upgrade`, 1 `dv_upgrade` — these
are actionable items, not noise.

**TV dominates at 63%**, and TV misses skew 1080p/720p while movie misses skew
4K. That asymmetry is the strongest signal in the dataset.

### 2.3 The decisive result: none were ever acquired

```
missed URLs later present in hdencode_candidates :   0 of 100
never acquired by RSS                            : 100 of 100
```

`hdencode_candidates` is the RSS ingestion table. **Not one of the 100 missed
releases ever arrived through RSS**, at any later cycle.

**Pruning is excluded as an explanation.** The candidates table holds 2,434 rows
spanning `2026-07-22T00:01:36Z → 2026-07-31T21:05:10Z` — the entire
qualification window, with no gap at the old end. Absence is genuine absence,
not eviction.

### 2.4 What this rules in and out

Against ChatGPT's eight causal buckets:

| Bucket | Verdict |
|---|---|
| Publication lag | **Excluded** — zero later acquisitions |
| Finite-window displacement | **Weakened** — displacement implies later arrival on a subsequent poll; none occurred |
| Taxonomy omission | **Leading candidate** — 63% TV against two broad feeds |
| Catch-up-only coverage | Open |
| Identity mismatch | Open — canonicalisation not yet compared |
| Parser/transport failure | Open |
| **Persistent upstream omission** | **Strongly indicated** |
| Ambiguous | Not needed for these 100 |

The finite-window theory from round 2 — including the indirect full-disc
displacement path we agreed on — predicts that a displaced release reappears
once the window slides. **It never does.** That is not what displacement looks
like.

---

## 3. What this implies for `#191`

`#191` remains correct on population-parity grounds and should still be done.

But rev 2.1 already stated it cannot repair finite-window displacement, and this
analysis now weakens displacement as the mechanism at all. **`#191` should be
expected to change none of these 100 outcomes.** If it is implemented and the
miss rate falls, that would be evidence my model is wrong — worth stating in
advance so the prediction is falsifiable rather than retrofitted.

---

## 4. What I have NOT done

- Per-cycle metrics (A2): feed depth in seconds, poll gap, **coverage margin**,
  full-disc proportion per upstream feed. Requires parsing the 46 evidence JSONs.
- Which listing source each miss came from — would confirm or kill the TV
  taxonomy hypothesis directly.
- Canonicalisation comparison between listing and feed URL shapes.
- Predeclared promotion thresholds (A5).
- Any rename work (Track B), which rev 2.1 prioritises after preservation.

---

## 5. For review

1. **Does "zero later acquisitions across 163 cycles" definitively exclude
   publication lag and displacement**, or is there a mechanism where a displaced
   release never returns — for example if the feed is ordered by publication and
   the release is never republished?
2. **Is `hdencode_candidates` the right table to test acquisition against?** I
   assumed it is the RSS ingestion target because `ingest_hdencode_feed()` writes
   to it. If listing-path items also land there, my zero is meaningless.
3. **Is the 63% TV skew sufficient to prioritise the taxonomy hypothesis** over
   per-cycle coverage-margin work, or should A2 run first regardless because it
   is the only thing that can measure the window directly?
4. **Should `#191` now be deprioritised?** It cannot explain these 100 and is
   predicted to change none of them. It is still correct, but it is no longer
   urgent, and Jesse's decision to "close the gap now" was made when we believed
   it was entangled with the miss count.
5. I have twice now stated something was unavailable and been wrong (the miss
   table here; the collector's networking earlier). **Is there a check I should
   be running before claiming an absence?**
