"""Round 20, M19-4: a claim is per RELEASE per arm, not per raw href.

The per-crawl claim key was `(RAW url, listing_type, arm_key)`. Two cosmetic
variants of one release in one arm therefore produced TWO claim entries;
`record_listing_claims()` then canonicalised both and incremented the same
aggregate row twice.

Nothing was authorised by that and the alias table correctly kept both hrefs,
which is why the reviewer graded it LOW. What it corrupted is `sightings` --
the durable count of how many times an arm observed a release. Persistence and
the required-arm policy are both going to read that column, so it has to mean
what it says before either is built.

Driven through the REAL crawler and the REAL DatabaseManager. A test that
constructed claim dicts by hand would pass whether or not the producer emits
duplicates, which is the exact gap that let this survive round 19.
"""
import sqlite3

import pytest

from tests.test_round16_traversal_emission import (_Scraper, _crawl, _listing,
                                                   _source)

BASE = "https://hdencode.example/one-release-2026-2160p/"
#: The same release, as the listing renders it twice on one page. Cosmetic
#: query variants are real on this source -- that is why an alias table exists.
VARIANTS = [BASE, BASE.rstrip("/") + "/?utm_source=rss"]
OTHER = "https://hdencode.example/a-different-release-2026-2160p/"


def _crawl_variants(monkeypatch, entries):
    return _crawl([_source("4K Movies", "movie", "4k")],
                  _Scraper([_listing(entries)]), monkeypatch)


class TestOneReleaseUnderTwoHrefsIsOneClaim:

    def test_the_producer_emits_exactly_one_claim(self, monkeypatch):
        shell = _crawl_variants(
            monkeypatch, [(v, "One Release 2026") for v in VARIANTS])
        claims = shell._last_crawl_listing_claims
        assert len(claims) == 1, (
            "one release seen twice in one arm produced %d claim entries; the "
            "durable sightings counter will be inflated by the difference"
            % len(claims))

    def test_the_traversal_still_records_BOTH_sightings(self, monkeypatch):
        """The claim collapses; the OBSERVATION must not. Coverage reasons
        about listing order and needs every position that was read."""
        shell = _crawl_variants(
            monkeypatch, [(v, "One Release 2026") for v in VARIANTS])
        sights = shell._last_crawl_traversal.arms[0].pages[0].sightings
        assert len(sights) == 2
        assert [s.raw_url for s in sights] == VARIANTS
        assert len({s.canonical_url for s in sights}) == 1

    def test_the_second_sighting_is_flagged_a_duplicate(self, monkeypatch):
        """Unchanged from round 17, asserted here so this fix cannot silently
        undo it: the repeat proves no new depth and must not anchor."""
        shell = _crawl_variants(
            monkeypatch, [(v, "One Release 2026") for v in VARIANTS])
        sights = shell._last_crawl_traversal.arms[0].pages[0].sightings
        assert sights[0].duplicate_in_run is False
        assert sights[1].duplicate_in_run is True

    def test_two_DIFFERENT_releases_still_produce_two_claims(self, monkeypatch):
        """The positive control. Collapsing by canonical URL must not collapse
        distinct releases -- an over-eager key would silently lose claims,
        which is worse than the defect being fixed."""
        shell = _crawl_variants(
            monkeypatch, [(BASE, "One Release 2026"), (OTHER, "A Different One 2026")])
        assert len(shell._last_crawl_listing_claims) == 2

    def test_the_claim_carries_the_arm_key(self, monkeypatch):
        shell = _crawl_variants(monkeypatch, [(BASE, "One Release 2026")])
        # Round 20: the opaque declared id, not the round-19 parsed triple.
        assert (shell._last_crawl_listing_claims[0]["arm_key"]
                == "arm.hdencode.4k-2160p")


