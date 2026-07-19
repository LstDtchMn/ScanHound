# HDEncode RSS qualification — Claude findings

Run date: 2026-07-19. Probe authored by ChatGPT; executed and analysed by Claude.

Probe SHA-256 `e36a37f7d55ef3c82b6af9dc166832eaea2bbfbac5fe0e257c71b56f02c49e3c`
— **verified matching before execution**; `python -m py_compile` OK. Script
unmodified. Defaults untouched: serial execution, 2 s delay, 20 s timeout,
2 MiB body cap, HTTPS + host allowlist, DTD/entity rejection.

**Environment:** a throwaway container on ScanHound's own `proxy` Docker
network — same egress path as production, without executing inside the live
container. No ScanHound code, config, database, branch, or production setting
was touched. No article pages or download links fetched. No Selenium/Chromium.

Raw reports in this directory:
- `hdencode_rss_qualification.json`
- `hdencode_rss_qualification.md`
- `qualify_hdencode_rss.py` (the exact probe executed, for checksum re-verification)

---

## Verdict

# RSS QUALIFIED WITH NAMED LIMITATIONS

---

## 1. Pilot gate (Step 2)

`movies_2160p` + `tv_2160p`, both clean against all five conditions:

| Gate | Result |
|---|---|
| Real RSS/Atom, not an HTML challenge page | RSS 2.0, `application/rss+xml; charset=UTF-8` |
| Final redirect host approved | `hdencode.org`, 0 redirects |
| Entry links on approved domains | 100%, no off-allowlist hosts |
| No parser or safety errors | none |
| Request rate did not trigger blocking | both 200 |

Proceeded to the full matrix on that basis.

## 2. HTTP behaviour — all 13 feeds

Every feed: **HTTP 200**, **0 redirects**, requested URL identical to final URL,
`application/rss+xml; charset=UTF-8`, `Content-Encoding: identity`,
64–77 KB body. No 403, 429, 503, challenge page, or foreign-host redirect at
any point.

Caching:

- **No `ETag` on any feed.**
- `Last-Modified` present on **all 13**.
- Conditional `If-Modified-Since` returned **304 Not Modified on all 13**.
- No `Cache-Control`, no `Expires`, no `Age`. Only `Vary: Accept-Encoding`.

This is the most important operational result: steady-state polling costs one
conditional request and no body.

## 3. Feed structure

RSS **2.0** (`<rss version="2.0">`) on every feed. Generator
`https://wordpress.org/?v=7.0.2`. `language` `en-US`. No `<ttl>`.
`lastBuildDate` present and parseable everywhere.

**Exactly 50 entries per feed** — a hard item cap, not a time window. Live time
depth therefore varies by publication rate:

| Feed | Depth of the 50 live entries |
|---|---|
| `home` | **~7 hours** |
| `movies_all` | ~12 hours |
| `tv_all` | ~14 hours |
| `movies_1080p` | ~1 day |
| `tv_1080p`, `tv_720p`, `tv_webdl` | ~1–1.5 days |
| `movies_2160p`, `tv_2160p` | ~2 days |
| `movies_720p` | ~5 days |
| `movies_remux` | ~7 days |
| `movies_bluray_disc` | ~9 days |
| `tv_webrip` | **~46 days** |

## 4. Entry identity

| Property | Result |
|---|---|
| GUID coverage | **100%** on all 13 feeds |
| GUID uniqueness within feed | **100%** — 0 duplicates anywhere |
| GUID marked `isPermaLink` | **No** — attribute absent; value is the post URL |
| Link coverage | **100%** |
| Approved canonical link | **100%** |
| Duplicate canonical URLs within feed | **0** on all feeds |
| `.org` / `.com` / `.ro` mixing | **None observed** — every link `hdencode.org` |

Cross-feed: 650 entry rows, **138 canonical URLs in more than one feed**, and
**138 duplicate GUIDs** — the two counts agree exactly, so GUID and canonical
URL are interchangeable identity keys.

## 5. Metadata coverage

100% on every feed: `title`, `pubDate`, parseable `pubDate`, `categories`,
`description`, non-empty body.

- **`author` 0–6%** — unusable.
- **`content:encoded` 0%** — absent on all 650 entries.

Title-derived signals (percent of entries):

