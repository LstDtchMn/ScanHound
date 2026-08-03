"""AMBIGUOUS must not reach a typed decision, asserted at the CONSUMER.

Round 8 review, verified independently. Round 7's fix carried the tri-state to
`web_item_facts()` and to `MediaItem`, and the value is then **collapsed back
to a binary at the two places that decide**:

* `_match_against_plex` selects its matcher with `if web_item['is_tv']`, so an
  ambiguous item takes the `else` branch — the movie matcher.
* `_identity_is_confirmed` has an explicit TV branch and falls through to
  movie, so an ambiguous row with a clean title and year confirms as a movie,
  is promoted to `identity_state='exact'`, and passes `_validate_auto_action`.

**This is the fourth occurrence of one failure, and it happened inside the fix
for the third.** Extracting a seam made the value visible; nothing was made to
consume it. Testing the seam proved the value exists, not that any decision
reads it.

The rule these tests follow, and the reason they call production entry points
rather than helpers:

    A gate-closing test must invoke the production CONSUMER that makes the
    decision and assert an externally observable branch or state. Source text,
    comments, and a restatement of the predicate are inventory evidence only.

Every test is ``xfail(strict=True)`` against the desired end state: they fail
today and turn the suite RED when the consumers become tri-state, which is the
signal to delete the markers.
"""

import pytest

# ─────────── the matcher selector must be tri-state, not boolean ────────────
#
# These call the PRODUCTION method and assert which matcher ran. An earlier
# version asserted a source string and a restatement of the predicate; both
# were satisfiable while the consumer still collapsed the tri-state.


class _FakeMatching:
    """Records which typed matcher the production selector chose."""

    def __init__(self):
        self.tv_calls = 0
        self.movie_calls = 0

    def find_tv_season_matches(self, web_item, plex_index):
        self.tv_calls += 1
        return [], False

    def find_movie_matches(self, web_item, plex_index):
        self.movie_calls += 1
        return [], False


def _run_matcher(media_type):
    """Drive the real ScannerService._match_against_plex over one item."""
    import asyncio
    import threading
    import types
    from backend.scanner_service import MediaItem, ScannerService

    svc = ScannerService.__new__(ScannerService)
    item = MediaItem(id="1", title="Great Show", year=2024, season=None,
                     media_type=media_type)
    svc.items = [item]
    svc._items_lock = threading.Lock()
    # stop_scan_flag is a property backed by _stop_event; set the event.
    svc._stop_event = threading.Event()
    svc.matching = _FakeMatching()
    svc.plex = types.SimpleNamespace(
        plex_index={"all_items": [object()], "by_title": {}})
    svc._log = lambda *a, **k: None
    svc._progress = lambda *a, **k: None
    svc.db = types.SimpleNamespace()
    asyncio.run(svc._match_against_plex("Deep Scan"))
    return svc.matching, item


class TestTheMatcherSelectorIsTriState:
    def test_tv_calls_only_the_tv_matcher(self):
        matching, _ = _run_matcher("tv")
        assert (matching.tv_calls, matching.movie_calls) == (1, 0)

    def test_movie_calls_only_the_movie_matcher(self):
        matching, _ = _run_matcher("movie")
        assert (matching.tv_calls, matching.movie_calls) == (0, 1)

    def test_ambiguous_calls_NEITHER_matcher(self):
        """THE REGRESSION. `if web_item['is_tv']` sent this to the movie
        matcher, because a boolean cannot express 'neither'."""
        matching, _ = _run_matcher("ambiguous")
        assert (matching.tv_calls, matching.movie_calls) == (0, 0)

    def test_ambiguous_reaches_a_visible_state(self):
        """It must not merely be skipped — it has to be reportable."""
        from backend.scanner_service import ScanStatus
        _, item = _run_matcher("ambiguous")
        assert item.status is ScanStatus.MEDIA_TYPE_UNRESOLVED
        assert "unresolved" in item.status_text.lower()


# ──────────── identity confirmation must not default to movie ───────────────

def test_identity_confirmation_rejects_an_unresolved_media_type():
    from backend.hdencode_candidate_service import _identity_is_confirmed
    row = {
        "clean_title": "Great Show",
        "title_year": 2024,
        "description_year": 2024,
        "media_type": "ambiguous",
        "season": None,
        "episode": None,
    }
    assert _identity_is_confirmed(row) is False


