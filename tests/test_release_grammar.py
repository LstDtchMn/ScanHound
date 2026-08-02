"""The shared release-title grammar, and the five divergences it exists to end.

Each divergence below was measured on 2026-08-01 by comparing the two readers
ScanHound had grown independently. The tests are written so that reverting the
grammar to either reader's old behaviour fails them — a test that both the
correct and the incorrect implementation pass would be worse than none, and
this suite exists precisely because two readers agreeing with themselves is
what let the defects survive.
"""

import pytest

from backend.release_grammar import (
    canonical_resolution,
    parse_resolution,
    parse_season_episode,
    parse_size_gb,
    parse_year,
    strip_trailing_size,
)


class TestYear:
    """Divergence (b): RSS read '1920x1080' as year 1920."""

    def test_pixel_dimensions_are_not_years(self):
        # The old RSS guard was (?!\d); 'x' is not a digit, so 1920 passed.
        assert parse_year("Concert Film 1920x1080 2019 1080p WEB-DL") == 2019

    @pytest.mark.parametrize("text", [
        "Show 1920x1080 WEB-DL",
        "Encode at 2016x1080",
        "1920x800 scope",
    ])
    def test_a_dimension_alone_yields_no_year(self, text):
        assert parse_year(text) is None

    @pytest.mark.parametrize("text,expected", [
        ("The Batman 2022 1080p BluRay", 2022),
        ("Movie.2019.1080p.WEB", 2019),
        ("Some Film (1975) 720p", 1975),
        ("2001 A Space Odyssey 1968 2160p", 2001),
        ("1999", 1999),
    ])
    def test_ordinary_years_still_parse(self, text, expected):
        assert parse_year(text) == expected

    def test_absent_year_is_none_not_zero(self):
        # The listing path's old sentinel was 0, which is indistinguishable
        # from a value in arithmetic and sorts before every real year.
        assert parse_year("Untitled 1080p WEB-DL") is None
        assert parse_year("") is None

    def test_resolutions_are_never_mistaken_for_years(self):
        assert parse_year("Film 2160p HEVC") is None
        assert parse_year("Film 1080p x264") is None


class TestSeasonEpisode:
    """Divergences (c) and (d)."""

    def test_audio_tags_do_not_supply_a_season(self):
        # (c): the listing path had no preceding-character guard, so 'DTS5.1'
        # gave season 5 and turned a movie into TV.
        result = parse_season_episode("Movie With DTS5.1 Audio 2021 2160p")
        assert result.season is None
        assert result.ambiguous is False, "a movie must not read as 'cannot tell'"

    @pytest.mark.parametrize("text", ["Movie DTS5.1", "Film TrueHD7.1", "AAC2.0 rip"])
    def test_other_audio_tags_too(self, text):
        assert parse_season_episode(text).season is None

    def test_overwide_season_is_ambiguous_not_guessed(self):
        # (d): RSS said 104, the listing path silently truncated to 10. Silent
        # truncation is the worse failure -- a confident wrong answer -- so
        # neither is kept.
        result = parse_season_episode("Long Run S104 2160p WEB-DL")
        assert result.season is None
        assert result.ambiguous is True

    def test_ambiguous_is_distinguishable_from_absent(self):
        """A caller must be able to tell 'no season here' from 'cannot tell'.
        Collapsing them would let an unreadable TV release be filed as a movie."""
        assert parse_season_episode("The Batman 2022 1080p").ambiguous is False
        assert parse_season_episode("Thing S104 1080p").ambiguous is True

    @pytest.mark.parametrize("text,season,episode", [
        ("Some Show S01E02 1080p WEB-DL", 1, 2),
        ("Another Series S03 1080p WEB-DL", 3, None),
        ("Show S10E04 2160p", 10, 4),
        ("Show s2e7 720p", 2, 7),
    ])
    def test_ordinary_seasons_still_parse(self, text, season, episode):
        result = parse_season_episode(text)
        assert (result.season, result.episode) == (season, episode)
        assert result.ambiguous is False

    def test_multi_episode_range_keeps_its_end(self):
        result = parse_season_episode("Show S01E01E02E03 1080p")
        assert (result.season, result.episode, result.episode_end) == (1, 1, 3)

    def test_leading_zeros_do_not_count_toward_the_width_limit(self):
        # 'S001' is season 1 written wide, not an over-wide season.
        assert parse_season_episode("Show S001E02 1080p").season == 1


