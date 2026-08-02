"""The two discovery paths must read a release title the same way.

ScanHound reads the same source two ways:

    RSS path      backend/sources/hdencode_feed_parser.py :: parse_release_title
    listing path  backend/sources/base.py :: SourceBase.extract_*
                  (inherited by hdencode.py, ddlbase.py and adithd.py)

They used to carry independent copies of the same four patterns. On 2026-08-01 a
harness compared them and found **five divergences on twelve titles** —
including RSS reading ``1920x1080`` as year 1920, and the listing path reading
``DTS5.1`` as season 5. Both reached real decisions.

**This file's property changed when the fix landed, and the change matters.**
The first version asserted "the two readers agree", comparing a test-only
transcription of ``SourceBase.extract_*`` against the RSS reader. That
transcription was a *third* implementation, free to drift from both — the very
failure mode being tested for. Now that both production readers delegate to
:mod:`backend.release_grammar`, agreement is true by construction and asserting
it would be vacuous.

So what is asserted here is what can actually regress:

  1. **both production paths still route through the shared grammar** — checked
     by perturbing the grammar and requiring each reader to move with it. A
     reader that quietly kept a private pattern keeps the old answer and fails.
     Same shape as ``test_rss_full_disc_symmetry.py``, which guards ``#191``.
  2. **each path keeps its own output contract** — stored spellings and legacy
     sentinels, which deliberately still differ.
  3. **the five historical divergences stay fixed end-to-end**, asserted through
     the production entry points rather than the grammar, so a broken hand-off
     fails too.

The grammar's own correctness is covered by ``tests/test_release_grammar.py``.
"""

import pytest

from backend import release_grammar as grammar
from backend.sources.base import SourceBase
from backend.sources.hdencode_feed_parser import parse_release_title


class _Source(SourceBase):
    """Minimal concrete subclass. SourceBase is abstract, but the extraction
    helpers under test are plain inherited methods that touch no network."""

    def __init__(self):  # deliberately skips SourceBase.__init__
        pass

    def get_config(self):  # pragma: no cover - never called
        raise NotImplementedError

    async def fetch_page(self, *a, **k):  # pragma: no cover - never called
        raise NotImplementedError

    def parse_release(self, *a, **k):  # pragma: no cover - never called
        raise NotImplementedError


listing = _Source()


# ───────────────── 1. both paths route through the shared grammar ───────────

class TestBothPathsUseTheSharedGrammar:
    def test_rss_year_follows_the_shared_grammar(self, monkeypatch):
        monkeypatch.setattr(grammar, "parse_year", lambda text: 1234)
        assert parse_release_title("The Batman 2022 1080p BluRay")["year"] == 1234

    def test_listing_year_follows_the_shared_grammar(self, monkeypatch):
        monkeypatch.setattr(grammar, "parse_year", lambda text: 1234)
        assert listing.extract_year("The Batman 2022 1080p BluRay") == 1234

    def test_rss_size_follows_the_shared_grammar(self, monkeypatch):
        monkeypatch.setattr(grammar, "find_size",
                            lambda text, **kw: grammar.SizeMatch(99.0, "99 GB", 10))
        assert parse_release_title("Film 2024 1080p 14.7 GB")["size_gb"] == 99.0

    def test_listing_size_follows_the_shared_grammar(self, monkeypatch):
        monkeypatch.setattr(grammar, "find_size",
                            lambda text, **kw: grammar.SizeMatch(99.0, "99 GB", 10))
        assert listing.extract_size("Film 2024 1080p 14.7 GB") == (
            "99 GB", int(99.0 * 1024 ** 3))

    def test_rss_resolution_follows_the_shared_grammar(self, monkeypatch):
        monkeypatch.setattr(grammar, "find_resolution",
                            lambda text: grammar.ResolutionMatch("720P", "720p", 5))
        assert parse_release_title("Film 2024 2160p BluRay")["resolution"] == "720p"

    def test_listing_resolution_follows_the_shared_grammar(self, monkeypatch):
        monkeypatch.setattr(grammar, "find_resolution",
                            lambda text: grammar.ResolutionMatch("720P", "720p", 5))
        assert listing.extract_resolution("Film 2024 2160p BluRay") == "720p"

    def test_rss_season_follows_the_shared_grammar(self, monkeypatch):
        monkeypatch.setattr(grammar, "parse_season_episode",
                            lambda text: grammar.SeasonEpisode(7, 3, None, False, 4))
        signals = parse_release_title("Show S01E02 1080p")
        assert (signals["season"], signals["episode"]) == (7, 3)

    def test_listing_season_follows_the_shared_grammar(self, monkeypatch):
        monkeypatch.setattr(grammar, "parse_season_episode",
                            lambda text: grammar.SeasonEpisode(7, 3, None, False, 4))
        assert listing.extract_season_episode("Show S01E02 1080p") == (7, 3)

    def test_neither_module_kept_a_private_pattern(self):
        """A dead-but-present duplicate is how the paths drifted apart before,
        so the old module-level patterns must be gone, not merely unused."""
        from backend.sources import hdencode_feed_parser as fp
        for name in ("_EPISODE_RE", "_SEASON_RE", "_YEAR_RE",
                     "_RESOLUTION_RE", "_SIZE_RE"):
            assert not hasattr(fp, name), (
                f"hdencode_feed_parser.{name} is back; it belongs in "
                "release_grammar or the two readers can diverge again")


# ─────────────── 2. each path keeps its own output contract ─────────────────

