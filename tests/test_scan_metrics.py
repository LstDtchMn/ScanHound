"""Tests for the recording-only scan stage counters.

The conservation equations are the point of this file: if they can drift, the
instrumentation is losing events and every ratio built on it is a lie.
"""
import threading

from backend.scan_metrics import (
    MAX_SAMPLES_PER_REASON,
    STAGE_FOR_CODE,
    TAXONOMY_VERSION,
    DiscardCode,
    PostOutcome,
    ScanStage,
    ScanStageCounters,
    message_for,
)


def _started_discard(counters, code, n, stage=None):
    """n posts that a worker actually started and then discarded."""
    for i in range(n):
        counters.note_scheduled()
        counters.note_started()
        counters.note_discard(code, stage=stage, url="https://example.test/%d" % i)


class TestSchedulingVersusExecution:
    """Scheduled, started and requested are three different quantities."""

    def test_cancelled_before_start_is_not_an_attempt(self):
        c = ScanStageCounters()
        c.note_scheduled(10)
        c.note_started()
        c.note_http_request()
        c.note_detail_data()
        c.note_item_created()
        c.note_cancelled_before_start(9)

        # the nine never ran, so they are not started work and cost no requests
        assert c.detail_started == 1
        assert c.detail_http_requests == 1
        assert c.detail_cancelled_before_start == 9
        assert c.conservation_errors() == []

    def test_stopped_scan_does_not_inflate_the_yield_denominator(self):
        """A Stop press must not make cancellation look like failed work."""
        c = ScanStageCounters()
        c.note_scheduled(100)
        c.note_started()
        c.note_detail_data()
        c.note_item_created()
        c.note_cancelled_before_start(99)

        # 1 of 1 started posts yielded a release - not 1 of 100
        assert c.end_to_end_item_yield == 1.0
        assert c.conservation_errors() == []

    def test_cancellation_after_worker_entry_is_started_work(self):
        c = ScanStageCounters()
        c.note_scheduled()
        c.note_started()
        c.note_discard(
            DiscardCode.DETAIL_CANCELLED_BEFORE_REQUEST,
            stage=ScanStage.DETAIL_FETCH,
            url="https://example.test/a",
        )
        assert c.detail_started == 1
        assert c.detail_cancelled_after_start == 1
        assert c.detail_cancelled_before_start == 0
        assert c.conservation_errors() == []

    def test_retries_are_counted_as_requests_not_as_posts(self):
        """One started post can make up to three requests."""
        c = ScanStageCounters()
        c.note_scheduled()
        c.note_started()
        c.note_http_request(3)
        c.note_discard(
            DiscardCode.DETAIL_NO_USABLE_RESPONSE,
            stage=ScanStage.DETAIL_FETCH,
            url="https://example.test/r",
        )

        assert c.detail_started == 1
        assert c.detail_http_requests == 3
        assert c.requests_per_started_post == 3.0
        assert c.conservation_errors() == []


class TestConservation:
    def test_balances_across_a_realistic_cycle(self):
        c = ScanStageCounters()
        for _ in range(2):
            c.note_scheduled()
            c.note_started()
            c.note_detail_data()
            c.note_item_created()
        _started_discard(c, DiscardCode.DETAIL_NO_FILENAME, 5)
        _started_discard(c, DiscardCode.DETAIL_PARSE_EXCEPTION, 3)

        assert c.detail_scheduled == 10
        assert c.conservation_errors() == []

    def test_construction_failure_balances_against_returned_data(self):
        c = ScanStageCounters()
        c.note_scheduled()
        c.note_started()
        c.note_detail_data()
        c.note_discard(
            DiscardCode.MEDIA_ITEM_EXCEPTION,
            stage=ScanStage.MEDIA_ITEM_CONSTRUCTION,
            url="https://example.test/x",
        )

        assert c.detail_returned_data == 1
        assert c.media_item_construction_failed == 1
        assert c.conservation_errors() == []

    def test_lost_event_is_reported_not_raised(self):
        c = ScanStageCounters()
        c.note_scheduled(2)
        c.note_started()
        c.note_detail_data()  # one scheduled post never resolved

        errors = c.conservation_errors()
        assert errors, "a lost event must be visible"
        assert any("detail_scheduled" in e for e in errors)


