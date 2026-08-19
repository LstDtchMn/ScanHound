# ScanHound Peer Review Request — Media-Kind Stack (Round 1)

**Repository:** `LstDtchMn/ScanHound`

This is a **four-PR stack**, reviewed bottom-up. Each PR's base is the one below
it, so each "Files changed" view shows only that PR's own delta.

| PR | Branch | Head | Base |
|----|--------|------|------|
| #89 | `fix/rss-history-keyed-on-release-url` | `c0b874a` | `main @ 3013556` |
| #90 | `feat/record-media-kind-at-ingest` | `81c6c68` | `fix/rss-history-keyed-on-release-url` |
| #91 | `feat/consume-media-kind-in-ui` | `fb3c073` | `feat/record-media-kind-at-ingest` |
| #92 | `feat/queue-records-category` | `96a5505` | `feat/consume-media-kind-in-ui` |

Merge order is forced: **#89, then #90, then #91, then #92.**

## Three corrections since this document was first sent

**#90's base was wrong.** It declared `main` while actually sitting on top of
#89, so GitHub rendered #89's changes inside #90's diff and would have
auto-closed #89 unreviewed if #90 merged first.

**#91 was not really stacked.** It declared #90 as its base but was branched
from `main` independently, so it could not build against the backend it
consumes. I only found this when work on top of it raised `TypeError` against
the real `DownloadService`. #90 has since been merged into #91 (`fb3c073`) and
the stack is now genuinely linear.

**#91's diff was 90% noise.** Its first commit rewrote every line of
`DetailPanel.svelte` from LF to CRLF, rendering as 520 insertions / 519
deletions for a **three-line** change. A follow-up commit restores LF; #91 is
now 114 insertions / 18 deletions rather than 631. If you pulled before that,
re-pull.

---

## Why this stack exists

The Downloads page can perform a **destructive** action: overwrite/trash one
release with another it believes is the same thing. Until now, the decision of
"are these the same thing?" was made in the **frontend, from the filename**, by a
113-line regex called `isCanonicalSeasonName`.

That is the wrong place and the wrong evidence. The backend already knows what
the user clicked — a **TV** grab, a **4K** grab, a **Remux** grab — and then
threw that knowledge away, forcing the UI to re-derive it from a string.

This stack moves the authorization evidence from *a guess about a filename* to
*a fact recorded at grab time*.

---

## #89 — RSS grabs: key history on the release url, not each file-host link

**Smallest PR; independent of the other three.** It is in the stack only because
#90 was branched from it.

`hdencode_action_service` wrote one history row per **file-host link**. A release
posted with 8 mirrors produced 8 history rows for one grab, which inflated every
count derived from history and made "have I already grabbed this?" answer wrong.

History is now keyed on `action.get("canonical_url")` — the release page — so one
grab is one row regardless of how many mirrors the post carries.

**Question:** is `canonical_url` the right identity for a release, or should this
key on something more stable that survives a site changing its URL shape?

---

## #90 — Record what kind of thing a download is, instead of inferring it

Adds a `media_kind` column to `downloads`, populated at grab time from the
category the user actually clicked:

```python
_CATEGORY_MEDIA_KIND = {"tv": "tv", "4k": "movie", "remux": "movie"}
```

and puts a **semantic identity** on the wire for each download result:

```text
identity_kind    tv_season | movie
identity_title
identity_year
identity_season
identity_source  provenance
```

### The contracts I want reviewed

The annotator in `backend/download_links.py` is deliberately **strictly
additive** — it can only ever turn *unknown* into *known*, never change one known
answer into a different one:

- **season present → `tv_season`**, regardless of recorded kind. This is what
  keeps the 42 TV identities that already existed from all dropping to unknown
  the moment this ships. My first version required `media_kind` for TV too, and
  it would have silently blanked every one of them.
- **`kind == "movie"` with a season → contradictory → emit nothing.** Two
  sources disagree; I would rather answer "unknown" than pick a winner.
- **movie with no year → unknown.** A movie's identity here is title+year; a
  bare title is not enough to authorize destroying a file.
- **kind not recorded → unknown.** No inference from the title.

Also in this PR: `list_plex_cache_movies_strict()` — a read that **raises**
rather than returning `[]`, because *an empty read is not a successful read*, and
the caller was about to use emptiness as evidence.

### Questions

1. Is "the category the user clicked" authoritative enough evidence to
   authorize a **destructive** overwrite later? It is a real user action rather
   than a parse, but it is recorded once and never re-validated against the file.
2. Is the contradictory case (`movie` + season) right to answer *unknown*? The
   alternative — trust the season, since it is the more specific signal — is
   defensible and would yield more identities.
3. The strict/soft reader pair share one SQL constant but keep different error
   contracts deliberately. Is that seam clear enough to survive someone editing
   one of them?

---

## #91 — Send the media kind on grab, and consume it in the UI

**Deletes `isCanonicalSeasonName` and its 113 lines.**

The key move is splitting one concept into two, because the old code used a
single parser for both jobs and had to compromise between them:

- **Display grouping stays permissive.** `seasonKey()` still groups loosely, so
  the UI keeps showing related releases together even when it cannot prove they
  match. Being wrong here costs nothing — it is a visual grouping.
- **Destructive authorization becomes narrow**, and reads only from the wire:

