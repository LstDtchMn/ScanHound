"""Each discard path must name the branch it ACTUALLY took.

These drive the production ``scrape_details`` and assert on the ticket it
fills in. The whole value of the taxonomy is that an operator can tell a site
outage from a layout change from their own Stop press. A test that only checked
"something got recorded" would pass just as happily with every branch
mislabelled, which is the failure mode worth guarding against: a wrong label is
worse than no label, because it sends the investigation somewhere confidently.

Where a test would still pass under a plausible WRONG implementation, the
disagreeing case is pinned explicitly and says so.
"""

import pytest
from unittest.mock import MagicMock, patch

from backend.detail_scraper import (
    DetailScraper,
    _DetailRequestCancelled,
)
from backend.hdencode_coordinator import (
    HDEncodeRequestCancelled,
    HDEncodeTrafficDenied,
)
from backend.scan_metrics import (
    DiscardCode,
    PostOutcome,
    ScanStage,
    ScanStageCounters,
    TerminalKind,
)
from tests.test_detail_scraper import MockApp, MOVIE_HTML


HDENCODE_URL = "https://hdencode.org/some-release/"
#: Outside the coordinator, so paths that must not depend on it can be driven
#: without patching it at all.
PLAIN_URL = "https://ddlbase.com/some-release/"


def make_ticket():
    counters = ScanStageCounters()
    return counters, PostOutcome(counters, url=HDENCODE_URL, source="hdencode")


def resp(status=200, content=MOVIE_HTML):
    r = MagicMock()
    r.status_code = status
    r.content = content
    return r


def http(status=200, content=MOVIE_HTML):
    cs = MagicMock()
    cs.get.return_value = resp(status, content)
    return cs


def booked(outcome):
    """The single terminal code on the ticket, or None."""
    return outcome.snapshot().terminal_code


def only_sample(counters):
    assert len(counters.samples) == 1, f"expected 1 sample, got {counters.samples}"
    return counters.samples[0]


# ── the seven scraper exits ──────────────────────────────────────────────

def test_stop_before_the_request_is_a_stop_not_a_failure():
    """An operator pressing Stop must never look like the source failing."""
    counters, ticket = make_ticket()
    s = DetailScraper(MockApp())

    out = s.scrape_details(PLAIN_URL, {}, http(), stop_requested=lambda: True,
                           outcome=ticket)

    assert out is None
    assert booked(ticket) == DiscardCode.DETAIL_CANCELLED_BEFORE_REQUEST.value
    sample = only_sample(counters)
    assert sample.stage == ScanStage.DETAIL_FETCH.value
    assert sample.terminal_kind == TerminalKind.CANCELLED_AFTER_START.value
    # The distinction that matters downstream: a Stop is not a content failure,
    # so it must not land in the failure buckets that drive health scoring.
    assert counters.detail_returned_none == 0
    assert counters.detail_raised_exception == 0


def test_cancelled_inside_the_coordinator_is_attributed_to_the_coordinator():
    counters, ticket = make_ticket()
    s = DetailScraper(MockApp())
    calls = {"n": 0}

    def stop():
        # False at the loop head, True once inside the coordinator's context.
        calls["n"] += 1
        return calls["n"] > 1

    with patch("backend.detail_scraper.get_hdencode_coordinator") as coord:
        coord.return_value.request.return_value.__enter__ = lambda *_: None
        coord.return_value.request.return_value.__exit__ = lambda *_: False
        out = s.scrape_details(HDENCODE_URL, {}, http(), stop_requested=stop,
                               outcome=ticket)

    assert out is None
    assert booked(ticket) == DiscardCode.DETAIL_CANCELLED_IN_COORDINATOR.value


