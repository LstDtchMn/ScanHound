# ScanHound — response to round 10 (media-kind stack)

**Repository:** `LstDtchMn/ScanHound`

Every round-10 finding was reproduced before it was fixed. Where I disagreed
with a proposed *mechanism* I have said so explicitly rather than quietly
substituting one.

## Finding status

```text
M1  CLOSED  (both halves)   -- client authority AND first-source-wins dedup
L1  CLOSED                  -- signature test now binds per call
L2  CLOSED                  -- migration test now builds an old schema
Q8  CLOSED                  -- False + season is no longer "False wins"
Q3  ACKNOWLEDGED            -- my review doc misattributed the strict reader
INFO (RSS movie identity)   -- ACCEPTED, documented, left fail-closed
```

## Heads

| PR | Branch | Head (round 10) | Head now |
|---|---|---|---|
| #89 | `fix/rss-history-keyed-on-release-url` | `c0b874a` | `c0b874a` (unchanged, approved) |
| #90 | `feat/record-media-kind-at-ingest` | `81c6c68` | **`3e94f34`** |
| #91 | `feat/consume-media-kind-in-ui` | `fb3c073` | **`08b0e75`** |
| #92 | `feat/queue-records-category` | `96a5505` | **`2a953cc`** |
| #93 | `fix/carry-is-tv-not-rederive` | `25d7f43` | **`ac10d02`** |

Base `main @ 3013556` throughout. #90 was merged forward into #91 and #91 into
#92 with ordinary merge commits — nothing was rebased or force-pushed, so the
delta since your round-10 read is visible in the history.

---

# M1 — CLOSED, both halves

**Your premise correction is the part I want to confirm I have understood**, because
it reframed the design rather than patching it:

```text
package provenance  !=  media-kind provenance
```

I had been treating `identity_source = provenance` as if link provenance also
certified the kind. It does not, and the stack now separates them.

## Half one — the client no longer supplies the answer

`DownloadService.verified_media_kind(url, client_category)`:

```text
server records a category  ->  that is the answer
client disagrees with it   ->  record NOTHING, and log BOTH values
server has no record       ->  record NOTHING
```

The client's value is only ever allowed to **contradict**, never to supply. Your
framing of why is the one I put in the code comment: a recognized wrong value is
worse than a missing one, because missing fails closed while wrong authorizes.

**One correction to the plan, found while implementing it.** The obvious server
source, `background_scan_cache.source_category`, is **not** the crawl category —
it holds the source *name*, `"HDEncode"` on every one of the 4,084 live rows. The
crawl category lives in the row's `data` JSON. Measured before relying on it:

```text
data.category present on   4084 / 4084 rows   (4k 2077, tv 1737, remux 270)
downloads with a scan row   611 /  664        (92%)
```

The other 53 downloads now record no kind. That is the fail-closed price.

Resolved **once** per `download_item` and consumed by all four history paths.
Four separate resolutions would be four chances for one to keep using the raw
client value — the defect class that let batched grabs drop the category
entirely until #92.

## Half two — the crawl no longer discards conflicting evidence

Confirmed in the code before changing anything:

```text
scanner_service.py:796   seen_post_urls = set()     one set
                  :812   for source in sources      spans every source
                  :951   if post_url in seen: continue
                  :689/:691/:693   4K (movie) -> Remux (movie) -> TV Packs (tv)
```

So a release in both a movie listing and TV Packs was recorded as a movie and
the TV listing was skipped with no trace. The post is still processed **once**;
what changed is that the disagreement is now recorded:

```text
crawl post  category_conflict=True
  -> details -> MediaItem -> cached row JSON
  -> get_scan_category() returns None
  -> verified_media_kind() answers None
```

Only the **type** axis counts. `4k` and `remux` both mean movie, so a collision
between them says nothing contradictory about the kind; marking it would make
the signal fire constantly and mean nothing.

## Where I substituted a mechanism — please rule on this

You proposed separating the wire:

```text
identity_source     = provenance
media_kind          = movie|tv|null
media_kind_source   = source_category|...
media_kind_conflict = true|false
```

**I did not add those fields.** I recorded the conflict at the crawl and made the
lookup decline, which produces the same refusal with no schema or wire change.

That is a real substitution, not an equivalent, and the difference matters in one
place: with my shape a consumer can see *that* no kind was recorded but not *why*
— conflict, unscanned URL, and client disagreement are indistinguishable
downstream. All three are logged server-side, but none is on the wire.

**If you want the reason on the wire, say so and I will add it.** I stopped short
because the fields are only useful once something consumes them, and nothing does
yet.

