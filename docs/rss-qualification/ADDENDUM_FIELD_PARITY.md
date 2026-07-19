# RSS qualification addendum 2 — field parity with the existing scraper

Question: can RSS supply everything `DetailScraper.scrape_details()` currently
returns, so the scraper is used only when necessary?

Method: read the real contract (`backend/models.py:37` `ScrapeResult`, 18 fields)
and diff it field-by-field against observed feed data. Same read-only
constraints throughout.

---

## 1. Field parity map

`ScrapeResult` fields versus RSS availability:

| # | Scraper field | RSS | Source | Coverage |
|---|---|---|---|---|
| 1 | `display_title` | yes | description head | ~86% movies |
| 2 | `year` | yes (×2) | title + description | 100% / 92% movies |
| 3 | `rating` | yes | description `N.N/10` | 43/50 mv, 23/50 tv |
| 4 | `search_key` | yes | derived from `display_title` | — |
| 5 | `url` | yes | `<link>` | 100% |
| 6 | **`imdb_link`** | **NO** | — | **0 / 100** |
| 7 | **`imdb_id`** | **NO** | — | **0 / 100** |
| 8 | `size` | yes | title trailing ` – NN GB` | **100%** |
| 9 | `res` | yes | title | **100%** |
| 10 | `hdr` | partial | title, tri-state | 40% asserted @2160p |
| 11 | `dovi` | partial | title, tri-state | 44% asserted @2160p |
| 12 | `tmdb_votes` | **better** | description vote count | scraper hardcodes `"-"` |
| 13 | `is_tv` | **better** | feed origin (definitive) | 100% |
| 14 | `season` | yes | title + `TV-Packs` category | 100% of packs |
| 15 | `episode_number` | yes | title `SxxEyy` | — |
| 16 | `episodes` | partial | per-episode blocks when untruncated | variable |
| 17 | `posted_date` | yes | `<pubDate>` | **100%** |
| 18 | `multi_episode_hint` | **NO** | needs full page body | — |

**14 of 18 available or better. 2 tri-state. 2 genuinely require the page.**

Two fields are *better* from RSS than from scraping:
- `tmdb_votes` — the scraper returns a literal `"-"`; the feed carries real
  vote counts.
- `is_tv` — currently inferred; from RSS the feed of origin is definitive.

## 2. Fields RSS provides that the scraper never captured

From the MediaInfo block inside `<description>`:

    Filename......: Declaration.2026.S01E01.The.Road.to.Independence.1080p.WEBRip.x264-CBFM.mkv
    FileSize......: 1.46 GiB
    Duration......: 57 min 51 s
    Video.........: AVC H264 | High@L4.1 | 1920x1080 @ 3 473 kb/s
    Audio.........: English | AAC LC - AAC | 2 CH @ 128 kb/s
    Subtitle......: English.

New signals, none of which exist in `ScrapeResult`:

| Field | Notes |
|---|---|
| **audio language** | `English` — the requested "language" field |
| **subtitle language** | separate from audio |
| audio codec / channels / bitrate | `AAC LC \| 2 CH @ 128 kb/s` |
| exact pixel resolution | `1920x1080`, not just the `1080p` class |
| video profile / level | `High@L4.1` |
| video bitrate | `3 473 kb/s` |
| duration | `57 min 51 s` |
| **per-episode filename + size** | inside season packs |
| genres | from the metadata head |
| cast + character names | feeds the existing deterministic matcher |
| plot synopsis | — |

Language coverage (feeds where MediaInfo survives best):

    tv_webrip : Audio 37/50  Channels 35  Subtitle 4   Bit rate 35  Frame rate 35
    tv_1080p  : Audio 33/50  Channels 33  Subtitle 16  Bit rate 33  Frame rate 33

## 3. Why body coverage varies 0–96% — it is excerpt truncation

Descriptions are **WordPress excerpts**, CDATA-wrapped and cut at a fixed
length. Every one ends `[&#8230;]` (`[…]`). The Declaration example truncates
*mid-MediaInfo*:

    Filename......: …S01E02.The.Road.to.Democracy…mkv
    FileSize......: […]

So the 0–96% spread is **not** posts lacking MediaInfo. It is whether the cast
list and plot synopsis consumed the excerpt budget before the MediaInfo block
was reached. `movies_bluray_disc` scores 0% because those posts front-load long
cast/plot text.

Design consequence: MediaInfo presence is **not predictable per feed** and must
never be assumed. Detect truncation explicitly (trailing `[…]`) and treat a
truncated description as "MediaInfo incomplete", distinct from "MediaInfo
absent".

## 4. Confirmed absence of identity IDs — byte-level

Searched the **raw feed bytes**, not parsed text, in case extraction was
dropping markup:

    imdb (any case) : 27 matches  -> all plain text label "IMDb"
    \btt\d{7,9}\b   : 0
    imdb.com URL    : 0
    themoviedb      : 0
    <a href         : 0        <- description CDATA contains no links at all
    CDATA           : 200

`IMDb ×` is a label plus a glyph carried into the excerpt, not a link. There is
no IMDb or TMDB identifier in the feed by any route.

## 5. Recommended RSS-first policy

Use RSS for **everything except** the four cases below; fall back to a single
page fetch only when one is actually triggered.

**Always RSS (no page fetch):**
- discovery, dedupe, identity-by-canonical-URL
- title, year(s), resolution, size, posted date
- season / episode / season-pack classification
- HDR / DV / HEVC claims (tri-state)
- rating, votes, genres, cast, plot
- language, audio, subtitle, duration, bitrate, exact resolution *when the
  excerpt is untruncated*

**Fetch the page only when:**
1. **An IMDb ID is required** and title+year+cast did not resolve confidently
   against TMDB. This is the main remaining reason to scrape.
2. **A multi-episode hint is needed** — combined/split detection reads the full
   page body.
3. **The description is truncated** *and* the missing MediaInfo actually matters
   for the decision at hand (e.g. per-episode sizes for a season pack).
4. **Actually grabbing** — download links are never in the feed.

Expected effect: page fetches drop from "every listed item" to "items that are
ambiguous, multi-episode, truncated-and-needed, or being downloaded." Cases 1–3
should be a minority of items; case 4 is bounded by what the user chooses to
download.

## 6. Schema additions implied

Beyond addendum 1, all nullable and monitored:

    audio_language      text
    subtitle_language   text
    audio_codec         text
    audio_channels      text
    audio_bitrate       text
    video_profile       text
    video_bitrate       text
    pixel_resolution    text     e.g. 1920x1080
    duration_text       text
    episode_files       json     per-episode filename + size for packs
    description_truncated bool   trailing […] detected
    mediainfo_complete  bool     block present AND not truncated

`description_truncated` and `mediainfo_complete` are the important pair — they
are what let the system decide whether a page fetch would actually add anything,
rather than fetching blindly or assuming absence means "no data exists".
