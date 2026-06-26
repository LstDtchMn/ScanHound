import pytest
from backend.rename.confidence import runtime_confidence_delta


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