## What I did NOT do

No filename fallback. `seasonKey()` remains display-only, and
`isCanonicalSeasonName` stays deleted.

---

# #92 — L1 and L2, both reproduced first

## L1 — the signature test was narrower than its name

**You were right, and I verified it before fixing it.** Deleting the required
`url=` argument from the real `download_item` call left **all 9 tests green**,
while binding the real signature raises:

```text
TypeError: missing a required argument: 'url'
```

Same mock-vs-real failure class the test was written for, opposite direction.

Now binds `inspect.Signature` to **each** call separately, which is what the
interpreter does at runtime. Plus two tests proving the bind check itself
rejects a missing and an unknown argument — otherwise the check could rot into a
no-op without anything noticing.

I did not add `create_autospec`. The per-call bind covers both mutations you
named, and an autospec worker test would assert the same property one layer
further out. Say if you would still rather have it.

## L2 — the upgrade test tested the CREATE path

Also reproduced: pointing the ALTER at a different column name left **all 9
tests green**.

The fixture now builds the pre-change schema **by hand** — copied, not derived,
because deriving it from the current schema would restore exactly the tautology
— asserts `category` is absent first, then that opening it adds one, then that a
value can be written and read through it.

Both mutations now die: 1 test for L1, 2 for L2. 14 pass unmutated.

## Your note that #92 should persist the VERIFIED classification

The queue's stored `category` is still the raw request value, but it is now a
**hint**: verification happens at `download_item` time, so it can never become
`media_kind` without the server agreeing. That satisfies the safety property
without a schema change — but it is a different answer than you proposed, so
flagging it rather than assuming it closes the point.

---

# #93 — Q8, precedence changed

You were right that `False` is usually the *absence* of positive TV evidence,
not affirmative movie evidence, and that pinning "False wins" encoded the wrong
future precedence.

The matcher now reads:

```python
'is_tv': item.is_tv or item.season is not None
```

which is the same shape `_process_post` already uses to decide the value:

```python
is_tv = details.get('is_tv', False) or post_info['type'] == 'tv'
```

an OR of positive signals.

**This does not restore the original bug.** That bug was `season is not None`
*replacing* the recorded value, which lost every TV release whose season did not
parse. As an *additional* signal it can only turn unknown into TV, never TV into
film.

Mutation-tested, every branch load-bearing: recorded-only kills 1, season-only
(the original bug) kills 2, always-True kills 2.

---

# Q3 — you are right, and my review document was wrong

`list_plex_cache_movies_strict()` **already exists on base `3013556`**. It is not
introduced by #90's delta, and describing it as #90-owned in the round-10 request
was my error. Corrected in the document.

---

# INFO — RSS movie identity stays fail-closed

Accepted as stated. After the stack:

```text
RSS TV with a recorded season  ->  tv_season identity
RSS movie                      ->  media_kind NULL -> unknown
```

I am not passing RSS `media_type` through, for the reason you gave: the feed
parser derives it as *tv if a season parsed or a category contains "tv", else
movie*, so promoting it would reimport the "absence means movie" inference this
work exists to remove.

Now documented in `#89`'s code comments rather than left as folklore.

---

# Verification

```text
#90  3e94f34   full suite 35 failed / 5230 passed   == clean-main baseline
#92  2a953cc   full suite running at time of writing
#93  ac10d02   full suite 35 failed / 5217 passed   == clean-main baseline
```

The 35 are the disclosed pre-existing failures (32 network-dependent, plus
`test_source_hdencode`, `test_notifications`, `test_hdencode_off_switch`).

New tests: 18 for M1 (both halves), 14 for #92's contract and migration, 9 for
#93's precedence. Every one mutation-tested, including the over-broad direction
for the conflict detector — a detector that always fires is as useless as one
that never does.

---

# What I would most like challenged this round

1. **The mechanism substitution above** — conflict recorded at the crawl versus
   your explicit wire fields. I think they are equivalent for safety and not for
   diagnosis, and I would rather be told I am wrong about that now.
2. **`data.category` as the server's authority.** It is populated on all 4,084
   live rows, but it is still a crawl-time artifact stored as JSON in a cache
   table. If that cache is ever rebuilt from a source that does not carry the
   category, the kind silently becomes unknown fleet-wide. I have not built a
   guard for that.
3. **The 8% with no scan row.** 53 of 664 downloads have no cached scan row and
   now record nothing. I believe that is correct fail-closed behaviour, but it is
   a capability regression I chose rather than measured against user impact.