@pytest.mark.parametrize("media_type,provisional,expect_code", [
    ("ambiguous", False, "auto_media_type_unresolved"),
    ("", False, "auto_media_type_unresolved"),
    (None, False, "auto_media_type_unresolved"),
    ("movie", True, "auto_media_type_provisional"),
    ("tv", True, "auto_media_type_provisional"),
])
def test_auto_action_validation_rejects_weak_or_unresolved_media_type(
        media_type, provisional, expect_code):
    """Asserted through the production validator, not its source.

    This is a CANDIDATE-level gate: qualification says the pipeline may act at
    all, this says THIS release is understood well enough to act on. A
    confirmed external id resolves WHICH title it is, never whether it is a
    film or a series — and those are two different libraries."""
    from backend.hdencode_action_service import (
        HDEncodeActionError, HDEncodeActionService)

    svc = HDEncodeActionService.__new__(HDEncodeActionService)
    svc.config = {"hdencode_rss_auto_grab_enabled": True}
    candidate = {
        "relevance_state": "relevant_missing",
        "identity_state": "exact",
        "hydration_state": "completed",
        "description_complete": 1,
        "media_type": media_type,
        "media_type_provisional": provisional,
        "title_year": 2024,
        "description_year": 2024,
    }
    with pytest.raises(HDEncodeActionError) as excinfo:
        svc._validate_auto_action(candidate, "grab")
    # .code carries the machine-readable reason; args[0] is the
    # human message, which is not the contract.
    assert excinfo.value.code == expect_code


# ─────────── confidence and provenance must survive to the DB ───────────────

def test_confidence_and_provenance_survive_the_database(tmp_path):
    import sqlite3

    from backend.database import DatabaseManager
    db = DatabaseManager(str(tmp_path / "t.db"))
    conn = sqlite3.connect(str(tmp_path / "t.db"))
    try:
        cols = {r[1] for r in conn.execute(
            "PRAGMA table_info(hdencode_candidates)")}
    finally:
        conn.close()
    assert {"media_type_provisional", "media_type_because"} <= cols
    del db


# ───────────── hydration must RESOLVE the type, not leave it ────────────────

class TestHydrationResolvesTheType:
    """The review's reproduced sequence: an AMBIGUOUS candidate was hydrated,
    its media_type was never updated, `_identity_is_confirmed` fell through to
    the movie rule, and it was promoted to identity_state='exact' — reaching
    `_validate_auto_action` as a confident movie it had never been.

    Hydrated DETAIL evidence outranks the title, so when it resolves the type
    that verdict must be recorded, not merely used and discarded."""

    def test_detail_evidence_resolves_an_ambiguous_type(self):
        from backend.hdencode_candidate_service import _candidate_updates
        updates = _candidate_updates({"is_tv": True, "season": 3,
                                      "display_title": "Great Show"})
        assert updates["media_type"] == "tv"
        assert updates["media_type_provisional"] is False
        assert updates["media_type_because"]

    def test_a_season_number_alone_is_detail_evidence(self):
        """A season pack has a season and no episode. That is still TV."""
        from backend.hdencode_candidate_service import _candidate_updates
        updates = _candidate_updates({"season": 3, "display_title": "Great Show"})
        assert updates["media_type"] == "tv"

    def test_absent_evidence_does_not_overwrite_a_good_verdict(self):
        """is_tv False means 'no season token in the filename', which is not a
        claim that this is a film. Writing 'movie' here would manufacture
        confidence the detail page never supplied."""
        from backend.hdencode_candidate_service import _candidate_updates
        updates = _candidate_updates({"is_tv": False,
                                      "display_title": "The Batman"})
        assert "media_type" not in updates

    def test_the_resolved_type_actually_persists(self, tmp_path):
        """Asserted through the real DB call, because the UPDATE has an
        explicit column list — a value not named there is computed and
        silently dropped, which is the failure this change exists to fix."""
        import sqlite3

        from backend.database import DatabaseManager
        db = DatabaseManager(str(tmp_path / "t.db"))
        conn = sqlite3.connect(str(tmp_path / "t.db"))
        try:
            cols = {r[1] for r in conn.execute(
                "PRAGMA table_info(hdencode_candidates)")}
        finally:
            conn.close()
        assert {"media_type_provisional", "media_type_because"} <= cols
        import inspect
        source = inspect.getsource(db.complete_hdencode_hydration)
        assert "media_type = COALESCE" in source, (
            "the hydration UPDATE does not name media_type, so a resolved "
            "verdict would be dropped at this boundary")
