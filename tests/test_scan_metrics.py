"""Tests for the recording-only scan stage counters.

The conservation equations are the point of this file: if they can drift, the
instrumentation is losing events and every ratio built on it is a lie.
"""
import threading

from backend.scan_metrics import (
    MAX_SAMPLES_PER_CYCLE,
    MAX_SAMPLES_PER_REASON,
    TerminalKind,
    FutureTerminalState,
    default_kind_for,
    default_stage_for,
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
            kind = TerminalKind.RETURNED_NONE
            if stage is ScanStage.MEDIA_ITEM_CONSTRUCTION:
                c.note_detail_data()
                kind = TerminalKind.CONSTRUCTION_FAILED
            c.note_discard(
                DiscardCode.UNKNOWN,
                stage=stage,
                terminal_kind=kind,
                url="https://e.test/u",
            )

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
            terminal_kind=TerminalKind.CONSTRUCTION_FAILED,
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
        # UNKNOWN and TERMINAL_OUTCOME_MISSING are deliberately absent: neither
        # has a natural stage, and defaulting one would hide where it happened.
        stageless = {DiscardCode.UNKNOWN, DiscardCode.TERMINAL_OUTCOME_MISSING}
        for code in DiscardCode:
            if code in stageless:
                assert code not in STAGE_FOR_CODE
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
            "canonical_url", "stage", "reason_code", "terminal_kind",
            "source", "category", "exception_type", "taxonomy_version",
            "parser_version", "content_fingerprint",
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


class TestAbandonedOnStop:
    """A worker can finish successfully and still have its result unused."""

    def test_successful_detail_stranded_by_stop_has_its_own_state(self):
        c = ScanStageCounters()
        c.note_scheduled()
        t = PostOutcome(c, url="https://e.test/stranded")
        t.note_started()
        t.data_returned()
        # Stop breaks the main loop before the result is consumed
        assert t.reconcile(FutureTerminalState.COMPLETED_WITH_DATA) is DiscardCode.MEDIA_ITEM_ABANDONED_ON_STOP

        assert c.media_item_abandoned_on_stop == 1
        assert c.media_item_created == 0
        assert c.media_item_construction_failed == 0
        # it is NOT a cancellation: the work completed
        assert c.detail_cancelled_after_start == 0
        assert c.conservation_errors() == []

    def test_abandoned_result_does_not_create_an_item(self):
        """Behaviour preservation: metrics must not manufacture a release."""
        c = ScanStageCounters()
        c.note_scheduled()
        t = PostOutcome(c, url="https://e.test/x")
        t.note_started()
        t.data_returned()
        t.reconcile(FutureTerminalState.COMPLETED_WITH_DATA)
        assert c.media_item_created == 0


class TestPostDrainReconciliation:
    def test_cancelled_future_books_before_start_only(self):
        c = ScanStageCounters()
        c.note_scheduled()
        t = PostOutcome(c, url="https://e.test/c")
        assert t.reconcile(FutureTerminalState.CANCELLED_BEFORE_START) is DiscardCode.DETAIL_CANCELLED_BEFORE_START

        assert c.detail_cancelled_before_start == 1
        assert c.detail_started == 0
        assert c.detail_http_requests == 0
        assert c.conservation_errors() == []

    def test_already_booked_ticket_is_left_alone(self):
        c = ScanStageCounters()
        c.note_scheduled()
        t = PostOutcome(c, url="https://e.test/b")
        t.note_started()
        t.discard(DiscardCode.DETAIL_NO_FILENAME, stage=ScanStage.DETAIL_PARSE)

        assert t.reconcile(FutureTerminalState.COMPLETED_FALSEY) is None
        assert c.reasons == {"detail_no_filename": 1}
        assert c.conservation_errors() == []

    def test_uninstrumented_falsy_result_gets_generic_fallback(self):
        c = ScanStageCounters()
        c.note_scheduled()
        t = PostOutcome(c, url="https://e.test/f")
        t.note_started()
        assert t.reconcile(FutureTerminalState.COMPLETED_FALSEY) is DiscardCode.DETAIL_EMPTY
        assert c.reasons == {"detail_empty": 1}
        assert c.conservation_errors() == []

    def test_unexpected_worker_exception_books_bounded_unknown(self):
        c = ScanStageCounters()
        c.note_scheduled()
        t = PostOutcome(c, url="https://e.test/e")
        t.note_started()
        t.note_exception_type("ValueError")
        assert t.reconcile(FutureTerminalState.COMPLETED_EXCEPTION) is DiscardCode.UNKNOWN

        assert c.reasons == {"unknown": 1}
        assert c.samples[0].exception_type == "ValueError"
        assert c.conservation_errors() == []

    def test_reconcile_without_terminal_state_does_not_claim(self):
        """A premature call must not steal the claim from a running worker."""
        c = ScanStageCounters()
        c.note_scheduled()
        t = PostOutcome(c, url="https://e.test/?")

        assert t.reconcile() is None
        assert t.reconcile(state=None) is None
        assert t.booked is False
        assert c.reconcile_misuse == 2
        assert c.reasons == {}

        # the worker can still record its own exact branch afterwards
        t.note_started()
        assert t.discard(
            DiscardCode.DETAIL_NO_FILENAME, stage=ScanStage.DETAIL_PARSE
        ) is True
        assert c.reasons == {"detail_no_filename": 1}
        assert c.conservation_errors() == []

    def test_mixed_stop_cohort_balances(self):
        """10 scheduled: 1 released, 1 stranded, 3 ran and failed, 5 never ran."""
        c = ScanStageCounters()
        c.note_scheduled(10)

        done = PostOutcome(c, url="https://e.test/1")
        done.note_started()
        done.data_returned()
        done.item_created()

        stranded = PostOutcome(c, url="https://e.test/2")
        stranded.note_started()
        stranded.data_returned()

        failed = []
        for i in range(3):
            t = PostOutcome(c, url="https://e.test/f%d" % i)
            t.note_started()
            t.discard(DiscardCode.DETAIL_NO_FILENAME, stage=ScanStage.DETAIL_PARSE)
            failed.append(t)

        never = [PostOutcome(c, url="https://e.test/n%d" % i) for i in range(5)]

        stranded.reconcile(FutureTerminalState.COMPLETED_WITH_DATA)
        for t in never:
            t.reconcile(FutureTerminalState.CANCELLED_BEFORE_START)

        assert c.detail_scheduled == 10
        assert c.detail_started == 5
        assert c.detail_cancelled_before_start == 5
        assert c.media_item_created == 1
        assert c.media_item_abandoned_on_stop == 1
        assert c.conservation_errors() == []


class TestTicketLifecycle:
    def test_note_started_is_idempotent(self):
        c = ScanStageCounters()
        t = PostOutcome(c, url="https://e.test/i")
        t.note_started()
        t.note_started()
        assert c.detail_started == 1

    def test_data_returned_is_idempotent(self):
        c = ScanStageCounters()
        t = PostOutcome(c, url="https://e.test/j")
        t.note_started()
        t.data_returned()
        t.data_returned()
        assert c.detail_returned_data == 1

    def test_http_requests_are_not_idempotent(self):
        """Each retry is a real extra request and must be counted."""
        c = ScanStageCounters()
        t = PostOutcome(c, url="https://e.test/k")
        t.note_started()
        t.note_http_request()
        t.note_http_request()
        t.note_http_request()
        assert c.detail_http_requests == 3

    def test_snapshot_reports_the_lifecycle(self):
        c = ScanStageCounters()
        t = PostOutcome(c, url="https://e.test/s")
        assert t.snapshot().started is False
        t.note_started()
        t.data_returned()
        s = t.snapshot()
        assert (s.started, s.data_returned, s.terminal_booked) == (True, True, False)
        t.discard(DiscardCode.MEDIA_ITEM_EXCEPTION, stage=ScanStage.MEDIA_ITEM_CONSTRUCTION)
        s = t.snapshot()
        assert s.terminal_booked is True
        assert s.terminal_code == "media_item_exception"

    def test_snapshot_records_item_creation_as_terminal(self):
        c = ScanStageCounters()
        t = PostOutcome(c, url="https://e.test/t")
        t.note_started()
        t.data_returned()
        t.item_created()
        assert t.snapshot().terminal_code == "item_created"

    def test_reconcile_races_still_book_once(self):
        c = ScanStageCounters()
        c.note_scheduled()
        t = PostOutcome(c, url="https://e.test/race2")
        t.note_started()
        t.data_returned()
        start = threading.Event()
        results = []

        def go():
            start.wait()
            results.append(t.reconcile(FutureTerminalState.COMPLETED_WITH_DATA))

        ts = [threading.Thread(target=go) for _ in range(4)]
        for th in ts:
            th.start()
        start.set()
        for th in ts:
            th.join()

        assert len([r for r in results if r is not None]) == 1
        assert c.conservation_errors() == []


class TestTerminalKindIndependence:
    """What happened to the lifecycle is not derivable from why."""

    def test_exceptional_unknown_counts_as_raised_not_returned_none(self):
        """The reason may be unknown; the lifecycle is not."""
        c = ScanStageCounters()
        c.note_scheduled()
        t = PostOutcome(c, url="https://e.test/x")
        t.note_started()
        t.note_exception_type("ValueError")
        t.reconcile(FutureTerminalState.COMPLETED_EXCEPTION)

        assert c.detail_raised_exception == 1
        assert c.detail_returned_none == 0
        assert c.reasons == {"unknown": 1}
        assert c.kinds == {"raised_exception": 1}
        assert c.conservation_errors() == []

    def test_same_reason_can_end_a_life_three_ways(self):
        for kind, attr in (
            (TerminalKind.RAISED_EXCEPTION, "detail_raised_exception"),
            (TerminalKind.RETURNED_NONE, "detail_returned_none"),
            (TerminalKind.CONSTRUCTION_FAILED, "media_item_construction_failed"),
        ):
            c = ScanStageCounters()
            c.note_scheduled()
            c.note_started()
            if kind is TerminalKind.CONSTRUCTION_FAILED:
                c.note_detail_data()
            c.note_discard(DiscardCode.UNKNOWN, terminal_kind=kind, url="u")
            assert getattr(c, attr) == 1
            assert c.conservation_errors() == []


class TestUnspecifiedStage:
    def test_stageless_codes_do_not_default_to_parse(self):
        for code in (DiscardCode.UNKNOWN, DiscardCode.TERMINAL_OUTCOME_MISSING):
            assert default_stage_for(code) is ScanStage.UNSPECIFIED

    def test_omission_stays_visible_in_the_tally(self):
        c = ScanStageCounters()
        c.note_scheduled()
        c.note_started()
        c.note_discard(DiscardCode.UNKNOWN, url="https://e.test/n")
        assert c.stages == {"unspecified": 1}
        assert c.samples[0].stage == "unspecified"

    def test_explicit_stage_still_wins(self):
        c = ScanStageCounters()
        c.note_scheduled()
        c.note_started()
        c.note_discard(
            DiscardCode.UNKNOWN, stage=ScanStage.DETAIL_FETCH, url="https://e.test/s"
        )
        assert c.stages == {"detail_fetch": 1}


class TestFutureFactNormalization:
    def test_completed_with_data_backfills_started_and_data(self):
        c = ScanStageCounters()
        c.note_scheduled()
        t = PostOutcome(c, url="https://e.test/b")
        t.reconcile(FutureTerminalState.COMPLETED_WITH_DATA)

        assert c.detail_started == 1
        assert c.detail_returned_data == 1
        assert c.media_item_abandoned_on_stop == 1
        assert c.conservation_errors() == []

    def test_completed_exception_backfills_started(self):
        c = ScanStageCounters()
        c.note_scheduled()
        t = PostOutcome(c, url="https://e.test/e")
        t.note_exception_type("RuntimeError")
        t.reconcile(FutureTerminalState.COMPLETED_EXCEPTION)

        assert c.detail_started == 1
        assert c.detail_raised_exception == 1
        assert c.conservation_errors() == []

    def test_completed_falsy_is_not_mistaken_for_never_started(self):
        c = ScanStageCounters()
        c.note_scheduled()
        t = PostOutcome(c, url="https://e.test/f")
        assert t.reconcile(FutureTerminalState.COMPLETED_FALSEY) is DiscardCode.DETAIL_EMPTY

        assert c.detail_started == 1
        assert c.scheduled_terminal_missing == 0
        assert c.conservation_errors() == []

    def test_backfill_does_not_invent_http_requests(self):
        c = ScanStageCounters()
        c.note_scheduled()
        PostOutcome(c, url="https://e.test/h").reconcile(
            FutureTerminalState.COMPLETED_WITH_DATA
        )
        assert c.detail_http_requests == 0

    def test_cancelled_future_is_not_backfilled_as_started(self):
        c = ScanStageCounters()
        c.note_scheduled()
        PostOutcome(c, url="https://e.test/c").reconcile(FutureTerminalState.CANCELLED_BEFORE_START)
        assert c.detail_started == 0
        assert c.conservation_errors() == []


class TestPopulationSeparation:
    def test_stop_does_not_read_as_construction_failure(self):
        """9 stranded results + 1 built = 100% construction success."""
        c = ScanStageCounters()
        c.note_scheduled(10)
        for _ in range(10):
            c.note_started()
            c.note_detail_data()
        c.note_item_created()
        for i in range(9):
            c.note_discard(
                DiscardCode.MEDIA_ITEM_ABANDONED_ON_STOP,
                stage=ScanStage.DETAIL_TO_ITEM_HANDOFF,
                url="https://e.test/s%d" % i,
            )

        assert c.media_item_construction_attempted == 1
        assert c.media_item_construction_success_ratio == 1.0
        assert c.conservation_errors() == []

    def test_groups_keep_failures_stop_and_gaps_apart(self):
        c = ScanStageCounters()
        c.note_scheduled(3)
        c.note_started()
        c.note_discard(DiscardCode.DETAIL_NO_FILENAME, url="a")
        c.note_started()
        c.note_discard(
            DiscardCode.DETAIL_CANCELLED_AFTER_START,
            stage=ScanStage.DETAIL_FETCH,
            url="b",
        )
        c.note_started()
        c.note_discard(DiscardCode.TERMINAL_OUTCOME_MISSING, url="c")

        assert c.outcome_groups() == {
            "failures": 1,
            "operator_stop_outcomes": 1,
            "instrumentation_gaps": 1,
        }
        assert c.conservation_errors() == []

    def test_a_cancellation_does_not_silence_a_real_regression(self):
        """Stop outcomes leave the denominators, they do not veto the cycle.

        Vetoing meant one routine cancellation silenced the exact parser
        regression these counters exist to catch - and since the pipeline stops
        early once it has enough releases, plausibly no real cycle would ever
        have been health-scored.
        """
        c = ScanStageCounters()
        for _ in range(127):
            c.note_scheduled()
            c.note_started()
            c.note_discard(
                DiscardCode.DETAIL_NO_FILENAME,
                terminal_kind=TerminalKind.RETURNED_NONE,
                url="a",
            )
        assert c.eligible_for_health_scoring is True
        assert c.detail_parse_success_ratio == 0.0

        # one cancelled post must not change either answer
        c.note_scheduled()
        c.note_cancelled_before_start(1)
        assert c.eligible_for_health_scoring is True
        assert c.detail_parse_success_ratio == 0.0
        assert c.conservation_errors() == []

    def test_instrumentation_gaps_do_disqualify(self):
        """A cycle whose own bookkeeping is broken is evidence of nothing."""
        c = ScanStageCounters()
        c.note_scheduled()
        c.note_started()
        c.note_discard(DiscardCode.UNKNOWN, url="a")   # no factual kind
        assert c.eligible_for_health_scoring is False

    def test_shipped_but_uncounted_release_disqualifies(self):
        c = ScanStageCounters()
        c.note_scheduled()
        t = PostOutcome(c, url="u")
        t.note_started()
        t.data_returned()
        t.reconcile(FutureTerminalState.COMPLETED_WITH_DATA)
        assert t.item_created() is False
        assert c.media_item_created_after_terminal == 1
        assert c.eligible_for_health_scoring is False
        assert any("created_after_terminal" in e for e in c.conservation_errors())

    def test_summary_does_not_call_stop_outcomes_failures(self):
        c = ScanStageCounters()
        c.note_scheduled(2)
        c.note_started()
        c.note_discard(DiscardCode.DETAIL_NO_FILENAME, url="a")
        c.note_cancelled_before_start(1)

        line = c.summary_line()
        assert "failures=1" in line
        assert "stopped=1" in line


class TestTerminalStateEnforcement:
    def test_contradictory_future_facts_are_unrepresentable(self):
        """A future cannot be both cancelled and carrying data."""
        for state in FutureTerminalState:
            c = ScanStageCounters()
            c.note_scheduled()
            PostOutcome(c, url="u").reconcile(state)
            if state is FutureTerminalState.CANCELLED_BEFORE_START:
                assert c.detail_started == 0
                assert c.detail_returned_data == 0
            else:
                assert c.detail_started == 1
            assert c.conservation_errors() == []

    def test_every_state_normalizes_its_lifecycle(self):
        expected = {
            FutureTerminalState.CANCELLED_BEFORE_START:
                DiscardCode.DETAIL_CANCELLED_BEFORE_START,
            FutureTerminalState.COMPLETED_WITH_DATA:
                DiscardCode.MEDIA_ITEM_ABANDONED_ON_STOP,
            FutureTerminalState.COMPLETED_FALSEY: DiscardCode.DETAIL_EMPTY,
            FutureTerminalState.COMPLETED_EXCEPTION: DiscardCode.UNKNOWN,
        }
        for state, code in expected.items():
            c = ScanStageCounters()
            c.note_scheduled()
            assert PostOutcome(c, url="u").reconcile(state) is code
            assert c.conservation_errors() == []


class TestNoPlausibleDefaultKind:
    def test_unknown_without_kind_is_an_instrumentation_gap(self):
        """An ambiguous event must never land in a content-failure bucket."""
        assert default_kind_for(DiscardCode.UNKNOWN) is TerminalKind.UNSPECIFIED

        c = ScanStageCounters()
        c.note_scheduled()
        c.note_started()
        c.note_discard(DiscardCode.UNKNOWN, url="u")

        assert c.detail_returned_none == 0
        assert c.outcome_groups()["instrumentation_gaps"] == 1
        assert c.outcome_groups()["failures"] == 0
        assert c.eligible_for_health_scoring is False
        assert c.conservation_errors() == []

    def test_explicit_kinds_still_route(self):
        for kind, attr in (
            (TerminalKind.RETURNED_NONE, "detail_returned_none"),
            (TerminalKind.RAISED_EXCEPTION, "detail_raised_exception"),
        ):
            c = ScanStageCounters()
            c.note_scheduled()
            c.note_started()
            c.note_discard(DiscardCode.UNKNOWN, terminal_kind=kind, url="u")
            assert getattr(c, attr) == 1


class TestKindConservationAndSamples:
    def test_sample_carries_terminal_kind(self):
        c = ScanStageCounters()
        c.note_scheduled()
        c.note_started()
        c.note_discard(
            DiscardCode.DETAIL_NO_FILENAME,
            terminal_kind=TerminalKind.RETURNED_NONE,
            url="u",
        )
        assert c.samples[0].to_dict()["terminal_kind"] == "returned_none"

    def test_same_reason_at_three_kinds_keeps_a_sample_each(self):
        """Capping by reason alone starved later kinds of any example."""
        c = ScanStageCounters()
        for kind in (
            TerminalKind.RETURNED_NONE,
            TerminalKind.RAISED_EXCEPTION,
            TerminalKind.CONSTRUCTION_FAILED,
        ):
            for i in range(10):
                c.note_scheduled()
                c.note_started()
                if kind is TerminalKind.CONSTRUCTION_FAILED:
                    c.note_detail_data()
                c.note_discard(
                    DiscardCode.UNKNOWN,
                    terminal_kind=kind,
                    url="https://e.test/%s%d" % (kind.value, i),
                )
        kinds_sampled = {s.terminal_kind for s in c.samples}
        assert kinds_sampled == {
            "returned_none", "raised_exception", "construction_failed",
        }
        assert c.conservation_errors() == []

    def test_cycle_sample_cap_bounds_a_pathological_scan(self):
        c = ScanStageCounters()
        for i in range(400):
            c.note_scheduled()
            c.note_started()
            c.note_discard(
                DiscardCode.DETAIL_NO_FILENAME,
                terminal_kind=TerminalKind.RETURNED_NONE,
                url="https://e.test/%d" % i,
            )
        assert len(c.samples) <= MAX_SAMPLES_PER_CYCLE
        assert c.reasons["detail_no_filename"] == 400


class TestHealthEligibility:
    def test_zero_observations_is_not_perfect_health(self):
        c = ScanStageCounters()
        assert c.detail_parse_success_ratio == 1.0
        assert c.eligible_for_health_scoring is False

    def test_imbalance_makes_a_cycle_ineligible(self):
        c = ScanStageCounters()
        c.note_scheduled(5)
        c.note_started()
        c.note_detail_data()
        c.note_item_created()
        assert c.conservation_errors()
        assert c.eligible_for_health_scoring is False

    def test_clean_measured_cycle_is_eligible(self):
        c = ScanStageCounters()
        c.note_scheduled(2)
        c.note_started()
        c.note_detail_data()
        c.note_item_created()
        c.note_started()
        c.note_discard(
            DiscardCode.DETAIL_NO_FILENAME,
            terminal_kind=TerminalKind.RETURNED_NONE,
            url="u",
        )
        assert c.conservation_errors() == []
        assert c.eligible_for_health_scoring is True
        assert c.eligible_for_construction_scoring is True

    def test_construction_scoring_needs_an_attempt(self):
        c = ScanStageCounters()
        c.note_scheduled()
        c.note_started()
        c.note_discard(
            DiscardCode.DETAIL_NO_FILENAME,
            terminal_kind=TerminalKind.RETURNED_NONE,
            url="u",
        )
        assert c.eligible_for_health_scoring is True
        assert c.eligible_for_construction_scoring is False


class TestLoudOnImbalance:
    def test_summary_surfaces_conservation_failure(self):
        c = ScanStageCounters()
        c.note_scheduled(5)
        c.note_started()
        c.note_discard(
            DiscardCode.DETAIL_NO_FILENAME,
            terminal_kind=TerminalKind.RETURNED_NONE,
            url="u",
        )
        assert "metrics_errors=" in c.summary_line()

    def test_clean_summary_says_nothing_about_errors(self):
        c = ScanStageCounters()
        c.note_scheduled()
        c.note_started()
        c.note_detail_data()
        c.note_item_created()
        assert "metrics_errors" not in c.summary_line()


class TestFetchVersusParseHealth:
    """A source outage and a parser regression must not read the same.

    Expected values here are hardcoded independently of DEFAULT_KIND_FOR_CODE
    and STAGE_FOR_CODE, so inverting those tables cannot make this pass.
    """

    def _cohort(self, code, stage, n=100):
        c = ScanStageCounters()
        for i in range(n):
            c.note_scheduled()
            c.note_started()
            c.note_discard(
                code,
                stage=stage,
                terminal_kind=TerminalKind.RETURNED_NONE,
                url="https://e.test/%d" % i,
            )
        return c

    def test_equal_cohorts_same_kind_differ_in_parse_health(self):
        fetch = self._cohort(
            DiscardCode.DETAIL_NO_USABLE_RESPONSE, ScanStage.DETAIL_FETCH
        )
        parse = self._cohort(DiscardCode.DETAIL_NO_FILENAME, ScanStage.DETAIL_PARSE)

        # identical size, identical TerminalKind, identical marginal totals
        assert fetch.detail_returned_none == parse.detail_returned_none == 100
        assert fetch.kinds == parse.kinds

        # ...but the parser is only implicated in one of them
        assert fetch.detail_parse_success_ratio == 1.0
        assert parse.detail_parse_success_ratio == 0.0
        assert fetch.conservation_errors() == []
        assert parse.conservation_errors() == []

    def test_fetch_and_parse_populations_are_reported_separately(self):
        fetch = self._cohort(
            DiscardCode.DETAIL_NO_USABLE_RESPONSE, ScanStage.DETAIL_FETCH
        )
        payload = fetch.to_dict()
        assert payload["detail_fetch_failed"] == 100
        assert payload["detail_reached_parse"] == 0

    def test_emitted_output_counts_releases_that_actually_shipped(self):
        """A release appended after ticket closure still shipped."""
        c = ScanStageCounters()
        c.note_scheduled()
        t = PostOutcome(c, url="u")
        t.note_started()
        t.data_returned()
        t.reconcile(FutureTerminalState.COMPLETED_WITH_DATA)
        t.item_created()

        payload = c.to_dict()
        assert payload["media_item_created"] == 0
        assert payload["media_items_emitted"] == 1, "it really shipped"
        assert payload["media_item_created_after_terminal"] == 1

    def test_stop_safe_yield_excludes_stop_affected_posts(self):
        c = ScanStageCounters()
        c.note_scheduled(2)
        c.note_started()
        c.note_detail_data()
        c.note_item_created()
        c.note_started()
        c.note_detail_data()
        c.note_discard(
            DiscardCode.MEDIA_ITEM_ABANDONED_ON_STOP,
            stage=ScanStage.DETAIL_TO_ITEM_HANDOFF,
            terminal_kind=TerminalKind.ABANDONED_ON_STOP,
            url="u",
        )
        payload = c.to_dict()
        # actual yield counts the stranded post; the stop-safe one does not
        assert payload["end_to_end_item_yield"] == 0.5
        assert payload["stop_safe_item_yield"] == 1.0
        assert c.conservation_errors() == []

    def test_to_dict_publishes_one_moment(self):
        """Counters and samples must come from the same snapshot."""
        c = ScanStageCounters()
        c.note_scheduled()
        c.note_started()
        c.note_discard(
            DiscardCode.DETAIL_NO_FILENAME,
            terminal_kind=TerminalKind.RETURNED_NONE,
            url="u",
        )
        payload = c.to_dict()
        sampled = {s["reason_code"] for s in payload["samples"]}
        assert sampled <= set(payload["reasons"]), (
            "a published sample referenced a reason absent from its own totals"
        )
