"""Round 19: the deployed ledger's legacy keys, moved without guessing.

The write path is unified (test_round19_one_arm_identity.py). This is the other
half: 209 rows already exist under two-part keys, written by the running
container. If they are left alone AND new rows arrive in the three-part shape,
the ledger carries both for the same feed -- the same release twice, and a
coverage summary reporting six arms where there are three.

The rule throughout: move a row only when the registry KNOWS where it belongs.
An unresolvable key is logged and left, because a legacy row is a true record of
a sighting and must not acquire a precision it never had.
"""
import logging
import sqlite3

import pytest

from backend.arms import ArmRegistry, ArmSpec, KNOWN_ARMS, default_registry
from backend.database import DatabaseManager

#: Read from the deployed container 2026-08-21.
LIVE = {"hdencode:tv": 78, "hdencode:4k": 69, "hdencode:remux": 62}


@pytest.fixture
def db(tmp_path):
    dm = DatabaseManager(str(tmp_path / "r19mig.db"))
    yield dm
    dm.close()


def _seed(dm, rows):
    """rows: (canonical_url, arm_key, listing_type[, sightings])"""
    with sqlite3.connect(dm.db_path) as conn:
        for r in rows:
            url, key, ltype = r[0], r[1], r[2]
            n = r[3] if len(r) > 3 else 1
            conn.execute(
                "INSERT OR REPLACE INTO listing_claims "
                "(canonical_url, arm_key, listing_type, raw_url, "
                " posted_date_raw, posted_date_changed, first_seen_at, "
                " last_seen_at, sightings) VALUES (?,?,?,?,?,?,?,?,?)",
                (url, key, ltype, url + "?raw", "August 19, 2026 at 9:00 PM",
                 0, "2026-08-01T00:00:00", "2026-08-20T00:00:00", n))
        conn.commit()


def _keys(dm):
    with sqlite3.connect(dm.db_path) as conn:
        return sorted(r[0] for r in conn.execute(
            "SELECT arm_key FROM listing_claims"))


class TestTheLiveLedgerShape:

    def test_all_three_live_keys_move(self, db):
        _seed(db, [("https://x.test/a/", "hdencode:4k", "movie"),
                   ("https://x.test/b/", "hdencode:remux", "movie"),
                   ("https://x.test/c/", "hdencode:tv", "tv")])
        out = db.migrate_listing_claim_arm_keys(default_registry())
        assert out["claims_moved"] == 3
        assert out["claims_merged"] == 0
        assert out["skipped"] == []
        assert _keys(db) == ["hdencode:4k:2160p", "hdencode:remux:remux",
                             "hdencode:tv:tv-packs"]

    def test_running_it_twice_changes_nothing(self, db):
        _seed(db, [("https://x.test/a/", "hdencode:4k", "movie")])
        db.migrate_listing_claim_arm_keys(default_registry())
        before = _keys(db)
        out = db.migrate_listing_claim_arm_keys(default_registry())
        assert out["claims_moved"] == 0
        assert _keys(db) == before

    def test_an_empty_ledger_is_a_no_op(self, db):
        out = db.migrate_listing_claim_arm_keys(default_registry())
        assert out == {"claims_moved": 0, "claims_merged": 0,
                       "aliases_moved": 0, "skipped": []}

    def test_no_row_is_lost(self, db):
        _seed(db, [("https://x.test/%d/" % i, "hdencode:4k", "movie")
                   for i in range(20)])
        db.migrate_listing_claim_arm_keys(default_registry())
        with sqlite3.connect(db.db_path) as conn:
            n = conn.execute("SELECT COUNT(*) FROM listing_claims").fetchone()[0]
        assert n == 20


