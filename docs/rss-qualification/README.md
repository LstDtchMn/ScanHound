# HDEncode RSS qualification — index

Complete, self-contained package for the RSS-first discovery design. Read in
order:

1. **FINDINGS.md** — the qualification: HTTP behaviour, feed structure,
   identity, coverage, rich-body, spot checks, feed-set recommendation, schema
   justification, named limitations, and the verdict
   (RSS QUALIFIED WITH NAMED LIMITATIONS).
2. **ADDENDUM_IDENTITY.md** — no IMDB/TMDB ids (confirmed), the `<description>`
   metadata block (clean title, cast, rating, genres), title-vs-description
   year conflicts, the `TV-Packs` season-pack marker, the size en-dash trap.
3. **ADDENDUM_FIELD_PARITY.md** — field-by-field parity vs the existing scraper
   (14/18 fields available or better), language/audio/subtitle and other new
   fields, the WordPress excerpt-truncation finding, and the RSS-first /
   fetch-only-when-necessary policy.
4. **ADDENDUM_DV.md** — Dolby Vision / HDR depth: no layer info exists (host
   detector stays mandatory), three-state DV semantics, the Compare/conflict
   safety rule, and the HDR10P/`\bDV\b` parser traps.

Raw artifacts:
- `hdencode_rss_qualification.json` / `.md` — the full 13-feed probe output.
- `qualify_hdencode_rss.py` — the exact probe executed
  (SHA-256 `e36a37f7d55ef3c82b6af9dc166832eaea2bbfbac5fe0e257c71b56f02c49e3c`).

Four naming traps confirmed from live data, all belong in the parser's
regression suite:
- `S\d+E\d+` before bare `S\d+` (episode before season pack)
- size separator is en dash `–` (U+2013), not `-`
- HDR10+ has two spellings: `HDR10\+` and `\bHDR10P\b`
- `DV` needs `\bDV\b`; also match `DoVi` and `Dolby.Vision`

Nothing here changes production. No routine live feed requests should be added
to CI — the foundation is testable entirely with fixtures.
