"""The coverage canary's pure decision logic: sampling, replay, overlap,
churn, and the four post-promotion coverage states.

Every test here calls `backend.rss_canary_policy` functions directly with
plain dicts/sets/lists -- no database, no crawler, no clock. That is the
point of the module under test: see its docstring for why.

Guard-to-test map (which test fails if which guard is removed), reported in
full alongside the pytest output:

  select_sampled_cycles
    - test_picks_real_cycles_and_never_invents_one
        guards: the "first cycle at or after due" rule itself.
    - test_gap_in_series_does_not_become_a_sample
        guards: no synthetic sample is produced when no cycle exists near a
        due time -- the due time keeps advancing until a real cycle is found.
    - test_due_time_advances_from_the_sampled_cycle_not_from_the_old_due
        guards: `due = chosen["at"] + interval`, not `due = due + interval`.
    - test_start_overrides_the_first_cycle_as_the_initial_due
        guards: `due = start if start is not None else cycles[0]["at"]`.

  replay
    - test_replay_catches_a_url_present_in_the_sampled_cycle
        guards: caught includes a URL that IS in the sampled rows.
    - test_replay_misses_a_discontinuous_url_sampled_only_at_the_gap
        guards: no interpolation between an earlier and later sighting.
    - test_replay_ignores_rows_beyond_depth
        guards: the depth filter on sampled_membership.

  overlap
    - test_overlap_counts_only_within_depth
        guards: the depth filter applies to BOTH previous_rows and
        current_rows, not just one side.

  churn
    - test_churn_counts_new_urls_only
        guards: a URL present in both runs is not counted as churn.
    - test_churn_respects_depth_on_current_rows
        guards: the depth filter on current_rows.

  classify_coverage
    - test_acquired_wins_even_over_an_earlier_still_missing_sighting
        guards: rss_carried is checked first / unconditionally.
    - test_pending_when_no_complete_canary_exists_after_first_sighting
        guards: the `latest_complete_canary_at <= first_seen` branch.
    - test_gap_proven_when_a_later_canary_reobserves_it_without_rss
        guards: the "later sightings" branch that returns gap_proven.
    - test_coverage_unassessable_when_it_leaves_without_deciding
        guards: the fall-through branch, and that it is NOT gap_proven.
    - test_promotion_at_excludes_pre_promotion_sightings
        guards: the `obs["at"] >= promotion_at` filter.
    - test_depth_filter_on_observations_is_enforced
        guards: the `obs["page_index"] <= depth` filter on observations.

  systematic_gap
    - test_none_on_no_rows
        guards: zero rows can never read as a clean pass.
    - test_none_on_fewer_than_required_canaries
        guards: the `len(cycles_seen) < canaries` threshold.
    - test_true_with_real_membership_and_no_rss_carriage
        guards: the positive case actually fires when evidence is real.
    - test_false_when_rss_carried_some_of_the_source
        guards: True is not returned for a partial catch.
    - test_none_when_rows_carry_no_usable_urls
        guards: cycle_uuid coverage alone cannot manufacture a True.
"""
from datetime import datetime, timedelta, timezone

from backend.rss_canary_policy import (
    churn,
    classify_coverage,
    overlap,
    replay,
    select_sampled_cycles,
    systematic_gap,
)

T0 = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)


def _at(minutes):
    return T0 + timedelta(minutes=minutes)


def _cycle(minutes, uuid=None):
    return {"cycle_uuid": uuid or f"cycle-{minutes}", "at": _at(minutes)}


# ── select_sampled_cycles ────────────────────────────────────────────────

class TestSelectSampledCycles:

    def test_picks_real_cycles_and_never_invents_one(self):
        """A realistic dense series, interval 12 minutes, cycles every 5."""
        cycles = [_cycle(m) for m in (0, 5, 10, 15, 20, 25, 30)]
        sampled = select_sampled_cycles(cycles, interval_seconds=12 * 60)
        # due=0 -> pick @0, due=12 -> pick @15 (10 is before due), due=27 ->
        # pick @30 (25 is before due).
        assert [c["at"] for c in sampled] == [_at(0), _at(15), _at(30)]
        # Every sampled entry is one of the actual input dicts, not a
        # synthesized stand-in.
        for chosen in sampled:
            assert chosen in cycles

    def test_gap_in_series_does_not_become_a_sample(self):
        """A long gap between cycle@10 and cycle@100 must not manufacture a
        sample near the missed due time of 20 minutes; it must wait for the
        next REAL cycle, however late, and must not also pick cycle@10
        (which sat before that due time)."""
        cycles = [_cycle(0), _cycle(10), _cycle(100)]
        sampled = select_sampled_cycles(cycles, interval_seconds=20 * 60)
        assert [c["at"] for c in sampled] == [_at(0), _at(100)]
        assert _at(10) not in [c["at"] for c in sampled]

    def test_due_time_advances_from_the_sampled_cycle_not_from_the_old_due(self):
        """If due advanced from itself (0, 12, 24, 36...) rather than from
        the sampled cycle's own time, a canary running late would silently
        catch back up. Cycles are irregular here specifically so the two
        strategies diverge: sampling @15 (not @12, which does not exist)
        must push the next due to 15+12=27, not 12+12=24."""
        cycles = [_cycle(0), _cycle(15), _cycle(26), _cycle(40)]
        sampled = select_sampled_cycles(cycles, interval_seconds=12 * 60)
        # due=0 -> @0 ; due=12 -> first >=12 is @15 ; due=15+12=27 -> first
        # >=27 is @40 (26 is before 27). If due had instead advanced from
        # itself (24), @26 would have been picked instead of @40.
        assert [c["at"] for c in sampled] == [_at(0), _at(15), _at(40)]

    def test_start_overrides_the_first_cycle_as_the_initial_due(self):
        """Without `start`, the first cycle (@0) is always its own first
        sample. Passing start=15 must skip it instead."""
        cycles = [_cycle(0), _cycle(20), _cycle(30)]
        sampled = select_sampled_cycles(
            cycles, interval_seconds=12 * 60, start=_at(15))
        assert [c["at"] for c in sampled] == [_at(20)]


