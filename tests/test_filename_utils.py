import pytest
from backend.filename_utils import parse_filename


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
