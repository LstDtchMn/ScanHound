"""Final adversarial follow-up for RSS recovery and identity conflict."""
from backend.background_scanner import BackgroundScanner
from backend.hdencode_candidate_service import HDEncodeCandidateService

class _Registry:
    lifespan_generation = 1
    config = {}
    scanner = None
    db = None
    def owns_lifespan(self, generation):
        return generation == self.lifespan_generation

def _eligible_metrics():
    return {"normal_feeds_complete": True, "rss_requests": 2,
            "listing_requests": 10, "outcome": "success"}

def test_ineligible_pass_does_not_consume_restart_marker():
    scanner = BackgroundScanner(_Registry())
    assert scanner._qualify_restart_recovery(
        preexisting_normal_feed_state=True,
        metrics={"normal_feeds_complete": False, "rss_requests": 0,
                 "listing_requests": 10, "outcome": "success"},
    ) is False
    assert scanner._rss_first_cycle_after_startup is True

def test_first_eligible_comparison_counts_recovery_once():
    scanner = BackgroundScanner(_Registry())
    assert scanner._qualify_restart_recovery(
        preexisting_normal_feed_state=True, metrics=_eligible_metrics()) is True
    assert scanner._qualify_restart_recovery(
        preexisting_normal_feed_state=True, metrics=_eligible_metrics()) is False

def test_fresh_install_consumes_marker_without_recovery():
    scanner = BackgroundScanner(_Registry())
    assert scanner._qualify_restart_recovery(
        preexisting_normal_feed_state=False, metrics=_eligible_metrics()) is False
    assert scanner._rss_first_cycle_after_startup is False

def test_completion_rejects_not_due_and_listing_failure():
    completed = [{"feed": "movies_all", "outcome": "changed"},
                 {"feed": "tv_all", "outcome": "not_modified"}]
    not_due = [{"feed": "movies_all", "outcome": "not_due"},
               {"feed": "tv_all", "outcome": "not_due"}]
    assert BackgroundScanner._rss_normal_feeds_complete(completed) is True
    assert BackgroundScanner._rss_normal_feeds_complete(not_due) is False
    assert BackgroundScanner._rss_normal_feeds_complete(
        completed, listing_error="listing failed") is False

def _row(**overrides):
    row = {"canonical_url": "https://hdencode.org/example/",
           "title": "Example", "clean_title": "Example",
           "media_type": "movie", "title_year": 2026,
           "description_year": 2025, "season": None, "episode": None,
           "resolution": "2160p", "size_gb": 50,
           "dv_evidence": "asserted", "hdr_evidence": "asserted",
           "hevc_evidence": "asserted", "hdr_formats": "[]",
           "description_complete": 1, "identity_state": "hydrated",
           "imdb_id": None, "tmdb_id": None}
    row.update(overrides)
    return row

class _Db:
    def __init__(self, context):
        self.context = context
        self.updated = []
    def recover_hdencode_hydration_queue(self): return 0
    def get_hdencode_candidate_context(self, **_kwargs): return self.context
    def update_hdencode_candidate_state(self, url, **kwargs):
        self.updated.append((url, kwargs))
    def enqueue_hdencode_hydration(self, *_args, **_kwargs): return None
    def resolve_hdencode_hydration(self, *_args, **_kwargs): return None

def _identity(row, context):
    db = _Db(context)
    HDEncodeCandidateService({}, db).classify_candidate(row)
    update = db.updated[-1][1]
    return update["identity_state"], update["detail_reason"]

def test_unique_plex_match_cannot_override_year_conflict():
    state, reason = _identity(_row(), {"exact_url_downloaded": False,
        "plex_matches": [{"title": "Example", "year": 2026}],
        "identity_basis": "year:2026"})
    assert state == "ambiguous"
    assert reason == "identity_year_conflict"

def test_title_only_match_cannot_override_year_conflict():
    state, reason = _identity(_row(), {"exact_url_downloaded": False,
        "plex_matches": [{"title": "Example", "year": None}],
        "identity_basis": "title_only"})
    assert state == "ambiguous"
    assert reason == "identity_year_conflict"

def test_external_id_match_may_override_year_conflict():
    state, _ = _identity(_row(imdb_id="tt1234567"), {
        "exact_url_downloaded": False,
        "plex_matches": [{"title": "Example", "year": 2026}],
        "identity_basis": "imdb_id"})
    assert state == "exact"