```ts
export function semanticGroupKey(r: DownloadResult): string | null {
  if (r.identity_source !== 'provenance') return null;
  const kind = r.identity_kind;
  if (kind !== 'tv_season' && kind !== 'movie') return null;
  ...
}
```

A card authorizes destructive action only when **every** item in it has a
non-null semantic key. Anything unproven falls back to the legacy grouping,
which groups but does not authorize.

### Questions

4. Is `identity_source !== 'provenance'` the right gate, or is it too strict —
   should a weaker source be allowed to authorize, given the fallback is
   "user cannot use the feature at all"?
5. Is deleting `isCanonicalSeasonName` outright correct, versus keeping it as a
   fallback for items with no identity? I removed it because a fallback that
   authorizes is the exact hazard this stack is closing — but that does mean a
   real capability regression for un-annotated rows.

---

## #92 — Record the media kind for batched grabs too

This closes what the first send disclosed as an accepted gap. **I was wrong to
accept it.**

I said batched grabs losing their kind was a fair trade because it fails closed.
Then I measured it: **398 items have completed through the queue**, and not one
of them could ever be dupe-compared. That is not a corner case, it is most of
the feature.

The queue normalised every request through `_request_dict`, which dropped
`category`, so `download_item()` was called without it. #92 carries it end to
end: stored on the queue item, forwarded by the worker, and actually sent by the
frontend. The migration is placed **after** the CREATE, not in the shared
`_column_migrations` list — that list runs first, so an ALTER there fails with
"no such table" and the guard leaves the column silently absent.

Two things surfaced while building it that are worth your attention:

- **Two of the three frontend batch callers already passed a `category`, and
  `downloadBatch` dropped it on the way out.** The wire had a field nobody
  filled. Same defect class as the whole stack, one layer up.
- **I had the queue passing `category=` to a `DownloadService` that did not
  accept it yet.** Every mocked test was green, because `MagicMock` accepts any
  keyword. Production would have raised `TypeError` on the first queued grab.

For the second, #92 adds a test that inspects the **real** signature of
`download_item` and compares it against the kwargs the queue actually sends,
read from source by AST rather than restated by hand.

**Question 6:** please check whether that test is as strong as I think it is. It
is the only thing standing between a mock and a runtime failure, and I wrote it
immediately after being fooled by exactly that gap — which is not the state of
mind that produces good adversarial tests.

---

## Known gaps — disclosed, not hidden

**Pre-existing, not in this stack:** `backend/scanner_service.py:1539` recomputes
`'is_tv': item.season is not None` for the matcher, discarding the authoritative
value computed at line 1138. It is the same class of bug this stack exists to
fix — re-deriving a fact that was already known — but it is on a different code
path and I did not want to widen the diff. Flagged for its own PR.

**Unmeasured:** `media_kind` is recorded from the category at grab time and never
re-validated against the file that actually arrives. If a source mislabels a
release, the wrong kind is recorded permanently and authorizes on that basis.

---

## What I specifically want challenged

The premise. This stack asserts that **provenance beats parsing** for
authorization. If there is a case where the recorded kind is wrong and the
filename is right, the whole design authorizes on the worse evidence, and I would
rather hear that now than after something gets overwritten.

---

# Added after the first send: #93

**#93 `fix/carry-is-tv-not-rederive`** — head `25d7f43`, base `main @ 3013556`.
**Independent of the stack above**, so review it in any order.

Same defect class, different code path — a fact that was already known, then
re-derived worse downstream.

`_process_post` settles whether a release is television properly:

```python
is_tv = details.get('is_tv', False) or post_info['type'] == 'tv'
```

The matcher then asked a different question:

```python
'is_tv': item.season is not None,
```

**A complete-series pack is television with no season number**, and so is any TV
release whose season the title regex failed to parse. Each one answered `False`
and was routed to `find_movie_matches` — compared against the film library.
`rematch_cache` runs that loop over every cached row; there are 4,068 live.

The authoritative value was already in scope: `_create_media_item` receives it
and both callers set it. It was never read onto the item.

**Cached rows fall back to the old derivation, not to `False`.** Every row on
the live instance predates the field; defaulting them `False` would route every
cached TV item to the movie matcher, which is worse than the bug.

## Questions

7. Is `MediaItem.is_tv` the right home, versus deriving from the existing
   `category` field (`'tv'` / `'4k'` / `'remux'`)? Category is already carried
   and is what #90 uses for `media_kind`, so there are now two adjacent
   answers to "is this a show" and I am not certain they cannot diverge.
8. The back-compat fallback keys on `'is_tv' in d` rather than truthiness, so a
   row that recorded `False` stays `False` even with a season present. Is that
   the right call, or should a stored `False` with a season be treated as
   suspect rather than authoritative?

## Verification

Seven tests on the axis the bug is on — TV with no season. Mutation-tested:
restoring `item.season is not None` fails exactly 2; defaulting old cache rows
to `False` fails 1; unmutated passes 7. The three "this already worked" cases
keep passing under the first mutation, which confirms they carry no proof.

Full suite **35 failed / 5217 passed** — identical to the `main` baseline.

One note worth your scrutiny: the first suite run showed a 36th failure,
`test_all_expected_field_names`. That is a deliberate field-inventory tripwire
and adding a field is supposed to trip it. I updated the inventory rather than
weakening the test — but that is exactly the move that would also hide a real
regression, so it is worth checking I did not.
