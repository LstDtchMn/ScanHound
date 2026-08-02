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

from backend.promotion_gate import ArtifactBindings, evaluate_promotion

BINDINGS = ArtifactBindings(
    acquisition_artifact_digest="sha256:acq",
    decision_bridge_digest="sha256:bridge",
    phase_a_corpus_digest="sha256:corpus",
    configuration_fingerprint="cfg-1",
    parser_version="grammar-1",
    equivalence_contract_version="contract-1",
)


def _passing_phase_b(**overrides):
    verdict = {
        "phase_b_status": "pass",
        "material_mismatch_count": 0,
        "inconclusive_count": 0,
        **BINDINGS._asdict(),
    }
    verdict.update(overrides)
    return verdict


# ─────────── the matcher selector must be tri-state, not boolean ────────────

@pytest.mark.xfail(strict=True, reason=(
    "_match_against_plex selects with `if web_item['is_tv']`, so AMBIGUOUS "
    "takes the else branch and calls find_movie_matches. The preserved "
    "media_type is never consulted at the decision point — the seam carries "
    "it and the consumer ignores it."))
def test_the_matcher_selector_consumes_media_type_not_is_tv():
    """The selector must read the tri-state directly.

    `is_tv` is an unsafe decision input once AMBIGUOUS is a legal value,
    because a boolean cannot express 'neither'."""
    import inspect

    from backend import scanner_service
    source = inspect.getsource(scanner_service.ScannerService._match_against_plex)
    assert "web_item['is_tv']" not in source, (
        "the matcher branch still routes on the lossy boolean")


@pytest.mark.xfail(strict=True, reason=(
    "There is no unresolved terminal state. An ambiguous item silently takes "
    "the movie matcher instead of entering a visible manual-review state with "
    "a stop condition."))
def test_an_unresolved_item_reaches_a_visible_state():
    from backend.scanner_service import ScanStatus
    assert any("UNRESOLVED" in name or "REVIEW" in name
               for name in ScanStatus.__members__)


# ──────────── identity confirmation must not default to movie ───────────────

@pytest.mark.xfail(strict=True, reason=(
    "_identity_is_confirmed has an explicit TV branch and then a bare movie "
    "fallthrough, so an AMBIGUOUS row with a clean title and year confirms as "
    "a movie, is promoted to identity_state='exact', and passes "
    "_validate_auto_action. Ambiguous is never movie-by-default."))
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


@pytest.mark.xfail(strict=True, reason=(
    "_validate_auto_action checks neither an unresolved media type nor the "
    "confidence of a resolved one. Programme-level qualification and "
    "candidate-level suitability are different gates; the combined promotion "
    "gate does not substitute for this one."))
def test_auto_action_validation_rejects_an_unresolved_media_type():
    import inspect

    from backend import hdencode_action_service
    source = inspect.getsource(
        hdencode_action_service.HDEncodeActionService._validate_auto_action)
    assert "media_type" in source


# ─────────── confidence and provenance must survive to the DB ───────────────

@pytest.mark.xfail(strict=True, reason=(
    "hdencode_candidates has no media_type_provisional or media_type_because "
    "columns and ingest_hdencode_feed writes neither, so the claim that weak "
    "route-only evidence stays distinguishable to a downstream decision is "
    "false at the persistence boundary. They live only on the parser object."))
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


# ─────────────── the promotion gate must reject malformed input ─────────────

@pytest.mark.xfail(strict=True, reason=(
    "evaluate_promotion coerces permissively: `not acquisition.get(...)` "
    "accepts the string 'false'; int(0.9) truncates to 0; False is an int and "
    "passes as a zero count. It returns allowed=True for deliberately "
    "malformed evidence. 41 tests covered ABSENCE and NONZERO, never MALFORMED."))
def test_the_gate_rejects_malformed_evidence():
    decision = evaluate_promotion(
        {"acquisition_ready": "false"},
        _passing_phase_b(material_mismatch_count=0.9, inconclusive_count=False),
        current_bindings=BINDINGS)
    assert decision.allowed is False


# Only TRUTHY non-True values are defects. [] and {} are falsy and the gate
# already blocks them, so including them would assert a passing case as a
# failure and make this file self-contradictory.
@pytest.mark.parametrize("ready", ["false", "0", 1, "yes"])
@pytest.mark.xfail(strict=True, reason=(
    "acquisition_ready must be the literal True, not merely truthy."))
def test_acquisition_ready_must_be_exactly_true(ready):
    decision = evaluate_promotion({"acquisition_ready": ready},
                                  _passing_phase_b(), current_bindings=BINDINGS)
    assert decision.allowed is False


# True is int 1, so the gate already blocks it. Only values that coerce to
# a clean zero are defects.
@pytest.mark.parametrize("count", [0.9, 0.4, False])
@pytest.mark.xfail(strict=True, reason=(
    "counts must be non-Boolean integers equal to zero. int() truncation and "
    "bool-is-int both let a non-zero or non-integer count read as clean."))
def test_counts_must_be_real_integers(count):
    decision = evaluate_promotion(
        {"acquisition_ready": True},
        _passing_phase_b(material_mismatch_count=count),
        current_bindings=BINDINGS)
    assert decision.allowed is False