class TestAmbiguityIsNeverResolvedByGuessing:

    def test_the_ddlbase_remux_rows_stay_where_they_are(self, db):
        _seed(db, [("https://d.test/a/", "ddlbase:remux", "movie")])
        out = db.migrate_listing_claim_arm_keys(default_registry())
        assert out["claims_moved"] == 0
        assert out["skipped"] == ["ddlbase:remux"]
        assert _keys(db) == ["ddlbase:remux"]

    def test_it_is_logged_not_silently_skipped(self, db, caplog):
        _seed(db, [("https://d.test/a/", "ddlbase:remux", "movie")])
        with caplog.at_level(logging.WARNING):
            db.migrate_listing_claim_arm_keys(default_registry())
        assert any("ddlbase:remux" in r.getMessage()
                   for r in caplog.records), (
            "an unmigrated key left no trace, which is indistinguishable from "
            "a migration that had nothing to do")

    def test_a_feed_that_no_longer_exists_stays(self, db):
        _seed(db, [("https://o.test/a/", "oldsite:4k", "movie")])
        out = db.migrate_listing_claim_arm_keys(default_registry())
        assert _keys(db) == ["oldsite:4k"]
        assert out["skipped"] == ["oldsite:4k"]

    def test_the_knowable_rows_still_move_alongside(self, db):
        """One unresolvable key must not block the rest."""
        _seed(db, [("https://d.test/a/", "ddlbase:remux", "movie"),
                   ("https://x.test/b/", "hdencode:4k", "movie")])
        out = db.migrate_listing_claim_arm_keys(default_registry())
        assert out["claims_moved"] == 1
        assert _keys(db) == ["ddlbase:remux", "hdencode:4k:2160p"]

    def test_a_partial_registry_cannot_be_used_to_resolve_it(self):
        """The trap this design exists to avoid. With only the 2160p feed
        registered, `ddlbase:remux` looks unambiguous -- and resolving it would
        attribute rows that may have come from the 1080p feed."""
        partial = ArmRegistry([ArmSpec("ddlbase", "remux",
                                       "movie-remux-2160p", "movie")])
        assert partial.resolve_legacy("ddlbase:remux") == \
            "ddlbase:remux:movie-remux-2160p"
        assert default_registry().resolve_legacy("ddlbase:remux") is None

    def test_the_migration_refuses_to_run_without_a_registry(self, db):
        with pytest.raises(ValueError):
            db.migrate_listing_claim_arm_keys(None)


class TestBothShapesPresentAreMergedNotClobbered:
    """Reachable after a deploy, a rollback and a redeploy."""

    URL = "https://x.test/shared/"

    def test_the_two_rows_become_one(self, db):
        _seed(db, [(self.URL, "hdencode:4k", "movie", 5),
                   (self.URL, "hdencode:4k:2160p", "movie", 3)])
        out = db.migrate_listing_claim_arm_keys(default_registry())
        assert out["claims_merged"] == 1
        assert _keys(db) == ["hdencode:4k:2160p"]

    def test_the_sightings_are_summed_not_replaced(self, db):
        _seed(db, [(self.URL, "hdencode:4k", "movie", 5),
                   (self.URL, "hdencode:4k:2160p", "movie", 3)])
        db.migrate_listing_claim_arm_keys(default_registry())
        with sqlite3.connect(db.db_path) as conn:
            n = conn.execute(
                "SELECT sightings FROM listing_claims").fetchone()[0]
        assert n == 8, (
            "one row's sightings were discarded; the two rows describe the "
            "same feed seeing the same release, so the union is the truth")

    def test_the_earliest_first_seen_survives(self, db):
        with sqlite3.connect(db.db_path) as conn:
            conn.execute(
                "INSERT INTO listing_claims (canonical_url, arm_key, "
                "listing_type, raw_url, posted_date_raw, posted_date_changed, "
                "first_seen_at, last_seen_at, sightings) VALUES "
                "(?,?,?,?,?,?,?,?,?)",
                (self.URL, "hdencode:4k", "movie", None, None, 0,
                 "2026-07-01T00:00:00", "2026-08-01T00:00:00", 1))
            conn.execute(
                "INSERT INTO listing_claims (canonical_url, arm_key, "
                "listing_type, raw_url, posted_date_raw, posted_date_changed, "
                "first_seen_at, last_seen_at, sightings) VALUES "
                "(?,?,?,?,?,?,?,?,?)",
                (self.URL, "hdencode:4k:2160p", "movie", None, None, 0,
                 "2026-08-10T00:00:00", "2026-08-20T00:00:00", 1))
            conn.commit()
        db.migrate_listing_claim_arm_keys(default_registry())
        with sqlite3.connect(db.db_path) as conn:
            first, last = conn.execute(
                "SELECT first_seen_at, last_seen_at FROM listing_claims"
            ).fetchone()
        assert first == "2026-07-01T00:00:00"
        assert last == "2026-08-20T00:00:00"

    def test_a_date_change_seen_by_either_row_survives(self, db):
        with sqlite3.connect(db.db_path) as conn:
            for key, changed in (("hdencode:4k", 1), ("hdencode:4k:2160p", 0)):
                conn.execute(
                    "INSERT INTO listing_claims (canonical_url, arm_key, "
                    "listing_type, raw_url, posted_date_raw, "
                    "posted_date_changed, first_seen_at, last_seen_at, "
                    "sightings) VALUES (?,?,?,?,?,?,?,?,?)",
                    (self.URL, key, "movie", None, None, changed,
                     "2026-08-01T00:00:00", "2026-08-20T00:00:00", 1))
            conn.commit()
        db.migrate_listing_claim_arm_keys(default_registry())
        with sqlite3.connect(db.db_path) as conn:
            changed = conn.execute(
                "SELECT posted_date_changed FROM listing_claims").fetchone()[0]
        assert changed == 1, (
            "an unstable posted_date observed by one row was erased by the "
            "merge; that flag disqualifies a release from anchoring a frontier")


