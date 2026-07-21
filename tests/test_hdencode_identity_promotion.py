"""Identity promotion safety: 'hydrated' is provenance, not confirmed identity.

Peer-review issue (ChatGPT): raw 'hydrated' must never be promoted to 'exact'
nor be sufficient for an autonomous grab. Promotion to 'exact' requires a real
identity — an external id, a unique Plex match, or a complete non-conflicting
identity tuple. Conflicting years, bare season packs, and multiple matches stay
unresolved/ambiguous.
"""
import pytest

from backend.hdencode_candidate_service import (
    HDEncodeCandidateService,
    _identity_is_confirmed,
)
from backend.hdencode_action_service import HDEncodeActionService, HDEncodeActionError


def _row(**overrides):
    base = {
        "canonical_url": "https://hdencode.org/example/",
        "title": "Example",
        "clean_title": "Example",
        "media_type": "movie",
        "title_year": 2026,
        "description_year": None,
        "season": None,
        "episode": None,
        "resolution": "2160p",
        "size_gb": 50,
        "dv_evidence": "asserted",
        "hdr_evidence": "asserted",
        "hevc_evidence": "asserted",
        "hdr_formats": "[]",
        "description_complete": 1,
        "identity_state": "hydrated",
        "imdb_id": None,
        "tmdb_id": None,
    }
    base.update(overrides)
    return base


class _Db:
    def __init__(self, context=None):
        self.context = context or {"exact_url_downloaded": False, "plex_matches": []}
        self.updated = []

    def recover_hdencode_hydration_queue(self):
        return 0

    def get_hdencode_candidate_context(self, **_kwargs):
        return self.context

    def update_hdencode_candidate_state(self, url, **kwargs):
        self.updated.append((url, kwargs))

    def enqueue_hdencode_hydration(self, url, **kwargs):
        pass

    def resolve_hdencode_hydration(self, url, **kwargs):
        pass


def _classify_identity(row, context=None):
    db = _Db(context)
    HDEncodeCandidateService({}, db).classify_candidate(row)
    return db.updated[-1][1]["identity_state"]


# ── _identity_is_confirmed unit cases ──────────────────────────────────────

def test_hydrated_title_only_is_not_confirmed():
    assert _identity_is_confirmed(_row(title_year=None)) is False


def test_hydrated_movie_title_year_is_confirmed():
    assert _identity_is_confirmed(_row(title_year=2026)) is True


def test_hydrated_tv_episode_is_confirmed():
    assert _identity_is_confirmed(
        _row(media_type="tv", season=1, episode=3)
    ) is True


def test_season_pack_without_episode_needs_explicit_identity():
    pack = _row(media_type="tv", season=1, episode=None)
    assert _identity_is_confirmed(pack) is False
    assert _identity_is_confirmed({**pack, "imdb_id": "tt1234567"}) is True


def test_year_conflict_is_not_confirmed():
    assert _identity_is_confirmed(
        _row(title_year=2026, description_year=2025)
    ) is False


def test_external_ids_confirm_identity():
    assert _identity_is_confirmed(_row(clean_title="", imdb_id="tt9999999")) is True
    assert _identity_is_confirmed(_row(clean_title="", tmdb_id="4242")) is True


# ── classify_candidate promotion (end-to-end) ──────────────────────────────

def test_classify_keeps_raw_hydrated_unresolved_without_confirmed_identity():
    # No Plex match, no external id, incomplete tuple -> stays 'hydrated'.
    assert _classify_identity(_row(title_year=None)) == "hydrated"


def test_classify_promotes_hydrated_movie_tuple_to_exact():
    assert _classify_identity(_row(title_year=2026)) == "exact"


def test_classify_multiple_plex_matches_are_ambiguous():
    ctx = {"exact_url_downloaded": False, "plex_matches": [{"a": 1}, {"a": 2}]}
    assert _classify_identity(_row(), ctx) == "ambiguous"


def test_classify_year_conflict_stays_unresolved():
    assert _classify_identity(_row(title_year=2026, description_year=2025)) == "hydrated"


# ── auto-grab gate ─────────────────────────────────────────────────────────

def _auto_candidate(**overrides):
    cand = {
        "canonical_url": "https://hdencode.org/example/",
        "relevance_state": "relevant_missing",
        "identity_state": "exact",
        "hydration_state": "completed",
        "description_complete": 1,
        "title_year": 2026,
        "description_year": 2026,
        "dv_evidence": "asserted",
        "hdr_evidence": "asserted",
    }
    cand.update(overrides)
    return cand


def _service():
    return HDEncodeActionService({"hdencode_rss_auto_grab_enabled": True}, None, None)


def test_raw_hydrated_cannot_auto_grab():
    svc = _service()
    with pytest.raises(HDEncodeActionError) as caught:
        svc._validate_auto_action(_auto_candidate(identity_state="hydrated"), "grab")
    assert caught.value.code == "auto_identity_unknown"


def test_exact_identity_passes_auto_grab_gate():
    svc = _service()
    # Should not raise.
    svc._validate_auto_action(_auto_candidate(identity_state="exact"), "grab")
