"""The media kind must be the SERVER's answer, never the client's.

Peer review round 10, finding M1. The kind was taken from
`DownloadRequest.category`, which is declared `category: str = ""`, documented
as unvalidated, and forwarded straight into `download_item(category=...)`. The
frontend fills it from `ScanResult.category`, so the real chain was:

    server scan  ->  frontend object  ->  JSON request  ->  unvalidated field
                 ->  downloads.media_kind  ->  identity_kind
                 ->  destructive Keep-best authority in the UI

The reviewer's phrasing of the defect is the one worth keeping:

    package provenance != media-kind provenance

Knowing which release a package came from does not certify what KIND of thing
that release is. The server scanned it and already recorded the listing it came
from, so the server answers; the client's value is only ever allowed to
CONTRADICT.

WHY A WRONG VALUE IS WORSE THAN A MISSING ONE: a missing kind fails closed --
the row groups but never authorizes. A recognized wrong kind authorizes. This
stack has already shipped one signal-propagation bug (batched grabs dropped the
category entirely until #92), so the mechanism is not hypothetical.
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from backend.database import DatabaseManager
from backend.download_service import DownloadService


URL = "https://hdencode.org/some-release-2026-2160p/"


@pytest.fixture
def svc(tmp_path):
    db = DatabaseManager(str(tmp_path / "m1.db"))
    service = DownloadService.__new__(DownloadService)
    service.db = db
    yield service
    db.close()


def _scanned_as(db, category, url=URL):
    """Record what THIS SERVER saw, the way a background scan does."""
    db.upsert_background_cache([{
        "url": url,
        "title": "Some Release",
        "year": 2026,
        "status": "missing",
        "source_category": "HDEncode",
        "data": json.dumps({"url": url, "title": "Some Release",
                            "category": category, "season": None}),
    }])


class TestTheServerAnswers:
    def test_the_recorded_category_decides(self, svc):
        _scanned_as(svc.db, "tv")
        assert svc.verified_media_kind(URL, "tv") == "tv"

    def test_a_silent_client_still_gets_the_servers_answer(self, svc):
        """The client's value is a cross-check, not the supply. Sending nothing
        is not a disagreement."""
        _scanned_as(svc.db, "4k")
        assert svc.verified_media_kind(URL, "") == "movie"
        assert svc.verified_media_kind(URL, None) == "movie"

    def test_remux_is_a_movie_and_tv_is_tv(self, svc):
        _scanned_as(svc.db, "remux", url=URL + "a")
        _scanned_as(svc.db, "tv", url=URL + "b")
        assert svc.verified_media_kind(URL + "a", "remux") == "movie"
        assert svc.verified_media_kind(URL + "b", "tv") == "tv"


class TestAClientCannotSupplyTheAnswer:
    """The finding itself."""

    def test_a_recognized_but_WRONG_category_records_nothing(self, svc):
        """The exact hazard. 'tv' is a recognized value, so the old code mapped
        it happily; the server scanned this release from a 4K listing."""
        _scanned_as(svc.db, "4k")
        assert svc.verified_media_kind(URL, "tv") is None

    def test_the_reverse_disagreement_also_records_nothing(self, svc):
        _scanned_as(svc.db, "tv")
        assert svc.verified_media_kind(URL, "4k") is None

    def test_an_unscanned_url_records_nothing_however_confident_the_client(self, svc):
        """No server record is 'cannot verify', not 'trust the caller'."""
        assert svc.verified_media_kind(URL, "tv") is None
        assert svc.verified_media_kind(URL, "4k") is None

    def test_a_disagreement_is_logged_not_silent(self, svc, caplog):
        """A conflict means a stale UI, a mismatched row, or a call-site bug.
        Failing closed silently would hide the bug that caused it."""
        _scanned_as(svc.db, "4k")
        with caplog.at_level("WARNING"):
            assert svc.verified_media_kind(URL, "tv") is None
        # getMessage() applies the args; r.message alone is the raw format
        # string, so a %-placeholder assertion silently tests the wrong text.
        messages = [r.getMessage() for r in caplog.records]
        assert any("media kind NOT recorded" in m for m in messages), messages
        assert any("category='tv'" in m and "'4k'" in m for m in messages), (
            "the log must name BOTH values; 'a mismatch happened' is not "
            "actionable when the point is to find which side is wrong")


class TestUnreadableEvidenceIsNotPermission:
    def test_a_failing_lookup_records_nothing(self, svc):
        """An exception from the evidence source must not fall through to
        trusting the client -- that would make a broken database the most
        permissive state."""
        svc.db = MagicMock()
        svc.db.get_scan_category.side_effect = RuntimeError("db down")
        assert svc.verified_media_kind(URL, "tv") is None

    def test_no_database_at_all_records_nothing(self, svc):
        svc.db = None
        assert svc.verified_media_kind(URL, "tv") is None

    def test_an_undecodable_cached_row_records_nothing(self, svc):
        svc.db.upsert_background_cache([{
            "url": URL, "title": "x", "year": 2026, "status": "missing",
            "source_category": "HDEncode", "data": "{not json",
        }])
        assert svc.verified_media_kind(URL, "tv") is None


class TestTheResolutionHappensOnce:
    def test_every_history_path_consumes_one_resolved_value(self):
        """Four call sites wrote the kind. Resolving per site would be four
        chances for one of them to keep using the raw client category -- the
        defect class that let batched grabs drop it entirely until #92.
        """
        import io as _io
        import re

        source = _io.open("backend/download_service.py", encoding="utf-8").read()
        assert source.count("_verified_kind = self.verified_media_kind(") == 1, (
            "the kind must be resolved exactly once per download_item call")
        assert source.count("media_kind=_verified_kind") == 4, (
            "every history-writing path must consume the resolved value")
        assert not re.search(r"media_kind=self\.media_kind_for_category\(category\)", source), (
            "a history path is still mapping the raw client category")
