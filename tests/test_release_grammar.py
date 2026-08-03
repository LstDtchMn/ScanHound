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
    title_indicates_tv,
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


class TestTvEvidence:
    """Divergence (f), found 2026-08-02 while closing the media-type gap.

    The listing path recognised four TV title forms that the RSS path did not,
    because RSS keyed purely off a parsed season. On the RSS side those releases
    were classified as MOVIES, which selects the wrong Plex library downstream.
    """

    @pytest.mark.parametrize("title", [
        "Great Show Complete Series 1080p WEB-DL",
        "Docu Mini Series 1080p WEB-DL",
        "Docu Mini-Series 1080p WEB-DL",
        "Old Programme TV Series 1080p WEB-DL",
        "Thing Season 4 1080p WEB-DL",
        "Thing Season 12 1080p WEB-DL",
    ])
    def test_tv_forms_without_an_sxxexx_token_are_still_tv(self, title):
        assert title_indicates_tv(title) is True

    @pytest.mark.parametrize("title", [
        "Some Show S01E02 1080p WEB-DL",
        "Another Series S03 1080p WEB-DL",
    ])
    def test_season_tokens_are_tv(self, title):
        assert title_indicates_tv(title) is True

    def test_an_uninterpretable_season_is_still_tv(self):
        # 'Cannot tell' must not collapse into 'not TV'.
        assert title_indicates_tv("Long Run S104 2160p WEB-DL") is True

    @pytest.mark.parametrize("title", [
        "The Batman 2022 1080p BluRay x264-SPARKS",
        "Movie With DTS5.1 Audio 2021 2160p WEB-DL",
        "Old Film 1975 720p BluRay",
    ])
    def test_films_are_not_tv(self, title):
        assert title_indicates_tv(title) is False

    def test_the_rule_is_title_only(self):
        """Out-of-band signals (listing crawl mode, RSS feed categories) are
        each path's own business and are additive. If this rule ever consulted
        them it could not be shared, which is the whole point."""
        assert title_indicates_tv("The Batman 2022 1080p") is False


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


class TestResolutionFromDimensions:
    """The one sanctioned dimension->resolution conversion (R-3). Exact
    standard values only — a nonstandard crop is None, never a guess."""

    def test_standard_dimensions_map(self):
        from backend.release_grammar import resolution_from_dimensions as rfd
        assert rfd("Resolution: 3840x2160") == "UHD"
        assert rfd("1920x1080") == "1080P"
        assert rfd("1280 x 720") == "720P"

    def test_nonstandard_dimensions_stay_none(self):
        from backend.release_grammar import resolution_from_dimensions as rfd
        assert rfd("1920x817") is None      # cinemascope crop: unknown, not 1080p
        assert rfd("Resolution: unknown") is None
        assert rfd("") is None and rfd(None) is None

    def test_a_dimension_is_still_not_a_resolution_token(self):
        from backend.release_grammar import find_resolution
        assert find_resolution("Some Film 1920x1080") is None


class TestFindAllSizes:
    def test_returns_every_size_in_order_including_tb(self):
        from backend.release_grammar import find_all_sizes
        got = find_all_sizes("Disc 1: 45.2 GB ... Disc 2: 1.2 TB ... sample 300 MB")
        assert [s.text for s in got] == ["45.2 GB", "1.2 TB", "300 MB"]
        assert got[1].gigabytes > got[0].gigabytes > got[2].gigabytes

    def test_empty_input_is_an_empty_list(self):
        from backend.release_grammar import find_all_sizes
        assert find_all_sizes("") == [] and find_all_sizes(None) == []


class TestSeasonTokenTrailingBoundary:
    """Round-10 internal review, executed-proof defect: _SEASON_RE had no
    trailing boundary, so a release-group name beginning S+digit parsed as a
    season — 'x264-S0MEGRP' became TV season 0, 'Tesla.S3XY.Story' season 3.
    PRE-EXISTED on the RSS path; imported to the detail path by R-3. A
    season-only token followed by a letter is a name, not a season."""

    def test_group_names_are_not_seasons(self):
        from backend.release_grammar import parse_season_episode
        assert parse_season_episode("Movie.2020.1080p.x264-S0MEGRP.mkv").season is None
        assert parse_season_episode("Tesla.S3XY.Story.2160p.mkv").season is None
        assert not parse_season_episode("Movie.2020.1080p.x264-S0MEGRP.mkv").ambiguous

    def test_real_season_forms_still_parse(self):
        from backend.release_grammar import parse_season_episode
        assert parse_season_episode("Show.Name.S01.Complete.1080p").season == 1
        assert parse_season_episode("Show.Name.S01E02.1080p").season == 1
        assert parse_season_episode("Show.Name.S104.2160p").ambiguous
        assert parse_season_episode("Show S2 2020").season == 2


class TestSizeUnitTrailingBoundary:
    """Same review: the size unit had no trailing boundary, so '15 GBps' in
    page prose parsed as a 15 GB release size — and being large it tends to
    win pick-the-largest."""

    def test_units_glued_to_letters_are_not_sizes(self):
        from backend.release_grammar import find_size, find_all_sizes
        assert find_size("server pushes 15 GBps easily") is None
        assert find_all_sizes("rated 200 TBW endurance") == []

    def test_real_sizes_still_parse(self):
        from backend.release_grammar import find_all_sizes, parse_size_gb
        got = find_all_sizes("Disc 1: 45.2 GB and 1.2 TB total (sample 300 MB).")
        assert [s.text for s in got] == ["45.2 GB", "1.2 TB", "300 MB"]
        assert parse_size_gb("Show.Title.2026.2160p - 82.4 GB", anchored=True) == 82.4
