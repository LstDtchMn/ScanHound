import pytest
from unittest.mock import MagicMock, patch
from backend.rename.service import (
    _try_episode_rescan,
    _detect_combined_episode,
    _detect_split_file,
    _find_split_sibling,
)


def _make_episodes(runtimes: dict) -> list:
    """Build a minimal TMDB episode list from {ep_number: runtime_minutes}."""
    return [{"episode_number": n, "runtime": rt, "name": f"Episode {n}"}
            for n, rt in runtimes.items()]


class TestTryEpisodeRescan:
    def _match(self, season=1, episode=3):
        return {
            "tmdb_id": 999,
            "media_type": "tv",
            "title": "Test Show",
            "season": season,
            "episode": episode,
            "confidence": 70.0,
            "original_filename": "Test.Show.S01E03.1080p.mkv",
        }

    def _client(self, season_data: dict):
        c = MagicMock()
        c.season.return_value = season_data
        return c

    def test_proposes_correction_when_runtime_matches_different_episode(self):
        # File is 44 min; current match E3 has 90 min runtime; E4 has 44 min
        episodes = _make_episodes({2: 44, 3: 90, 4: 44, 5: 44})
        client = self._client({"episodes": episodes})
        season_cache = {1: {"episodes": episodes}}
        result = _try_episode_rescan(
            self._match(), client, 44.0, season_cache, 999, {})
        assert result is not None
        assert result["type"] == "episode_correction"
        assert result["proposed"]["episode"] in (2, 4, 5)

    def test_returns_none_when_no_clear_winner(self):
        # All episodes same runtime as current — no one is better
        episodes = _make_episodes({2: 44, 3: 44, 4: 44})
        client = self._client({"episodes": episodes})
        season_cache = {1: {"episodes": episodes}}
        result = _try_episode_rescan(
            self._match(), client, 44.0, season_cache, 999, {})
        assert result is None

    def test_checks_adjacent_season(self):
        # Current season E3 has 90 min; adjacent season S2E3 has 44 min
        s1_eps = _make_episodes({2: 90, 3: 90, 4: 90})
        s2_eps = _make_episodes({3: 44})
        client = MagicMock()
        client.season.side_effect = lambda tmdb_id, s: (
            {"episodes": s1_eps} if s == 1 else {"episodes": s2_eps}
        )
        season_cache = {1: {"episodes": s1_eps}}
        result = _try_episode_rescan(
            self._match(), client, 44.0, season_cache, 999, {})
        assert result is not None
        assert result["proposed"]["season"] == 2
        assert result["proposed"]["episode"] == 3


class TestDetectCombinedEpisode:
    def _episodes(self):
        return _make_episodes({1: 44, 2: 44, 3: 44, 4: 44})

    def test_detects_pair_when_runtime_matches_sum(self):
        # File is 88 min ≈ E3(44) + E4(44)
        match = {"episode": 3, "tmdb_id": 1}
        result = _detect_combined_episode(match, 88.0, self._episodes())
        assert result is not None
        assert result["episode_start"] == 3
        assert result["episode_end"] == 4

    def test_returns_none_when_ratio_outside_window(self):
        # File is 50 min — only 1.14× single ep, not 1.7–2.4×
        match = {"episode": 1, "tmdb_id": 1}
        result = _detect_combined_episode(match, 50.0, self._episodes())
        assert result is None

    def test_returns_none_when_no_next_episode(self):
        # File is 88 min, current match is E4 (last episode) — no E5 in list
        match = {"episode": 4, "tmdb_id": 1}
        result = _detect_combined_episode(match, 88.0, self._episodes())
        assert result is None

    def test_returns_none_when_sum_does_not_match(self):
        # E3=44, E4=120 — sum=164, file=88 — too far off
        episodes = _make_episodes({3: 44, 4: 120})
        match = {"episode": 3, "tmdb_id": 1}
        result = _detect_combined_episode(match, 88.0, episodes)
        assert result is None


class TestDetectSplitFile:
    def test_returns_none_when_runtime_above_threshold(self):
        # file_min=40, tmdb_min=44 — 40/44 = 0.91 ≥ 0.6, not a split
        result = _detect_split_file("/show/S01E05.mkv", 40.0, 44.0)
        assert result is None

    def test_returns_none_when_no_sibling(self):
        # File is short but sibling doesn't exist
        with patch("backend.rename.service._find_split_sibling", return_value=None):
            result = _detect_split_file("/show/S01E05.Part1.mkv", 20.0, 44.0)
        assert result is None

    def test_detects_split_when_sibling_present(self):
        with patch("backend.rename.service._find_split_sibling",
                   return_value="/show/S01E05.Part2.mkv"):
            result = _detect_split_file("/show/S01E05.Part1.mkv", 20.0, 44.0)
        assert result is not None
        assert result["sibling_path"] == "/show/S01E05.Part2.mkv"
        assert result["part"] in (1, 2)
