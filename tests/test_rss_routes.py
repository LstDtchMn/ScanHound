"""API truthfulness and lifecycle tests for RSS operations."""
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from backend.api.routes import rss


class Db:
    def __init__(self, *, ready=True):
        self.ready = ready
        self.enqueued = []

    def get_hdencode_rss_readiness(self, **_kwargs):
        return {
            "ready": self.ready,
            "required_cycles": 20,
            "successful_cycles": 20 if self.ready else 4,
            "required_days": 7,
            "observed_days": 8 if self.ready else 2,
            "normal_feeds_healthy": self.ready,
            "reasons": [] if self.ready else ["insufficient_days"],
        }

    def list_hdencode_candidates(self, **_kwargs):
        return [{
            "canonical_url": "https://hdencode.org/example/",
            "title": "Example",
            "title_year": 2026,
            "description_year": 2025,
            "dv_evidence": "unknown",
            "hdr_evidence": "asserted",
            "hevc_evidence": "unknown",
            "hdr_formats": '["HDR10+"]',
            "categories": '["Movies"]',
            "identity_state": "ambiguous",
            "relevance_state": "detail_required",
            "hydration_state": "queued",
            "raw_description": "<script>not public</script>",
        }]

    def get_hdencode_candidate(self, url):
        for candidate in self.list_hdencode_candidates():
            if candidate["canonical_url"] == url:
                return candidate
        return None

    def list_hdencode_feed_states(self):
        return [{"feed_key": "movies_all", "last_status": 304}]

    def list_hdencode_hydration_queue(self, **_kwargs):
        return [{"state": "queued", "canonical_url": "https://hdencode.org/example/"}]

    def enqueue_hdencode_hydration(self, url, **kwargs):
        self.enqueued.append((url, kwargs))
        return True

    def update_hdencode_candidate_state(self, *_args, **_kwargs):
        return True

    def recover_hdencode_hydration_queue(self):
        return 0


class Registry:
    def __init__(self, *, ready=True, enabled=True):
        self.db = Db(ready=ready)
        self.config = {
            "hdencode_enabled": enabled,
            "hdencode_discovery_mode": "rss_shadow",
            "hdencode_rss_listing_fallback_enabled": False,
            "hdencode_rss_auto_grab_enabled": False,
            "hdencode_rss_hydration_limit": 10,
            "hdencode_rss_shadow_min_cycles": 20,
            "hdencode_rss_shadow_min_days": 7,
        }
        self.background_scanner = SimpleNamespace(last_run=None)
        self.backend = SimpleNamespace(save_config=lambda: None)
        self.scanner = SimpleNamespace(
            scrapers=SimpleNamespace(_detail=object())
        )
        self.lifespan_generation = 7
        self._owns = True

    def owns_lifespan(self, generation):
        return self._owns and generation == self.lifespan_generation


def test_status_reports_readiness_unknowns_and_safe_defaults():
    result = rss.rss_status(Registry())
    assert result["readiness"]["ready"] is True
    assert result["unknown_counts"]["dv"] == 1
    assert result["unknown_counts"]["identity"] == 1
    assert result["unknown_counts"]["year_conflict"] == 1
    assert result["safe_defaults"]["listing_fallback"] is False
    assert result["safe_defaults"]["rss_auto_grab"] is False


def test_candidate_payload_preserves_unknown_without_raw_description():
    result = rss.rss_candidates(reg=Registry())
    item = result["items"][0]
    assert item["dv_evidence"] == "unknown"
    assert item["evidence_incomplete"] is True
    assert item["year_conflict"] is True
    assert item["hdr_formats"] == ["HDR10+"]
    assert "raw_description" not in item


def test_primary_mode_is_blocked_until_readiness():
    reg = Registry(ready=False)
    with pytest.raises(HTTPException) as caught:
        rss.set_rss_mode(rss.ModeRequest(mode="rss_primary"), reg)
    assert caught.value.status_code == 409
    assert reg.config["hdencode_discovery_mode"] == "rss_shadow"


def test_primary_mode_and_one_setting_rollback_when_ready():
    reg = Registry(ready=True)
    assert rss.set_rss_mode(
        rss.ModeRequest(mode="rss_primary"),
        reg,
    )["mode"] == "rss_primary"
    assert rss.set_rss_mode(
        rss.ModeRequest(mode="listing"),
        reg,
    )["mode"] == "listing"


def test_hydration_rejects_when_hdencode_disabled():
    reg = Registry(enabled=False)
    with pytest.raises(HTTPException) as caught:
        rss.hydrate_candidate(
            rss.CandidateRequest(
                canonical_url="https://hdencode.org/example/"
            ),
            reg,
        )
    assert caught.value.status_code == 409


def test_explicit_hydration_uses_captured_lifespan_generation(monkeypatch):
    reg = Registry()
    observed = {}

    class CandidateService:
        def __init__(self, config, db):
            observed["context"] = (config, db)

        def hydrate_pending(
            self,
            detail_scraper,
            *,
            limit,
            stop_requested,
        ):
            observed["limit"] = limit
            observed["before"] = stop_requested()
            reg._owns = False
            observed["after"] = stop_requested()
            return {
                "claimed": 0,
                "completed": 0,
                "failed": 0,
                "cancelled": 0,
            }

    class ImmediateThread:
        def __init__(self, *, target, **_kwargs):
            self.target = target

        def start(self):
            self.target()

    monkeypatch.setattr(rss, "HDEncodeCandidateService", CandidateService)
    monkeypatch.setattr(rss.threading, "Thread", ImmediateThread)

    result = rss.hydrate_candidate(
        rss.CandidateRequest(
            canonical_url="https://hdencode.org/example/"
        ),
        reg,
    )
    assert result["status"] == "started"
    assert observed["before"] is False
    assert observed["after"] is True
    assert observed["limit"] == 1
