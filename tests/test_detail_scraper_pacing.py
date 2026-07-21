"""Safety tests for the process-wide HDEncode detail coordinator."""
from contextlib import contextmanager
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

import pytest

import backend.detail_scraper as detail_scraper
import backend.hdencode_coordinator as coordinator_module
from backend.detail_scraper import DetailScraper
from backend.hdencode_coordinator import HDEncodeTrafficCoordinator


_VALID_DETAIL_HTML = b"""
<html><body>
  <div class="entry-content">
    Filename.: Example.Movie.2024.2160p.mkv
    Size: 10 GB
    Resolution.: 2160p
  </div>
</body></html>
"""


@dataclass
class _Response:
    status_code: int = 200
    content: bytes = _VALID_DETAIL_HTML


class _App:
    config = {"debug_mode": False, "hdencode_enabled": True}
    db = None

    @staticmethod
    def parse_size(value):
        number = float(value.split()[0])
        return number / 1024 if "MB" in value.upper() else number

    @staticmethod
    def clean_string(value):
        return value.lower()

    @staticmethod
    def safe_log(*_args, **_kwargs):
        return None


class _FakeClock:
    def __init__(self, start=100.0):
        self.now = start
        self.sleeps = []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


def _install_coordinator(
    monkeypatch,
    *,
    max_concurrent=3,
    interval=2.0,
):
    coordinator = HDEncodeTrafficCoordinator()
    coordinator._MIN_START_INTERVAL = interval
    coordinator._HEALTH_CACHE_SECONDS = 0
    coordinator._semaphores["detail"] = threading.BoundedSemaphore(
        max_concurrent
    )
    coordinator.configure({"hdencode_enabled": True}, None)
    monkeypatch.setattr(
        coordinator_module,
        "_COORDINATOR",
        coordinator,
    )
    return coordinator


def test_non_hdencode_sources_bypass_hdencode_coordinator(monkeypatch):
    coordinator = _install_coordinator(monkeypatch)

    @contextmanager
    def forbidden_request(*_args, **_kwargs):
        raise AssertionError(
            "HDEncode coordinator must not wrap this source"
        )
        yield

    monkeypatch.setattr(coordinator, "request", forbidden_request)

    class Scraper:
        def get(self, *_args, **_kwargs):
            return _Response()

    detail = DetailScraper(_App())
    session = Scraper()
    for url in (
        "https://ddlbase.com/post/example",
        "https://www.ddlbase.com/post/example",
        "https://adit-hd.com/threads/example",
        "https://forum.adit-hd.com/threads/example",
    ):
        assert detail.scrape_details(url, {}, session)


def test_hdencode_query_text_cannot_spoof_source_classification(monkeypatch):
    coordinator = _install_coordinator(monkeypatch)
    entered = 0

    @contextmanager
    def recording_request(request_class, **_kwargs):
        nonlocal entered
        assert request_class == "detail"
        entered += 1
        yield

    monkeypatch.setattr(coordinator, "request", recording_request)

    class Scraper:
        def get(self, *_args, **_kwargs):
            return _Response()

    result = DetailScraper(_App()).scrape_details(
        "https://hdencode.org/release/?next=https://ddlbase.com/post/example",
        {},
        Scraper(),
    )
    assert result
    assert entered == 1


def test_malformed_detail_url_fails_closed_to_hdencode_coordinator(monkeypatch):
    coordinator = _install_coordinator(monkeypatch)
    entered = 0

    @contextmanager
    def recording_request(request_class, **_kwargs):
        nonlocal entered
        assert request_class == "detail"
        entered += 1
        yield

    monkeypatch.setattr(coordinator, "request", recording_request)

    class Scraper:
        def get(self, *_args, **_kwargs):
            return _Response()

    result = DetailScraper(_App()).scrape_details(
        "not a valid page URL",
        {},
        Scraper(),
    )
    assert result
    assert entered == 1


def test_concurrent_request_starts_are_globally_spaced(monkeypatch):
    interval = 0.04
    _install_coordinator(
        monkeypatch,
        max_concurrent=4,
        interval=interval,
    )
    starts = []
    starts_lock = threading.Lock()

    class Scraper:
        def get(self, *_args, **_kwargs):
            with starts_lock:
                starts.append(coordinator_module.time.monotonic())
            return _Response()

    detail = DetailScraper(_App())
    session = Scraper()
    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = [
            executor.submit(
                detail.scrape_details,
                f"https://hdencode.org/concurrent-{index}",
                {},
                session,
            )
            for index in range(4)
        ]
        for future in futures:
            assert future.result(timeout=3)

    ordered = sorted(starts)
    assert len(ordered) == 4
    gaps = [
        later - earlier
        for earlier, later in zip(ordered, ordered[1:])
    ]
    assert all(gap >= interval * 0.75 for gap in gaps), gaps


