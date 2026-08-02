# Release-metadata parser inventory

**Date:** 2026-08-02 · **Author:** Claude · **Status:** finding, mostly unfixed

Every review so far — mine and ChatGPT's — framed this problem as **"RSS versus
listing"**. That framing is why each round found a divergence the previous one
missed: it described a comparison between two things in a system that has more
than two.

This is the enumeration that should have been done first. It is derived by
searching for *extraction regexes*, not for mentions of `2160p`, because most
files that mention a resolution consume it rather than parse it.

---

## Backend — resolution, season, year, size

| # | Module | Status | UHD spelling | Live? |
|---|---|---|---|---|
| 1 | `backend/release_grammar.py` | **shared, authoritative** | `UHD` (comparison token) | yes |
| 2 | `backend/sources/hdencode_feed_parser.py` | delegates ✓ | stores `2160p` | yes — the RSS path |
| 3 | `backend/sources/base.py` | delegates ✓ | emits `4K` | **unclear — see below** |
| 4 | `backend/detail_scraper.py` | **independent** | `2160p` only | yes — the deployed listing path |
| 5 | `backend/filename_utils.py` | **independent, never reviewed** | folds 4K/UHD → `2160p` | yes — the rename pipeline |

**Three canonical spellings for one concept**: `UHD`, `2160p`, `4K`.

### #4 `detail_scraper.py` — four known defects, all live

* size units `GiB|GB|MiB|MB|KB` — **no `TB`**, so a terabyte release parses to
  no size (this is divergence (e), unfixed on this path);
* resolution `(\d+x\d+|2160p|1080p)` — **omits 720p and 4K/UHD**, so a release
  titled `4K UHD` yields *no* resolution, and it **accepts a pixel dimension as
  the resolution**;
* season `S(\d{1,2})` — silently truncates `S104` to season 10, with no
  ambiguity concept;
* its own year and show-title extraction.

### #5 `filename_utils.py` — entirely unreviewed

`:196` parses resolution and folds `4k`/`uhd` to **`2160p`** — the opposite of
`base.py`. `:123-145` also produce year, season, episode and `is_tv`. This is
the parser the **rename pipeline** uses, so it feeds a different decision
surface from everything examined so far. No review has touched it, and no test
binds it to any other implementation.

### #3 `sources/base.py` — may be unreachable during a scan

`ScannerService` carries all ten source descriptors (HDEncode, DDLBase,
Adit-HD) and crawls them itself. The only reachable use of
`backend/sources/registry.py` found so far is the `/sources` and `/pipeline`
API routes — **not** the scan path. If that holds, the commit wiring
`SourceBase` to the shared grammar changed code that never runs during
discovery, and DDLBase and Adit-HD share HDEncode's gap.

**Not yet confirmed.** Stated as a question, not a finding.

*(Its now-dead `YEAR_PATTERN` / `RESOLUTION_PATTERN` / `SIZE_PATTERN` /
`SEASON_PATTERN` class attributes were deleted in the same commit as this
document — they were left behind when `extract_*` began delegating, which is
the same dead-duplicate hazard removed from the feed parser and then
reproduced here.)*

---

## Frontend — four more, in another language

| Module | What it independently knows |
|---|---|
| `lib/stores/results.ts` | `canonicalResolution` map, `2160p` → `4K` |
| `lib/resultActions/findOtherResolution.ts` | its own resolution **tier** comparison |
| `lib/downloads/dupes.ts` | its own year-stripping `(?:19\|20)\d{2}` |
| `lib/renames/category.ts` | `/2160p\|4k\|uhd/` category test |

Each encodes "4K and 2160p are the same thing" separately. The July 2026 fix
that made the 4K chip match `2160p` items corrected **one** of them; nothing
binds the rest together, and no test spans the language boundary.

---

## The count

**Nine independent definitions** of release-metadata parsing across two
languages, of which **two** now delegate to the shared grammar, **one** is the
shared grammar, and **six** remain independent — carrying three different
canonical spellings for UHD and at least four known defects.

The reviews found (a)–(f) by comparing implementations 2 and 3. Implementations
4, 5 and the frontend group were never in the comparison.

---

## What follows from this

1. **A divergence count is meaningless without the denominator.** "Six
   divergences, all fixed" was true of a pair and false of the system.
2. **The next defect of this class is more likely in `filename_utils` than
   anywhere already examined**, because it is live, unreviewed, feeds the
   rename pipeline, and disagrees with `base.py` about the canonical spelling.
3. **Unifying all nine is not obviously correct.** The rename pipeline parses
   *filenames on disk*; discovery parses *release titles from a web page*. They
   are related but not identical problems, and forcing one grammar on both could
   be a worse error than leaving them apart. What is NOT defensible is the
   current position: separate by accident, with nobody having decided.
4. **The frontend cannot share Python.** Cross-language parity needs either a
   generated constant, a contract test that reads both sources, or an accepted
   documented divergence — not silence.

## What this does not claim

No behavioural comparison has been run between `filename_utils`,
`detail_scraper` and `release_grammar`. The defects listed for `detail_scraper`
are read from its regexes; the spelling disagreement with `filename_utils` is
read from its code. **Reading a regex is weaker evidence than running it**, and
the corpus comparison that produced (a)–(f) has not been repeated across these
implementations. That measurement is the obvious next step and has not been
done.
