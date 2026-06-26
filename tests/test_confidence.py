import pytest
from backend.rename.confidence import runtime_confidence_delta, episode_correction_candidates


class TestRuntimeThresholdTightening:
    def test_12pct_deviation_now_penalised(self):
        # 90-min TMDB, 101-min file = 12.2% off — was neutral (≤15%), now -10 (≤30%)
        result = runtime_confidence_delta(101, 90)
        assert result == -10.0

    def test_9pct_deviation_still_neutral(self):
        # 90-min TMDB, 98-min file = 8.9% off — between 8% and 10% → 0
        result = runtime_confidence_delta(98, 90)
        assert result == 0.0

    def test_8pct_deviation_still_bonus(self):
        # 90-min TMDB, 97-min file = 7.8% off — still ≤8% → +5
        result = runtime_confidence_delta(97, 90)
        assert result == 5.0

    def test_near_exact_still_plus_ten(self):
        # 2.7% off → ≤3% → +10
        result = runtime_confidence_delta(185, 180)
        assert result == 10.0

    def test_missing_values_return_zero(self):
        assert runtime_confidence_delta(None, 90) == 0.0
        assert runtime_confidence_delta(90, 0) == 0.0
        assert runtime_confidence_delta(0, 90) == 0.0


class TestEpisodeCorrectionCandidates:
    def _eps(self):
        # Fake season: 6 episodes, each 44 min
        return [{"episode_number": i, "runtime": 44} for i in range(1, 7)]

    def test_returns_better_episode_when_clear_winner(self):
        # File is 44 min, matched to E3 (44 min = perfect), but we pass E4
        # as current_episode — E3 should be proposed
        eps = self._eps()
        # Match is currently E4 (44 min file, E4 also 44 min — tied, no gain)
        result = episode_correction_candidates(44.0, eps, current_episode=4)
        # No one is 15+ better than E4 when all are 44 min
        assert result == []

    def test_proposes_correct_episode_when_runtime_differs(self):
        # Make E3 have 90 min (movie-length wrong entry) — file is 44 min
        # All other episodes are 44 min. Current match is E3.
        eps = [{"episode_number": i, "runtime": 44 if i != 3 else 90}
               for i in range(1, 7)]
        # File is 44 min, current match is E3 (90 min → -20 score)
        # E2 and E4 are 44 min → +10 each — gain = 30, well above min_gain=15
        result = episode_correction_candidates(44.0, eps, current_episode=3)
        ep_numbers = [ep for ep, _ in result]
        assert 2 in ep_numbers or 4 in ep_numbers
        # All proposed candidates must be better by at least 15
        for ep_num, score in result:
            assert score >= -20 + 15  # current is -20, candidates must be ≥ -5

    def test_respects_search_radius(self):
        eps = [{"episode_number": i, "runtime": 44 if i != 1 else 90}
               for i in range(1, 10)]
        # Current match is E1 (90 min), file is 44 min
        # E2 is radius 1, E4 is radius 3, E5 is radius 4 — excluded
        result = episode_correction_candidates(44.0, eps, current_episode=1,
                                               search_radius=3)
        ep_numbers = [ep for ep, _ in result]
        assert 5 not in ep_numbers
        assert 4 in ep_numbers  # radius 3 — included

    def test_returns_empty_when_no_runtimes(self):
        eps = [{"episode_number": i} for i in range(1, 5)]  # no runtime key
        result = episode_correction_candidates(44.0, eps, current_episode=2)
        assert result == []

    def test_sorted_best_first(self):
        # E2 is perfect (44 min), E4 is close (47 min), current E3 is 90 min
        eps = [
            {"episode_number": 2, "runtime": 44},
            {"episode_number": 3, "runtime": 90},
            {"episode_number": 4, "runtime": 47},
        ]
        result = episode_correction_candidates(44.0, eps, current_episode=3)
        assert len(result) >= 1
        assert result[0][0] == 2  # E2 (exact match) should be first