class TestStageIndependence:
    """Stage is a fact about where it happened, not a lookup from the reason."""

    def test_unknown_can_occur_at_any_stage(self):
        for stage in (
            ScanStage.DETAIL_FETCH,
            ScanStage.DETAIL_PARSE,
            ScanStage.MEDIA_ITEM_CONSTRUCTION,
        ):
            c = ScanStageCounters()
            c.note_scheduled()
            c.note_started()
            if stage is ScanStage.MEDIA_ITEM_CONSTRUCTION:
                c.note_detail_data()
            c.note_discard(DiscardCode.UNKNOWN, stage=stage, url="https://e.test/u")

            assert c.stages == {stage.value: 1}, "stage must follow the event site"
            assert c.samples[0].stage == stage.value
            assert c.conservation_errors() == []

    def test_unknown_has_no_baked_in_stage(self):
        assert DiscardCode.UNKNOWN not in STAGE_FOR_CODE

    def test_stage_map_is_a_default_not_the_only_source(self):
        c = ScanStageCounters()
        c.note_scheduled()
        c.note_started()
        c.note_detail_data()
        # a code whose default stage is parse, recorded at construction
        c.note_discard(
            DiscardCode.DETAIL_EMPTY,
            stage=ScanStage.MEDIA_ITEM_CONSTRUCTION,
            url="https://e.test/o",
        )
        assert c.stages == {"media_item_construction": 1}
        assert c.conservation_errors() == []


class TestTerminalOwnership:
    """Exactly one terminal event per post, across two instrumented layers."""

    def test_inner_reason_is_not_double_counted_by_outer_fallback(self):
        c = ScanStageCounters()
        c.note_scheduled()
        outcome = PostOutcome(c, url="https://e.test/a")
        outcome.note_started()

        # inner scraper books its exact branch
        assert outcome.discard(
            DiscardCode.DETAIL_NO_FILENAME, stage=ScanStage.DETAIL_PARSE
        ) is True
        # outer scanner sees falsy and tries a generic fallback
        assert outcome.booked is True
        assert outcome.discard(DiscardCode.DETAIL_EMPTY) is False

        assert c.reasons == {"detail_no_filename": 1}
        assert c.conservation_errors() == []

    def test_uninstrumented_falsy_result_gets_the_generic_reason(self):
        c = ScanStageCounters()
        c.note_scheduled()
        outcome = PostOutcome(c, url="https://e.test/b")
        outcome.note_started()

        assert outcome.booked is False
        assert outcome.discard(DiscardCode.DETAIL_EMPTY) is True
        assert c.reasons == {"detail_empty": 1}
        assert c.conservation_errors() == []

    def test_created_item_is_terminal_too(self):
        c = ScanStageCounters()
        c.note_scheduled()
        outcome = PostOutcome(c, url="https://e.test/c")
        outcome.note_started()
        outcome.data_returned()
        outcome.item_created()

        assert outcome.discard(DiscardCode.MEDIA_ITEM_EXCEPTION) is False
        assert c.media_item_created == 1
        assert c.conservation_errors() == []

    def test_recorder_faults_never_escape(self):
        """A recorder fault must not reach scan control flow."""

        class Exploding(ScanStageCounters):
            def note_discard(self, *a, **kw):
                raise RuntimeError("boom")

            def note_started(self):
                raise RuntimeError("boom")

        outcome = PostOutcome(Exploding(), url="https://e.test/d")
        outcome.note_started()
        assert outcome.discard(DiscardCode.DETAIL_NO_FILENAME) is False

    def test_tolerates_absent_counters(self):
        outcome = PostOutcome(None, url="https://e.test/e")
        outcome.note_started()
        outcome.note_http_request()
        assert outcome.discard(DiscardCode.DETAIL_EMPTY) is True


class TestVersionFields:
    def test_parser_version_is_not_the_taxonomy_version(self):
        c = ScanStageCounters()
        c.note_scheduled()
        c.note_started()
        c.note_discard(DiscardCode.DETAIL_NO_FILENAME, url="https://e.test/v")

        sample = c.samples[0].to_dict()
        assert sample["taxonomy_version"] == TAXONOMY_VERSION
        assert sample["parser_version"] is None, "must not be copied from taxonomy"

    def test_explicit_parser_version_is_preserved(self):
        c = ScanStageCounters()
        c.note_scheduled()
        c.note_started()
        c.note_discard(
            DiscardCode.DETAIL_NO_FILENAME,
            url="https://e.test/w",
            parser_version="hdencode-detail-2026.07",
        )
        assert c.samples[0].parser_version == "hdencode-detail-2026.07"


class TestTaxonomy:
    def test_every_code_has_a_message(self):
        for code in DiscardCode:
            assert message_for(code)

    def test_known_codes_have_an_expected_stage(self):
        for code in DiscardCode:
            if code is DiscardCode.UNKNOWN:
                continue
            assert isinstance(STAGE_FOR_CODE[code], ScanStage)

    def test_unknown_counts_against_yield(self):
        c = ScanStageCounters()
        c.note_scheduled()
        c.note_started()
        c.note_discard(
            DiscardCode.UNKNOWN, stage=ScanStage.DETAIL_PARSE, url="https://e.test/u"
        )
        assert c.end_to_end_item_yield == 0.0

    def test_unreachable_codes_are_declared(self):
        for name in (
            "MISSING_REQUIRED_TITLE",
            "MISSING_REQUIRED_URL",
            "INVALID_METADATA",
            "SOURCE_BLOCKED",
        ):
            assert hasattr(DiscardCode, name)

    def test_all_seven_scraper_branches_have_codes(self):
        for name in (
            "DETAIL_CANCELLED_BEFORE_REQUEST",
            "DETAIL_CANCELLED_IN_COORDINATOR",
            "DETAIL_TRAFFIC_DENIED",
            "DETAIL_RETRY_SLEEP_CANCELLED",
            "DETAIL_NO_USABLE_RESPONSE",
            "DETAIL_NO_FILENAME",
            "DETAIL_PARSE_EXCEPTION",
        ):
            assert hasattr(DiscardCode, name)