class TestOutputContractsAreUnchanged:
    """The paths deliberately still differ in what they EMIT. Only the parsing
    is shared; rewriting persisted values is a migration, not a parser fix."""

    def test_rss_still_stores_2160p(self):
        assert parse_release_title(
            "Dune 2024 2160p UHD BluRay 82.4 GB")["resolution"] == "2160p"

    def test_rss_normalises_4k_to_the_same_stored_token(self):
        assert parse_release_title(
            "Dune 2024 4K UHD BluRay 61.2 GB")["resolution"] == "2160p"

    def test_listing_still_stores_4K(self):
        assert listing.extract_resolution("Dune 2024 2160p UHD BluRay") == "4K"
        assert listing.extract_resolution("Dune 2024 4K UHD BluRay") == "4K"

    def test_the_two_stored_spellings_compare_equal_when_canonicalised(self):
        """The stored tokens differ on purpose; every comparison must fold
        them. This is the assertion that keeps divergence (a) from mattering."""
        rss = parse_release_title("Dune 2024 2160p UHD BluRay 82.4 GB")["resolution"]
        lst = listing.extract_resolution("Dune 2024 4K UHD BluRay")
        assert rss != lst, "if these ever match, this test has lost its point"
        assert grammar.canonical_resolution(rss) == grammar.canonical_resolution(lst)

    def test_listing_keeps_its_zero_sentinel_for_an_absent_year(self):
        assert listing.extract_year("Untitled 1080p WEB-DL") == 0

    def test_listing_keeps_its_empty_string_for_an_absent_resolution(self):
        assert listing.extract_resolution("Untitled WEB-DL") == ''

    def test_listing_size_is_still_display_string_plus_bytes(self):
        text, size_bytes = listing.extract_size("Film 2024 1080p 14.7 GB")
        assert text == "14.7 GB"
        assert size_bytes == int(14.7 * 1024 ** 3)

    def test_rss_keeps_size_text_verbatim(self):
        assert parse_release_title(
            "Film 2024 1080p 14.7 GB")["size_text"] == "14.7 GB"


# ────────────── 3. the five divergences, through production ─────────────────

class TestTheFiveDivergencesStayFixed:
    def test_a_uhd_spellings_are_comparable(self):
        rss = parse_release_title("Doc 2023 UHD BluRay 55.1 GB")["resolution"]
        lst = listing.extract_resolution("Doc 2023 UHD BluRay")
        assert grammar.canonical_resolution(rss) == grammar.canonical_resolution(lst)

    def test_b_pixel_dimensions_are_not_years_on_either_path(self):
        title = "Concert Film 1920x1080 2019 1080p WEB-DL 5.5 GB"
        assert parse_release_title(title)["year"] == 2019
        assert listing.extract_year(title) == 2019

    def test_c_audio_tags_do_not_create_a_season_on_either_path(self):
        title = "Movie With DTS5.1 Audio 2021 2160p WEB-DL 44.0 GB"
        assert parse_release_title(title)["season"] is None
        assert listing.extract_season_episode(title) == (None, None)

    def test_c_and_such_a_movie_is_not_classified_as_tv(self):
        assert listing.is_tv_release(
            "Movie With DTS5.1 Audio 2021 2160p WEB-DL") is False

    def test_d_overwide_seasons_agree_and_are_still_treated_as_tv(self):
        title = "Long Run S104 2160p WEB-DL 12.0 GB"
        signals = parse_release_title(title)
        assert signals["season"] is None
        assert signals["season_ambiguous"] is True
        assert listing.extract_season_episode(title) == (None, None)
        # Neither path may quietly downgrade an unreadable season to "a movie".
        assert listing.is_tv_release(title) is True

    def test_e_terabyte_sizes_parse_on_both_paths(self):
        title = "Big Set 2018 2160p Complete BluRay 1.2 TB"
        assert parse_release_title(title)["size_gb"] == pytest.approx(1228.8)
        _, size_bytes = listing.extract_size(title)
        assert size_bytes == pytest.approx(1.2 * 1024 ** 4, rel=1e-6)


# ─────────────────────── 4. unchanged behaviour ────────────────────────────

class TestOrdinaryTitlesAreUnaffected:
    """The fix must not move anything that was already right."""

    @pytest.mark.parametrize("title,year,resolution,season", [
        ("The Batman 2022 1080p BluRay x264-SPARKS 14.7 GB", 2022, "1080p", None),
        ("Some Show S01E02 1080p WEB-DL DDP5.1 H.264-NTb 2.1 GB", None, "1080p", 1),
        ("Another Series S03 1080p WEB-DL DD+5.1 H.265-EDITH 18.9 GB", None, "1080p", 3),
        ("Old Film 1975 720p BluRay x264-AMIABLE 4.4 GB", 1975, "720p", None),
    ])
    def test_rss_fields(self, title, year, resolution, season):
        signals = parse_release_title(title)
        assert signals["year"] == year
        assert signals["resolution"] == resolution
        assert signals["season"] == season

    def test_clean_title_still_cuts_at_the_metadata(self):
        assert parse_release_title(
            "The Batman 2022 1080p BluRay 14.7 GB")["clean_title"] == "The Batman"
        assert parse_release_title(
            "Some Show S01E02 1080p WEB-DL 2.1 GB")["clean_title"] == "Some Show"

    def test_multi_episode_ranges_survive(self):
        signals = parse_release_title("Show S01E01E02E03 1080p WEB-DL 6.0 GB")
        assert (signals["season"], signals["episode"],
                signals["episode_end"]) == (1, 1, 3)