class TestTheDurableCounterMatchesReality:
    """The consumer. The producer emitting one claim is only half the fix --
    what matters is the number that lands in the column."""

    @pytest.fixture
    def db(self, tmp_path):
        from backend.database import DatabaseManager
        dm = DatabaseManager(str(tmp_path / "r20.db"))
        yield dm
        dm.close()

    def test_one_release_seen_twice_counts_as_ONE_sighting(self, db, monkeypatch):
        shell = _crawl_variants(
            monkeypatch, [(v, "One Release 2026") for v in VARIANTS])
        db.record_listing_claims(shell._last_crawl_listing_claims)
        with sqlite3.connect(db.db_path) as conn:
            rows = conn.execute(
                "SELECT canonical_url, arm_id, sightings FROM listing_claims"
            ).fetchall()
        assert len(rows) == 1, "expected one claim row, got %d" % len(rows)
        assert rows[0][2] == 1, (
            "the durable sightings counter says %d for one release observed "
            "once per arm in one crawl" % rows[0][2])

    def test_BOTH_raw_variants_survive_as_aliases(self, db, monkeypatch):
        """Collapsing the claim must not lose identity history. Revocation
        enumerates aliases, and a variant it cannot find is a download row that
        keeps its media kind after the release has been contradicted."""
        shell = _crawl_variants(
            monkeypatch, [(v, "One Release 2026") for v in VARIANTS])
        db.record_listing_claims(shell._last_crawl_listing_claims)
        with sqlite3.connect(db.db_path) as conn:
            aliases = sorted(r[0] for r in conn.execute(
                "SELECT raw_url FROM listing_claim_aliases"))
        # BOTH, as the name says. Round 21 (R21-10a).
        #
        # This previously asserted len(aliases) >= 1 and explained in a comment
        # that only one variant could reach the alias table. That was a test
        # whose NAME claimed one contract while its body accepted a strictly
        # weaker one -- which is how the M19-4 alias obligation was lost
        # silently when the round-19 suite was retired.
        #
        # The weaker behaviour was a real safety regression, not an untidy
        # test: consume_cross_crawl_conflicts() revokes by enumerating
        # listing_claim_aliases, so a variant missing here is a download row
        # that keeps its media kind after the release has been contradicted.
        assert aliases == sorted(VARIANTS), (
            "expected every cosmetic variant to survive as a durable alias, "
            "got %s" % aliases)

    def test_the_aggregate_claim_is_still_deduped(self, db, monkeypatch):
        """The other half of the obligation, and the anti-vacuity control for
        the test above: keeping every alias must NOT reintroduce a second claim
        row or a double sightings increment."""
        shell = _crawl_variants(
            monkeypatch, [(v, "One Release 2026") for v in VARIANTS])
        db.record_listing_claims(shell._last_crawl_listing_claims)
        with sqlite3.connect(db.db_path) as conn:
            rows = conn.execute(
                "SELECT sightings FROM listing_claims").fetchall()
        assert len(rows) == 1, "the alias fix split one release into %d claims" % len(rows)
        assert rows[0][0] == 1, "sightings double-counted: %d" % rows[0][0]

    def test_every_alias_is_reachable_from_the_revocation_query(
            self, db, monkeypatch):
        """The CONSUMER, not the column.

        Storing both aliases is only half of it; what matters is that the query
        revocation actually uses returns both. This is the exact enumeration
        consume_cross_crawl_conflicts() performs.
        """
        shell = _crawl_variants(
            monkeypatch, [(v, "One Release 2026") for v in VARIANTS])
        db.record_listing_claims(shell._last_crawl_listing_claims)
        canonical = {c["url"] for c in shell._last_crawl_listing_claims}
        from backend.url_identity import canonicalize_listing_url
        canon = [canonicalize_listing_url(u) for u in canonical]
        with sqlite3.connect(db.db_path) as conn:
            reachable = sorted(r[0] for r in conn.execute(
                "SELECT DISTINCT a.raw_url FROM listing_claim_aliases a "
                "JOIN listing_claims c ON c.claim_id = a.claim_id "
                "WHERE c.canonical_url IN (%s)" % ",".join("?" * len(canon)),
                canon))
        assert reachable == sorted(VARIANTS), (
            "revocation would reach %s, leaving the rest un-revoked"
            % reachable)
