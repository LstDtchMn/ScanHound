"""Feed-owned fields on a COMPLETED row must heal on a grammar change (R-4).

Round-15 blocker. Authority is per ROW, not per field. ``_candidate_updates``
omits any field the detail payload did not carry, and the hydration sink
COALESCEs — so a completed row is a MIXTURE: the fields detail supplied are
detail-authoritative, and every other protected field is still whatever the
feed grammar derived.

The previous repair re-derived exactly one hardcoded field (``title_year``)
and then stamped ``feed_parse_version`` current for the whole row, which
certified far more than had actually been re-derived:

    1. old feed grammar derives resolution/size
    2. detail hydration completes but the detail page LACKS them
    3. COALESCE preserves the feed values
    4. the grammar changes
    5. the repair updates only title_year, stamps the feed leg current
    6. the row reads 'current' carrying an old-grammar fact, forever

The existing rich-detail fixture cannot expose this, because nearly every
field in it becomes detail-owned. These tests use a deliberately SPARSE
detail payload — the shape that actually occurs when a detail page omits
technical fields — so the feed-owned fallbacks are the majority.
"""
import json
import sqlite3

import pytest

from backend.database import DatabaseManager
from backend.sources.hdencode_feed_parser import parse_feed

URL = "https://hdencode.org/movie-2026-2160p-web-dl/"


def _body(title, guid_suffix=""):
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>HDEncode</title>
<item><title>{title}</title>
  <link>{URL}</link>
  <guid>{URL}{guid_suffix}</guid>
  <pubDate>Fri, 01 Aug 2026 12:00:00 +0000</pubDate>
  <category>Movies</category><description>x</description></item>
