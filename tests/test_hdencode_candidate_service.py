"""Tests for conservative RSS classification and hydration."""
from datetime import datetime, timedelta, timezone
import json

from backend.database import DatabaseManager
from backend.hdencode_candidate_service import HDEncodeCandidateService


def row(**overrides):
    base = {
        "canonical_url": "https://hdencode.org/example/",
        "title": "Example",
        "clean_title": "Example",
        "media_type": "movie",
        "title_year": 2026,
        "description_year": None,
        "season": None,
        "resolution": "2160p",
        "size_gb": 50,
        "dv_evidence": "unknown",
        "hdr_evidence": "unknown",
        "hevc_evidence": "asserted",
        "hdr_formats": "[]",
        "description_complete": 1,
        "identity_state": "unknown",
    }
    base.update(overrides)
    return base


class Db:
    def __init__(self, context=None):
        self.context = context or {
            "exact_url_downloaded": False,
            "plex_matches": [],
        }
        self.updated = []
        self.queued = []
        self.resolved = []
        self.claimed = []

    def recover_hdencode_hydration_queue(self):
        return 0

    def list_hdencode_candidates(self, **_kwargs):
        return []

    def get_hdencode_candidate_context(self, **_kwargs):
        return self.context

    def update_hdencode_candidate_state(self, url, **kwargs):
        self.updated.append((url, kwargs))

    def enqueue_hdencode_hydration(self, url, **kwargs):
        self.queued.append((url, kwargs))

    def resolve_hdencode_hydration(self, url, **kwargs):
        self.resolved.append((url, kwargs))

    def claim_hdencode_hydration(self, *, limit):
        return self.claimed[:limit]

    def complete_hdencode_hydration(
        self, url, *, payload, candidate_updates
    ):
        self.completed = (url, payload, candidate_updates)

    def fail_hdencode_hydration(self, url, *, error_code):
        self.failed = (url, error_code)

    def release_hdencode_hydration(self, url, *, reason):
        self.released = (url, reason)


def test_exact_url_history_skips_without_hydration():
    db = Db({"exact_url_downloaded": True, "plex_matches": []})
    service = HDEncodeCandidateService({}, db)
    assert service.classify_candidate(row()) == "irrelevant_conclusive"
    assert db.queued == []
    assert db.resolved


def test_no_exact_identity_requires_detail_not_missing_guess():
    db = Db()
    service = HDEncodeCandidateService({}, db)
    assert service.classify_candidate(row()) == "detail_required"
    assert db.queued[0][1]["reason"] == "identity_unresolved"


def test_unknown_dv_against_known_dv_requires_detail():
    db = Db({
        "exact_url_downloaded": False,
        "plex_matches": [{
            "resolution": "2160p",
            "size_gb": 40,
            "dovi": 1,
        }],
    })
    service = HDEncodeCandidateService({}, db)
    assert service.classify_candidate(row()) == "detail_required"
    assert db.queued[0][1]["reason"] == "dolby_vision_unknown"


def test_asserted_dv_gain_is_upgrade_without_detail():
    db = Db({
        "exact_url_downloaded": False,
        "plex_matches": [{
            "resolution": "2160p",
            "size_gb": 60,
            "dovi": 0,
        }],
    })
    service = HDEncodeCandidateService({}, db)
    assert service.classify_candidate(
        row(dv_evidence="asserted")
    ) == "relevant_upgrade"
    assert db.queued == []
    assert db.resolved


def test_hydration_is_bounded_merges_authoritative_evidence_and_never_downloads():
    db = Db()
    db.claimed = [
        {"canonical_url": "https://hdencode.org/a/"},
        {"canonical_url": "https://hdencode.org/b/"},
    ]

    class Detail:
        def __init__(self):
            self.calls = []

        def scrape_details(self, url, **_kwargs):
            self.calls.append(url)
            return {
                "display_title": "Hydrated",
                "year": 2026,
                "season": None,
                "episode_number": None,
                "res": "4K",
                "size": "55 GB",
                "dovi": True,
                "hdr": "HDR10+",
            }

    detail = Detail()
    result = HDEncodeCandidateService(
        {"hdencode_rss_hydration_limit": 1},
        db,
    ).hydrate_pending(detail)
    assert result["claimed"] == 1
    assert detail.calls == ["https://hdencode.org/a/"]
    _, _, updates = db.completed
    assert updates["identity_state"] == "exact"
    assert updates["dv_evidence"] == "asserted"
    assert updates["hdr_evidence"] == "asserted"
    assert updates["hdr_formats"] == ["HDR10+"]
    assert updates["size_gb"] == 55


