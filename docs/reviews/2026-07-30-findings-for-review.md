# Findings for adversarial review — 2026-07-30

**Author:** Claude · **Reviewer:** ChatGPT · **Arbiter:** Jesse
**Standing protocol:** equal peers. Merge, deploy, force-push, mark-ready, production-settings
changes and feature enablement remain Jesse-only. Nothing below has been merged or deployed
except where explicitly marked SHIPPED.

Every figure in this document was **re-measured on 2026-07-30**. None is carried over from an
earlier document or a code comment. This is deliberate: a previous round's headline finding
rested on a stale "~48s" copied out of a config comment and was refuted twice. Where a number
is an estimate or unverified, it says so.

---

## What I want attacked, in priority order

1. **Finding C's fix** — new code, unmerged, touches what the user sees as "missing". Highest risk.
2. **Finding B's interpretation** — I may be over-reading a structural cause into the RSS data.
3. **Finding A's residual gap** — RSS-side symmetry is genuinely absent, not merely untested.
4. **My reasoning failure in §5** — I shipped a fix based on inference instead of measurement.

---

## Finding A — full-disc `[BD]` releases were invisible (SHIPPED, verified)

**Root cause.** HDEncode full-disc releases carry no `Filename:` field, because a disc is not a
single file. `detail_scraper.py:212` does `if not fn_match: return None` with **no logging at any
level**, so every full-disc release was fetched, silently discarded, and re-fetched on the next
cycle forever. Zero of 2432 catalogued releases were full-disc.

**Fix.** Recognise `^\s*\[\s*BD\s*\]` on the TITLE (anchored, case-insensitive), exclude before
scheduling any detail fetch, and persist the exclusion in `listing_policy_exclusions` keyed by a
canonicalised URL.

**Production evidence, first two cycles after deploy:**

| | Before (7-29) | Cycle 1 | Cycle 2 |
|---|---|---|---|
| 4K crawl depth | page 25 | page 25 | **page 1** |
| Full-disc excluded | 0 | 102 (102 newly seen) | 7 (**0 newly seen**) |
| Posts → items | 128 → 2 | 8 → 7 | 2 → 2 |
| Wall clock | — | ~70 s | ~7 s |

Cycle 2 is the load-bearing one: `0 newly seen` proves the exclusions persisted and are being
recognised from disk, which is why the crawl collapsed from 25 pages to 1.

**Known residual gap.** The exclusion exists only on the **listing** path. The RSS path has no
equivalent, so if `rss_primary` is ever promoted, full-disc releases return through the other
door. I consider this a hard gate on promotion.

### Questions for review

- Is title-anchored `[BD]` the right discriminator, or should absence of `Filename:` be treated
  as the signal, so other fieldless release types are covered too? My concern with the latter:
  it converts a parse failure into a silent policy decision, which is the exact class of bug
  this branch exists to remove.
- The early-stop guard is `page_unique > 0 and page_new == 0`. An earlier draft used
  `page_posts`, which counts duplicates and would have ended a crawl on a page of repeated
  links, hiding genuinely new releases on the next page. I believe `page_unique` closes that.
  Does it?

---

## Finding B — the RSS shadow trial says NOT READY, and I am unsure why

Measured from the live evidence file `05_shadow_evidence_20260730T225659Z.json`:

```
successful_cycles   151   (required 20)
observed_days       8.88  (required 7)
request_reduction   90.02%
normal_feeds_healthy True
relevant_misses     81    <- the only failing criterion
reasons             ['relevant_misses_detected']
```

- 43 of 170 cycles contain at least one miss; the **newest** cycle (2026-07-30T22:42) has one, so
  this is ongoing, not historical.
- Every miss is a `listing_only` item — present in the page crawl, absent from RSS.
- **`rss_count` is exactly 100 in all 170 cycles.** Never 99, never 101.
- `listing_count` averages 3.5 (min 0, max 19).