class TestSize:
    """Divergence (e): RSS could not parse TB at all."""

    def test_terabytes_parse(self):
        assert parse_size_gb("Big Set 2018 2160p BluRay 1.2 TB",
                             anchored=True) == pytest.approx(1228.8)

    @pytest.mark.parametrize("text,expected", [
        ("Film 2024 2160p REMUX 82.4 GB", 82.4),
        ("Mini Doc 2020 1080p WEBRip 850 MB", 850 / 1024),
        ("Show 2021 1080p 4.5 GiB", 4.5),
        ("Set 2018 2160p 2 TiB", 2048.0),
    ])
    def test_every_unit_is_understood(self, text, expected):
        assert parse_size_gb(text, anchored=True) == pytest.approx(expected)

    def test_anchored_requires_the_size_to_end_the_string(self):
        """HDEncode puts the size last in a feed title. Anchoring stops a
        stray number mid-title being read as the release size."""
        assert parse_size_gb("Film 12 GB Edition 2160p BluRay", anchored=True) is None
        assert parse_size_gb("Film 12 GB Edition 2160p BluRay",
                             anchored=False) == pytest.approx(12.0)

    def test_unanchored_finds_a_size_inside_article_html(self):
        html = "<div>Size: 14.7 GB</div><div>Runtime: 120 min</div>"
        assert parse_size_gb(html) == pytest.approx(14.7)

    def test_absent_size_is_none(self):
        assert parse_size_gb("Film 2024 2160p BluRay", anchored=True) is None
        assert parse_size_gb("") is None

    def test_strip_trailing_size_protects_the_year_parse(self):
        # Without stripping, a size like '2019 GB' would be read as a year.
        assert strip_trailing_size("Film 2024 1080p 2019 GB") == "Film 2024 1080p"
        assert parse_year(strip_trailing_size("Film 2024 1080p 2019 GB")) == 2024

    def test_strip_is_a_no_op_when_there_is_no_trailing_size(self):
        assert strip_trailing_size("Film 2024 1080p") == "Film 2024 1080p"


class TestResolution:
    """Divergence (a): RSS emitted '2160p', the listing path emitted '4K'."""

    @pytest.mark.parametrize("spelling", ["4K", "4k", "2160p", "2160P", "UHD", "uhd"])
    def test_every_uhd_spelling_folds_to_one_token(self, spelling):
        assert canonical_resolution(spelling) == "UHD"

    def test_uhd_spellings_compare_equal_to_each_other(self):
        assert canonical_resolution("2160p") == canonical_resolution("4K")
        assert canonical_resolution("UHD") == canonical_resolution("2160p")

    def test_distinct_resolutions_stay_distinct(self):
        """Guard against over-folding. If this ever passed, the canonical form
        would be hiding real differences instead of spelling differences."""
        assert canonical_resolution("1080p") != canonical_resolution("4K")
        assert canonical_resolution("720p") != canonical_resolution("1080p")
        assert canonical_resolution("480p") != canonical_resolution("720p")

    def test_interlaced_1080_folds_to_1080p(self):
        assert canonical_resolution("1080i") == canonical_resolution("1080p")

    def test_unknown_spellings_pass_through_rather_than_vanishing(self):
        # Mapping the unknown to None would recreate the original defect in a
        # new form: items that quietly match no filter at all.
        assert canonical_resolution("1440p") == "1440P"

    def test_absent_resolution_is_none(self):
        assert canonical_resolution(None) is None
        assert canonical_resolution("") is None
        assert canonical_resolution("   ") is None

    @pytest.mark.parametrize("text,expected", [
        ("Dune 2024 2160p UHD BluRay", "UHD"),
        ("Dune 2024 4K UHD BluRay", "UHD"),
        ("Doc 2023 UHD BluRay", "UHD"),
        ("The Batman 2022 1080p BluRay", "1080P"),
        ("Old Film 1975 720p BluRay", "720P"),
    ])
    def test_parse_resolution_returns_the_canonical_token(self, text, expected):
        assert parse_resolution(text) == expected

    def test_resolution_must_be_a_standalone_token(self):
        assert parse_resolution("Codec x2160puppet") is None
        assert parse_resolution("Film 2024 BluRay") is None
