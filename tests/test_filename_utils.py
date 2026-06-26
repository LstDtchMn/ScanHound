"""Tests for backend/filename_utils.py — parse_filename and sanitize_filename."""

import pytest

from backend.filename_utils import parse_filename, sanitize_filename


class TestParseFilenameMovies:

    def test_basic_movie(self):
        r = parse_filename("The.Dark.Knight.2008.1080p.BluRay.x265.mkv")
        assert r["title"] == "The Dark Knight"
        assert r["year"] == 2008
        assert r["resolution"] == "1080p"
        assert r["is_tv"] is False
        assert r["season"] is None
        assert r["episode"] is None

    def test_year_extracted(self):
        r = parse_filename("Inception.2010.1080p.WEB-DL.mkv")
        assert r["year"] == 2010

    def test_no_year_returns_none(self):
        r = parse_filename("NoYearMovie.1080p.BluRay.mkv")
        assert r["year"] is None

    def test_resolution_1080p(self):
        r = parse_filename("Movie.2020.1080p.WEB-DL.mkv")
        assert r["resolution"] == "1080p"

    def test_resolution_720p(self):
        r = parse_filename("Movie.2020.720p.HDTV.mkv")
        assert r["resolution"] == "720p"

    def test_resolution_2160p(self):
        r = parse_filename("Avatar.2009.2160p.UHD.BluRay.mkv")
        assert r["resolution"] == "2160p"

    def test_resolution_4k_keyword(self):
        r = parse_filename("Film.2020.4K.BluRay.mkv")
        assert r["resolution"] == "2160p"

    def test_resolution_uhd_keyword(self):
        r = parse_filename("Film.2020.UHD.BluRay.mkv")
        assert r["resolution"] == "2160p"

    def test_no_resolution_returns_none(self):
        r = parse_filename("Movie.2020.BluRay.mkv")
        assert r["resolution"] is None

    def test_no_extension(self):
        r = parse_filename("The.Dark.Knight.2008.1080p")
        assert r["title"] == "The Dark Knight"
        assert r["year"] == 2008

    def test_tags_stripped_from_title(self):
        r = parse_filename("Movie.REMUX.2023.1080p.BluRay.mkv")
        assert "REMUX" not in r["title"]
        assert "BluRay" not in r["title"]

    def test_title_with_dots_replaced_by_spaces(self):
        r = parse_filename("Some.Great.Film.2022.1080p.mkv")
        assert r["title"] == "Some Great Film"

    def test_title_with_underscores(self):
        r = parse_filename("Some_Great_Film_2022_1080p.mkv")
        assert "Some" in r["title"]
        assert "Great" in r["title"]

    def test_is_tv_false_for_movie(self):
        r = parse_filename("Avengers.2012.1080p.mkv")
        assert r["is_tv"] is False

    def test_movie_no_episode_title(self):
        r = parse_filename("Interstellar.2014.1080p.BluRay.mkv")
        assert "filename_episode_title" not in r

    def test_1990s_year(self):
        r = parse_filename("The.Matrix.1999.1080p.BluRay.mkv")
        assert r["year"] == 1999

    def test_brackets_in_filename(self):
        # Year must come before the bracketed resolution so it's not discarded
        r = parse_filename("Movie.2021.[1080p].BluRay.mkv")
        assert r["year"] == 2021


