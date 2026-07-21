"""Hydration year provenance and identity-conflict regression tests."""
from __future__ import annotations

from backend.database import DatabaseManager
from backend.hdencode_candidate_service import (
    HDEncodeCandidateService,
    _candidate_updates,
    _identity_is_confirmed,
)


def _feed_entry(url: str) -> dict:
    return {
        "canonical_url": url,
        "guid": "guid-example",
        "title": "Example.2025.2160p",
        "pub_date": "2026-07-20T00:00:00+00:00",
        "media_type": "movie",
        "clean_title": "Example",
        "title_year": 2025,
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
        "raw_description": "",
        "raw_hash": "hash-example",
        "description_complete": False,
    }


def test_detail_year_is_description_evidence_not_title_evidence():
    updates = _candidate_updates({
        "url": "https://hdencode.org/example/",
        "display_title": "Example",
        "year": 2026,
    })
    assert updates["description_year"] == 2026
    assert "title_year" not in updates


def test_movie_can_confirm_from_description_year_when_title_year_missing():
    assert _identity_is_confirmed({
        "clean_title": "Example",
        "media_type": "movie",
        "title_year": None,
        "description_year": 2026,
        "imdb_id": None,
        "tmdb_id": None,
    }) is True


def test_hydration_preserves_conflicting_title_and_description_years(tmp_path):
    db = DatabaseManager(str(tmp_path / "crawler.db"))
    url = "https://hdencode.org/example/"
    db.ingest_hdencode_feed(
        feed_key="movies_all",
        feed_url="https://hdencode.org/tag/movies/feed/",
        last_modified="validator",
        http_status=200,
        body_sha256="body",
        channel_last_build_date=None,
        entries=[_feed_entry(url)],
        started_at="2026-07-20T00:00:00+00:00",
        completed_at="2026-07-20T00:01:00+00:00",
    )

    updates = _candidate_updates({
        "url": url,
        "display_title": "Example",
        "year": 2026,
        "res": "2160p",
        "size": "45 GB",
        "dovi": True,
        "hdr": "HDR10",
    })
    db.complete_hdencode_hydration(
        url,
        payload={"year": 2026},
        candidate_updates=updates,
    )

    row = db.get_hdencode_candidate(url)
    assert row["title_year"] == 2025
    assert row["description_year"] == 2026
    assert row["identity_state"] == "hydrated"

    service = HDEncodeCandidateService({}, db)
    assert service.classify_candidate(row) == "detail_required"
    after = db.get_hdencode_candidate(url)
    assert after["identity_state"] == "hydrated"