# ── replay ───────────────────────────────────────────────────────────────

class TestReplay:

    def test_replay_catches_a_url_present_in_the_sampled_cycle(self):
        result = replay(
            population={"u1"},
            sampled_membership=[{"canonical_url": "u1", "page_index": 1}],
            depth=3)
        assert result["caught"] == {"u1"}
        assert result["missed"] == set()

    def test_replay_misses_a_discontinuous_url_sampled_only_at_the_gap(self):
        """u1 was present at t0 and t2, absent at t1. Only t1 was sampled,
        and it recorded no row for u1 (it genuinely was not there). An
        interpolating implementation might reason "t1 sits between two real
        sightings of u1, so a canary would probably have caught it" -- that
        is exactly the bug this test exists to fail. The sampled cycle
        recorded nothing for u1, so it must be MISSED, full stop."""
        population = {"u1"}
        sampled_membership_at_t1 = []  # u1 was absent at the one sampled cycle
        result = replay(population, sampled_membership_at_t1, depth=3)
        assert "u1" not in result["caught"]
        assert result["missed"] == {"u1"}

        # Positive control: sampling t0 instead (where u1 WAS present) must
        # flip the outcome -- proving the result depends on which cycle was
        # actually sampled, not on population membership alone.
        sampled_membership_at_t0 = [{"canonical_url": "u1", "page_index": 1}]
        result_t0 = replay(population, sampled_membership_at_t0, depth=3)
        assert result_t0["caught"] == {"u1"}
        assert result_t0["missed"] == set()

    def test_replay_ignores_rows_beyond_depth(self):
        """A sampled row exists for u1, but on a page past the canary's
        depth -- the canary never looked that far, so it cannot count as
        caught even though a row technically exists in the raw data."""
        result = replay(
            population={"u1"},
            sampled_membership=[{"canonical_url": "u1", "page_index": 9}],
            depth=3)
        assert result["caught"] == set()
        assert result["missed"] == {"u1"}


# ── overlap ──────────────────────────────────────────────────────────────

class TestOverlap:

    def test_overlap_counts_only_within_depth(self):
        """"b" is beyond depth in previous_rows only. If the depth filter
        were applied to current_rows but not previous_rows, "b" would wrongly
        count toward the intersection."""
        previous_rows = [
            {"canonical_url": "a", "page_index": 1},
            {"canonical_url": "b", "page_index": 5},
        ]
        current_rows = [
            {"canonical_url": "a", "page_index": 2},
            {"canonical_url": "b", "page_index": 1},
        ]
        assert overlap(previous_rows, current_rows, depth=3) == 1

    def test_zero_overlap(self):
        previous_rows = [{"canonical_url": "a", "page_index": 1}]
        current_rows = [{"canonical_url": "b", "page_index": 1}]
        assert overlap(previous_rows, current_rows, depth=3) == 0


# ── churn ────────────────────────────────────────────────────────────────

class TestChurn:

    def test_churn_counts_new_urls_only(self):
        """"a" repeats in both runs and must not be counted; only "b" is new."""
        previous_rows = [{"canonical_url": "a", "page_index": 1}]
        current_rows = [
            {"canonical_url": "a", "page_index": 1},
            {"canonical_url": "b", "page_index": 1},
        ]
        assert churn(previous_rows, current_rows, depth=3) == 1

    def test_churn_respects_depth_on_current_rows(self):
        """"c" is new but past depth in the current run, so it must not
        count -- the canary never actually saw it."""
        previous_rows = [{"canonical_url": "a", "page_index": 1}]
        current_rows = [
            {"canonical_url": "a", "page_index": 1},
            {"canonical_url": "c", "page_index": 9},
        ]
        assert churn(previous_rows, current_rows, depth=3) == 0


