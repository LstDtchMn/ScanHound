"""Round 19, gate item 1: one arm identity, computed in one place.

Round 18 (M18-1) found three incompatible identities for the same object. The
consequence that matters is not inconsistency for its own sake -- it is that a
policy cannot JOIN a durable claim to a coverage proof when the two name
different things, and the widest of the three names two feeds as one.

The merge is live in the shipped source list, not hypothetical:

    DDLBase Remux 4K      /cat/movie-remux-2160p
    DDLBase Remux 1080p   /cat/movie-remux-1080p

Both were `ddlbase:remux` in the ledger.

Nothing here grants anything. Naming an arm is not evidence about it, and a
registry entry is not an ordering contract.
"""
import sqlite3

import pytest

from backend.arms import (ArmKeyCollision, ArmRegistry, ArmSpec,
                          arm_key_from_descriptor, endpoint_slug,
                          spec_from_descriptor)
from tests.test_round16_traversal_emission import (_Scraper, _crawl, _listing,
                                                   _source)

#: The shipped descriptors, verbatim in shape.
SHIPPED = [
    {"name": "4K Movies", "base": "https://hdencode.org/quality/2160p/",
     "suffix": "?tag=movies", "type": "movie", "source": "hdencode",
     "category": "4k"},
    {"name": "Remux Movies", "base": "https://hdencode.org/quality/remux/",
     "suffix": "?tag=movies", "type": "movie", "source": "hdencode",
     "category": "remux"},
    {"name": "TV Packs", "base": "https://hdencode.org/tag/tv-packs/",
     "suffix": "", "type": "tv", "source": "hdencode", "category": "tv"},
    {"name": "DDLBase WEB-DL 4K", "base": "https://ddlbase.com/cat/movie-webdl-2160p",
     "suffix": "", "type": "movie", "source": "ddlbase", "category": "4k"},
    {"name": "DDLBase Remux 4K", "base": "https://ddlbase.com/cat/movie-remux-2160p",
     "suffix": "", "type": "movie", "source": "ddlbase", "category": "remux"},
    {"name": "DDLBase Remux 1080p", "base": "https://ddlbase.com/cat/movie-remux-1080p",
     "suffix": "", "type": "movie", "source": "ddlbase", "category": "remux"},
]

#: What the DEPLOYED container has actually written, read 2026-08-21.
LIVE_LEGACY_KEYS = ["hdencode:tv", "hdencode:4k", "hdencode:remux"]


class TestTheShippedSourcesDoNotMerge:

    def test_every_shipped_feed_has_its_own_arm_key(self):
        keys = [arm_key_from_descriptor(d) for d in SHIPPED]
        assert len(set(keys)) == len(SHIPPED), (
            "two shipped feeds share one arm key: %s"
            % [k for k in keys if keys.count(k) > 1])

    def test_the_two_ddlbase_remux_feeds_are_distinct(self):
        """The concrete instance of M18-1."""
        a = arm_key_from_descriptor(SHIPPED[4])
        b = arm_key_from_descriptor(SHIPPED[5])
        assert a != b
        assert a == "ddlbase:remux:movie-remux-2160p"
        assert b == "ddlbase:remux:movie-remux-1080p"

    def test_they_were_the_same_under_the_legacy_shape(self):
        """Anti-vacuity: if the legacy keys had ALSO been distinct, the test
        above would prove nothing about the defect it names."""
        assert (spec_from_descriptor(SHIPPED[4]).legacy_key
                == spec_from_descriptor(SHIPPED[5]).legacy_key
                == "ddlbase:remux")

    def test_the_live_hdencode_keys_are_unchanged_in_source_and_category(self):
        """The migration must be a REFINEMENT of the live keys, not a rename.
        A key whose source or category moved would strand the rows."""
        for d in SHIPPED[:3]:
            spec = spec_from_descriptor(d)
            assert spec.legacy_key in LIVE_LEGACY_KEYS
            assert spec.arm_key.startswith(spec.legacy_key + ":")


class TestTheRegistryRefusesToMergeFeeds:

    def test_a_genuine_collision_raises_rather_than_merging(self):
        """Two descriptors differing only by a query suffix produce one key.
        Refused loudly: a silent merge is invisible -- the crawl runs, the
        ledger fills, and two feeds quietly share one identity."""
        dupes = [
            {"base": "https://x.test/cat/a", "source": "s", "category": "c",
             "type": "movie"},
            {"base": "https://x.test/cat/a", "source": "s", "category": "c",
             "type": "tv"},
        ]
        with pytest.raises(ArmKeyCollision) as ei:
            ArmRegistry.from_descriptors(dupes)
        assert "s:c:a" in str(ei.value)

    def test_the_shipped_set_builds_cleanly(self):
        """The positive control. A registry that refused everything would pass
        the test above and make the whole scheme unusable."""
        reg = ArmRegistry.from_descriptors(SHIPPED)
        assert len(reg) == 6
        assert "ddlbase:remux:movie-remux-1080p" in reg

    def test_an_identical_repeat_is_not_a_collision(self):
        """The same feed listed twice is a configuration wart, not two feeds."""
        reg = ArmRegistry.from_descriptors([SHIPPED[0], dict(SHIPPED[0])])
        assert len(reg) == 1