def test_detail_requests_are_spaced_across_separate_calls(monkeypatch):
    _install_coordinator(monkeypatch, interval=2.0)
    clock = _FakeClock()
    monkeypatch.setattr(
        coordinator_module.time,
        "monotonic",
        clock.monotonic,
    )
    monkeypatch.setattr(
        coordinator_module.time,
        "sleep",
        clock.sleep,
    )

    starts = []

    class Scraper:
        def get(self, *_args, **_kwargs):
            starts.append(clock.now)
            return _Response()

    scraper = DetailScraper(_App())
    session = Scraper()
    assert scraper.scrape_details("https://hdencode.org/a", {}, session)
    assert scraper.scrape_details("https://hdencode.org/b", {}, session)

    assert starts == [100.0, 102.0]
    assert sum(clock.sleeps) == pytest.approx(2.0)
    assert all(0 < value <= 0.1 for value in clock.sleeps)


def test_each_retry_attempt_uses_shared_start_clock(monkeypatch):
    _install_coordinator(monkeypatch, interval=2.0)
    clock = _FakeClock()
    monkeypatch.setattr(
        coordinator_module.time,
        "monotonic",
        clock.monotonic,
    )
    monkeypatch.setattr(
        coordinator_module.time,
        "sleep",
        clock.sleep,
    )

    starts = []
    responses = iter([
        _Response(status_code=429, content=b""),
        _Response(),
    ])

    class Scraper:
        def get(self, *_args, **_kwargs):
            starts.append(clock.now)
            return next(responses)

    result = DetailScraper(_App()).scrape_details(
        "https://hdencode.org/retry",
        {},
        Scraper(),
    )
    assert result
    # Existing 429 backoff advances the shared fake clock two seconds.
    assert starts == [100.0, 102.0]
    assert clock.sleeps == [2]


def test_no_more_than_three_detail_requests_are_in_flight(monkeypatch):
    _install_coordinator(
        monkeypatch,
        max_concurrent=3,
        interval=0.0,
    )
    lock = threading.Lock()
    release = threading.Event()
    three_active = threading.Event()
    active = 0
    maximum = 0
    calls = 0

    class BlockingScraper:
        def get(self, *_args, **_kwargs):
            nonlocal active, maximum, calls
            with lock:
                active += 1
                calls += 1
                maximum = max(maximum, active)
                if active == 3:
                    three_active.set()
            assert release.wait(timeout=3)
            with lock:
                active -= 1
            return _Response()

    scraper = DetailScraper(_App())
    session = BlockingScraper()
    with ThreadPoolExecutor(max_workers=6) as executor:
        futures = [
            executor.submit(
                scraper.scrape_details,
                f"https://hdencode.org/post-{index}",
                {},
                session,
            )
            for index in range(6)
        ]
        assert three_active.wait(timeout=3)
        with lock:
            assert active == 3
            assert maximum == 3
        release.set()
        for future in futures:
            assert future.result(timeout=3)

    assert calls == 6
    assert maximum == 3


def test_waiting_worker_never_requests_after_cancellation(monkeypatch):
    _install_coordinator(
        monkeypatch,
        max_concurrent=1,
        interval=0.0,
    )
    release_first = threading.Event()
    first_started = threading.Event()
    cancelled = threading.Event()
    calls = []
    lock = threading.Lock()

    class Scraper:
        def get(self, url, *_args, **_kwargs):
            with lock:
                calls.append(url)
                call_number = len(calls)
            if call_number == 1:
                first_started.set()
                assert release_first.wait(timeout=3)
            return _Response()

    detail = DetailScraper(_App())
    session = Scraper()
    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(
            detail.scrape_details,
            "https://hdencode.org/first",
            {},
            session,
            stop_requested=cancelled.is_set,
        )
        assert first_started.wait(timeout=3)
        waiting = executor.submit(
            detail.scrape_details,
            "https://hdencode.org/waiting",
            {},
            session,
            stop_requested=cancelled.is_set,
        )
        cancelled.set()
        release_first.set()
        assert first.result(timeout=3)
        assert waiting.result(timeout=3) is None

    assert calls == ["https://hdencode.org/first"]


def test_retry_backoff_stops_before_next_request(monkeypatch):
    _install_coordinator(
        monkeypatch,
        max_concurrent=1,
        interval=0.0,
    )
    cancelled = threading.Event()
    calls = 0

    class Scraper:
        def get(self, *_args, **_kwargs):
            nonlocal calls
            calls += 1
            cancelled.set()
            return _Response(status_code=429, content=b"")

    result = DetailScraper(_App()).scrape_details(
        "https://hdencode.org/retry-cancel",
        {},
        Scraper(),
        stop_requested=cancelled.is_set,
    )
    assert result is None
    assert calls == 1