class TestBoundedSamples:
    def test_samples_are_capped_per_reason(self):
        c = ScanStageCounters()
        _started_discard(c, DiscardCode.DETAIL_NO_FILENAME, 126)

        assert c.reasons["detail_no_filename"] == 126
        assert len(c.samples) == MAX_SAMPLES_PER_REASON
        assert c.conservation_errors() == []

    def test_samples_carry_no_response_body(self):
        c = ScanStageCounters()
        c.note_scheduled()
        c.note_started()
        c.note_discard(
            DiscardCode.DETAIL_NO_FILENAME,
            stage=ScanStage.DETAIL_PARSE,
            url="https://e.test/a",
            source="hdencode",
            category="4k",
            content_fingerprint="deadbeef",
        )
        assert set(c.samples[0].to_dict()) == {
            "canonical_url", "stage", "reason_code", "source", "category",
            "exception_type", "taxonomy_version", "parser_version",
            "content_fingerprint",
        }


class TestThreadSafety:
    def test_concurrent_discards_do_not_lose_events(self):
        """d[k] += 1 is interruptible; without a lock this drops updates."""
        c = ScanStageCounters()
        per_thread, threads = 200, 8

        def worker():
            for _ in range(per_thread):
                c.note_scheduled()
                c.note_started()
                c.note_discard(
                    DiscardCode.DETAIL_NO_FILENAME, url="https://e.test/x"
                )

        ts = [threading.Thread(target=worker) for _ in range(threads)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()

        assert c.detail_started == threads * per_thread
        assert c.reasons["detail_no_filename"] == threads * per_thread
        assert c.conservation_errors() == []

    def test_one_outcome_survives_a_race_between_layers(self):
        """Both layers booking concurrently must still yield one terminal event."""
        c = ScanStageCounters()
        c.note_scheduled()
        outcome = PostOutcome(c, url="https://e.test/race")
        outcome.note_started()
        start = threading.Event()
        results = []

        def claim(code):
            start.wait()
            results.append(outcome.discard(code))

        ts = [
            threading.Thread(target=claim, args=(DiscardCode.DETAIL_NO_FILENAME,)),
            threading.Thread(target=claim, args=(DiscardCode.DETAIL_EMPTY,)),
        ]
        for t in ts:
            t.start()
        start.set()
        for t in ts:
            t.join()

        assert results.count(True) == 1
        assert sum(c.reasons.values()) == 1
        assert c.conservation_errors() == []


class TestReporting:
    def test_three_ratios_are_distinct(self):
        """Parse success, construction success and item yield differ."""
        c = ScanStageCounters()
        # 10 started: 4 yield data, of which 3 become releases
        for _ in range(4):
            c.note_scheduled()
            c.note_started()
            c.note_detail_data()
        for _ in range(3):
            c.note_item_created()
        c.note_discard(
            DiscardCode.MEDIA_ITEM_EXCEPTION,
            stage=ScanStage.MEDIA_ITEM_CONSTRUCTION,
            url="https://e.test/m",
        )
        _started_discard(c, DiscardCode.DETAIL_NO_FILENAME, 6)

        assert c.detail_started == 10
        assert c.detail_parse_success_ratio == 0.4
        assert c.media_item_construction_success_ratio == 0.75
        assert c.end_to_end_item_yield == 0.3
        assert c.conservation_errors() == []

    def test_production_shape(self):
        """The observed failure: ~128 started, 2 released."""
        c = ScanStageCounters()
        for _ in range(2):
            c.note_scheduled()
            c.note_started()
            c.note_detail_data()
            c.note_item_created()
        _started_discard(c, DiscardCode.DETAIL_NO_FILENAME, 126)

        assert c.detail_started == 128
        assert round(c.end_to_end_item_yield, 4) == round(2 / 128.0, 4)
        assert "detail_no_filename=126" in c.summary_line()
        assert c.conservation_errors() == []

    def test_empty_scan_is_not_reported_as_failure(self):
        c = ScanStageCounters()
        assert c.end_to_end_item_yield == 1.0
        assert c.detail_parse_success_ratio == 1.0
        assert c.conservation_errors() == []