**My reading, offered for attack:** an exactly-constant 100 is a fixed-length feed window, and a
release missing from a 100-item window refreshed hourly, when the crawl only surfaces ~3.5 items
per cycle, smells like a **coverage** gap rather than a timing one. The evidence shows two feeds
(`movies_all`, `tv_all`) against three crawled sources (4K Movies, Remux Movies, TV Packs). If a
crawled category has no corresponding feed, its items would be missed permanently.

**I could not confirm this.** The per-miss detail rows are not in `crawler.db`, so I do not have
the missed titles. Everything above is structure, not content.

**Two caveats that may deflate the number:**

1. **Finding A may inflate it.** Full-disc releases were invisible system-wide until this morning.
   If some of those 81 misses are full-disc, RSS was blamed for missing items that could never
   have been ingested. The 81 should not be trusted until checked against titles.
2. **One gate check has been blind the entire window.** `summary.app_readiness` is
   `{"error": "<urlopen error [Errno 111] Connection refused>"}` — the collector cannot reach
   ScanHound's API. That portion of the verdict never ran, for 8.9 days.

### Questions for review

- Is the constant `rss_count=100` sufficient to infer a fixed feed window, or is there a reading
  where it is an artefact of the collector rather than the feed?
- Does a gate that reports a verdict while one of its checks is erroring constitute a fail-open?
  I lean yes, and think it should refuse to grade rather than grade partially.

---

## Finding C — the 4K filter hides most of the 4K library (FIX WRITTEN, NOT MERGED)

**Defect.** UHD is stored under two spellings and every filter compared the raw string:

```python
# write path — backend/filename_utils.py:199
result["resolution"] = "2160p" if r in ("4k", "uhd") else r

# read path — backend/api/routes/results.py (pre-fix)
result = [i for i in result if i.get("resolution") == "4K"]
```

`"2160p" == "4K"` is never true. Confirmed there is **no** normalisation anywhere on the read
path, and the frontend twin had the identical defect.

**Measured impact on the production DB (`/dbvol/crawler.db`, 2026-07-30):**

| Stored as | Movies | Reachable via the '4K' chip |
|---|---|---|
| `4K` | 153 | yes |
| `2160p` | 242 | **no** |

**242 of 395 4K movies — 61% — were unreachable.** And the share was growing: the parser writes
`2160p`, so every newly parsed release became unfilterable while legacy rows kept working.
`1080p` was unaffected because parser and chip happen to agree on that spelling, which is
precisely why a defect this size stayed green in a suite that has 1,300+ lines of filter tests.

**Fix.** Canonicalise at the **filter boundary** (not by migrating stored values) in both twins:
`2160p`/`4k`/`uhd` → `4K`, `1080i` → `1080p`. Unknown spellings pass through **unchanged** rather
than mapping to null — mapping unknowns to null would recreate the original defect in a new form.
Four sites changed: `_resolution_keys` and the quick-filter branch in the backend, and their two
frontend counterparts. The quick-4K branches deliberately do **not** reuse `_resolution_keys`,
because that folds every TV item to `{"TV"}` and would silently drop TV from a chip that today
includes it.

**Verification.** 20 backend tests pass. More importantly, reverting only the fix inside the test
container makes the regression test fail with the actual defect:

```
AssertionError: assert ({'2160p'} & {'4K'})
  where {'2160p'} = _resolution_keys({'resolution': '2160p'})
```

### Questions for review

- Boundary canonicalisation versus a data migration: I chose the boundary because a migration
  must be re-run for every future writer that reintroduces a spelling, and because the raw value
  is still what gets displayed. Is that the right trade, or does it leave the two-spelling
  problem alive to bite somewhere I have not looked?
- I found `grouping.ts:138` and `ResultRow.svelte:320` already doing ad-hoc
  `=== '2160p' ? '4K'` normalisation inline. That means the problem was known locally and fixed
  piecemeal. Should those now route through the shared helper, or is that scope creep on a
  defect fix?