class TestParseFilenameTv:

    def test_se_notation_season(self):
        r = parse_filename("Breaking.Bad.S01E03.1080p.WEB-DL.mkv")
        assert r["season"] == 1
        assert r["is_tv"] is True

    def test_se_notation_episode(self):
        r = parse_filename("Breaking.Bad.S01E03.1080p.WEB-DL.mkv")
        assert r["episode"] == 3

    def test_se_notation_double_digit_season(self):
        r = parse_filename("Show.Name.S12E05.720p.mkv")
        assert r["season"] == 12
        assert r["episode"] == 5

    def test_alt_notation_season(self):
        r = parse_filename("Show.Name.1x03.720p.mkv")
        assert r["season"] == 1
        assert r["episode"] == 3
        assert r["is_tv"] is True

    def test_alt_notation_double_digit_episode(self):
        r = parse_filename("Show.2x13.1080p.mkv")
        assert r["season"] == 2
        assert r["episode"] == 13

    def test_episode_title_extracted(self):
        r = parse_filename("The.Boys.S03E04.Glorious.Five.Year.Plan.1080p.mkv")
        assert r.get("filename_episode_title") == "Glorious Five Year Plan"

    def test_episode_title_stops_at_quality_marker(self):
        r = parse_filename("Show.S01E01.Pilot.Episode.1080p.WEB-DL.mkv")
        ep_title = r.get("filename_episode_title", "")
        assert "1080p" not in ep_title
        assert "WEB-DL" not in ep_title

    def test_short_episode_title_not_extracted(self):
        # "AB" is only 2 chars — should not be set
        r = parse_filename("Show.S01E01.AB.1080p.mkv")
        assert "filename_episode_title" not in r

    def test_no_episode_title_when_quality_immediately_follows(self):
        r = parse_filename("Show.S01E01.1080p.WEB-DL.mkv")
        # No text between episode code and quality marker
        assert "filename_episode_title" not in r

    def test_tv_title_extracted(self):
        r = parse_filename("Breaking.Bad.S01E03.1080p.WEB-DL.mkv")
        assert r["title"] == "Breaking Bad"

    def test_is_tv_true(self):
        r = parse_filename("Game.of.Thrones.S08E06.1080p.mkv")
        assert r["is_tv"] is True

    def test_case_insensitive_se(self):
        r = parse_filename("show.s02e10.720p.mkv")
        assert r["season"] == 2
        assert r["episode"] == 10


class TestSanitizeFilename:

    def test_colon_replaced_with_dash(self):
        assert sanitize_filename("Movie: Part Two") == "Movie - Part Two"

    def test_multiple_colons(self):
        result = sanitize_filename("A: B: C")
        assert ":" not in result
        assert " - " in result

    def test_angle_brackets_removed(self):
        assert "<" not in sanitize_filename("Movie <2023>")
        assert ">" not in sanitize_filename("Movie <2023>")

    def test_quotes_removed(self):
        assert '"' not in sanitize_filename('Movie "Title"')

    def test_pipe_removed(self):
        assert "|" not in sanitize_filename("Movie|Title")

    def test_question_mark_removed(self):
        assert "?" not in sanitize_filename("What Movie?")

    def test_asterisk_removed(self):
        assert "*" not in sanitize_filename("Movie*")

    def test_trailing_dots_stripped(self):
        assert not sanitize_filename("Movie...").endswith(".")

    def test_trailing_spaces_stripped(self):
        assert not sanitize_filename("Movie   ").endswith(" ")

    def test_clean_string_unchanged(self):
        s = "The Dark Knight"
        assert sanitize_filename(s) == s

    def test_forward_slash_removed(self):
        assert "/" not in sanitize_filename("A/B")

    def test_backslash_removed(self):
        assert "\\" not in sanitize_filename("A\\B")

    def test_empty_string(self):
        assert sanitize_filename("") == ""


class TestMultiEpisodeParsing:
    def test_double_episode_concatenated(self):
        r = parse_filename("Show.Name.S01E01E02.1080p.WEB-DL.mkv")
        assert r["season"] == 1
        assert r["episode"] == 1
        assert r["episode_end"] == 2

    def test_double_episode_dash_notation(self):
        r = parse_filename("Show.Name.S02E05-E06.720p.HDTV.mkv")
        assert r["episode"] == 5
        assert r["episode_end"] == 6

    def test_double_episode_dot_notation(self):
        r = parse_filename("Show.Name.S03E11.E12.1080p.mkv")
        assert r["episode"] == 11
        assert r["episode_end"] == 12

    def test_part_one(self):
        r = parse_filename("Show.Name.S01E05.Part1.1080p.mkv")
        assert r["part"] == 1
        assert r.get("episode_end") is None

    def test_part_two_dot_notation(self):
        r = parse_filename("Show.Name.S01E05.Part.2.720p.mkv")
        assert r["part"] == 2

    def test_pt_notation(self):
        r = parse_filename("Show.Name.S01E05.Pt1.1080p.mkv")
        assert r["part"] == 1

    def test_clean_single_episode_unchanged(self):
        r = parse_filename("Show.Name.S01E03.1080p.WEB-DL.mkv")
        assert r["episode"] == 3
        assert r.get("episode_end") is None
        assert r.get("part") is None

    def test_movie_unchanged(self):
        r = parse_filename("Movie.Title.2024.1080p.BluRay.mkv")
        assert r.get("episode_end") is None
        assert r.get("part") is None
        assert r["is_tv"] is False