class TestLegacyKeysMigrateOnlyWhenTheAnswerIsKNOWN:

    def setup_method(self):
        self.reg = ArmRegistry.from_descriptors(SHIPPED)

    def test_every_live_key_resolves_deterministically(self):
        rewrites, unresolved = self.reg.legacy_migration_plan(LIVE_LEGACY_KEYS)
        assert rewrites == {
            "hdencode:4k": "hdencode:4k:2160p",
            "hdencode:remux": "hdencode:remux:remux",
            "hdencode:tv": "hdencode:tv:tv-packs",
        }
        assert unresolved == []

    def test_an_ambiguous_legacy_key_is_reported_not_guessed(self):
        """`ddlbase:remux` is BOTH remux feeds. Picking either would give the
        old row a precision it never carried -- inventing an attribution and
        then treating it as observed fact."""
        rewrites, unresolved = self.reg.legacy_migration_plan(["ddlbase:remux"])
        assert rewrites == {}
        assert unresolved == ["ddlbase:remux"]
        assert self.reg.resolve_legacy("ddlbase:remux") is None

    def test_a_key_for_a_feed_that_no_longer_exists_is_unresolved(self):
        rewrites, unresolved = self.reg.legacy_migration_plan(["oldsite:4k"])
        assert rewrites == {}
        assert unresolved == ["oldsite:4k"]

    def test_a_key_that_is_already_modern_is_left_entirely_alone(self):
        rewrites, unresolved = self.reg.legacy_migration_plan(
            ["hdencode:4k:2160p"])
        assert rewrites == {}
        assert unresolved == [], "a finished key was reported as a problem"

    def test_a_mixed_ledger_migrates_the_knowable_part(self):
        rewrites, unresolved = self.reg.legacy_migration_plan(
            LIVE_LEGACY_KEYS + ["ddlbase:remux", "hdencode:4k:2160p"])
        assert len(rewrites) == 3
        assert unresolved == ["ddlbase:remux"]


class TestTheProducerStampsTheKeyTheTraversalReports:
    """The producer-versus-component gap, again. The registry can be perfect
    and the ledger still be keyed differently, because the ledger is written by
    a separate call path that used to compute its own key."""

    A = "https://hdencode.example/film-a-2026/"
    B = "https://hdencode.example/film-b-2026/"

    def _claims(self, monkeypatch):
        shell = _crawl(
            [_source("4K Movies", "movie", "4k")],
            _Scraper([_listing([(self.A, "Film A 2026"), (self.B, "Film B 2026")])]),
            monkeypatch)
        return shell

    def test_the_claim_carries_an_arm_key(self, monkeypatch):
        shell = self._claims(monkeypatch)
        claims = shell._last_crawl_listing_claims
        assert claims, "the crawl recorded no listing claims at all"
        for c in claims:
            assert c.get("arm_key"), "a claim was recorded with no arm key"

    def test_it_is_the_SAME_key_the_traversal_reports(self, monkeypatch):
        """The join the policy depends on. If these two disagree, a coverage
        proof and the claims it should govern name different objects and no
        amount of correctness in either fixes it."""
        shell = self._claims(monkeypatch)
        traversal_keys = {a.arm_key for a in shell._last_crawl_traversal.arms}
        claim_keys = {c["arm_key"] for c in shell._last_crawl_listing_claims}
        assert claim_keys == traversal_keys, (
            "ledger claims are keyed %s but the traversal reports %s"
            % (sorted(claim_keys), sorted(traversal_keys)))

    def test_the_key_is_three_parts_not_the_legacy_two(self, monkeypatch):
        shell = self._claims(monkeypatch)
        for c in shell._last_crawl_listing_claims:
            assert c["arm_key"].count(":") == 2, (
                "the ledger is still being written in the merged legacy shape: %r"
                % c["arm_key"])