- **Out of scope, flagged deliberately:** `720p` has no chip at all (228 movies, 230 TV), and
  Plex-side display badges still compare `pv.res === '4K'`, so a `2160p` Plex version does not
  get the highlight. Both are real. Neither is this defect.

---

## Design question — TV cannot be filtered by resolution at all

Not a bug; a deliberate choice, documented identically in both twins:

```python
"""A TV show keys ONLY as 'TV' (never by resolution) so the 4K/1080p
   filters are movies-only..."""
```

The data does not support the restriction. All 1,036 TV items carry a resolution: 478 are
`1080p`, 246 `4K`, 230 `720p`, 80 `2160p`. Jesse hit this on mobile and assumed a mobile bug; it
is app-wide.

**Deliberately not changed in this branch.** It shares the exact lines the defect fix touches,
and bundling a capability change into a correctness fix would make the diff unreviewable. Is that
the right call, or is splitting them artificial given they are the same lines?

---

## §5 — my reasoning failure tonight, offered as a case study

The mount auto-recovery task painted a PowerShell window on the desktop every 5 minutes. I
diagnosed it as a console window, shipped `-WindowStyle Hidden`, and told Jesse to expect "at
most a brief flash".

**It kept appearing.** Windows 11's default terminal resolves to Windows Terminal, which is a
separate window that ignores `-WindowStyle Hidden` entirely, and his Terminal is configured to
stay open after the process exits. I had reasoned about the classic console host without checking
which host the machine actually uses — a one-command registry read I did not perform until after
the fix failed.

Worse, I asserted the outcome to Jesse **before** he could observe it, on a claim I had no way to
verify myself, having no view of his desktop.

Compounding it: I then identified the wrong culprit. Jesse said "every couple of minutes" in his
first message; my task repeats every 5. Enumerating scheduled tasks by interval found **Docker
Port Watchdog** at `PT2M` — a pre-existing monitor, not mine. He was the one who said "I think
this is a different thing."

Two attempted bypasses (`conhost --headless`) failed to run the child process at all, so I have
**no** verified fix for the window, and said so rather than proposing a third guess.

The generalisable lesson I would like challenged: **when a claim is only checkable on a surface I
cannot observe, I should state the prediction as a prediction and route verification to the
person who can see it — not report it as done.**

---

## Other errors this session, disclosed

- Told Jesse a stale scheduled task was "failing, doing nothing" from its name and exit code, and
  he authorised deleting it. Reading it first showed exit 3 was the collector's **stop-condition
  signal**, and the task was the only thing tracking RSS readiness. Deleting it would have
  destroyed the mechanism behind Finding B. Retracted before acting.
- Reported the newest RSS miss as 7-28 from a sorted slice that was actually the oldest twenty.
  Corrected in the same turn; the newest is 7-30T22:42.
- Wrote `-Repo` in an installer command; the parameter is `-SourceRepo`. Copied from the wrapper
  script without checking the target's own parameter block.
- Caught pre-ship: the installer asserted its repetition interval by **string** equality against
  `"PT60M"`, but Windows normalises 60 minutes to `PT1H`. The interval change would have
  registered correctly, then failed its own integrity check and left the task disabled — looking
  exactly like a genuine tamper failure. Now compares elapsed minutes.

---

## State of play

| Item | Status |
|---|---|
| Full-disc listing exclusion | SHIPPED, verified over two cycles |
| Full-disc RSS symmetry | **absent** — gate on any `rss_primary` promotion |
| RSS shadow trial | running, day 8.9, `ready=False`, promotion paused by Jesse 7-27 |
| Resolution canonicalisation | written, tested, **unmerged** |
| TV-by-resolution | not started, deliberately |
| Mount task | hourly, hidden flag set but **unproven** against Windows Terminal |
| Docker Port Watchdog | **DISABLED** 7-30 for popup noise — logged in infra-ops BACKLOG.md |
| Auto-rename | remains paused |
