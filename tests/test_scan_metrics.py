"""Tests for the recording-only scan stage counters.

The conservation equations are the point of this file: if they can drift, the
instrumentation is losing events and every ratio built on it is a lie.
"""
import threading

import pytest

from backend.scan_metrics import (
    MAX_SAMPLES_PER_REASON,
    STAGE_FOR_CODE,
    TAXONOMY_VERSION,
    DiscardCode,
    ScanStage,
    ScanStageCounters,
    message_for,
)


def _drain(counters, code, n, **kw):
    for i in range(n):
        counters.note_detail_attempt()
        counters.note_discard(code, url="https://example.test/%d" % i, **kw)


class TestConservation:
    def test_balances_when_every_post_is_accounted_for(self):
        c = ScanStageCounters()
        # two survive
        for _ in range(2):
            c.note_detail_attempt()
            c.note_detail_data()
            c.note_item_created()
        # and a realistic tail of discards
        _drain(c, DiscardCode.DETAIL_NO_FILENAME, 5)
        _drain(c, DiscardCode.DETAIL_PARSE_EXCEPTION, 3)
        _drain(c, DiscardCode.DETAIL_CANCELLED, 1)

        assert c.detail_attempted == 11
        assert c.conservation_errors() == []

    def test_construction_failure_balances_against_returned_data(self):
        c = ScanStageCounters()
        c.note_detail_attempt()
        c.note_detail_data()
        c.note_discard(DiscardCode.MEDIA_ITEM_EXCEPTION, url="https://example.test/x")

        # the post reached construction, so it counts as data returned
        assert c.detail_returned_data == 1
        assert c.media_item_construction_failed == 1
        assert c.media_item_created == 0
        assert c.conservation_errors() == []

    def test_imbalance_is_reported_not_raised(self):
        c = ScanStageCounters()
        c.note_detail_attempt()
        c.note_detail_attempt()
        c.note_detail_data()  # one attempt never resolved

        errors = c.conservation_errors()
        assert errors, "a lost event must be visible"
        assert any("detail_attempted" in e for e in errors)

    def test_bulk_cancellation_keeps_the_books_balanced(self):
        """A Stop press abandons queued posts wholesale; they must still count."""
        c = ScanStageCounters()
        for _ in range(10):
            c.note_detail_attempt()
        c.note_detail_data()
        c.note_item_created()
        c.note_bulk_cancelled(9)

        assert c.detail_cancelled == 9
        assert c.conservation_errors() == []


class TestTaxonomy:
    def test_every_code_maps_to_a_stage_and_a_message(self):
        for code in DiscardCode:
            assert code in STAGE_FOR_CODE, "%s has no stage" % code
            assert isinstance(STAGE_FOR_CODE[code], ScanStage)
            assert message_for(code)

    def test_unknown_is_first_class_and_counted(self):
        c = ScanStageCounters()
        c.note_detail_attempt()
        c.note_discard(DiscardCode.UNKNOWN, url="https://example.test/u")

        assert c.reasons["unknown"] == 1
        assert c.conservation_errors() == []
        # and it must not be silently excluded from the failure picture
        assert c.detail_success_ratio == 0.0

    def test_unreachable_codes_are_declared(self):
        """Declared-but-zero keeps the taxonomy stable when checks are added.

        These cannot fire today: no corresponding check exists, and adding one
        would delete items that currently survive.
        """
        for name in (
            "MISSING_REQUIRED_TITLE",
            "MISSING_REQUIRED_URL",
            "INVALID_METADATA",
            "SOURCE_BLOCKED",
        ):
            assert hasattr(DiscardCode, name)

    def test_dict_carries_the_taxonomy_version(self):
        assert ScanStageCounters().to_dict()["taxonomy_version"] == TAXONOMY_VERSION


class TestBoundedSamples:
    def test_samples_are_capped_per_reason(self):
        c = ScanStageCounters()
        _drain(c, DiscardCode.DETAIL_NO_FILENAME, 126)

        assert c.reasons["detail_no_filename"] == 126
        assert len(c.samples) == MAX_SAMPLES_PER_REASON
        assert c.conservation_errors() == []

    def test_samples_carry_no_response_body(self):
        c = ScanStageCounters()
        c.note_detail_attempt()
        c.note_discard(
            DiscardCode.DETAIL_NO_FILENAME,
            url="https://example.test/a",
            source="hdencode",
            category="4k",
            content_fingerprint="deadbeef",
        )
        payload = c.samples[0].to_dict()
        assert set(payload) == {
            "canonical_url", "stage", "reason_code", "source", "category",
            "exception_type", "parser_version", "content_fingerprint",
        }
        assert payload["stage"] == "detail_parse"


class TestThreadSafety:
    def test_concurrent_discards_do_not_lose_events(self):
        """d[k] += 1 is interruptible; without a lock this drops updates."""
        c = ScanStageCounters()
        per_thread = 200
        threads = 8

        def worker():
            for _ in range(per_thread):
                c.note_detail_attempt()
                c.note_discard(DiscardCode.DETAIL_NO_FILENAME, url="https://e.test/x")

        ts = [threading.Thread(target=worker) for _ in range(threads)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()

        assert c.detail_attempted == threads * per_thread
        assert c.reasons["detail_no_filename"] == threads * per_thread
        assert c.conservation_errors() == []


class TestReporting:
    def test_ratio_reflects_the_production_shape(self):
        """The observed failure: ~128 attempted, 2 released."""
        c = ScanStageCounters()
        for _ in range(2):
            c.note_detail_attempt()
            c.note_detail_data()
            c.note_item_created()
        _drain(c, DiscardCode.DETAIL_NO_FILENAME, 126)

        assert c.detail_attempted == 128
        assert round(c.detail_success_ratio, 4) == round(2 / 128.0, 4)
        assert c.conservation_errors() == []

    def test_summary_line_names_the_dominant_reason(self):
        c = ScanStageCounters()
        _drain(c, DiscardCode.DETAIL_NO_FILENAME, 126)
        _drain(c, DiscardCode.DETAIL_PARSE_EXCEPTION, 2)

        line = c.summary_line()
        assert "detail_no_filename=126" in line
        assert line.index("detail_no_filename") < line.index("detail_parse_exception")

    def test_empty_scan_is_not_reported_as_failure(self):
        c = ScanStageCounters()
        assert c.detail_success_ratio == 1.0
        assert c.conservation_errors() == []
