"""Cancellation tests for confirmed source blocking and background stop."""
import asyncio
from unittest.mock import MagicMock

from backend.background_scanner import BackgroundScanner
from backend.scanner_service import ScannerService


class _Response:
    def __init__(self, status_code):
        self.status_code = status_code
        self.content = b""


class _BlockedScraper:
    def __init__(self, statuses):
        self._statuses = iter(statuses)
        self.calls = 0

    def get(self, *_args, **_kwargs):
        self.calls += 1
        return _Response(next(self._statuses))


def _scanner_shell():
    scanner = ScannerService.__new__(ScannerService)
    scanner._stop_event = __import__("threading").Event()
    scanner._last_crawl_seen_urls = set()
    scanner._last_crawl_early_stopped = False
    scanner._log = MagicMock()
    scanner._progress = MagicMock()
    return scanner


def test_three_consecutive_blocks_set_existing_stop_event(monkeypatch):
    scanner = _scanner_shell()
    scraper = _BlockedScraper([403, 403, 403, 200])
    source = {
        "name": "4K Movies",
        "base": "https://hdencode.org/quality/2160p/",
        "suffix": "?tag=movies",
        "type": "movie",
        "source": "hdencode",
        "category": "4k",
    }

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr("backend.scanner_service.asyncio.sleep", no_sleep)

    async def run():
        loop = asyncio.get_running_loop()
        return await scanner._crawl_pages(
            [source],
            pages=4,
            base_url="https://hdencode.org",
            scraper=scraper,
            loop=loop,
            previously_scanned=set(),
            early_stop=False,
        )

    assert asyncio.run(run()) == []
    assert scraper.calls == 3
    assert scanner.stop_scan_flag is True
    assert scanner._last_crawl_early_stopped is True


def test_background_stop_interrupts_active_shared_scan():
    scanner = MagicMock()
    registry = MagicMock()
    registry.scanner = scanner
    background = BackgroundScanner(registry)
    background._running.set()

    background.stop()

    assert scanner.stop_scan_flag is True


def test_background_stop_does_not_set_scan_flag_when_idle():
    scanner = MagicMock()
    registry = MagicMock()
    registry.scanner = scanner
    background = BackgroundScanner(registry)

    background.stop()

    assert not scanner.mock_calls