| Feed | year | season | resolution | size | HEVC | HDR | DV |
|---|---:|---:|---:|---:|---:|---:|---:|
| home | 82 | 16 | 100 | 100 | 22 | 12 | 10 |
| movies_all | 100 | 0 | 100 | 100 | 24 | 14 | 16 |
| tv_all | 30 | 48 | 100 | 100 | 16 | 4 | 6 |
| movies_2160p | 100 | 0 | 100 | 100 | 92 | 40 | 44 |
| movies_1080p | 100 | 0 | 100 | 100 | 4 | 4 | 4 |
| movies_720p | 100 | 0 | 100 | 100 | 0 | 0 | 0 |
| movies_remux | 100 | 0 | 100 | 100 | 40 | 22 | 32 |
| movies_bluray_disc | 100 | 0 | 94 | 100 | 46 | 24 | 20 |
| tv_2160p | 18 | 34 | 100 | 100 | 100 | 42 | 36 |
| tv_1080p | 36 | 34 | 100 | 100 | 6 | 0 | 0 |
| tv_720p | 30 | 54 | 100 | 100 | 0 | 0 | 0 |
| tv_webdl | 8 | 52 | 100 | 100 | 12 | 4 | 0 |
| tv_webrip | 70 | 30 | 100 | 100 | 0 | 0 | 0 |

**Resolution and size are 100% on every feed.** Year is 100% on movie feeds and
low on TV feeds (correctly — TV titles carry S/E instead).

Body-derived coverage is **highly variable** and must not be assumed:

| Feed | filename | filesize | duration | video codec | frame rate | audio | subtitle |
|---|---:|---:|---:|---:|---:|---:|---:|
| tv_webrip | 96 | 88 | 88 | 70 | 70 | 74 | 8 |
| tv_webdl | 90 | 74 | 64 | 48 | 48 | 48 | 26 |
| tv_1080p | 84 | 82 | 72 | 66 | 66 | 66 | 32 |
| tv_2160p | 68 | 68 | 68 | 66 | 66 | 66 | 20 |
| movies_all | 26 | 14 | 12 | 12 | 6 | 6 | 0 |
| movies_2160p | 10 | 6 | 4 | 0 | 0 | 0 | 0 |
| **movies_bluray_disc** | **0** | **0** | **0** | **0** | **0** | **0** | **0** |

## 6. Rich-body determination

Detailed technical metadata is stored in **`<description>`**. `body_source` was
`description` for **all 650 entries**; `content:encoded` never appeared, and
Atom `<content>` does not apply (no Atom feeds).

Sanitized structural example — element names and field lengths only:

```
title(76) link(96) comments(104) creator(0) pubDate(31)
category(13) category(8) guid(96) description(548) commentRss(101) comments(1)
```

Descriptions run roughly 360–610 characters: a compact spec block, not article
prose.

## 7. Independent spot checks (Step 5)

Raw XML re-fetched for three feeds using the probe's own `request_feed` /
`validate_host_and_ip`, so the same allowlist, IP validation, size cap and DTD
rejection applied.

| Sample | Movie/TV | Title | Year | S/E | Res | Size | Codec | HDR | DV |
|---|---|---|---|---|---|---|---|---|---|
| `The.Westies.S01E03…2160p.AMZN.WEB-DL…` | TV ep | Y | – | S01E03 | Y | Y | Y | – | – |
| `Life.Larry…S01E04…DoVi.HDR.H.265` | TV ep | Y | – | S01E04 | Y | Y | Y | Y | Y |
| `Heartstopper.S03.DV.2160p.WEB.h265-BETTY – 37.7 GB` | TV pack | Y | – | S03 | Y | Y | Y | – | Y |
| `Heartstopper.S02.DV.2160p…` | TV pack | Y | – | S02 | Y | Y | Y | – | Y |
| `Mortal.Kombat.II.2026.2160p.UHD.BluRay.HDR10+.DoVi…` | movie | Y | 2026 | – | Y | Y | Y | Y | Y |
| `Scary.Movie.2026.DV.2160p.WEB.h265-ETHEL` | movie | Y | 2026 | – | Y | Y | Y | – | Y |
| `The.Devil.Wears.Prada.2.2026…Blu-ray.Remux.HEVC.DV.HDR10P…` | movie | Y | 2026 | – | Y | Y | Y | Y | Y |
| `Soylent.Green.1973.2160p.UHD.Blu-ray.Remux.HEVC.DV.FLAC.1.0-HDT – 61.4 GB` | movie | Y | 1973 | – | Y | Y | Y | – | Y |