</channel></rss>""".encode()


@pytest.fixture
def db(tmp_path):
    return DatabaseManager(str(tmp_path / "authority.db"))


def _ingest(db, body, sha):
    parsed = parse_feed(body, "movies_all")
    db.ingest_hdencode_feed(
        feed_key="movies_all", feed_url="https://hdencode.org/feed/",
        last_modified=None, http_status=200, body_sha256=sha,
        channel_last_build_date=None,
        entries=[e.as_database_row() for e in parsed.entries],
        started_at="2026-08-01T12:00:00+00:00",
        completed_at="2026-08-01T12:00:05+00:00")


def _row(db):
    conn = sqlite3.connect(db.db_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT * FROM hdencode_candidates WHERE canonical_url = ?",
        (URL,)).fetchone()
    conn.close()
    return dict(row)


def _set_feed_stamp(db, value):
    """Age the feed stamp so the repair pass considers the row."""
    with db.transaction() as conn:
        conn.execute(
            "UPDATE hdencode_candidates SET feed_parse_version = ? "
            "WHERE canonical_url = ?", (value, URL))


#: A detail page that resolved only IDENTITY. This is the realistic sparse
#: case: no resolution, no size, no HDR/DV/codec evidence, no media type.
SPARSE_DETAIL = {
    "clean_title": "Movie",
    "description_year": 2026,
}


def _hydrate_sparse(db):
    db.complete_hdencode_hydration(
        URL, payload={"url": URL}, candidate_updates=dict(SPARSE_DETAIL))


# ── the blocker ──────────────────────────────────────────────────────

def test_feed_owned_fields_heal_when_the_grammar_changes(db):
    """The defect itself: fields detail never supplied must be re-derived."""
    _ingest(db, _body("Movie 2026 2160p WEB-DL - 50.0 GB"), "sha-v1")
    before = _row(db)
    assert before["resolution"], "fixture: the feed must have derived one"
    _hydrate_sparse(db)

    row = _row(db)
    assert row["hydration_state"] == "completed"
    assert row["clean_title"] == "Movie", "detail supplied this"
    assert row["resolution"] == before["resolution"], (
        "COALESCE preserved the FEED value, because sparse detail omitted it "
        "-- this is the mixed-authority row the repair must handle")

    # The feed poll now carries different facts, and the grammar moved on.
    _ingest(db, _body("Movie 2026 1080p WEB-DL - 4.0 GB", "?v=2"), "sha-v2")
    _set_feed_stamp(db, "grammar-from-last-year")

    healed = db._reparse_completed_feed_only()
    assert healed == 1

    after = _row(db)
    assert after["resolution"] != before["resolution"], (
        "a feed-owned field stayed frozen at the OLD grammar's parse while "
        "the row was stamped current -- the stale-forever defect")
    assert after["clean_title"] == "Movie", (
        "a DETAIL-owned field must not be reverted by the feed repair")


def test_detail_owned_fields_are_not_touched_by_the_repair(db):
    """The other half. A repair that healed everything would pass the test
    above and destroy exactly what the authority model protects."""
    _ingest(db, _body("Movie 2026 2160p WEB-DL - 50.0 GB"), "sha-v1")
    rich = {
        "clean_title": "Detail Title",
        "resolution": "2160P",
        "size_text": "50.0 GB",
        "size_gb": 50.0,
        "hdr_evidence": "asserted",
        "hdr_formats": ["HDR10"],
    }
    db.complete_hdencode_hydration(
        URL, payload={"url": URL}, candidate_updates=dict(rich))

    _ingest(db, _body("Movie 2026 1080p WEB-DL - 4.0 GB", "?v=2"), "sha-v2")
    _set_feed_stamp(db, "grammar-from-last-year")
    db._reparse_completed_feed_only()

    row = _row(db)
    assert row["clean_title"] == "Detail Title"
    assert row["resolution"] == "2160P"
    assert row["size_gb"] == 50.0
    assert row["hdr_evidence"] == "asserted"


def test_coupled_fields_move_together_or_not_at_all(db):
    """size_text and size_gb must never disagree.

    Detail supplied size_text only. Re-deriving size_gb from a new grammar
    while size_text stays at the detail value produces a row whose two size
    columns contradict each other — worse than either being stale.
    """
    _ingest(db, _body("Movie 2026 2160p WEB-DL - 50.0 GB"), "sha-v1")
    db.complete_hdencode_hydration(
        URL, payload={"url": URL},
        candidate_updates={"clean_title": "Movie", "size_text": "50.0 GB"})

    _ingest(db, _body("Movie 2026 2160p WEB-DL - 4.0 GB", "?v=2"), "sha-v2")
    _set_feed_stamp(db, "grammar-from-last-year")
    db._reparse_completed_feed_only()

    row = _row(db)
    assert row["size_text"] == "50.0 GB", "detail claimed this member"
    assert row["size_gb"] == 50.0, (
        f"size_gb was re-derived to {row['size_gb']} while size_text stayed "
        "at the detail value -- the two columns now contradict each other")


def test_a_row_owning_nothing_feed_side_is_not_stamped_current(db):
    """If detail owns every protected field, nothing was re-derived — so the
    feed stamp must NOT advance. Stamping it would certify work not done."""
    _ingest(db, _body("Movie 2026 2160p WEB-DL - 50.0 GB"), "sha-v1")
    everything = {f: None for f in DatabaseManager._PROTECTED_FIELDS}
    everything.update({
        "clean_title": "T", "title_year": 2026, "description_year": 2026,
        "season": 1, "episode": 2, "episode_end": 3, "resolution": "2160P",
        "size_text": "50.0 GB", "size_gb": 50.0, "dv_evidence": "asserted",
        "hdr_evidence": "asserted", "hevc_evidence": "asserted",
        "hdr_formats": ["HDR10"], "media_type": "movie",
        "media_type_provisional": False, "media_type_because": ["detail"],
    })
    db.complete_hdencode_hydration(
        URL, payload={"url": URL}, candidate_updates=everything)

    _set_feed_stamp(db, "grammar-from-last-year")
    healed = db._reparse_completed_feed_only()

    assert healed == 0
    assert _row(db)["feed_parse_version"] == "grammar-from-last-year", (
        "the stamp advanced without a single feed fact being re-derived")


# ── the guards on the authority set itself ───────────────────────────

def test_an_unknown_claim_set_is_not_treated_as_an_empty_one(db):
    """A row written before the column existed has claimed_json = NULL.

    Reading NULL as "detail claimed nothing" would let the repair overwrite
    detail facts it cannot see — turning a missing record into data loss.
    """
    assert db._feed_owned_fields(None) == ()
    assert db._feed_owned_fields("{not json") == ()
    assert db._feed_owned_fields("[]") == DatabaseManager._PROTECTED_FIELDS


def test_claiming_one_member_of_a_group_claims_the_whole_group(db):
    owned = db._feed_owned_fields(json.dumps(["hdr_evidence"]))
    assert "hdr_evidence" not in owned
    assert "hdr_formats" not in owned, (
        "a new-grammar format list beside an old-grammar HDR verdict")
    # ...and an unrelated field is still feed-owned.
    assert "resolution" in owned


def test_the_sink_records_exactly_what_detail_supplied(db):
    _ingest(db, _body("Movie 2026 2160p WEB-DL - 50.0 GB"), "sha-v1")
    _hydrate_sparse(db)

    claimed = json.loads(_row(db)["detail_authority_fields"])
    assert claimed == ["clean_title", "description_year"], (
        "the claim set must reflect THIS payload, not the fields the adapter "
        "is capable of emitting on some other row")


def test_a_sparser_refetch_does_not_hand_a_detail_fact_back_to_the_feed(db):
    """The claim set must be CUMULATIVE, because the sink COALESCEs.

    Detail supplies resolution, then a later refetch omits it. COALESCE keeps
    the earlier DETAIL value, so the stored resolution is still
    detail-derived — but a per-payload claim set would mark it feed-owned and
    let the repair overwrite it with a lower-authority feed value. That is the
    downgrade the whole authority model exists to prevent.

    Verified against the real sink before the fix: rich hydration then sparse
    refetch left resolution='2160P' with 'resolution' absent from the claim
    set.
    """
    _ingest(db, _body("Movie 2026 2160p WEB-DL - 50.0 GB"), "sha-v1")
    db.complete_hdencode_hydration(
        URL, payload={"url": URL},
        candidate_updates={"clean_title": "T", "resolution": "2160P"})
    assert "resolution" in json.loads(_row(db)["detail_authority_fields"])

    # The refetch omits resolution entirely.
    db.complete_hdencode_hydration(
        URL, payload={"url": URL}, candidate_updates={"clean_title": "T"})

    row = _row(db)
    assert row["resolution"] == "2160P", "COALESCE kept the detail value"
    assert "resolution" in json.loads(row["detail_authority_fields"]), (
        "the claim set forgot that detail owns this column, so the feed "
        "repair would overwrite a detail fact with a feed one")

    # ...and the repair must indeed leave it alone.
    _ingest(db, _body("Movie 2026 1080p WEB-DL - 4.0 GB", "?v=2"), "sha-v2")
    _set_feed_stamp(db, "grammar-from-last-year")
    db._reparse_completed_feed_only()
    assert _row(db)["resolution"] == "2160P"


class TestBackfillForRowsPredatingTheColumn:
    """Rows hydrated before detail_authority_fields existed carry NULL, and a
    NULL claim set repairs nothing. Correct, but on the live database that is
    2466 of 3431 rows stranded until each happens to re-hydrate.

    The stored hydration payload makes reconstruction exact rather than
    approximate: re-running the same _candidate_updates over it reproduces
    which fields that row's detail actually supplied.
    """

    def _completed_row_with_no_claim_set(self, db, updates):
        _ingest(db, _body("Movie 2026 2160p WEB-DL - 50.0 GB"), "sha-v1")
        db.complete_hdencode_hydration(
            URL, payload={"url": URL, "display_title": "Movie", "res": "2160P"},
            candidate_updates=dict(updates))
        # Simulate a row written before the column existed.
        with db.transaction() as conn:
            conn.execute("UPDATE hdencode_candidates "
                         "SET detail_authority_fields = NULL "
                         "WHERE canonical_url = ?", (URL,))
        assert _row(db)["detail_authority_fields"] is None

    def test_the_claim_set_is_reconstructed_from_the_stored_payload(self, db):
        self._completed_row_with_no_claim_set(
            db, {"clean_title": "Movie", "resolution": "2160P"})

        assert db._backfill_detail_authority_fields() == 1

        claimed = json.loads(_row(db)["detail_authority_fields"])
        assert "clean_title" in claimed and "resolution" in claimed, claimed

    def test_a_backfilled_row_then_heals_its_feed_owned_fields(self, db):
        """End to end: the point of the backfill is that repair becomes
        possible, not merely that a column gets populated."""
        self._completed_row_with_no_claim_set(db, {"clean_title": "Movie"})
        db._backfill_detail_authority_fields()

        _ingest(db, _body("Movie 2026 1080p WEB-DL - 4.0 GB", "?v=2"), "sha-v2")
        _set_feed_stamp(db, "grammar-from-last-year")
        assert db._reparse_completed_feed_only() == 1

        assert _row(db)["clean_title"] == "Movie", "detail still owns this"

    def test_it_is_idempotent_and_leaves_existing_claim_sets_alone(self, db):
        """POSITIVE CONTROL. A backfill that overwrote real claim sets would
        pass the tests above while destroying the authority record."""
        _ingest(db, _body("Movie 2026 2160p WEB-DL - 50.0 GB"), "sha-v1")
        db.complete_hdencode_hydration(
            URL, payload={"url": URL},
            candidate_updates={"clean_title": "Movie", "resolution": "2160P"})
        before = _row(db)["detail_authority_fields"]

        assert db._backfill_detail_authority_fields() == 0, "nothing to do"
        assert _row(db)["detail_authority_fields"] == before

    def test_an_undecodable_payload_is_left_unknown_not_invented(self, db):
        """A fabricated claim set is worse than no claim set: it would let the
        repair act on an authority record that was guessed."""
        self._completed_row_with_no_claim_set(db, {"clean_title": "Movie"})
        with db.transaction() as conn:
            conn.execute("UPDATE hdencode_candidate_details SET payload = ? "
                         "WHERE canonical_url = ?", ("{not json", URL))

        assert db._backfill_detail_authority_fields() == 0
        assert _row(db)["detail_authority_fields"] is None
