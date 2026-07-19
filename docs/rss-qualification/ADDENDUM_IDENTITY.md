# RSS qualification addendum — identity, metadata and size fields

Follow-up to `FINDINGS.md`, prompted by the question: can the feed supply title,
year, IMDB/TMDB identity, DV/HDR status and total file size?

Same constraints as the original run: read-only, probe's own
`request_feed`/`validate_host_and_ip` reused (host allowlist, IP validation,
2 MiB cap, DTD rejection), serial with 2 s delay, no article or download pages,
no Selenium. Sample: `movies_all` + `tv_all`, 50 entries each.

---

## 1. Answer per requested field

| Requested | Available | Coverage | Source |
|---|---|---|---|
| Title (release name) | yes | 100% | `<title>` |
| Clean title | yes | ~86% movies | `<description>` head |
| Year | yes | 100% movies (title) / 92% (description) | both |
| **IMDB / TMDB id** | **NO** | **0 / 100** | — |
| DV / HDR / HDR10+ | partial, tri-state | 44% / 40% asserted on 2160p | `<title>` |
| Total file size | yes | **100%** | `<title>` trailing ` – NN GB` |

## 2. There are no identity IDs — confirmed negative

Searched `<title>`, `<description>` and `<category>` across 100 entries for:

    \btt\d{7,9}\b      imdb.com      themoviedb.org / tmdb
    thetvdb / tvdb     trakt.tv      rottentomatoes

**Zero matches of any kind.** RSS cannot supply an identity key. TMDB/IMDB
resolution remains mandatory downstream. This is a hard constraint, not a
coverage percentage.

## 3. The description is a metadata block, not prose

Structure observed (this is the real content of one entry, quoted only as far
as needed to show the shape):

    Beginners 2011 ⭐ 7.2/10 — 98K votes
    Feel-Good Romance, Quirky Comedy, Comedy, Drama, Romance
    Oliver, a graphic designer, is attracted to a free-spirited …
    Ewan McGregor  Oliver Fields   Christopher Plummer  Hal Fields   Mélanie Laurent

Fields recoverable: **clean title**, **year**, rating (`N.N/10`), vote count,
genre list, plot synopsis, **cast names paired with character names**.

Coverage:

| Signal | movies_all | tv_all |
|---|---:|---:|
| rating block (`N.N/10`) | 43/50 | 23/50 |
| vote count | 43/50 | 23/50 |
| multi-name cast text | 40/50 | 17/50 |
| near-empty description | 0/50 | 0/50 |

Practical consequence: the description gives a materially better TMDB query
input than the dotted release name, and the cast list can feed the existing
deterministic cast/director matcher without OCR or vision.

## 4. Title year and description year disagree — 14% of movie entries

    movies_all:  agree=39   DISAGREE=7   title-only=4   desc-only=0
    tv_all:      agree=0    DISAGREE=0   title-only=15  desc-only=12  neither=23

Observed disagreements:

    title 2010 / desc 2011   'Beginners'
    title 2023 / desc 2024   'Woman of…'   (x2, 1080p and 720p releases)

`Beginners` is genuinely a 2011 film with a 2010 festival run. The description
year is the metadata year; the title year is whatever the release group encoded.
Querying TMDB with the title year on those entries searches the wrong year.

**Requirement:** persist `title_year` and `desc_year` as separate nullable
fields, prefer `desc_year` for lookup, fall back to `title_year`, and set a
`year_conflict` flag rather than silently choosing. TV entries frequently have
only one of the two (or neither) — the flag must tolerate absence.

## 5. `TV-Packs` category is a perfect season-pack marker

    category "TV-Packs"        : 24
    regex S\d+ without E\d+     : 24
    agreement                   : 24 / 24
    category-only (regex missed): 0
    regex-only (no category)    : 0

Exact agreement in both directions. This is the site's own classification and
should be the **primary** season-pack signal, with the regex retained as a
cross-check — a future divergence is then a detectable parser bug rather than a
silent misclassification. It also sidesteps the `S\d+E\d+`-before-`S\d+`
precedence trap entirely for the pack case.

Other category content:

- `movies_all`: `Movies` + `Uncategorized` on every entry — **no signal**.
- `tv_all`: `TV-Shows` + `Uncategorized`, plus show-name categories
  (`Interview.with.the.Vampire` 7, `FIFA.World.Cup` 6, `Formula1` 5,
  `Wrestling` 4, `MMA` 3, …). Useful but inconsistent; not a reliable series key.

## 6. File size is 100% reliable

Every title in both feeds carries a trailing size, in GB or MB:

    Beginners.2010.2160p.UHD.BluRay.x265-B0MBARDiERS – 28.2 GB
    FIFA.World.Cup.2026.Final.Halftime.Show.1080p.HDTV.H264-DARKFLiX – 651.7 MB

50/50 in `movies_all`, 50/50 in `tv_all`. The separator is an en dash (`–`),
not a hyphen — a parser matching only `-` will miss every entry. Match both, and
handle MB as well as GB.

## 7. Suggested additions to the foundation candidate schema

All nullable, all monitored:

    clean_title        text     from description head
    desc_year          int      metadata year
    title_year         int      release-name year
    year_conflict      bool     desc_year != title_year, both present
    genres             text[]   from description
    cast               text[]   name/character pairs
    rating             real     N.N/10
    vote_count         int
    is_season_pack     bool     from TV-Packs category (regex as cross-check)
    size_bytes         int      normalised from the en-dash suffix
    size_text          text     as published

Nothing here is required-NOT-NULL; the rating block alone is absent on 7/50
movie and 27/50 TV entries.
