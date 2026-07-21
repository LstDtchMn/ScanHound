"""Tests for conditional RSS shadow discovery and lifecycle safety."""
from datetime import datetime, timedelta, timezone
from io import BytesIO
import gzip
import socket
import threading

import pytest

from backend.hdencode_rss_service import HDEncodeRSSService
from backend.sources.hdencode_feed_client import (
    FeedResponse,
    _read_limited,
    validate_feed_url,
)
from backend.sources.hdencode_feed_parser import MAX_FEED_BYTES
from backend.sources.hdencode_feeds import get_feed


RSS = b"""<?xml version="1.0"?>
<rss version="2.0"><channel>
<item><title>Movie.2026.2160p.DV.HDR10P.H265 - 20 GB</title>
<link>https://hdencode.org/movie-2026/</link>
<guid>https://hdencode.org/movie-2026/</guid>
<pubDate>Sun, 19 Jul 2026 20:00:00 +0000</pubDate>
<category>Movies</category><description>Year: 2026</description>
</item></channel></rss>"""


class FakeDb:
    def __init__(self):
        self.state = {}
        self.ingests = []
        self.not_modified = []
        self.failures = []
        self.depths = []
        self.source_successes = []
        self.source_failures = []

    def get_source_health(self):
        return {}

    def record_source_success(self, source):
        self.source_successes.append(source)

    def record_source_failure(self, *args, **kwargs):
        self.source_failures.append((args, kwargs))

    def get_hdencode_feed_state(self, key):
        return self.state.get(key)

    def list_hdencode_feed_states(self):
        return list(self.state.values())

    def get_hdencode_rss_readiness(self, **_kwargs):
        return {
            "ready": False,
            "reasons": ["insufficient_cycles", "insufficient_days"],
            "successful_cycles": 0,
            "observed_days": 0,
            "normal_feeds_healthy": False,
        }

    def ingest_hdencode_feed(self, **kwargs):
        self.ingests.append(kwargs)
        self.state[kwargs["feed_key"]] = {
            "feed_key": kwargs["feed_key"],
            "last_modified": kwargs["last_modified"],
            "last_checked_at": kwargs["completed_at"],
        }
        return len(kwargs["entries"])

    def record_hdencode_feed_not_modified(self, **kwargs):
        self.not_modified.append(kwargs)

    def record_hdencode_feed_failure(self, **kwargs):
        self.failures.append(kwargs)

    def update_hdencode_feed_depth(self, key, depth):
        self.depths.append((key, depth))


class FakeClient:
    def __init__(self, response, *, after_fetch=None):
        self.response = response
        self.calls = []
        self.after_fetch = after_fetch

    def fetch(self, url, *, last_modified=None):
        self.calls.append((url, last_modified))
        if self.after_fetch:
            self.after_fetch()
        return self.response


def service(response, *, mode="rss_shadow", config=None, after_fetch=None):
    db = FakeDb()
    effective = {
        "hdencode_enabled": True,
        "hdencode_discovery_mode": mode,
        "hdencode_rss_poll_minutes": 60,
        "hdencode_rss_catchup_hours": 4,
    }
    if config:
        effective.update(config)
    return HDEncodeRSSService(
        effective,
        db,
        client=FakeClient(response, after_fetch=after_fetch),
    ), db


def test_changed_feed_ingests_and_never_downloads():
    rss, db = service(
        FeedResponse(
            status=200,
            final_url="https://hdencode.org/tag/movies/feed/",
            last_modified="validator",
            body=RSS,
        )
    )
    result = rss.poll_feed(get_feed("movies_all"))
    assert result["outcome"] == "changed"
    assert len(db.ingests) == 1
    assert result["candidate_count"] == 1


def test_304_does_not_parse_or_write_candidates():
    rss, db = service(
        FeedResponse(
            status=304,
            final_url="https://hdencode.org/tag/movies/feed/",
            last_modified="old",
            body=b"",
        )
    )
    db.state["movies_all"] = {
        "last_modified": "old",
        "last_checked_at": (
            datetime.now(timezone.utc) - timedelta(hours=2)
        ).isoformat(),
    }
    result = rss.poll_feed(get_feed("movies_all"))
    assert result["outcome"] == "not_modified"
    assert db.ingests == []
    assert len(db.not_modified) == 1


def test_shadow_cycle_does_not_start_listing_fallback_or_download():
    rss, _ = service(
        FeedResponse(
            status=304,
            final_url="https://hdencode.org/tag/movies/feed/",
            last_modified="old",
            body=b"",
        )
    )
    cycle = rss.poll_cycle(include_catchup=False)
    assert cycle["listing_fallback_started"] is False
    assert cycle["downloads_started"] == 0


def test_listing_mode_skips_rss():
    rss, db = service(
        FeedResponse(200, "https://hdencode.org/feed/", None, RSS),
        mode="listing",
    )
    result = rss.poll_cycle()
    assert result["skipped"] is True
    assert db.ingests == []


def test_missing_enable_switch_inherits_application_default():
    db = FakeDb()
    rss = HDEncodeRSSService(
        {
            "hdencode_discovery_mode": "rss_shadow",
            "hdencode_rss_poll_minutes": 60,
            "hdencode_rss_catchup_hours": 4,
        },
        db,
        client=FakeClient(
            FeedResponse(
                status=304,
                final_url="https://hdencode.org/tag/movies/feed/",
                last_modified=None,
                body=b"",
            )
        ),
    )
    assert rss.poll_feed(get_feed("movies_all"))["outcome"] == "not_modified"


def test_stop_before_start_issues_no_request():
    response = FeedResponse(
        status=200,
        final_url="https://hdencode.org/tag/movies/feed/",
        last_modified=None,
        body=RSS,
    )
    rss, _ = service(response)
    result = rss.poll_cycle(
        stop_requested=lambda: True,
        include_catchup=False,
    )
    assert result["feeds"][0]["outcome"] == "cancelled_before_start"
    assert rss.client.calls == []


def test_late_response_from_stale_lifespan_cannot_publish():
    stale = threading.Event()
    rss, db = service(
        FeedResponse(
            status=200,
            final_url="https://hdencode.org/tag/movies/feed/",
            last_modified="new",
            body=RSS,
        ),
        after_fetch=stale.set,
    )
    result = rss.poll_feed(
        get_feed("movies_all"),
        stop_requested=stale.is_set,
    )
    assert result["outcome"] == "cancelled_after_response"
    assert db.ingests == []
    assert db.not_modified == []
    assert db.failures == []


def test_bounded_gzip_reader_rejects_decompression_bomb():
    compressed = gzip.compress(b"x" * (MAX_FEED_BYTES + 1))
    with pytest.raises(ValueError, match="2 MiB"):
        _read_limited(BytesIO(compressed), gzip_encoded=True)


def test_url_validation_rejects_private_resolution(monkeypatch):
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))
        ],
    )
    with pytest.raises(ValueError, match="Unsafe"):
        validate_feed_url("https://hdencode.org/tag/movies/feed/")
