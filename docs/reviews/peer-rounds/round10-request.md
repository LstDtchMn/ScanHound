# ScanHound Peer Review Request — Media-Kind Stack (Round 1)

**Repository:** `LstDtchMn/ScanHound`

This is a **three-PR stack**, reviewed bottom-up. Each PR's base is the one below
it, so each "Files changed" view shows only that PR's own delta.

| PR | Branch | Head | Base |
|----|--------|------|------|
| #89 | `fix/rss-history-keyed-on-release-url` | `c0b874a` | `main @ 3013556` |
| #90 | `feat/record-media-kind-at-ingest` | `81c6c68` | `fix/rss-history-keyed-on-release-url` |
| #91 | `feat/consume-media-kind-in-ui` | `92bc88a` | `feat/record-media-kind-at-ingest` |

Merge order is forced: **#89, then #90, then #91.** (#90 originally declared
`main` as its base, which made GitHub render #89's changes inside #90's diff and
would have auto-closed #89 unreviewed if #90 merged first. Corrected before
sending.)

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

**Smallest PR; independent of the other two.** It is in the stack only because
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

## Known gaps — disclosed, not hidden

**Batch grabs record no media kind.** `download_queue_items` has no `category`
column, so a batched grab reaches `save_to_history` with nothing to record. Those
rows get `media_kind = NULL` → identity unknown → the UI groups them but will not
authorize a destructive action on them.

This is **fail-closed**: the failure mode is "the feature is unavailable for
these rows", not "the feature destroys the wrong file." I chose to ship it that
way rather than widen the schema inside this stack.

**Question 6:** is fail-closed acceptable to merge, or should the `category`
column land before #91 goes in? Batched grabs are a meaningful share of real
usage, so shipping this way means the feature is partly dark on arrival.

**Pre-existing, not in this stack:** `backend/scanner_service.py:1539` recomputes
`'is_tv': item.season is not None` for the matcher, discarding the authoritative
value computed at line 1138. It is the same class of bug this stack exists to
fix — re-deriving a fact that was already known — but it is on a different code
path and I did not want to widen the diff. Flagged for its own PR.

---

## What I specifically want challenged

The premise. This stack asserts that **provenance beats parsing** for
authorization. If there is a case where the recorded kind is wrong and the
filename is right, the whole design authorizes on the worse evidence, and I would
rather hear that now than after something gets overwritten.