def test_a_cancelled_request_is_not_recorded_as_the_source_denying_us():
    """NEGATIVE CONTROL for the except-clause ordering.

    ``HDEncodeRequestCancelled`` SUBCLASSES ``HDEncodeTrafficDenied``. If the
    two handlers are ordered parent-first, this exact input books
    DETAIL_TRAFFIC_DENIED and every operator Stop reads as a site problem --
    the opposite diagnosis, arrived at confidently. Both exception types are
    driven here precisely because they disagree only under the wrong order.
    """
    assert issubclass(HDEncodeRequestCancelled, HDEncodeTrafficDenied), (
        "the trap this test guards no longer exists; re-check the handlers")

    s = DetailScraper(MockApp())

    for exc, expected in (
        (HDEncodeRequestCancelled(),
         DiscardCode.DETAIL_CANCELLED_IN_COORDINATOR),
        (HDEncodeTrafficDenied("blocked", "traffic is blocked"),
         DiscardCode.DETAIL_TRAFFIC_DENIED),
        (_DetailRequestCancelled(),
         DiscardCode.DETAIL_CANCELLED_IN_COORDINATOR),
    ):
        counters, ticket = make_ticket()
        with patch("backend.detail_scraper.get_hdencode_coordinator") as coord:
            coord.return_value.request.side_effect = exc
            out = s.scrape_details(HDENCODE_URL, {}, http(), outcome=ticket)

        assert out is None
        assert booked(ticket) == expected.value, (
            f"{type(exc).__name__} booked {booked(ticket)}, expected "
            f"{expected.value}")

    # And the two must land in DIFFERENT lifecycle buckets, not merely carry
    # different labels: a denial is an exception, a Stop is not.
    c1, t1 = make_ticket()
    c2, t2 = make_ticket()
    with patch("backend.detail_scraper.get_hdencode_coordinator") as coord:
        coord.return_value.request.side_effect = HDEncodeRequestCancelled()
        s.scrape_details(HDENCODE_URL, {}, http(), outcome=t1)
    with patch("backend.detail_scraper.get_hdencode_coordinator") as coord:
        coord.return_value.request.side_effect = HDEncodeTrafficDenied(
            "blocked", "traffic is blocked")
        s.scrape_details(HDENCODE_URL, {}, http(), outcome=t2)
    assert c1.detail_raised_exception == 0, "a Stop is not an exception"
    assert c2.detail_raised_exception == 1, "a denial IS an exception"


def test_stop_between_retries_is_recorded_as_the_retry_sleep_being_cut_short():
    counters, ticket = make_ticket()
    s = DetailScraper(MockApp())
    cs = MagicMock()
    cs.get.side_effect = OSError("connection reset")

    with patch("backend.detail_scraper._interruptible_sleep",
               side_effect=_DetailRequestCancelled()):
        out = s.scrape_details(PLAIN_URL, {}, cs, stop_requested=lambda: False,
                               outcome=ticket)

    assert out is None
    assert booked(ticket) == DiscardCode.DETAIL_RETRY_SLEEP_CANCELLED.value
    # The transport error that triggered the retry is preserved, so "we kept
    # failing to connect and then you stopped" stays distinguishable from
    # "you stopped a healthy scan".
    assert only_sample(counters).exception_type == "OSError"


def test_a_page_that_never_returns_200_is_a_fetch_failure_not_a_parse_failure():
    counters, ticket = make_ticket()
    s = DetailScraper(MockApp())

    with patch("backend.detail_scraper._interruptible_sleep"):
        out = s.scrape_details(PLAIN_URL, {}, http(status=503), outcome=ticket)

    assert out is None
    assert booked(ticket) == DiscardCode.DETAIL_NO_USABLE_RESPONSE.value
    # Staging is the actionable half: fetch failures mean the source or the
    # network, parse failures mean our parser. Filing one as the other sends
    # the investigation to the wrong layer.
    assert only_sample(counters).stage == ScanStage.DETAIL_FETCH.value


