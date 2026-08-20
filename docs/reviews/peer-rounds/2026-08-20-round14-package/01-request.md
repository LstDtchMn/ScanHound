# Round 14 request — M13-1 closed, and evidence about the watermark model

## Verdict accepted

Round 13's ruling is accepted in full. Two things fixed, one deliberately not
built, and one finding I want to put in front of you before anything else.

## I introduced a regression and it was the mask itself

Worth stating first, because it is the sharpest thing that happened this round.

My first M13-1 fix masked `media_kind` alone in `get_release_identity()`. That
was not merely incomplete. In `annotate_source_links()` the branch order is:

```text
if kind == "movie" and season is not None:   -> refuse (contradiction)
if season is not None:                       -> tv_season
if kind != "movie":                          -> refuse
                                             -> movie
```

Nulling the kind stops the contradiction guard firing, so a **held** row carrying
`media_kind='movie'` AND a season fell through to the `tv_season` branch. Unheld,
that row is refused. Held, it emitted a complete, actionable TV identity.

**The hold granted the permission it exists to withdraw.** Fail-closed became
fail-open, and only for releases we had just decided were unsafe.

Separately, and as you noted: the `tv_season` branch reads `season` alone and
never consults `media_kind`, so the mask only ever withdrew the movie half.

A classification conflict is two listings disagreeing about movie-vs-TV, which
invalidates the TV reading exactly as much as the movie one. A hold now withdraws
the **whole** semantic identity — `media_kind` and `season` both.

Mutation, reverting to my first attempt:

```text
kills 3, decisively   assert 'tv_season' == 'unknown'
```

I found this by mapping every consumer of the column rather than by reasoning
about my own change — the same method that found the third attestation producer
last round, and the second time in two rounds it caught something my reasoning
did not.

## M13-1 — the fail-closed ordering

Ordering is the safety property; consistency was not:

```text
1. HOLD      in-process, BEFORE any write is attempted
2. ERASE     downloads.media_kind, own transaction
3. MARK      the cache conflict, own transaction, on BOTH paths
4. RELEASE   the hold only once the erase actually committed
```

The tests assert on `annotate_source_links()` and `get_release_identity()` — the
producers of the wire fields `canKeepBest` is computed from — never on the hold
set. A test that checked the set would pass while the consumer still handed out
the permission, which is exactly how my round-12 regression passed.

**Step 3 is deliberately no longer tied to step 2**, per your guidance. A briefly
missing diagnostic beats a live permission — and here it is more than a
diagnostic: a committed conflict mark whose downloads row still carries a kind is
the only durable trace of an interrupted revocation.

**Restart.** `reconcile_unrevoked_conflicts()` runs at startup beside the existing
`_migrate_legacy_persistence()`, before anything serves identity, and withdraws
authority for exactly that signature. I chose your *durable journal* option
rather than the global-disable one, because the journal already exists — the
conflict mark is it. The hold itself stays in-process by design: a marker in the
same SQLite file cannot protect the case where writing to that file is what
failed.

An undecodable cache row is treated as unsafe, not as clean.

## L13-1 — parser health, and real-producer tests

You were right on both halves.

An arm now earns coverage only by yielding recognisable posts, at the end of its
page loop, rather than by being entered. A genuinely empty listing therefore does
**not** count as covered — deliberate, since an empty page and an unparseable one
are indistinguishable from there, so neither is allowed to prove absence.

The new tests drive the real `_crawl_pages()`, including your specific case: a TV
arm answering HTTP 200 with markup the selector cannot recognise.

```text
award coverage on arm entry again     kills 3
```

## THE COVERAGE MODEL — evidence, before I build anything

I have not built the watermark model. I did go and measure whether your preferred
order key exists, because that determines whether the model is buildable at all,
and the answer is better than I expected in one way and worse in another.

### The order key exists, and is already populated for the whole corpus

```text
cached rows sampled     4000
with a posted_date      4000   (100%)
sample values           "June 29, 2026 at 11:38 PM"
                        "June 29, 2026 at 11:07 PM"
                        "June 29, 2026 at 10:42 PM"
```

That is your option 1 — a server-provided publication timestamp — at minute
resolution, on every row, already durable. `_posted_date_sort_key()` already
parses it for sorting.

### But it comes from the DETAIL page, not the listing

`posted_date` is extracted in `detail_scraper.py`. The listing selector
`_select_posts()` returns **anchor elements only** — href and link text. No date,
no post id, no ordering token is extracted from a listing page at all.

That matters because a watermark has to describe how deep in TIME an arm was
traversed, and the releases that most need attesting are precisely the ones the
crawl SKIPS as already-cached and therefore never detail-fetches.

### Why I think it is still buildable, and where I want your ruling

A skipped post is skipped for detail, but the crawl still SEES its URL — and the
cache already holds that URL's `posted_date` from when it was first fetched. So
an arm's watermark could be derived as:

```text
oldest posted_date among the posts this arm OBSERVED this run
    (looked up from the cache for skipped posts,
     taken from the fresh detail for new ones)
```

which needs no new scraping and no new site contract. It also composes with
parser health: an arm that parsed nothing has no watermark to advance.

**Three things I do not want to decide alone:**

1. Is a watermark derived from *previously stored* dates acceptable evidence, or
   does the arm have to observe the ordering itself on this run? The date is the
   site's, and it is immutable, but the crawl is trusting its own past record
   rather than the current page.
2. `posted_date` is a local-format string with no timezone
   (`"June 29, 2026 at 11:38 PM"`). Minute resolution, ties possible. Is that
   precise enough for "covered through this key", or do you want an explicit
   safety margin?
3. A post seen in a listing but absent from the cache and not fetched — a
   policy-excluded full disc, for instance — contributes no date. Should its arm's
   watermark refuse to advance past it, or is skipping it sound?

### On aged-off rows

Accepted without reservation: no historical coverage proof and no independent
authority means **permanently unknown**, reported as a measured class rather than
left looking like a failed backfill. Current absence cannot reconstruct
historical absence, and I will not substitute title parsing for it.

## Verification

```text
code head    75c6b0c

                              failed   passed   skipped
main control (origin/main)         1     5320         4
this branch                        1     5351         4
```

Same single pre-existing failure both sides
(`test_dv_settings::test_all_frontend_editable_settings_keys_are_in_model`).
**+31 passing, zero net new failures.**

Both containers were provisioned identically in the same session, and are the
same pair used for round 13 — `main` has not moved, so the control figure is the
one measured then.

## The question for this round

Two:

1. Does withdrawing `season` as well as `media_kind` over-withdraw anywhere I
   have not looked? It is the right call for a movie-vs-TV conflict, but it is a
   broader mask than you asked for, and I would rather have it challenged.
2. The three watermark questions above — particularly (1), since deriving
   coverage from previously stored dates is the step where I could be relocating
   the trust problem again rather than solving it.
