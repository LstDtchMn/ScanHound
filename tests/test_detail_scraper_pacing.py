"""Safety tests for HDEncode detail-request concurrency and pacing."""

import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

import backend.detail_scraper as detail_scraper
from backend.detail_scraper import DetailScraper


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
    config = {"debug_mode": False}

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


def _reset_limiter(monkeypatch, *, max_concurrent=3, interval=2.0):
    monkeypatch.setattr(
        detail_scraper,
        "_hdencode_request_semaphore",
        threading.BoundedSemaphore(max_concurrent),
    )
    monkeypatch.setattr(
        detail_scraper,
        "_HDENCODE_MIN_REQUEST_INTERVAL_SECONDS",
        interval,
    )
    monkeypatch.setattr(detail_scraper, "_hdencode_last_request_started", None)


def test_detail_requests_are_spaced_across_separate_calls(monkeypatch):
    _reset_limiter(monkeypatch, interval=2.0)
    clock = _FakeClock()
    monkeypatch.setattr(detail_scraper.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(detail_scraper.time, "sleep", clock.sleep)

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
    assert clock.sleeps == [2.0]


def test_each_retry_attempt_uses_the_shared_start_clock(monkeypatch):
    _reset_limiter(monkeypatch, interval=2.0)
    clock = _FakeClock()
    monkeypatch.setattr(detail_scraper.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(detail_scraper.time, "sleep", clock.sleep)

    starts = []
    responses = iter([_Response(status_code=429, content=b""), _Response()])

    class Scraper:
        def get(self, *_args, **_kwargs):
            starts.append(clock.now)
            return next(responses)

    result = DetailScraper(_App()).scrape_details(
        "https://hdencode.org/retry", {}, Scraper()
    )

    assert result
    # Existing 429 backoff advances the fake clock by two seconds. The shared
    # pacer sees that the minimum interval has already elapsed.
    assert starts == [100.0, 102.0]
    assert clock.sleeps == [2]


def test_no_more_than_three_detail_requests_are_in_flight(monkeypatch):
    _reset_limiter(monkeypatch, max_concurrent=3, interval=0.0)

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
            assert release.wait(timeout=3), "test did not release blocked requests"
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
        assert three_active.wait(timeout=3), "three requests never became active"
        with lock:
            assert active == 3
            assert maximum == 3
        release.set()
        for future in futures:
            assert future.result(timeout=3)

    assert calls == 6
    assert maximum == 3
