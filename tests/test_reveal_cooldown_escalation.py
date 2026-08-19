"""The reveal-stall cooldown escalates, resets on success, and is configurable.

WHY THIS EXISTS. A reveal stall previously called observe_challenge(), which
hard-codes 60*60 -- a value chosen for Cloudflare interstitials. A 2026-08-06
peer review called inheriting it "a reasonable emergency safety value, but not a
validated source policy" and asked for a configurable, escalating cooldown with
telemetry.

Measurement the same night made the case concrete:

    ~18:00Z  reveal stalls begin
     22:49Z  the 1h cooldown expires
     23:02Z  the ONE automatic batch probe fires -> still refused
             (the source was still throttling ~5 hours in)

Batch auto-resume is ONE-SHOT. So a too-short cooldown spends the single probe
against a closed door and leaves the batch parked until a human intervenes --
which is exactly what happened, and had to be corrected by hand-editing the
database twice. That manual step is what this replaces.
"""
import pytest

from backend.hdencode_coordinator import HDEncodeTrafficCoordinator


class _FixedRng:
    """Zero jitter, so durations are exact and assertions are not flaky."""

    @staticmethod
    def uniform(_a, _b):
        return 0.0


@pytest.fixture
def coord():
    c = HDEncodeTrafficCoordinator()
    c._config = {"hdencode_enabled": True}
    return c


def seconds_of(coord_obj):
    return coord_obj.reveal_telemetry()["last_cooldown_seconds"]


class TestEscalation:

    def test_first_stall_uses_the_base(self, coord):
        coord.observe_reveal_stall(rng=_FixedRng)
        assert seconds_of(coord) == 60 * 60
        assert coord.reveal_telemetry()["last_escalation_step"] == 1

    def test_consecutive_stalls_escalate_1h_2h_4h(self, coord):
        got = []
        for _ in range(3):
            coord.observe_reveal_stall(rng=_FixedRng)
            got.append(seconds_of(coord) // 3600)
        assert got == [1, 2, 4], (
            "escalation must climb so the one-shot probe is not spent on a "
            "source that is still shut")

    def test_escalation_is_held_at_the_ceiling(self, coord):
        for _ in range(6):
            coord.observe_reveal_stall(rng=_FixedRng)
        assert seconds_of(coord) == 4 * 60 * 60, "must not grow without bound"

    def test_a_success_resets_the_escalation(self, coord):
        for _ in range(3):
            coord.observe_reveal_stall(rng=_FixedRng)
        assert seconds_of(coord) == 4 * 60 * 60
        coord.observe_reveal_success()
        coord.observe_reveal_stall(rng=_FixedRng)
        assert seconds_of(coord) == 60 * 60, (
            "without a reset the streak ratchets forever and every later stall "
            "draws the maximum regardless of intervening health")


class TestConfigurable:
    """The point of the change: no longer inheriting the Cloudflare constant."""

    def test_the_base_comes_from_config(self, coord):
        coord._config = {"hdencode_reveal_cooldown_minutes": 240}
        coord.observe_reveal_stall(rng=_FixedRng)
        assert seconds_of(coord) == 240 * 60

    def test_an_explicit_argument_wins(self, coord):
        coord._config = {"hdencode_reveal_cooldown_minutes": 60}
        coord.observe_reveal_stall(base_minutes=120, rng=_FixedRng)
        assert seconds_of(coord) == 120 * 60

    @pytest.mark.parametrize("bad", ["not a number", None, "", [], {}])
    def test_a_bad_config_value_falls_back_to_the_default(self, coord, bad):
        coord._config = {"hdencode_reveal_cooldown_minutes": bad}
        coord.observe_reveal_stall(rng=_FixedRng)
        assert seconds_of(coord) == 60 * 60

    def test_the_base_is_clamped(self, coord):
        coord._config = {"hdencode_reveal_cooldown_minutes": 0}
        coord.observe_reveal_stall(rng=_FixedRng)
        assert seconds_of(coord) >= 60
        coord.observe_reveal_success()
        coord._config = {"hdencode_reveal_cooldown_minutes": 99999}
        coord.observe_reveal_stall(rng=_FixedRng)
        assert seconds_of(coord) <= 24 * 60 * 60


class TestJitter:
    """Many deferred items must not all probe at the same instant."""

    def test_jitter_stays_within_ten_percent(self, coord):
        base = 60 * 60
        for _ in range(40):
            c = HDEncodeTrafficCoordinator()
            c._config = {}
            c.observe_reveal_stall()
            assert base * 0.89 <= seconds_of(c) <= base * 1.11

    def test_jitter_actually_varies(self, coord):
        seen = set()
        for _ in range(30):
            c = HDEncodeTrafficCoordinator()
            c._config = {}
            c.observe_reveal_stall()
            seen.add(seconds_of(c))
        assert len(seen) > 1, "no jitter means a synchronised stampede on resume"


class TestTheDecisionTheQueueConsumes:
    """The queue needs cooldown_until to pause and later resume the batch."""

    def test_the_decision_carries_a_cooldown_and_reason(self, coord):
        d = coord.observe_reveal_stall(rng=_FixedRng)
        assert d.cooldown_until, "without this the batch cannot schedule a resume"
        assert d.reason_code == "reveal_verification_stalled"

    def test_the_shared_cooldown_is_set(self, coord):
        """The pre-scrape source gate reads this, which is what stops later items
        paying the 60-second detection cost again."""
        coord.observe_reveal_stall(rng=_FixedRng)
        assert coord._local_cooldown_until is not None
        assert coord._local_cooldown_reason == "reveal_verification_stalled"


class TestTelemetry:
    """The review asked for enough evidence to CHOOSE a policy, not guess one."""

    def test_stalls_and_successes_are_counted(self, coord):
        coord.observe_reveal_stall(rng=_FixedRng)
        coord.observe_reveal_stall(rng=_FixedRng)
        coord.observe_reveal_success()
        t = coord.reveal_telemetry()
        assert t["stalls"] == 2
        assert t["successes"] == 1
        assert t["stall_streak"] == 0

    def test_timestamps_are_recorded(self, coord):
        coord.observe_reveal_stall(rng=_FixedRng)
        assert coord.reveal_telemetry()["last_stall_at"]
        coord.observe_reveal_success()
        assert coord.reveal_telemetry()["last_success_at"]

    def test_telemetry_is_empty_before_anything_happens(self, coord):
        t = coord.reveal_telemetry()
        assert t["stalls"] == 0 and t["stall_streak"] == 0
        assert t["last_stall_at"] is None


class TestObserveChallengeIsUntouched:
    """Cloudflare handling must keep its own fixed value and its own counter."""

    def test_challenge_still_uses_one_hour_and_its_own_metric(self, coord):
        d = coord.observe_challenge()
        assert d.cooldown_until
        assert coord.reveal_telemetry()["stalls"] == 0, (
            "a Cloudflare challenge must not advance the reveal-stall streak")
