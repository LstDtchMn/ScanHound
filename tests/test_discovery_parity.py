"""Field-level parity between ScanHound's two HDEncode discovery paths.

Plan item 0.1 (R6), scoped by the 2026-08-01 A4 structural finding.

ScanHound reads the same source two ways, and the two readers share no code:

    RSS path      backend/sources/hdencode_feed_parser.py :: parse_release_title
    listing path  backend/sources/base.py :: SourceBase.extract_* (used by
                  backend/sources/hdencode.py:193-194)

Resolution, size, year and season are therefore each derived twice, by two
independent implementations, from the same release title. Nothing in the system
compares them -- `compare_shadow` reduces both sides to URL sets and stops
there. That is the gap A4 names, and it is not hypothetical: the full-disc [BD]
defect was exactly an asymmetry between these two readers, and it hid full-disc
releases for months.

This module is the missing comparison. It does NOT establish A4 -- decision
equivalence needs a listing-side decision object that does not exist yet. It
establishes that the *inputs* to the decision agree, which is necessary and not
sufficient.

Two kinds of test below:

  * parity tests, which must pass, guarding the semantics both readers agree on;
  * `xfail(strict=True)` tests, one per divergence measured on 2026-08-01. They
    document a real defect, prove it still exists, and turn RED the moment
    someone fixes it -- at which point the marker comes off. A plain skip or a
    baked-in "expected" value would let these rot silently.
"""

import pytest

from backend.sources.base import SourceBase
from backend.sources.hdencode_feed_parser import parse_release_title


# ───────────────────────────── the two readers ──────────────────────────────

def listing_fields(title: str) -> dict:
    """Mirror of what backend/sources/hdencode.py derives per listing item.

    Kept as a thin transcription of `SourceBase.extract_*` rather than calling
    the scraper, so the comparison needs no network, no HTML and no browser.
    """
    res_match = SourceBase.RESOLUTION_PATTERN.search(title)
    resolution = None
    if res_match:
        raw = res_match.group().upper()
        resolution = "4K" if raw in ("2160P", "UHD") else (
            raw.lower() if raw != "4K" else raw)

    year_match = SourceBase.YEAR_PATTERN.search(title)
    season_match = SourceBase.SEASON_PATTERN.search(title)
    size_match = SourceBase.SIZE_PATTERN.search(title)

    size_gb = None
    if size_match:
        value = float(size_match.group(1))
        size_gb = {"MB": value / 1024, "GB": value, "TB": value * 1024}.get(
            size_match.group(2).upper())

    return {
        "resolution": resolution,
        "year": int(year_match.group()) if year_match else None,
        "season": int(season_match.group(1)) if season_match else None,
        "size_gb": size_gb,
    }


def rss_fields(title: str) -> dict:
    signals = parse_release_title(title)
    return {k: signals.get(k) for k in ("resolution", "year", "season", "size_gb")}


def canonical_resolution(value):
    """Fold the two spellings of UHD onto one token.

    The readers genuinely disagree on spelling -- RSS emits '2160p', the
    listing path emits '4K' -- but they mean the same thing, so comparing them
    raw would report a semantic difference that does not exist. The spelling
    split is itself a hazard and is covered by its own test below; it is the
    same defect shape that made 242 of 395 4K movies unreachable through the
    frontend's 4K chip on 2026-07-30.
    """
    if value is None:
        return None
    folded = str(value).strip().upper()
    return "UHD" if folded in {"4K", "2160P", "UHD"} else folded


# ───────────────────────────────── corpus ───────────────────────────────────
# Real release-title shapes. Sizes are in the trailing position HDEncode uses.

AGREEING_CORPUS = [
    "The Batman 2022 1080p BluRay x264-SPARKS 14.7 GB",
    "Some Show S01E02 1080p WEB-DL DDP5.1 H.264-NTb 2.1 GB",
    "Another Series S03 1080p WEB-DL DD+5.1 H.265-EDITH 18.9 GB",
    "Old Film 1975 720p BluRay x264-AMIABLE 4.4 GB",
    "Mini Doc 2020 1080p WEBRip 850 MB",
    "Dune Part Two 2024 2160p UHD BluRay REMUX DV HDR10+ HEVC TrueHD 7.1 Atmos-GRP 82.4 GB",
    "Dune Part Two 2024 4K UHD BluRay x265-TERMINAL 61.2 GB",
    "Doc Feature 2023 UHD BluRay 55.1 GB",
]


@pytest.mark.parametrize("title", AGREEING_CORPUS)
@pytest.mark.parametrize("field", ["resolution", "year", "season", "size_gb"])
def test_both_readers_agree(title, field):
    """The load-bearing test: two independent readers, one meaning.

    A failure here means a release is described differently depending on which
    way ScanHound happened to find it -- which is how the [BD] releases went
    missing.
    """
    rss, listing = rss_fields(title)[field], listing_fields(title)[field]
    if field == "resolution":
        rss, listing = canonical_resolution(rss), canonical_resolution(listing)
    if isinstance(rss, float) and isinstance(listing, float):
        assert rss == pytest.approx(listing, rel=1e-6), title
    else:
        assert rss == listing, title


