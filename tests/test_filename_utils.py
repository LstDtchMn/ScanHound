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

    def test_windows_reserved_name_con(self):
        assert sanitize_filename("Con") == "Con_"

    def test_windows_reserved_name_case_insensitive(self):
        assert sanitize_filename("con") == "con_"
        assert sanitize_filename("CON") == "CON_"

    def test_windows_reserved_name_nul_stem(self):
        # sanitize_filename operates on a component/stem, not a full path;
        # "nul" as a bare stem must be guarded the same as "NUL".
        assert sanitize_filename("nul") == "nul_"

    def test_windows_reserved_name_com_lpt_ports(self):
        assert sanitize_filename("COM1") == "COM1_"
        assert sanitize_filename("com9") == "com9_"
        assert sanitize_filename("LPT1") == "LPT1_"
        assert sanitize_filename("lpt9") == "lpt9_"

    def test_windows_reserved_name_prn_aux(self):
        assert sanitize_filename("PRN") == "PRN_"
        assert sanitize_filename("AUX") == "AUX_"

    def test_non_reserved_lookalike_unchanged(self):
        # Must not false-positive on titles that merely start with/contain
        # a reserved token as a substring.
        assert sanitize_filename("Conan") == "Conan"
        assert sanitize_filename("Con Air") == "Con Air"
        assert sanitize_filename("COM1the Movie") == "COM1the Movie"

    def test_control_chars_stripped(self):
        result = sanitize_filename("Movie\x00Title\x1f")
        assert "\x00" not in result
        assert "\x1f" not in result
        assert result == "MovieTitle"

    def test_control_chars_stripped_various(self):
        s = "".join(chr(c) for c in range(0x00, 0x20))
        assert sanitize_filename(s) == ""

    def test_normal_title_unchanged_with_new_guards(self):
        assert sanitize_filename("Rush (2013)") == "Rush (2013)"


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


# ── Regression: underscore-delimited scene releases (real failures) ──

class TestUnderscoreSceneReleases:
    def test_smallfoot_year_and_junk_stripped(self):
        p = parse_filename(
            "Smallfoot_2018_Hybrid_2160p_MA_WEB-DL_DoVi_HDR_H.265_DTS-HD_MA_5.1.mkv")
        assert p["title"] == "Smallfoot"
        assert p["year"] == 2018
        assert p["resolution"] == "2160p"

    def test_kombucha_year_extracted(self):
        p = parse_filename("Kombucha_2025_2160p_WEB-DL_HDR10__H.265_DTS-HD_MA_5.1.mkv")
        assert p["title"] == "Kombucha"
        assert p["year"] == 2025

    def test_ohikkoshi_aka_split(self):
        p = parse_filename(
            "Ohikkoshi_a.k.a._Moving_1993_2160p_MUBI_WEB-DL_AAC2.0_H.265-JBD.mkv")
        assert p["title"] == "Ohikkoshi"
        assert p["aka"] == "Moving"
        assert p["year"] == 1993

    def test_dot_delimited_still_works(self):
        p = parse_filename("The.Matrix.1999.1080p.BluRay.x265-GROUP.mkv")
        assert p["title"] == "The Matrix"
        assert p["year"] == 1999
        assert p.get("aka") is None


# ── Embedded IMDB id ─────────────────────────────────────────────────

class TestImdbId:
    def test_braced_imdb_id_stripped_from_title(self):
        p = parse_filename("The Matrix (1999) {imdb-tt0133093} 1080p BluRay.mkv")
        assert p["imdb_id"] == "tt0133093"
        assert p["title"] == "The Matrix"

    def test_bracketed_imdb_id(self):
        p = parse_filename("Inception.2010.1080p.[tt1375666].mkv")
        assert p["imdb_id"] == "tt1375666"

    def test_8digit_imdb_id(self):
        p = parse_filename("Some.Show.S01E01.{imdb-tt12345678}.mkv")
        assert p["imdb_id"] == "tt12345678"

    def test_no_imdb_id(self):
        p = parse_filename("The.Matrix.1999.1080p.BluRay.x265-GROUP.mkv")
        assert p.get("imdb_id") is None


# ── Leading title word must survive tag stripping (review fix #3) ─────

class TestLeadingTitleWordPreserved:
    def test_platform_word_leading_title(self):
        assert parse_filename("Stan.And.Ollie.2018.1080p.WEB-DL.mkv")["title"] == "Stan And Ollie"

    def test_single_tag_word_title(self):
        assert parse_filename("Opus.2025.2160p.WEB-DL.mkv")["title"] == "Opus"
        assert parse_filename("Hybrid.2007.1080p.BluRay.mkv")["title"] == "Hybrid"
        assert parse_filename("Dual.2022.1080p.mkv")["title"] == "Dual"

    def test_mid_title_junk_still_stripped(self):
        assert parse_filename("Some.Movie.MULTI.2020.1080p.mkv")["title"] == "Some Movie"

    def test_trailing_junk_after_year_unaffected(self):
        assert parse_filename("Smallfoot.2018.Hybrid.2160p.WEB-DL.mkv")["title"] == "Smallfoot"


# ── Minor review fixes: multi-digit Part, bare tt-id leak ────────────

class TestMinorParsingFixes:
    def test_multi_digit_part(self):
        assert parse_filename("Concert.2020.Part.10.1080p.mkv")["part"] == 10
        assert parse_filename("Movie.2020.Part.2.1080p.mkv")["part"] == 2

    def test_bare_tt_id_stripped_from_title(self):
        p = parse_filename("Scott.Pilgrim.tt0446029.2010.1080p.mkv")
        assert p["imdb_id"] == "tt0446029"
        assert "tt0446029" not in p["title"]
        assert p["title"] == "Scott Pilgrim"


# ── Trailing-group strip must not eat the last title word ───────────
# A scene group only sits at the END of the full stem, after year/res/SxxEyy.
# Once the title was truncated at one of those tokens, the group is already
# gone — stripping anyway deleted the last word of underscore-delimited names
# ("The_Threesome" → "The" → matched the wrong movie) and maimed hyphenated
# titles ("Spider-Man" → "Spider").

class TestTrailingGroupStrip:
    def test_underscore_title_keeps_last_word(self):
        p = parse_filename("The_Threesome_2025_2160p_AMZN_WEB-DL_H.265_DTS-HD_MA_5.1.mkv")
        assert p["title"] == "The Threesome"
        assert p["year"] == 2025

    def test_underscore_multiword_title_intact(self):
        assert parse_filename("The_Da_Vinci_Code_2006.mkv")["title"] == "The Da Vinci Code"
        assert parse_filename("Murder_at_1600_1997_1080p.mkv")["title"] == "Murder at 1600"

    def test_hyphenated_title_survives(self):
        assert parse_filename("Spider-Man.2002.1080p.BluRay.x264-GROUP.mkv")["title"] == "Spider Man"
        assert parse_filename("Kick-Ass.2010.720p.mkv")["title"] == "Kick Ass"

    def test_group_still_stripped_when_no_structural_tokens(self):
        # No year/resolution → title_part is the whole stem → the trailing
        # -GROUP is really a group and must still be removed.
        assert parse_filename("Movie.Title-SPARKS.mkv")["title"] == "Movie Title"

    def test_tv_underscore_title_intact(self):
        p = parse_filename("Some_Show_S01E03_1080p_WEB-DL.mkv")
        assert p["title"] == "Some Show"
        assert p["season"] == 1 and p["episode"] == 3