def test_cancelled_hydration_releases_queue_without_request():
    db = Db()
    db.claimed = [{"canonical_url": "https://hdencode.org/a/"}]

    class Detail:
        def scrape_details(self, *_args, **_kwargs):
            raise AssertionError("detail request must not start")

    result = HDEncodeCandidateService({}, db).hydrate_pending(
        Detail(),
        limit=1,
        stop_requested=lambda: True,
    )
    assert result["cancelled"] == 1
    assert db.released == ("https://hdencode.org/a/", "cancelled")


def test_cancelled_classification_does_not_publish():
    db = Db()
    service = HDEncodeCandidateService({}, db)
    assert service.classify_candidate(
        row(),
        stop_requested=lambda: True,
    ) == "cancelled"
    assert db.updated == []
    assert db.queued == []


def _candidate_entry():
    return {
        "canonical_url": "https://hdencode.org/example/",
        "guid": "example-guid",
        "title": "Example.2026.2160p",
        "pub_date": "2026-07-19T20:00:00+00:00",
        "media_type": "movie",
        "clean_title": "Example",
        "title_year": 2026,
        "description_year": None,
        "season": None,
        "episode": None,
        "episode_end": None,
        "resolution": "2160p",
        "size_text": "40 GB",
        "size_gb": 40.0,
        "dv": "unknown",
        "hdr": "unknown",
        "hevc": "asserted",
        "hdr_formats": [],
        "categories": ["Movies"],
        "raw_description": "Year: 2026",
        "raw_hash": "hash",
        "description_complete": True,
    }


def _real_db(tmp_path):
    db = DatabaseManager(str(tmp_path / "crawler.db"))
    db.ingest_hdencode_feed(
        feed_key="movies_all",
        feed_url="https://hdencode.org/tag/movies/feed/",
        last_modified="validator",
        http_status=200,
        body_sha256="body",
        channel_last_build_date=None,
        entries=[_candidate_entry()],
        started_at="2026-07-19T20:00:00+00:00",
        completed_at="2026-07-19T20:01:00+00:00",
    )
    return db


def test_lifecycle_cancellation_does_not_consume_retry_attempt(tmp_path):
    db = _real_db(tmp_path)
    url = _candidate_entry()["canonical_url"]
    db.enqueue_hdencode_hydration(
        url,
        reason="identity_unresolved",
        priority=70,
    )
    assert len(db.claim_hdencode_hydration(limit=1)) == 1
    db.release_hdencode_hydration(url, reason="cancelled")
    with db.transaction() as conn:
        queue = conn.execute(
            "SELECT state, attempts FROM hdencode_hydration_queue "
            "WHERE canonical_url = ?",
            (url,),
        ).fetchone()
    assert tuple(queue) == ("queued", 0)


def test_hydration_updates_candidate_and_breaks_detail_loop(tmp_path):
    db = _real_db(tmp_path)
    url = _candidate_entry()["canonical_url"]
    db.enqueue_hdencode_hydration(
        url,
        reason="identity_unresolved",
        priority=70,
    )

    class Detail:
        def scrape_details(self, *_args, **_kwargs):
            return {
                "display_title": "Example",
                "year": 2026,
                "season": None,
                "episode_number": None,
                "res": "4K",
                "size": "55 GB",
                "dovi": True,
                "hdr": "HDR10+",
            }

    service = HDEncodeCandidateService({}, db)
    result = service.hydrate_pending(Detail(), limit=1)
    assert result["completed"] == 1
    candidate = db.get_hdencode_candidate(url)
    assert candidate["identity_state"] == "exact"
    assert candidate["dv_evidence"] == "asserted"
    assert candidate["size_gb"] == 55
    assert service.classify_candidate(candidate) == "relevant_missing"


def test_stale_running_claim_recovers_after_restart(tmp_path):
    db = _real_db(tmp_path)
    url = _candidate_entry()["canonical_url"]
    db.enqueue_hdencode_hydration(
        url,
        reason="identity_unresolved",
        priority=70,
    )
    assert len(db.claim_hdencode_hydration(limit=1)) == 1
    old = (
        datetime.now(timezone.utc) - timedelta(hours=1)
    ).isoformat()
    with db.transaction() as conn:
        conn.execute(
            "UPDATE hdencode_hydration_queue SET claimed_at = ? "
            "WHERE canonical_url = ?",
            (old, url),
        )
    assert db.recover_hdencode_hydration_queue(
        stale_after_minutes=30
    ) == 1
    with db.transaction() as conn:
        queue = conn.execute(
            "SELECT state, attempts, last_error_code "
            "FROM hdencode_hydration_queue WHERE canonical_url = ?",
            (url,),
        ).fetchone()
    assert tuple(queue) == ("queued", 0, "recovered_after_restart")