**Feed-only data is sufficient** for movie-vs-TV, title, year, season/episode,
resolution, size, codec, HDR type and Dolby Vision on every sample inspected.
**Plex / download-history comparison can be performed without loading the post
page** — canonical URL and GUID are both stable keys and the title carries the
full quality tuple.

### Defect in Claude's spot-check classifier (not the feed, not the probe)

My first classifier reported `movie` for three obvious `S01E03` episodes. The
precedence was wrong: `\bS\d{1,3}\b` does not match `S01E03` because of the word
boundary, so testing season before episode misclassifies every episodic entry.

**Implementation requirement:** test `S\d+E\d+` **first**, then bare `S\d+` for
season packs. This belongs in the parser's test suite as an explicit case.

## 8. Coverage and overlap recommendation

**Smallest sufficient set: `movies_all` + `tv_all`, plus `movies_remux` and
`movies_bluray_disc` for depth.**

- **`movies_all` vs individual movie-quality feeds** — the quality feeds are
  subsets. 138 cross-feed duplicates confirm heavy overlap; sampled memberships
  are consistently `['movies_1080p','movies_all']`,
  `['home','movies_720p','movies_all']`, etc.
- **`tv_all` vs individual TV-quality feeds** — same, e.g.
  `['tv_1080p','tv_webdl']`, `['tv_720p','tv_webdl']`.
- **`home` vs movies+TV** — drop it. Shallowest depth of all (~7 h) and a
  strict subset.
- **Season packs are present in the ordinary TV feeds** (`Heartstopper.S03…`
  appears in `tv_2160p`). No separate season-pack feed needed.
- **Remux / WEB-DL / WEBRip / Blu-ray Disc do not need separate feeds for
  coverage.** Keep `movies_remux` (~7 d) and `movies_bluray_disc` (~9 d) purely
  as **depth insurance** — `movies_all` only reaches back ~12 h, so after any
  outage longer than that they are the catch-up path. `tv_webrip` (~46 d) is a
  cheap optional backfill.
- **Quality feeds add no unique releases** in this snapshot — they re-slice the
  all-feeds. Their value is time depth, not new content.

Because of the hard 50-entry cap and `movies_all`'s ~12 h depth, poll interval
must stay well inside that window. Conditional 304s make frequent polling cheap.

## 9. Schema justification

**Justified NOT NULL** (100% observed on all 13 feeds):
`guid`, `canonical_url`, `title`, `pub_date`, `feed_key`, `resolution`,
`size_text`.

**Must remain nullable / optional:**
- `year` — absent on most TV entries
- `season`, `episode` — absent on movies
- `hevc`, `hdr`, `dv` — **absent ≠ false**; 720p feeds are legitimately 0%
- all body-derived fields (`filename`, `filesize`, `duration`, `video_codec`,
  `frame_rate`, `audio`, `subtitles`) — `movies_bluray_disc` is 0% across every
  one of them

**Do not model:** `author` (0–6%), `content:encoded` (0%), `ETag` (absent —
use `Last-Modified` + `If-Modified-Since`).

**Candidate states justified by observed data:** `discovered` (GUID + canonical
URL + pubDate always present), `parsed` (title tuple always yields resolution +
size), `enriched` (body fields — must tolerate total absence).

## 10. Named limitations

1. **No `ETag`** — conditional requests must use `Last-Modified`.
2. **Hard 50-entry cap**; `movies_all` depth ~12 h. Poll faster than the
   shallowest subscribed feed or miss releases.
3. **Rich-body coverage ranges 0–96% by feed.** `movies_bluray_disc` supplies
   none. Any body-dependent feature must degrade gracefully.
4. **DV/HDR must be treated as unknown-when-absent**, never false.
5. **Single snapshot.** Depth figures reflect one moment's publication rate and
   should be re-measured before poll intervals are finalised.
6. `guid` is not flagged `isPermaLink`; it is the post URL in practice, but that
   is convention here, not a declared guarantee.