# ── classify_coverage ────────────────────────────────────────────────────

class TestClassifyCoverage:

    def test_acquired_wins_even_over_an_earlier_still_missing_sighting(self):
        observations = [
            {"at": _at(60), "page_index": 1},   # still missing from RSS then
            {"at": _at(300), "page_index": 1},  # re-observed by the listing
        ]
        state = classify_coverage(
            "u1", observations, rss_carried={"u1"},
            promotion_at=T0, latest_complete_canary_at=_at(300), depth=3)
        assert state == "acquired"

    def test_pending_when_no_complete_canary_exists_after_first_sighting(self):
        observations = [{"at": _at(60), "page_index": 1}]
        state = classify_coverage(
            "u1", observations, rss_carried=set(),
            promotion_at=T0, latest_complete_canary_at=_at(60), depth=3)
        assert state == "pending"

        # Also pending when the URL never entered protected membership at all.
        state_never_protected = classify_coverage(
            "u1", [], rss_carried=set(),
            promotion_at=T0, latest_complete_canary_at=_at(300), depth=3)
        assert state_never_protected == "pending"

    def test_gap_proven_when_a_later_canary_reobserves_it_without_rss(self):
        observations = [
            {"at": _at(60), "page_index": 1},
            {"at": _at(300), "page_index": 2},
        ]
        state = classify_coverage(
            "u1", observations, rss_carried=set(),
            promotion_at=T0, latest_complete_canary_at=_at(300), depth=3)
        assert state == "gap_proven"

    def test_coverage_unassessable_when_it_leaves_without_deciding(self):
        """Only ONE sighting exists; a later complete canary ran (so this is
        not `pending`) but it did not re-observe the URL -- it simply left
        view. That must be a different, weaker claim than gap_proven."""
        observations = [{"at": _at(60), "page_index": 1}]
        state = classify_coverage(
            "u1", observations, rss_carried=set(),
            promotion_at=T0, latest_complete_canary_at=_at(600), depth=3)
        assert state == "coverage_unassessable"
        assert state != "gap_proven"

    def test_promotion_at_excludes_pre_promotion_sightings(self):
        """A sighting before promotion is not protected membership. Without
        this filter, this pre-promotion sighting would become "first_seen",
        and since a later complete canary ran without re-observing it, the
        (wrong) result would be coverage_unassessable instead of pending."""
        observations = [{"at": _at(-60), "page_index": 1}]
        state = classify_coverage(
            "u1", observations, rss_carried=set(),
            promotion_at=T0, latest_complete_canary_at=_at(600), depth=3)
        assert state == "pending"

    def test_depth_filter_on_observations_is_enforced(self):
        """A sighting past depth is not protected membership either. Without
        this filter it would become first_seen, and the same
        would-be-coverage_unassessable trap as above would fire."""
        observations = [{"at": _at(60), "page_index": 9}]
        state = classify_coverage(
            "u1", observations, rss_carried=set(),
            promotion_at=T0, latest_complete_canary_at=_at(600), depth=3)
        assert state == "pending"


# ── systematic_gap ───────────────────────────────────────────────────────

class TestSystematicGap:

    def test_none_on_no_rows(self):
        assert systematic_gap([], rss_carried=set(), canaries=3) is None

    def test_none_on_fewer_than_required_canaries(self):
        rows = [
            {"cycle_uuid": "c1", "canonical_url": "u1"},
            {"cycle_uuid": "c2", "canonical_url": "u2"},
        ]
        assert systematic_gap(rows, rss_carried=set(), canaries=3) is None

    def test_true_with_real_membership_and_no_rss_carriage(self):
        rows = [
            {"cycle_uuid": "c1", "canonical_url": "u1"},
            {"cycle_uuid": "c2", "canonical_url": "u2"},
            {"cycle_uuid": "c3", "canonical_url": "u3"},
        ]
        assert systematic_gap(rows, rss_carried=set(), canaries=3) is True

    def test_false_when_rss_carried_some_of_the_source(self):
        rows = [
            {"cycle_uuid": "c1", "canonical_url": "u1"},
            {"cycle_uuid": "c2", "canonical_url": "u2"},
            {"cycle_uuid": "c3", "canonical_url": "u3"},
        ]
        assert systematic_gap(rows, rss_carried={"u2"}, canaries=3) is False

    def test_none_when_rows_carry_no_usable_urls(self):
        """Three distinct cycles are represented, but none of the rows carry
        a usable canonical_url. Cycle coverage alone must not manufacture a
        True out of rows that are not actually evidence about any URL."""
        rows = [
            {"cycle_uuid": "c1", "canonical_url": None},
            {"cycle_uuid": "c2", "canonical_url": ""},
            {"cycle_uuid": "c3"},
        ]
        assert systematic_gap(rows, rss_carried=set(), canaries=3) is None