class TestAliasHistoryMovesWithTheClaim:

    def test_aliases_are_rekeyed(self, db):
        url = "https://x.test/a/"
        _seed(db, [(url, "hdencode:4k", "movie")])
        with sqlite3.connect(db.db_path) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO listing_claim_aliases "
                "(canonical_url, arm_key, raw_url, first_seen_at, "
                " last_seen_at, sightings) VALUES (?,?,?,?,?,?)",
                (url, "hdencode:4k", url + "?utm_source=rss",
                 "2026-08-01T00:00:00", "2026-08-20T00:00:00", 1))
            conn.commit()
        db.migrate_listing_claim_arm_keys(default_registry())
        with sqlite3.connect(db.db_path) as conn:
            keys = [r[0] for r in conn.execute(
                "SELECT arm_key FROM listing_claim_aliases")]
        assert keys == ["hdencode:4k:2160p"], (
            "the raw-href history was stranded under the old key; revocation "
            "enumerates aliases, and a variant it cannot find is a download "
            "row that keeps its media kind after the release is contradicted")

    def test_an_unresolvable_arms_aliases_stay_put(self, db):
        url = "https://d.test/a/"
        _seed(db, [(url, "ddlbase:remux", "movie")])
        with sqlite3.connect(db.db_path) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO listing_claim_aliases "
                "(canonical_url, arm_key, raw_url, first_seen_at, "
                " last_seen_at, sightings) VALUES (?,?,?,?,?,?)",
                (url, "ddlbase:remux", url + "?x=1",
                 "2026-08-01T00:00:00", "2026-08-20T00:00:00", 1))
            conn.commit()
        db.migrate_listing_claim_arm_keys(default_registry())
        with sqlite3.connect(db.db_path) as conn:
            keys = [r[0] for r in conn.execute(
                "SELECT arm_key FROM listing_claim_aliases")]
        assert keys == ["ddlbase:remux"]


class TestTheStaticTableMatchesWhatTheProducerEmits:
    """KNOWN_ARMS is hand-written. A feed added to `_build_sources` and not
    here becomes unmigratable, silently -- so assert the two agree."""

    ALL_FLAGS = {"4k": True, "1080p": True, "remux": True, "tv": True,
                 "4k_webdl": True, "4k_remux": True, "1080p_remux": True}

    def _emitted(self):
        from unittest.mock import MagicMock
        from backend.arms import SEARCH_CATEGORY, spec_from_descriptor
        from backend.scanner_service import ScannerService

        svc = ScannerService.__new__(ScannerService)
        svc.config = {"hdencode_enabled": True, "ddlbase_enabled": True,
                      "adithd_enabled": True}
        svc._log = MagicMock()
        out = []
        for stype in ("HDEncode", "DDLBase", "Adit-HD"):
            for d in svc._build_sources(
                    "Incremental", stype, "https://hdencode.org",
                    self.ALL_FLAGS, ""):
                spec = spec_from_descriptor(d)
                if spec.category != SEARCH_CATEGORY:
                    out.append(spec)
        return out

    def test_every_emitted_feed_is_in_the_table(self):
        emitted = self._emitted()
        assert emitted, "the producer emitted nothing; this test proves nothing"
        known = {s.arm_key for s in KNOWN_ARMS}
        missing = sorted({s.arm_key for s in emitted} - known)
        assert not missing, (
            "feeds the crawler can produce are absent from KNOWN_ARMS, so "
            "their legacy rows can never be migrated: %s" % missing)

    def test_the_table_has_no_feeds_the_producer_cannot_emit(self):
        emitted = {s.arm_key for s in self._emitted()}
        extra = sorted({s.arm_key for s in KNOWN_ARMS} - emitted)
        assert not extra, (
            "KNOWN_ARMS names feeds nothing produces, which would let an "
            "ambiguous legacy key look resolvable: %s" % extra)

    def test_the_table_itself_builds_a_registry(self):
        assert len(default_registry()) == len(KNOWN_ARMS)