def test_a_200_page_with_no_filename_field_is_the_layout_change_signal():
    counters, ticket = make_ticket()
    s = DetailScraper(MockApp())
    page = b"<html><body><div class='entry-content'>nothing useful</div></body></html>"

    out = s.scrape_details(PLAIN_URL, {}, http(content=page), outcome=ticket)

    assert out is None
    assert booked(ticket) == DiscardCode.DETAIL_NO_FILENAME.value
    sample = only_sample(counters)
    assert sample.stage == ScanStage.DETAIL_PARSE.value
    assert sample.terminal_kind == TerminalKind.RETURNED_NONE.value


def test_an_exception_after_the_page_arrived_is_staged_at_parse():
    counters, ticket = make_ticket()
    s = DetailScraper(MockApp())

    with patch("backend.detail_scraper.BeautifulSoup",
               side_effect=ValueError("boom")):
        out = s.scrape_details(PLAIN_URL, {}, http(), outcome=ticket)

    assert out is None
    assert booked(ticket) == DiscardCode.DETAIL_PARSE_EXCEPTION.value
    sample = only_sample(counters)
    assert sample.stage == ScanStage.DETAIL_PARSE.value
    assert sample.exception_type == "ValueError"


def test_an_exception_before_any_page_arrived_is_not_called_a_parse_failure():
    """Disagreeing case for the outer handler.

    The outer ``except`` spans the whole method. Blanket-labelling it
    DETAIL_PARSE_EXCEPTION would assert that parsing was reached when no
    response ever existed -- pointing an investigation at the parser during
    what is actually a connectivity failure.
    """
    counters, ticket = make_ticket()
    s = DetailScraper(MockApp())

    with patch("backend.detail_scraper._detail_source_kind",
               side_effect=RuntimeError("resolver exploded")):
        out = s.scrape_details(PLAIN_URL, {}, http(), outcome=ticket)

    assert out is None
    assert booked(ticket) == DiscardCode.UNKNOWN.value
    sample = only_sample(counters)
    assert sample.stage == ScanStage.UNSPECIFIED.value, (
        "with no response in hand the fault cannot be attributed to parsing")
    assert sample.terminal_kind == TerminalKind.RAISED_EXCEPTION.value


# ── the success path and non-interference ────────────────────────────────

def test_a_successful_scrape_books_data_but_no_terminal_outcome():
    """Success is not terminal: construction or a Stop can still follow."""
    counters, ticket = make_ticket()
    s = DetailScraper(MockApp())

    out = s.scrape_details(PLAIN_URL, {}, http(), outcome=ticket)

    assert out is not None and out["display_title"]
    snap = ticket.snapshot()
    assert snap.data_returned is True
    assert snap.terminal_booked is False, (
        "booking a terminal here would make the scanner's item_created a "
        "created-after-terminal defect on every single success")
    assert counters.detail_returned_data == 1
    assert counters.samples == []


@pytest.mark.parametrize("case", ["success", "no_filename", "bad_status"])
def test_instrumentation_changes_no_return_value(case):
    """The recorder is diagnostic. Identical inputs, identical outputs."""
    s = DetailScraper(MockApp())
    page, status = MOVIE_HTML, 200
    if case == "no_filename":
        page = b"<html><body>nope</body></html>"
    if case == "bad_status":
        status = 500

    with patch("backend.detail_scraper._interruptible_sleep"):
        without = s.scrape_details(PLAIN_URL, {}, http(status, page))
        _c, ticket = make_ticket()
        with_ = s.scrape_details(PLAIN_URL, {}, http(status, page),
                                 outcome=ticket)

    assert without == with_


def test_http_requests_are_counted_per_attempt_not_per_post():
    """Retries are real cost. Collapsing them hides a source that needs three
    tries to answer, which looks identical to one that answers immediately."""
    counters, ticket = make_ticket()
    s = DetailScraper(MockApp())

    with patch("backend.detail_scraper._interruptible_sleep"):
        s.scrape_details(PLAIN_URL, {}, http(status=503), outcome=ticket)

    assert counters.detail_http_requests == 3, "three attempts were made"
    assert counters.detail_started == 1, "but it is one post"