class TestTheLedgerStoresTheStampedKey:

    def _db(self, tmp_path):
        from backend.database import DatabaseManager
        return DatabaseManager(str(tmp_path / "r19.db"))

    def test_a_stamped_claim_is_stored_under_its_own_key(self, tmp_path):
        db = self._db(tmp_path)
        db.record_listing_claims([
            {"url": "https://ddlbase.com/p/one/", "source": "ddlbase",
             "listing_type": "movie", "listing_category": "remux",
             "arm_key": "ddlbase:remux:movie-remux-2160p"},
            {"url": "https://ddlbase.com/p/one/", "source": "ddlbase",
             "listing_type": "movie", "listing_category": "remux",
             "arm_key": "ddlbase:remux:movie-remux-1080p"},
        ])
        with sqlite3.connect(db.db_path) as conn:
            keys = sorted(r[0] for r in conn.execute(
                "SELECT arm_key FROM listing_claims"))
        assert keys == ["ddlbase:remux:movie-remux-1080p",
                        "ddlbase:remux:movie-remux-2160p"], (
            "the two remux feeds collapsed to one row: the second feed's claim "
            "about this release was lost")

    def test_an_unstamped_claim_still_records_rather_than_dropping(self, tmp_path):
        """The fallback exists so a claim from an older producer is kept. It
        merges, which is why it must never be the path a new deploy takes --
        but losing the observation would be worse."""
        db = self._db(tmp_path)
        n = db.record_listing_claims([
            {"url": "https://x.test/p/two/", "source": "hdencode",
             "listing_type": "movie", "listing_category": "4k"},
        ])
        assert n == 1
        with sqlite3.connect(db.db_path) as conn:
            keys = [r[0] for r in conn.execute(
                "SELECT arm_key FROM listing_claims")]
        assert keys == ["hdencode:4k"]


class TestEndpointSlug:

    @pytest.mark.parametrize("base,want", [
        ("https://hdencode.org/quality/2160p/", "2160p"),
        ("https://hdencode.org/tag/tv-packs/", "tv-packs"),
        ("https://ddlbase.com/cat/movie-remux-1080p", "movie-remux-1080p"),
        ("https://hdencode.org", "hdencode.org"),
        ("", "root"),
        (None, "root"),
    ])
    def test_slugs(self, base, want):
        assert endpoint_slug(base) == want

    def test_case_and_whitespace_are_normalised(self):
        assert endpoint_slug("https://x.test/CAT/Movie-Remux ") == "movie-remux"


class TestArmSpecIsInert:
    """A spec describes a feed. It grants nothing."""

    def test_a_spec_has_no_authority_field(self):
        spec = ArmSpec("hdencode", "4k", "2160p", "movie")
        for attr in ("authoritative", "attested", "contract", "ordering_contract"):
            assert not hasattr(spec, attr), (
                "ArmSpec carries %r: naming an arm would become evidence about "
                "it, which is the observation-versus-permission confusion this "
                "work exists to keep separate" % attr)

    def test_specs_are_frozen(self):
        spec = ArmSpec("hdencode", "4k", "2160p", "movie")
        with pytest.raises(Exception):
            spec.category = "remux"


class TestTwoFeedsOfOneCategoryBothKeepTheirClaim:
    """The merge, driven end to end through the real crawler.

    `listing_claim_seen` was keyed (url, listing_type, CATEGORY). The shipped
    instance is the DDLBase remux pair -- both movie/remux -- so whichever
    listed a release SECOND had its claim discarded as a repeat of the first,
    and the ledger exists precisely to keep what each arm said, before releases
    age off the listing and the claim becomes unreconstructable.

    Driven here with two HDEncode endpoints of one category, because the
    DDLBase parser cannot read this fixture's markup. The shipped DDLBase pair
    is covered statically by TestTheShippedSourcesDoNotMerge above; what this
    class adds is that the real crawl path keeps both claims.
    """

    SHARED = "https://hdencode.example/shared-remux-2026/"
    KEYS = ["hdencode:remux:remux-1080p", "hdencode:remux:remux-2160p"]

    def _feeds(self):
        return [
            {"name": "Remux 4K", "base": "https://hdencode.org/quality/remux-2160p/",
             "suffix": "", "type": "movie", "source": "hdencode",
             "category": "remux"},
            {"name": "Remux 1080p", "base": "https://hdencode.org/quality/remux-1080p/",
             "suffix": "", "type": "movie", "source": "hdencode",
             "category": "remux"},
        ]

    def _crawl_both(self, monkeypatch):
        page = _listing([(self.SHARED, "Shared Remux 2026")])
        return _crawl(self._feeds(), _Scraper([page, page]), monkeypatch)

    def test_both_feeds_record_their_own_claim(self, monkeypatch):
        shell = self._crawl_both(monkeypatch)
        keys = sorted(c["arm_key"] for c in shell._last_crawl_listing_claims)
        assert keys == self.KEYS, (
            "one feed's claim about the shared release was dropped as a repeat "
            "of the other; recorded keys were %s" % keys)

    def test_the_traversal_reports_two_arms_for_them(self, monkeypatch):
        """Both halves must agree, or the join breaks in the other
        direction."""
        shell = self._crawl_both(monkeypatch)
        assert sorted(a.arm_key for a in shell._last_crawl_traversal.arms) == self.KEYS

    def test_a_genuine_repeat_within_one_feed_is_still_collapsed(
            self, monkeypatch):
        """The positive control for the dedup itself. Per-arm keying must not
        mean recording the same claim twice."""
        page = _listing([(self.SHARED, "Shared Remux 2026"),
                         (self.SHARED, "Shared Remux 2026")])
        shell = _crawl(self._feeds()[:1], _Scraper([page]), monkeypatch)
        assert len(shell._last_crawl_listing_claims) == 1
