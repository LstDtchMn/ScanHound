import pytest
from backend.rename.naming import build_target


class TestMultiEpNaming:
    def _base(self, **kwargs):
        return {
            "media_type": "tv",
            "title": "The Show",
            "year": 2024,
            "season": 1,
            "episode": 1,
            "resolution": "1080p",
            "original_filename": "show.s01e01.mkv",
            **kwargs,
        }

    def test_single_episode_unchanged(self):
        fname, _ = build_target(self._base(), tv_root="/tv")
        assert "S01E01" in fname
        assert "E02" not in fname

    def test_combined_episode_code(self):
        fname, _ = build_target(self._base(episode_end=2), tv_root="/tv")
        assert "S01E01E02" in fname

    def test_three_episode_code(self):
        fname, _ = build_target(self._base(episode=3, episode_end=5), tv_root="/tv")
        assert "S01E03E05" in fname

    def test_part_suffix(self):
        fname, _ = build_target(self._base(part=1), tv_root="/tv")
        assert "Part 1" in fname

    def test_part_two_suffix(self):
        fname, _ = build_target(self._base(part=2), tv_root="/tv")
        assert "Part 2" in fname

    def test_movie_naming_unchanged(self):
        meta = {
            "media_type": "movie",
            "title": "Great Film",
            "year": 2024,
            "resolution": "1080p",
            "original_filename": "great.film.mkv",
        }
        fname, _ = build_target(meta, movie_root="/movies")
        assert "Part" not in fname
        assert "E0" not in fname