# ─────────────────────── measured divergences, 2026-08-01 ───────────────────
# Each is a real defect. strict=True means fixing one turns this file RED,
# which is the signal to delete the marker rather than let it decay.

@pytest.mark.xfail(strict=True, reason=(
    "RSS emits '2160p', the listing path emits '4K'. Same meaning, different "
    "token. Harmless only while nothing compares them as raw strings -- and "
    "the frontend did exactly that until 2026-07-30, hiding 61% of 4K movies."))
def test_uhd_spelling_is_shared():
    assert rss_fields("Dune 2024 2160p UHD BluRay 82.4 GB")["resolution"] == \
        listing_fields("Dune 2024 2160p UHD BluRay 82.4 GB")["resolution"]


@pytest.mark.xfail(strict=True, reason=(
    "RSS _YEAR_RE guards with (?!\\d), so a pixel dimension parses as a year: "
    "'1920x1080' -> 1920. The listing path's \\b...\\b correctly rejects it. "
    "This one reaches a decision: a wrong title_year sets year_conflict in "
    "classify_candidate, which resolves the candidate to 'ambiguous' and "
    "blocks it in the gate."))
def test_pixel_dimensions_are_not_read_as_years():
    title = "Concert Film 1920x1080 2019 1080p WEB-DL 5.5 GB"
    assert rss_fields(title)["year"] == 2019
    assert listing_fields(title)["year"] == 2019


@pytest.mark.xfail(strict=True, reason=(
    "The listing SEASON_PATTERN has no (?<![A-Z0-9]) guard, so an audio tag "
    "supplies a season: 'DTS5.1' -> season 5, turning a movie into TV. RSS "
    "guards correctly and returns None."))
def test_audio_tags_do_not_produce_a_season():
    title = "Movie With DTS5.1 Audio 2021 2160p WEB-DL 44.0 GB"
    assert rss_fields(title)["season"] is None
    assert listing_fields(title)["season"] is None


@pytest.mark.xfail(strict=True, reason=(
    "Season digit caps differ: RSS accepts S\\d{1,3} and reads 'S104' as "
    "season 104; the listing path caps at \\d{1,2} and truncates to 10. They "
    "disagree, and which is correct depends on whether S104 means a season or "
    "S10E4 -- so the disagreement is the finding, not the value."))
def test_season_digit_width_agrees():
    title = "Long Run S104 2160p WEB-DL 12.0 GB"
    assert rss_fields(title)["season"] == listing_fields(title)["season"]


@pytest.mark.xfail(strict=True, reason=(
    "RSS _SIZE_RE accepts only GiB|GB|MiB|MB, so a terabyte release parses to "
    "size_gb=None while the listing path reads 1228.8 GB. Size feeds the "
    "quality/upgrade comparison, so on the RSS path such a release is judged "
    "with no size at all. Exposure is reduced but NOT removed by #191: "
    "full-disc [BD] titles are excluded, and TB-sized releases are usually "
    "full-disc -- 'usually' is not 'always'."))
def test_terabyte_sizes_are_understood_by_both():
    title = "Big Set 2018 2160p Complete BluRay 1.2 TB"
    assert rss_fields(title)["size_gb"] is not None
    assert rss_fields(title)["size_gb"] == pytest.approx(
        listing_fields(title)["size_gb"], rel=1e-6)


# ──────────────────────── guard on the harness itself ───────────────────────

def test_the_readers_are_actually_two_different_implementations():
    """Cheap insurance against this whole file silently comparing one reader
    with itself -- which would make every parity test above vacuous."""
    assert parse_release_title.__module__ == "backend.sources.hdencode_feed_parser"
    assert SourceBase.RESOLUTION_PATTERN.pattern != "", "listing pattern missing"
    # The two resolution patterns are textually different implementations.
    from backend.sources import hdencode_feed_parser as fp
    assert fp._RESOLUTION_RE.pattern != SourceBase.RESOLUTION_PATTERN.pattern


@pytest.mark.parametrize("field", ["resolution", "year", "season", "size_gb"])
def test_the_corpus_actually_exercises_every_field(field):
    """The other way parity tests go vacuous: agreeing on None.

    If a reader stopped extracting a field entirely -- or if
    `canonical_resolution` were reduced to `return None` -- then both sides
    would produce None for every title and `test_both_readers_agree` would
    pass while comparing nothing. Require real values on both sides.
    """
    rss_values = [rss_fields(t)[field] for t in AGREEING_CORPUS]
    listing_values = [listing_fields(t)[field] for t in AGREEING_CORPUS]
    assert any(v is not None for v in rss_values), f"RSS never extracted {field}"
    assert any(v is not None for v in listing_values), (
        f"listing never extracted {field}")


def test_canonical_resolution_still_separates_distinct_resolutions():
    """`canonical_resolution` folds 4K/2160p/UHD together on purpose. If it
    ever folded further, the resolution parity test would stop being able to
    see a genuine mismatch."""
    assert canonical_resolution("2160p") == canonical_resolution("4K")
    assert canonical_resolution("1080p") != canonical_resolution("4K")
    assert canonical_resolution("720p") != canonical_resolution("1080p")
