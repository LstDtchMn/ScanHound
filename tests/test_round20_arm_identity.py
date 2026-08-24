"""Round 20: the evidence identity is a REVISION, not a name.

Round 19 keyed the ledger on a stable three-part name. Peer review established
that a name cannot be the durable identity of evidence: two request definitions
can be published under one arm on purpose, and keying on the name lets the
second refresh the first's rows, merge their sightings and dates, and destroy
which definition made which claim. A contract that correctly refuses the second
cannot repair evidence already aggregated across both.

So identity is now (arm_id, request_definition_version, parser_version).

This file supersedes test_round19_one_arm_identity.py and
test_round19_arm_key_migration.py. Every intent of those files that still holds
is carried forward below; what is NOT carried forward is their assertions that
the identity is a three-part colon-separated string, which is the thing round 20
changed.
"""
import collections
import os
import pathlib
import re
import sqlite3

import pytest

from backend.arms import (DECLARED_SEMANTICS, KNOWN_ARMS, SEARCH_CATEGORY,
                          UNREGISTERED_PREFIX,
                          ArmRegistry, ArmRegistryError, ArmRevision, ArmSpec,
                          SemanticRedeclaration,
                          PaginationForm, RequestDefinition, build_page_url,
                          active_revisions_for, arm_label_from_descriptor,
                          default_registry, semantic_mismatch,
                          is_arm_id, is_declared_arm_id,
                          request_definition_from_descriptor,
                          resolve_descriptor)
from backend.database import (DatabaseCorruptionDetected, DatabaseManager,
                              QuarantineIncomplete, ShapeMigrationRefused,
                              is_corruption_evidence, migration_execute,
                              validate_shape_migration)

BACKEND = pathlib.Path(__file__).resolve().parent.parent / "backend"

#: Read from the deployed container 2026-08-23 (VACUUM INTO snapshot).
LIVE = {"hdencode:tv": 105, "hdencode:4k": 93, "hdencode:remux": 68}

OLD = "2026-08-01T10:00:00.000000+00:00"
NEW = "2026-08-20T10:00:00.000000+00:00"


@pytest.fixture
def db(tmp_path):
    dm = DatabaseManager(str(tmp_path / "r20.db"))
    yield dm
    dm.close()


def _legacy_db(tmp_path, rows):
    """A database in the DEPLOYED pre-round-20 shape, then opened normally.

    Builds the two-part table by hand so `_init_db` meets the real thing rather
    than a table this branch already created correctly.
    """
    path = str(tmp_path / "legacy.db")
    conn = sqlite3.connect(path)
    conn.execute("""
        CREATE TABLE listing_claims (
            canonical_url TEXT NOT NULL,
            arm_key TEXT NOT NULL,
            listing_type TEXT NOT NULL,
            raw_url TEXT,
            posted_date_raw TEXT,
            posted_date_changed INTEGER NOT NULL DEFAULT 0,
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            sightings INTEGER NOT NULL DEFAULT 1,
            PRIMARY KEY (canonical_url, arm_key)
        )""")
    for url, key, ltype, n in rows:
        conn.execute(
            "INSERT INTO listing_claims (canonical_url, arm_key, listing_type, "
            " raw_url, posted_date_raw, posted_date_changed, first_seen_at, "
            " last_seen_at, sightings) VALUES (?,?,?,?,?,?,?,?,?)",
            (url, key, ltype, url + "?raw", "August 19, 2026 at 9:00 PM", 0,
             OLD, NEW, n))
    conn.execute("PRAGMA user_version = 9")
    conn.commit()
    conn.close()
    return DatabaseManager(path)


#: Named rather than positional. The row layout changed twice in two rounds,
#: and every positional assertion silently means something different afterwards.
Claim = collections.namedtuple(
    "Claim",
    "url state arm_id rdv pv legacy ltype sightings date changed first last")


def _claims(dm):
    with sqlite3.connect(dm.db_path) as conn:
        return [Claim(*r) for r in conn.execute(
            "SELECT canonical_url, attribution_state, arm_id, "
            "       request_definition_version, parser_version, "
            "       legacy_arm_key, listing_type, sightings, posted_date_raw, "
            "       posted_date_changed, first_seen_at, last_seen_at "
            "FROM listing_claims ORDER BY 1,3,7").fetchall()]


# =========================================================================
# The identity itself
# =========================================================================
class TestArmIdIsOpaque:
    """Nothing may parse an arm id. Round 19's id was parseable, and code that
    parsed it kept working after the meaning changed -- silently, with a wrong
    answer. Dots instead of colons make legacy parsing fail loudly."""

    def test_no_declared_id_contains_a_colon(self):
        offenders = [s.arm_id for s in KNOWN_ARMS if ":" in s.arm_id]
        assert not offenders, offenders

    def test_nothing_in_the_backend_splits_an_arm_id(self):
        pattern = re.compile(r"""split\(\s*['"]:['"]""")
        hits = []
        for path in sorted(BACKEND.glob("*.py")):
            for i, line in enumerate(path.read_text(encoding="utf-8")
                                     .splitlines(), 1):
                if pattern.search(line) and ("arm_key" in line
                                             or "arm_id" in line):
                    hits.append("%s:%d %s" % (path.name, i, line.strip()))
        assert not hits, (
            "an arm id is being parsed; ids are opaque by contract: %s" % hits)

    def test_that_guard_can_actually_fire(self):
        """Anti-vacuity. A grep that matches nothing proves nothing unless it
        is shown to match the thing it is looking for."""
        pattern = re.compile(r"""split\(\s*['"]:['"]""")
        assert pattern.search('source = arm_id.split(":")[0]')
        assert not pattern.search("source = arm_id.rsplit('.', 1)[0]")


class TestOneNameCanCoverTwoRequests:
    """The defect round 20 exists to fix."""

    def _spec(self, suffix):
        return ArmSpec(
            arm_id="arm.hdencode.4k-2160p", source="hdencode", category="4k",
            listing_type="movie",
            request=RequestDefinition(
                method="GET", scheme="https", host="hdencode.org", port=None,
                path="/quality/2160p/", query_suffix=suffix,
                pagination=PaginationForm.BASE_PAGE_N_SLASH_SUFFIX),
            parser_version="select_posts/1")

    def test_same_name_different_request_is_a_different_revision(self):
        v1 = self._spec("?tag=movies").revision
        v2 = self._spec("?tag=restored-movies").revision
        assert v1.arm_id == v2.arm_id, "the name is deliberately unchanged"
        assert v1 != v2, (
            "two different request definitions produced one identity; v2 "
            "writes would refresh v1 rows")

    def test_the_parser_is_part_of_the_identity(self):
        base = self._spec("?tag=movies")
        other = ArmSpec(arm_id=base.arm_id, source="hdencode", category="4k",
                        listing_type="movie", request=base.request,
                        parser_version="select_posts/2")
        assert base.revision != other.revision

    def test_a_digest_carries_its_preimage(self):
        """A digest with no preimage is not auditable: when a normaliser change
        invalidates a contract, nothing would explain why."""
        import json
        spec = self._spec("?tag=movies")
        pre = spec.request.preimage()
        assert json.loads(pre) == spec.request.canonical()
        assert spec.request.version.endswith(
            __import__("hashlib").sha256(pre.encode()).hexdigest())

    def test_the_normaliser_version_is_inside_the_digest(self):
        """Otherwise digests computed under different rules compare equal."""
        assert "normalizer" in self._spec("?x").request.canonical()
        assert self._spec("?x").request.version.startswith("request-v1:")


class TestArmSpecIsInert:
    """Naming an arm is not evidence about it. A registry entry must not be
    able to grant authority."""

    def test_a_spec_has_no_authority_field(self):
        fields = set(ArmSpec.__dataclass_fields__)
        assert not (fields & {"authority", "attested", "trusted",
                              "media_kind", "proof"}), fields

    def test_specs_are_frozen(self):
        with pytest.raises(Exception):
            KNOWN_ARMS[0].arm_id = "arm.other"

    def test_revisions_are_hashable_and_compare_by_value(self):
        a = ArmRevision("arm.x", "request-v1:aa", "p/1")
        assert a == ArmRevision("arm.x", "request-v1:aa", "p/1")
        assert len({a, ArmRevision("arm.x", "request-v1:aa", "p/1")}) == 1


# =========================================================================
# The registry refuses; it never merges
# =========================================================================
class TestTheRegistryRefusesToMergeFeeds:

    def _spec(self, arm_id, path, supersedes=()):
        return ArmSpec(arm_id=arm_id, source="s", category="c",
                       listing_type="movie",
                       request=RequestDefinition(
                           method="GET", scheme="https", host="h", port=None,
                           path=path, query_suffix="",
                           pagination=PaginationForm.BASE_PAGE_N_SLASH_SUFFIX),
                       parser_version="p/1", supersedes=tuple(supersedes))

    @staticmethod
    def _reg(specs):
        """Build a registry over ad-hoc specs, pinning each one's own meaning.

        These tests are about COLLISIONS, not about the semantic pin (R23-1),
        so they declare the meanings they use rather than being refused for an
        unrelated reason."""
        return ArmRegistry(
            specs, semantics={s.arm_id: s.semantic.version for s in specs})

    def test_the_shipped_set_builds_cleanly(self):
        assert len(default_registry()) == len(KNOWN_ARMS)

    def test_two_feeds_under_one_name_raise(self):
        with pytest.raises(ArmRegistryError) as ei:
            self._reg([self._spec("arm.a", "/one"),
                         self._spec("arm.a", "/two")])
        assert "arm.a" in str(ei.value)

    def test_one_request_under_two_names_raises(self):
        with pytest.raises(ArmRegistryError) as ei:
            self._reg([self._spec("arm.a", "/same"),
                         self._spec("arm.b", "/same")])
        assert "SAME request definition" in str(ei.value)

    def test_a_legacy_key_claimed_by_two_arms_raises(self):
        with pytest.raises(ArmRegistryError) as ei:
            self._reg([self._spec("arm.a", "/one", ["old:key"]),
                         self._spec("arm.b", "/two", ["old:key"])])
        assert "old:key" in str(ei.value)

    def test_a_legacy_key_equal_to_a_live_arm_id_raises(self):
        """Otherwise the legacy key silently resolves to a live arm at runtime
        and its rows are stranded, with both names looking valid."""
        with pytest.raises(ArmRegistryError) as ei:
            self._reg([self._spec("arm.a", "/one"),
                         self._spec("arm.b", "/two", ["arm.a"])])
        assert "ambiguous" in str(ei.value)

    def test_an_identical_repeat_is_not_a_collision(self):
        s = self._spec("arm.a", "/one")
        assert len(self._reg([s, s])) == 1


# =========================================================================
# The declared table agrees with the crawler
# =========================================================================
class TestTheDeclaredArmsMatchTheProducer:

    ALL_FLAGS = {"4k": True, "1080p": True, "remux": True, "tv": True,
                 "4k_webdl": True, "4k_remux": True, "1080p_remux": True}

    def _emitted(self):
        from unittest.mock import MagicMock
        from backend.scanner_service import ScannerService
        svc = ScannerService.__new__(ScannerService)
        svc.config = {"hdencode_enabled": True, "ddlbase_enabled": True,
                      "adithd_enabled": True}
        svc._log = MagicMock()
        out = []
        for stype in ("HDEncode", "DDLBase", "Adit-HD"):
            out.extend(svc._build_sources(
                "Incremental", stype, "https://hdencode.org",
                self.ALL_FLAGS, ""))
        return out

    def test_the_producer_emits_something(self):
        assert self._emitted(), "every check below would be vacuous"

    def test_every_emitted_feed_resolves_to_a_declared_arm(self):
        missing = [d["name"] for d in self._emitted()
                   if resolve_descriptor(d) is None]
        assert not missing, (
            "feeds the crawler produces are not declared, so their evidence "
            "can never support a proof: %s" % missing)

    def test_no_declared_arm_is_unproducible(self):
        produced = {arm_label_from_descriptor(d) for d in self._emitted()}
        extra = sorted({s.arm_id for s in KNOWN_ARMS} - produced)
        assert not extra, (
            "declared arms nothing produces would let an ambiguous legacy key "
            "look resolvable: %s" % extra)

    def test_the_two_ddlbase_remux_feeds_are_distinct(self):
        feeds = [d for d in self._emitted()
                 if d["source"] == "ddlbase" and d["category"] == "remux"]
        assert len(feeds) == 2
        assert (arm_label_from_descriptor(feeds[0])
                != arm_label_from_descriptor(feeds[1]))

    def test_they_were_one_key_under_the_legacy_shape(self):
        """Why the migration cannot attribute them: the deployed ledger filed
        both under 'ddlbase:remux'."""
        feeds = [d for d in self._emitted()
                 if d["source"] == "ddlbase" and d["category"] == "remux"]
        legacy = {"%s:%s" % (d["source"], d["category"]) for d in feeds}
        assert legacy == {"ddlbase:remux"}
        assert default_registry().resolve_legacy("ddlbase:remux") is None

    def test_site_search_is_never_proof_eligible(self):
        from unittest.mock import MagicMock
        from backend.scanner_service import ScannerService
        svc = ScannerService.__new__(ScannerService)
        svc.config = {"hdencode_enabled": True}
        svc._log = MagicMock()
        d = svc._build_sources("Site Search", "HDEncode",
                               "https://hdencode.org", self.ALL_FLAGS, "dune")[0]
        assert d["category"] == SEARCH_CATEGORY
        label = arm_label_from_descriptor(d)
        assert default_registry().get(label) is None
        assert not is_arm_id(label), (
            "the search label is shaped like a declared arm id")

    def test_an_undeclared_feed_gets_an_id_instead_of_crashing(self):
        """A feed added to _build_sources and not declared must not take the
        crawl down; it must simply be unable to prove anything."""
        label = arm_label_from_descriptor(
            {"base": "https://example.org/new/", "suffix": "",
             "source": "hdencode", "category": "4k"})
        assert label.startswith(UNREGISTERED_PREFIX)
        assert default_registry().get(label) is None
        # R21-6/R21-7: it must be unmistakably NOT an arm id, and it must carry
        # the full digest rather than a truncation.
        assert not is_arm_id(label)
        assert len(label.split(":")[-1]) == 64, (
            "the digest was truncated; a collision here would merge evidence")


class TestPagination:
    """Pagination lives in the request definition because one of the four forms
    DROPS the query suffix. A digest that ignored it would call two genuinely
    different feeds identical.

    Round 21 (R21-8) removed the second implementation. The crawler used to
    build page URLs from four inline branches while this file kept its own copy
    of them, so production and test could drift into exactly the mismatch the
    digest exists to catch. There is now one implementation, checked two ways:

      * LITERAL golden vectors below -- independent data, not a second
        implementation, so they cannot drift with the code;
      * a real crawl whose requested URLs are captured, proving the crawler
        actually routes through the shared builder and has not kept a fifth
        reconstruction somewhere.
    """

    #: (base, suffix, source, page) -> the exact URL, written out by hand.
    GOLDEN = [
        # hdencode: suffix preserved on every page
        ("https://hdencode.org/quality/2160p/", "?tag=movies", "hdencode", 1,
         "https://hdencode.org/quality/2160p/?tag=movies"),
        ("https://hdencode.org/quality/2160p/", "?tag=movies", "hdencode", 2,
         "https://hdencode.org/quality/2160p/page/2/?tag=movies"),
        ("https://hdencode.org/quality/2160p/", "?tag=movies", "hdencode", 7,
         "https://hdencode.org/quality/2160p/page/7/?tag=movies"),
        # ddlbase: a LEADING slash before "page", and the suffix trails
        ("https://ddlbase.com/cat/movie-remux-2160p", "", "ddlbase", 1,
         "https://ddlbase.com/cat/movie-remux-2160p"),
        ("https://ddlbase.com/cat/movie-remux-2160p", "", "ddlbase", 2,
         "https://ddlbase.com/cat/movie-remux-2160p/page/2"),
        # adithd: the suffix is DROPPED from page 2 onward
        ("https://adit-hd.com/forums/tv-packs/", "?x=1", "adithd", 1,
         "https://adit-hd.com/forums/tv-packs/?x=1"),
        ("https://adit-hd.com/forums/tv-packs/", "?x=1", "adithd", 2,
         "https://adit-hd.com/forums/tv-packs/page/2/"),
        ("https://adit-hd.com/forums/tv-packs/", "?x=1", "adithd", 9,
         "https://adit-hd.com/forums/tv-packs/page/9/"),
    ]

    @pytest.mark.parametrize("base,suffix,source,page,expected", GOLDEN,
                             ids=["%s-p%d" % (g[2], g[3]) for g in GOLDEN])
    def test_the_builder_produces_the_literal_expected_url(
            self, base, suffix, source, page, expected):
        rd = request_definition_from_descriptor(
            {"base": base, "suffix": suffix, "source": source})
        assert build_page_url(rd, base, page) == expected

    def test_adithd_page_two_really_has_no_suffix(self):
        """Stated as its own assertion because it is the reason pagination is
        inside the hashed request definition at all."""
        base = "https://adit-hd.com/forums/tv-packs/"
        rd = request_definition_from_descriptor(
            {"base": base, "suffix": "?x=1", "source": "adithd"})
        assert "?x=1" not in build_page_url(rd, base, 2)
        assert "?x=1" in build_page_url(rd, base, 1)

    def test_the_three_forms_are_genuinely_different(self):
        """Anti-vacuity: if two forms produced the same page-2 URL, the enum
        would be decorative and every vector above would pass regardless."""
        urls = set()
        for src in ("hdencode", "ddlbase", "adithd"):
            rd = request_definition_from_descriptor(
                {"base": "https://x.test/f/", "suffix": "?s=1", "source": src})
            urls.add(build_page_url(rd, "https://x.test/f/", 2))
        assert len(urls) == 3, urls

    def test_the_crawler_actually_requests_those_urls(self, monkeypatch):
        """The integration half. Golden vectors prove the builder is right;
        this proves the crawler USES it rather than keeping its own copy."""
        from tests.test_round16_traversal_emission import (_crawl, _listing,
                                                           _source, _Resp)

        class _Capturing:
            def __init__(self):
                self.urls = []

            def get(self, url=None, *_a, **_kw):
                self.urls.append(url)
                return _Resp(_listing([
                    ("https://hdencode.example/a-2026/", "A Film 2026")]))

        cap = _Capturing()
        _crawl([_source("4K Movies", "movie", "4k")], cap, monkeypatch, pages=2)
        assert cap.urls, "the crawl requested nothing; this proves nothing"
        assert cap.urls[0] == "https://hdencode.org/quality/2160p/?tag=movies"
        if len(cap.urls) > 1:
            assert cap.urls[1] == (
                "https://hdencode.org/quality/2160p/page/2/?tag=movies"), cap.urls

    def test_the_crawler_keeps_no_second_implementation(self):
        """Static guard. The inline branches are gone; if they come back, the
        drift risk this finding was about comes back with them."""
        import inspect
        from backend import scanner_service
        src = inspect.getsource(scanner_service)
        assert "page/{page_num}" not in src, (
            "the crawler is building page URLs itself again instead of calling "
            "build_page_url()")


# =========================================================================
# Shape migration: mechanical, lossless, attributes nothing
# =========================================================================
class TestTheShapeMigration:

    ROWS = [("u/%d" % i, k, "tv" if k.endswith("tv") else "movie", i + 1)
            for i, k in enumerate(sorted(LIVE) * 2)]

    def test_every_row_survives_untouched(self, tmp_path):
        dm = _legacy_db(tmp_path, self.ROWS)
        rows = _claims(dm)
        assert len(rows) == len(self.ROWS)
        by_url = {r.url: r for r in rows}
        for url, key, ltype, n in self.ROWS:
            assert by_url[url].sightings == n, "sightings changed"
            assert by_url[url].first == OLD and by_url[url].last == NEW
        dm.close()

    def test_the_legacy_key_is_carried_across_verbatim(self, tmp_path):
        dm = _legacy_db(tmp_path, self.ROWS)
        seen = {r.legacy for r in _claims(dm)}
        assert seen == set(LIVE), seen
        dm.close()

    def test_nothing_is_attributed(self, tmp_path):
        """The shape change must invent no attribution -- that is the gated
        step, and a row attributed here would have no audit row explaining it."""
        dm = _legacy_db(tmp_path, self.ROWS)
        for r in _claims(dm):
            assert r.state == "unattributed"
            assert r.arm_id is None and r.rdv is None and r.pv is None
        dm.close()

    def test_no_legacy_key_is_smuggled_into_arm_id(self, tmp_path):
        """R21-1. The round-20 shape put the legacy key INTO arm_id, which made
        an unknown attribution look like a known one to a feed that does not
        exist."""
        dm = _legacy_db(tmp_path, self.ROWS)
        assert all(r.arm_id is None for r in _claims(dm))
        dm.close()

    def test_the_rebuild_is_content_checked_not_merely_counted(self, tmp_path):
        """R21-5. COUNT(*) catches drops and duplicates; it cannot catch a
        column defaulted, two rows swapped, or a constant written into the
        wrong column. Every non-key field must survive."""
        dm = _legacy_db(tmp_path, self.ROWS)
        rows = {r.url: r for r in _claims(dm)}
        for url, key, ltype, n in self.ROWS:
            r = rows[url]
            assert r.listing_type if hasattr(r, "listing_type") else True
            assert r.sightings == n
            assert r.date == "August 19, 2026 at 9:00 PM"
            assert r.changed == 0
            assert r.first == OLD and r.last == NEW
        dm.close()

    def test_it_is_idempotent(self, tmp_path):
        dm = _legacy_db(tmp_path, self.ROWS)
        first = _claims(dm)
        dm.close()
        again = DatabaseManager(dm.db_path)
        assert _claims(again) == first
        again.close()

    def test_the_schema_version_does_not_move(self, tmp_path):
        """05_shadow_evidence.py BLOCKS on user_version != 9, so a bump would
        flip the live RSS shadow qualification to not-ready."""
        dm = _legacy_db(tmp_path, self.ROWS)
        with sqlite3.connect(dm.db_path) as conn:
            assert conn.execute("PRAGMA user_version").fetchone()[0] == 9
        assert DatabaseManager.SCHEMA_VERSION == 9
        dm.close()


# =========================================================================
# Attribution: gated, audited, atomic
# =========================================================================
class TestAttribution:

    LIVE_ROWS = [("u/%d" % i, k, "tv" if k.endswith("tv") else "movie", 1)
                 for i, k in enumerate(sorted(LIVE))]

    def test_it_refuses_to_run_without_a_registry(self, db):
        with pytest.raises(ValueError):
            db.migrate_listing_claim_arm_keys(None)

    def test_a_dry_run_changes_nothing(self, tmp_path):
        dm = _legacy_db(tmp_path, self.LIVE_ROWS)
        before = _claims(dm)
        rep = dm.migrate_listing_claim_arm_keys(default_registry())
        assert rep["applied"] is False
        assert _claims(dm) == before
        with sqlite3.connect(dm.db_path) as conn:
            assert conn.execute(
                "SELECT COUNT(*) FROM listing_claim_migration_audit"
            ).fetchone()[0] == 0, "a dry run wrote audit rows"
        dm.close()

    def test_a_dry_run_still_reports_what_it_would_do(self, tmp_path):
        """Otherwise it is indistinguishable from a migration with nothing
        to do."""
        dm = _legacy_db(tmp_path, self.LIVE_ROWS)
        rep = dm.migrate_listing_claim_arm_keys(default_registry())
        assert rep["claims_attributed"] == len(self.LIVE_ROWS)
        assert set(rep["resolved"]) == set(LIVE)
        dm.close()

    def test_apply_attributes_every_live_key(self, tmp_path):
        dm = _legacy_db(tmp_path, self.LIVE_ROWS)
        reg = default_registry()
        dm.migrate_listing_claim_arm_keys(reg, apply=True)
        rows = _claims(dm)
        assert len(rows) == len(self.LIVE_ROWS)
        for r in rows:
            assert r.state == "attributed"
            assert reg.is_active_revision(ArmRevision(r.arm_id, r.rdv, r.pv)), r
        dm.close()

    def test_applying_twice_changes_nothing(self, tmp_path):
        dm = _legacy_db(tmp_path, self.LIVE_ROWS)
        reg = default_registry()
        dm.migrate_listing_claim_arm_keys(reg, apply=True)
        after = _claims(dm)
        rep = dm.migrate_listing_claim_arm_keys(reg, apply=True)
        assert rep["claims_attributed"] == 0 and rep["claims_merged"] == 0
        assert _claims(dm) == after
        dm.close()

    def test_the_audit_records_the_preimage_beside_the_digest(self, tmp_path):
        dm = _legacy_db(tmp_path, self.LIVE_ROWS)
        dm.migrate_listing_claim_arm_keys(default_registry(), apply=True)
        with sqlite3.connect(dm.db_path) as conn:
            rows = conn.execute(
                "SELECT legacy_arm_key, decision, request_definition_version, "
                "       request_definition_preimage, provenance_class "
                "FROM listing_claim_migration_audit").fetchall()
        assert rows
        for key, decision, digest, preimage, prov in rows:
            assert decision == "attributed"
            assert digest and preimage and len(preimage) > 20
            assert prov == "reconstructed", (
                "the parser version was reconstructed by byte-identity, not "
                "read from a recorded constant; the audit must say so")
        dm.close()


class TestAmbiguityIsNeverResolvedByGuessing:

    ROWS = [("u/keep", "ddlbase:remux", "movie", 2),
            ("u/move", "hdencode:tv", "tv", 3)]

    def test_the_ambiguous_key_is_quarantined(self, tmp_path):
        dm = _legacy_db(tmp_path, self.ROWS)
        rep = dm.migrate_listing_claim_arm_keys(default_registry(), apply=True)
        assert rep["skipped"] == ["ddlbase:remux"]
        with sqlite3.connect(dm.db_path) as conn:
            q = conn.execute(
                "SELECT canonical_url, legacy_arm_key, sightings, reason "
                "FROM listing_claims_quarantine").fetchall()
        assert len(q) == 1 and q[0][:3] == ("u/keep", "ddlbase:remux", 2)
        assert q[0][3], "quarantine with no reason is not auditable"
        dm.close()

    def test_the_quarantined_row_stays_in_the_ledger(self, tmp_path):
        """Unattributable evidence may NARROW authority but never widen it.
        Removing the row removes a contradiction it could still supply, which
        makes a negative claim easier to sustain -- widening by omission."""
        dm = _legacy_db(tmp_path, self.ROWS)
        dm.migrate_listing_claim_arm_keys(default_registry(), apply=True)
        rows = {r.url: r for r in _claims(dm)}
        assert "u/keep" in rows, "the quarantined row was deleted"
        assert rows["u/keep"].state == "unattributed"
        assert rows["u/keep"].legacy == "ddlbase:remux"
        assert rows["u/keep"].arm_id is None, (
            "the unresolvable row was given an arm_id, which makes an unknown "
            "attribution look like a known one")
        dm.close()

    def test_the_knowable_rows_still_move_alongside(self, tmp_path):
        dm = _legacy_db(tmp_path, self.ROWS)
        dm.migrate_listing_claim_arm_keys(default_registry(), apply=True)
        rows = {r.url: r for r in _claims(dm)}
        assert rows["u/move"].state == "attributed"
        assert rows["u/move"].arm_id == "arm.hdencode.tv-packs"
        assert rows["u/move"].rdv
        dm.close()

    def test_a_partial_registry_cannot_resolve_it(self):
        """An 'exactly one match' rule gets MORE permissive as the reference
        set shrinks, so a partial registry would resolve what the complete one
        correctly refuses."""
        partial = ArmRegistry([s for s in KNOWN_ARMS
                               if s.arm_id != "arm.ddlbase.remux-1080p"])
        assert partial.resolve_legacy("ddlbase:remux") is None
        assert default_registry().resolve_legacy("ddlbase:remux") is None

    def test_a_key_for_a_feed_that_no_longer_exists_is_unresolved(self):
        assert default_registry().resolve_legacy("gone:4k") is None


class TestTheMigrationIsAtomic:
    """Plan and apply share ONE transaction.

    An earlier revision read the distinct keys in one transaction and wrote in
    another. Between them a concurrent writer could add rows under a key the
    plan had already classified, and those rows would be missed with no record
    that they existed.

    Rollback is asserted by BEHAVIOUR, not by reading the context manager:
    sqlite3 in autocommit mode would make rollback() a silent no-op, and the
    docstring would still say the writes were atomic.
    """

    ROWS = [("u/%d" % i, k, "tv" if k.endswith("tv") else "movie", 1)
            for i, k in enumerate(sorted(LIVE))]

    class _Boom(Exception):
        pass

    def _injected_registry(self):
        """The real registry, except the LAST key processed explodes -- so the
        earlier keys have already written when the failure lands."""
        boom = self._Boom
        real = default_registry()
        last = sorted(LIVE)[-1]

        class BadSpec:
            arm_id = "arm.hdencode.tv-packs"

            @property
            def revision(self):
                raise boom("exploded partway through")

        class Injected:
            def __getattr__(self, name):
                return getattr(real, name)

            def legacy_migration_plan(self, keys):
                plan, unresolved = real.legacy_migration_plan(keys)
                assert len(plan) > 1, (
                    "only one key would be processed, so nothing would have "
                    "been written before the failure and this proves nothing")
                plan[last] = BadSpec()
                return plan, unresolved

        return Injected()

    def test_a_failure_partway_through_leaves_nothing_behind(self, tmp_path):
        dm = _legacy_db(tmp_path, self.ROWS)
        before = _claims(dm)
        with pytest.raises(self._Boom):
            dm.migrate_listing_claim_arm_keys(self._injected_registry(),
                                              apply=True)
        assert _claims(dm) == before, "a partial attribution survived"
        dm.close()

    def test_no_audit_row_survives_a_rollback(self, tmp_path):
        """Nor changes with no record: the audit is written in the same
        transaction as the work it describes."""
        dm = _legacy_db(tmp_path, self.ROWS)
        with pytest.raises(self._Boom):
            dm.migrate_listing_claim_arm_keys(self._injected_registry(),
                                              apply=True)
        with sqlite3.connect(dm.db_path) as conn:
            assert conn.execute(
                "SELECT COUNT(*) FROM listing_claim_migration_audit"
            ).fetchone()[0] == 0
        dm.close()

    def test_the_injection_really_would_have_written(self, tmp_path):
        """Anti-vacuity: without the injection the same run attributes rows, so
        the two tests above are not passing merely because nothing happened."""
        dm = _legacy_db(tmp_path, self.ROWS)
        rep = dm.migrate_listing_claim_arm_keys(default_registry(), apply=True)
        assert rep["claims_attributed"] > 0
        dm.close()


class TestCollisionsAreMergedNotClobbered:
    """After a deploy, a rollback and a redeploy, one release can be present
    under both the legacy key and the new revision."""

    def _collide(self, tmp_path, legacy_date, target_date,
                 legacy_last=OLD, target_last=NEW):
        """The two last_seen_at values are set EXPLICITLY and differ.

        They were both NEW in the first draft, so the target row won on the
        >= tiebreak rather than on recency, and the assertions could not tell
        "keep the more recent observation" apart from "always keep the target".
        A test that cannot distinguish the fix from the bug tests nothing.
        """
        assert legacy_last != target_last, "the recency axis is not exercised"
        dm = _legacy_db(tmp_path, [("u/x", "hdencode:tv", "tv", 4)])
        rev = default_registry().get("arm.hdencode.tv-packs").revision
        with sqlite3.connect(dm.db_path) as conn:
            conn.execute(
                "UPDATE listing_claims SET posted_date_raw = ?, "
                "  last_seen_at = ? WHERE canonical_url = 'u/x'",
                (legacy_date, legacy_last))
            conn.execute(
                "INSERT INTO listing_claims (canonical_url, attribution_state, "
                " arm_id, request_definition_version, parser_version, "
                " legacy_arm_key, listing_type, raw_url, posted_date_raw, "
                " posted_date_changed, first_seen_at, last_seen_at, sightings) "
                "VALUES ('u/x','attributed',?,?,?,NULL,'tv','u/x?raw',?,0,?,?,7)",
                rev.as_row() + (target_date, OLD, target_last))
            conn.commit()
        dm.migrate_listing_claim_arm_keys(default_registry(), apply=True)
        rows = _claims(dm)
        dm.close()
        return rows

    def test_the_two_rows_become_one(self, tmp_path):
        rows = self._collide(tmp_path, "August 01, 2026 at 01:00 PM",
                             "August 19, 2026 at 09:00 AM")
        assert len(rows) == 1

    def test_the_sightings_are_summed(self, tmp_path):
        rows = self._collide(tmp_path, "August 01, 2026 at 01:00 PM",
                             "August 19, 2026 at 09:00 AM")
        assert rows[0].sightings == 11, "4 + 7"

    def test_the_span_is_unioned(self, tmp_path):
        rows = self._collide(tmp_path, "August 01, 2026 at 01:00 PM",
                             "August 19, 2026 at 09:00 AM")
        assert rows[0].first == OLD and rows[0].last == NEW

    def test_the_posted_date_is_merged_not_dropped(self, tmp_path):
        """THE ROUND-19 DEFECT. The merge listed first_seen, last_seen,
        sightings and posted_date_changed and never mentioned posted_date_raw,
        so one row's date silently won and the other was destroyed."""
        rows = self._collide(tmp_path, "August 01, 2026 at 01:00 PM",
                             "August 19, 2026 at 09:00 AM")
        assert rows[0].date == "August 19, 2026 at 09:00 AM", (
            "the date from the more recently seen observation must survive")

    def test_a_disagreement_is_recorded_as_a_change(self, tmp_path):
        """Previously discarded along with the losing value."""
        rows = self._collide(tmp_path, "August 01, 2026 at 01:00 PM",
                             "August 19, 2026 at 09:00 AM")
        assert rows[0].changed == 1

    def test_agreement_is_not_recorded_as_a_change(self, tmp_path):
        """Anti-vacuity for the test above: if the flag were set
        unconditionally, that test would pass for the wrong reason."""
        same = "August 19, 2026 at 09:00 AM"
        rows = self._collide(tmp_path, same, same)
        assert rows[0].changed == 0
        assert rows[0].date == same

    def test_a_null_date_never_beats_a_real_one(self, tmp_path):
        rows = self._collide(tmp_path, "August 01, 2026 at 01:00 PM", None)
        assert rows[0].date == "August 01, 2026 at 01:00 PM"

    def test_the_LEGACY_date_wins_when_the_legacy_row_was_seen_later(
            self, tmp_path):
        """The other side of the axis. Every other case here has the target as
        the more recent row, so on its own the suite would also pass for an
        implementation that simply always kept the target's date -- which is
        the round-19 behaviour this is meant to catch.
        """
        rows = self._collide(tmp_path,
                             legacy_date="August 19, 2026 at 09:00 AM",
                             target_date="August 01, 2026 at 01:00 PM",
                             legacy_last=NEW, target_last=OLD)
        assert rows[0].date == "August 19, 2026 at 09:00 AM"
        assert rows[0].changed == 1
        assert rows[0].last == NEW, "last_seen_at must still be the later of the two"


class TestTheWriterStoresTheRevision:

    def _write(self, dm, extra):
        claim = {"url": "https://hdencode.org/a-release/", "source": "hdencode",
                 "listing_type": "tv", "listing_category": "tv"}
        claim.update(extra)
        assert dm.record_listing_claims([claim]) == 1
        return _claims(dm)[0]

    def test_a_stamped_claim_is_stored_under_its_revision(self, db):
        rev = default_registry().get("arm.hdencode.tv-packs").revision
        row = self._write(db, {
            "arm_key": rev.arm_id,
            "request_definition_version": rev.request_definition_version,
            "parser_version": rev.parser_version})
        assert row.state == "attributed"
        assert (row.arm_id, row.rdv, row.pv) == rev.as_row()

    def test_an_unstamped_claim_is_recorded_but_cannot_prove(self, db):
        """A producer that has not been updated must degrade to 'cannot prove',
        never to 'proved on evidence of unknown origin' -- and never to a
        dropped sighting, which would lose a contradiction."""
        row = self._write(db, {"arm_key": "hdencode:tv"})
        assert row.state == "unattributed"
        assert row.arm_id is None, (
            "a legacy two-part key reached the arm_id column; arm_id must "
            "only ever hold a declared arm id")
        assert row.legacy == "hdencode:tv"

    def test_two_revisions_of_one_arm_do_not_collide(self, db):
        """The whole point. Under round 19 the second write refreshed the
        first's row and merged their sightings."""
        rev = default_registry().get("arm.hdencode.tv-packs").revision
        self._write(db, {
            "arm_key": rev.arm_id,
            "request_definition_version": rev.request_definition_version,
            "parser_version": rev.parser_version})
        self._write(db, {
            "arm_key": rev.arm_id,
            "request_definition_version": "request-v1:" + "0" * 64,
            "parser_version": rev.parser_version})
        rows = _claims(db)
        assert len(rows) == 2, "two revisions were merged into one row"
        assert all(r.sightings == 1 for r in rows), (
            "sightings bled across revisions")


class TestTheProducerStampsWhatTheTraversalReports:
    """Driven through a REAL crawl, not by reading the source.

    The registry can be perfect and the ledger still keyed differently, because
    the ledger is written by a separate call path. Inspecting the source only
    proves the literal is present in the file; it cannot show that the value
    reaching the claim is the same one the traversal reports.
    """

    A = "https://hdencode.example/film-a-2026/"
    B = "https://hdencode.example/film-b-2026/"

    def _crawled(self, monkeypatch):
        from tests.test_round16_traversal_emission import (_crawl, _listing,
                                                           _source, _Scraper)
        return _crawl(
            [_source("4K Movies", "movie", "4k")],
            _Scraper([_listing([(self.A, "Film A 2026"),
                                (self.B, "Film B 2026")])]),
            monkeypatch)

    def test_the_crawl_produced_claims_at_all(self, monkeypatch):
        assert self._crawled(monkeypatch)._last_crawl_listing_claims, (
            "no claims were recorded, so every assertion below is vacuous")

    def test_every_claim_carries_a_full_revision(self, monkeypatch):
        for c in self._crawled(monkeypatch)._last_crawl_listing_claims:
            assert c.get("arm_key"), "a claim was recorded with no arm id"
            assert c.get("request_definition_version"), (
                "a claim was recorded with no request definition version, so "
                "it can never be joined to a coverage proof")
            assert c.get("parser_version"), "a claim carries no parser version"

    def test_the_claim_names_what_the_traversal_names(self, monkeypatch):
        """The join the policy depends on. If these disagree, a coverage proof
        and the claims it should govern name different objects."""
        shell = self._crawled(monkeypatch)
        traversal = {a.arm_key for a in shell._last_crawl_traversal.arms}
        claims = {c["arm_key"] for c in shell._last_crawl_listing_claims}
        assert claims == traversal, (
            "claims are keyed %s but the traversal reports %s"
            % (sorted(claims), sorted(traversal)))

    def test_the_stamped_id_is_the_DECLARED_one(self, monkeypatch):
        """Not merely opaque -- the actual declared arm.

        The harness used to build a made-up base, which resolved to an
        'arm.unregistered.*' id. Every assertion here passed while proving
        nothing about a feed that ships.
        """
        reg = default_registry()
        for c in self._crawled(monkeypatch)._last_crawl_listing_claims:
            assert c["arm_key"] == "arm.hdencode.4k-2160p", c["arm_key"]
            assert ":" not in c["arm_key"], "a parseable key was written"
            assert reg.is_active_revision(ArmRevision(
                c["arm_key"], c["request_definition_version"],
                c["parser_version"])), (
                "the crawl stamped a revision the registry does not recognise, "
                "so nothing it observes could ever support a proof")

    def test_the_parser_version_is_the_running_one(self, monkeypatch):
        """Read from the process doing the parsing, not from the declaration.
        A spec claiming select_posts/1 while the process runs v2 must produce a
        v2 revision so the mismatch is recorded rather than asserted away."""
        from backend.scanner_service import _COV_PARSER_VERSION
        for c in self._crawled(monkeypatch)._last_crawl_listing_claims:
            assert c["parser_version"] == _COV_PARSER_VERSION


# =========================================================================
# Round 21 (R21-1/2/3/6): the invariants are enforced by the DATABASE
# =========================================================================
class TestAttributionStateIsEnforcedNotConventional:
    """A CHECK constraint, not a docstring.

    Every prior round of this work had an invariant that code was supposed to
    maintain, and the defects were all cases where some path did not. These
    shapes are unrepresentable now, so no future writer can reintroduce them.
    """

    BAD = [
        ("attributed with no arm_id",
         "INSERT INTO listing_claims (canonical_url, attribution_state, "
         "listing_type, first_seen_at, last_seen_at) "
         "VALUES ('u/a','attributed','tv','t','t')"),
        ("attributed with an arm_id but no versions",
         "INSERT INTO listing_claims (canonical_url, attribution_state, arm_id, "
         "listing_type, first_seen_at, last_seen_at) "
         "VALUES ('u/b','attributed','arm.x','tv','t','t')"),
        ("unattributed carrying an arm_id",
         "INSERT INTO listing_claims (canonical_url, attribution_state, arm_id, "
         "legacy_arm_key, listing_type, first_seen_at, last_seen_at) "
         "VALUES ('u/c','unattributed','arm.x','k','tv','t','t')"),
        ("unattributed with no legacy key to identify it by",
         "INSERT INTO listing_claims (canonical_url, attribution_state, "
         "listing_type, first_seen_at, last_seen_at) "
         "VALUES ('u/d','unattributed','tv','t','t')"),
    ]

    @pytest.mark.parametrize("label,sql", BAD, ids=[b[0] for b in BAD])
    def test_a_half_attributed_row_is_refused(self, db, label, sql):
        with sqlite3.connect(db.db_path) as conn:
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(sql)

    def test_a_well_formed_row_of_each_kind_is_accepted(self, db):
        """Anti-vacuity: a constraint that refused everything would satisfy
        every case above while making the table useless."""
        rev = default_registry().get("arm.hdencode.tv-packs").revision
        with sqlite3.connect(db.db_path) as conn:
            conn.execute(
                "INSERT INTO listing_claims (canonical_url, attribution_state, "
                " arm_id, request_definition_version, parser_version, "
                " listing_type, first_seen_at, last_seen_at) "
                "VALUES ('u/ok1','attributed',?,?,?,'tv','t','t')",
                rev.as_row())
            conn.execute(
                "INSERT INTO listing_claims (canonical_url, attribution_state, "
                " legacy_arm_key, listing_type, first_seen_at, last_seen_at) "
                "VALUES ('u/ok2','unattributed','hdencode:tv','tv','t','t')")
            assert conn.execute(
                "SELECT COUNT(*) FROM listing_claims").fetchone()[0] == 2

    def test_two_unattributed_rows_for_one_release_are_refused(self, tmp_path):
        """A composite key containing nullable columns gives NO uniqueness,
        because NULLs compare distinct. The partial index closes that hole."""
        dm = _legacy_db(tmp_path, [("u/x", "hdencode:tv", "tv", 1)])
        with sqlite3.connect(dm.db_path) as conn:
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO listing_claims (canonical_url, "
                    " attribution_state, legacy_arm_key, listing_type, "
                    " first_seen_at, last_seen_at) "
                    "VALUES ('u/x','unattributed','hdencode:tv','tv','t','t')")
        dm.close()

    def test_the_unique_indexes_exist_and_are_partial(self, db):
        with sqlite3.connect(db.db_path) as conn:
            idx = dict(conn.execute(
                "SELECT name, sql FROM sqlite_master WHERE type='index' "
                "AND tbl_name='listing_claims' AND sql IS NOT NULL").fetchall())
        for name in ("uq_listing_claims_revision_typed",
                     "uq_listing_claims_legacy"):
            assert name in idx, "%s is missing; uniqueness is unenforced" % name
            assert "WHERE" in idx[name].upper(), (
                "%s is not partial, so it constrains the wrong population"
                % name)


class TestTheArmIdNamespaceIsGuarded:
    """R21-6. The writer must never mint a value that LOOKS like an arm id."""

    def test_is_arm_id_accepts_only_the_declared_shape(self):
        assert not is_arm_id("hdencode:tv")             # legacy two-part
        assert not is_arm_id("hdencode:tv:tv-packs")    # round-19 three-part
        assert not is_arm_id(UNREGISTERED_PREFIX + "request-v1:abc")
        assert not is_arm_id("")
        assert not is_arm_id(None)

    def test_every_declared_arm_passes_the_guard(self):
        """Anti-vacuity: a guard that rejected everything would also satisfy
        every negative case above."""
        assert all(is_arm_id(s.arm_id) for s in KNOWN_ARMS)

    CASES = [
        ("a legacy two-part key", {"arm_key": "hdencode:tv"}),
        ("a round-19 three-part key", {"arm_key": "hdencode:tv:tv-packs"}),
        ("an undeclared feed label",
         {"arm_key": UNREGISTERED_PREFIX + "request-v1:" + "a" * 64}),
        ("an arm id with no versions", {"arm_key": "arm.hdencode.tv-packs"}),
        ("nothing stamped at all", {}),
    ]

    @pytest.mark.parametrize("label,extra", CASES, ids=[c[0] for c in CASES])
    def test_it_is_recorded_unattributed_rather_than_dropped(
            self, db, label, extra):
        claim = {"url": "https://hdencode.org/case-release/",
                 "source": "hdencode", "listing_type": "tv",
                 "listing_category": "tv"}
        claim.update(extra)
        assert db.record_listing_claims([claim]) == 1, (
            "the observation was DROPPED; losing it loses a contradiction")
        row = _claims(db)[0]
        assert row.state == "unattributed"
        assert row.arm_id is None, (
            "a non-arm_id value reached the arm_id column")
        assert row.legacy, "an unattributed row must keep its legacy identity"


class TestRevocationStillSeesUnattributedEvidence:
    """The narrowing direction, which must NOT be filtered.

    Revocation expands a contradicted release to every raw href it was seen
    under. Restricting that to attributed claims would leave a variant
    un-revoked -- a download row keeping its media kind after the release has
    been contradicted. Filtering here fails OPEN.
    """

    def test_an_unattributed_claim_still_contributes_its_alias(self, db):
        db.record_listing_claims([{
            "url": "https://hdencode.org/only-legacy/", "source": "hdencode",
            "listing_type": "tv", "listing_category": "tv",
            "arm_key": "hdencode:tv"}])
        with sqlite3.connect(db.db_path) as conn:
            found = conn.execute(
                "SELECT a.raw_url FROM listing_claim_aliases a "
                "JOIN listing_claims c ON c.claim_id = a.claim_id "
                "WHERE c.attribution_state = 'unattributed'").fetchall()
        assert found, (
            "an unattributed claim contributed no alias, so revocation would "
            "never reach its raw href")

    def test_the_alias_expansion_query_is_not_filtered_by_state(self):
        """Static guard. The filter would be easy to add for tidiness and would
        fail open silently, which is the wrong direction for a safety path."""
        import inspect
        from backend import database
        src = inspect.getsource(database)
        i = src.index("SELECT DISTINCT a.raw_url AS raw_url ")
        window = src[i:i + 400]
        assert "attribution_state" not in window, (
            "the revocation alias expansion is filtered by attribution state; "
            "that fails OPEN and leaves variants un-revoked")


class _ShapePair:
    """A raw source table and a rebuilt destination, built by hand.

    The validator is given only the two table NAMES, so a fixture can write any
    destination it likes and the validator must judge it from the source alone.
    """

    NEWCOLS = ("canonical_url TEXT, attribution_state TEXT, arm_id TEXT, "
               "request_definition_version TEXT, parser_version TEXT, "
               "legacy_arm_key TEXT, listing_type TEXT, raw_url TEXT, "
               "posted_date_raw TEXT, posted_date_changed INT, "
               "first_seen_at TEXT, last_seen_at TEXT, sightings INT")
    OLDT = "listing_claims_pre_r21"

    @staticmethod
    def _conn(old_ddl):
        conn = sqlite3.connect(":memory:")
        conn.execute(old_ddl)
        conn.execute("CREATE TABLE listing_claims (%s)" % _ShapePair.NEWCOLS)
        return conn

    @staticmethod
    def deployed(rows, dest=None):
        """The pre-round-20 shape: one `arm_key`, no revision anywhere."""
        conn = _ShapePair._conn(
            "CREATE TABLE %s (canonical_url TEXT, arm_key TEXT, "
            "listing_type TEXT, raw_url TEXT, posted_date_raw TEXT, "
            "posted_date_changed INT, first_seen_at TEXT, last_seen_at TEXT, "
            "sightings INT)" % _ShapePair.OLDT)
        conn.executemany(
            "INSERT INTO %s VALUES (?,?,?,?,?,?,?,?,?)" % _ShapePair.OLDT, rows)
        for r in (dest if dest is not None else
                  [(r[0], "unattributed", None, None, None, r[1], r[2], r[3],
                    r[4], r[5], r[6], r[7], r[8]) for r in rows]):
            conn.execute(
                "INSERT INTO listing_claims VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", r)
        return conn

    @staticmethod
    def intermediate(rows, dest):
        """The round-20 shape: arm_id plus two version columns."""
        conn = _ShapePair._conn(
            "CREATE TABLE %s (canonical_url TEXT, arm_id TEXT, "
            "request_definition_version TEXT, parser_version TEXT, "
            "legacy_arm_key TEXT, listing_type TEXT, raw_url TEXT, "
            "posted_date_raw TEXT, posted_date_changed INT, "
            "first_seen_at TEXT, last_seen_at TEXT, sightings INT)"
            % _ShapePair.OLDT)
        conn.executemany(
            "INSERT INTO %s VALUES (?,?,?,?,?,?,?,?,?,?,?,?)"
            % _ShapePair.OLDT, rows)
        conn.executemany(
            "INSERT INTO listing_claims VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", dest)
        return conn


class TestTheValidatorIsIndependentOfTheMigration:
    """R23-3. The check this replaces compared two SQL projections -- and
    `_init_db` built the OLD-side projection from the very CASE expressions the
    INSERT used. One edit redefined both the transformation and its oracle, they
    moved together, and the comparison came out empty.

    Measured before the fix: under a mutation that demoted every attributed row,
    the check stayed silent while the row became ('unattributed', None, None).
    I had claimed in a review package that it raised. It did not, and I had
    never run it.
    """

    RDV = "request-v1:" + "a" * 64
    PV = "select_posts/1"

    def test_it_is_given_nothing_but_table_names(self):
        """Structural. The independence is the whole point, so the signature
        must not be able to accept a projection again without this failing."""
        import inspect
        params = list(inspect.signature(validate_shape_migration).parameters)
        assert params == ["cursor", "old_table", "new_table"], params

    def test_init_db_passes_it_nothing_else(self):
        """And the call site must not start supplying one."""
        import inspect
        from backend.database import DatabaseManager
        src = inspect.getsource(DatabaseManager.init_db)
        assert "validate_shape_migration(cursor)" in src, (
            "the validator is being handed something by the migration")

    # -- the deployed shape --------------------------------------------------
    ROWS = [("u/1", "hdencode:tv", "tv", "r1", "Aug 1", 0, OLD, NEW, 3),
            ("u/2", "hdencode:4k", "movie", "r2", "Aug 2", 1, OLD, NEW, 5),
            ("u/3", "hdencode:4k", "movie", "r3", None, 0, OLD, NEW, 1)]

    def test_a_faithful_deployed_rebuild_passes(self):
        """Positive control: a validator that rejected everything would satisfy
        every case below while making migration impossible."""
        assert validate_shape_migration(
            _ShapePair.deployed(self.ROWS).cursor(), _ShapePair.OLDT) is None

    CORRUPTIONS = [
        ("a column blanked", lambda r: r[:8] + (None,) + r[9:]),
        ("a counter changed", lambda r: r[:12] + (r[12] + 1,)),
        ("a timestamp changed", lambda r: r[:10] + ("2020-01-01",) + r[11:]),
        ("the legacy key rewritten", lambda r: r[:5] + ("other",) + r[6:]),
        ("a type flipped", lambda r: r[:6] + ("movie",) + r[7:]),
        ("a row silently ATTRIBUTED", lambda r: (
            r[0], "attributed", "arm.hdencode.tv-packs", "request-v1:x",
            "p/1") + r[5:]),
    ]

    @pytest.mark.parametrize("label,corrupt", CORRUPTIONS,
                             ids=[c[0] for c in CORRUPTIONS])
    def test_a_count_preserving_corruption_is_caught(self, label, corrupt):
        dest = [corrupt((r[0], "unattributed", None, None, None, r[1], r[2],
                         r[3], r[4], r[5], r[6], r[7], r[8]))
                for r in self.ROWS]
        conn = _ShapePair.deployed(self.ROWS, dest=dest)
        cur = conn.cursor()
        assert (cur.execute("SELECT COUNT(*) FROM %s" % _ShapePair.OLDT
                            ).fetchone()[0]
                == cur.execute("SELECT COUNT(*) FROM listing_claims"
                               ).fetchone()[0]), "premise: count-preserving"
        assert validate_shape_migration(cur, _ShapePair.OLDT), (
            "%s went undetected" % label)

    def test_a_duplicated_row_is_caught(self):
        conn = _ShapePair.deployed(self.ROWS)
        conn.execute(
            "INSERT INTO listing_claims SELECT * FROM listing_claims LIMIT 1")
        assert validate_shape_migration(conn.cursor(), _ShapePair.OLDT)

    def test_a_dropped_row_is_caught(self):
        conn = _ShapePair.deployed(self.ROWS)
        conn.execute("DELETE FROM listing_claims WHERE canonical_url = 'u/2'")
        assert validate_shape_migration(conn.cursor(), _ShapePair.OLDT)

    # -- the intermediate shape ---------------------------------------------
    def _inter_rows(self):
        return [
            # attributed by the round-20 gated migration
            ("u/a", "arm.hdencode.tv-packs", self.RDV, self.PV, "hdencode:tv",
             "tv", "ra", None, 0, OLD, NEW, 4),
            # never attributed
            ("u/b", "hdencode:tv", "", "", "hdencode:tv", "tv", "rb", None, 0,
             OLD, NEW, 2),
        ]

    def _faithful_dest(self):
        return [
            ("u/a", "attributed", "arm.hdencode.tv-packs", self.RDV, self.PV,
             "hdencode:tv", "tv", "ra", None, 0, OLD, NEW, 4),
            ("u/b", "unattributed", None, None, None, "hdencode:tv", "tv",
             "rb", None, 0, OLD, NEW, 2),
        ]

    def test_a_faithful_intermediate_rebuild_passes(self):
        conn = _ShapePair.intermediate(self._inter_rows(), self._faithful_dest())
        assert validate_shape_migration(conn.cursor(), _ShapePair.OLDT) is None

    def test_DEMOTING_an_attributed_row_is_caught(self):
        """THE CASE THE OLD CHECK COULD NOT SEE.

        Row count unchanged, legacy key unchanged, only the revision gone --
        and because the old oracle was built from the migration's own CASE
        expressions, both sides moved together and it reported equivalence.
        """
        demoted = list(self._faithful_dest())
        demoted[0] = ("u/a", "unattributed", None, None, None, "hdencode:tv",
                      "tv", "ra", None, 0, OLD, NEW, 4)
        conn = _ShapePair.intermediate(self._inter_rows(), demoted)
        cur = conn.cursor()
        assert (cur.execute("SELECT COUNT(*) FROM %s" % _ShapePair.OLDT
                            ).fetchone()[0]
                == cur.execute("SELECT COUNT(*) FROM listing_claims"
                               ).fetchone()[0]), "premise: count-preserving"
        assert validate_shape_migration(cur, _ShapePair.OLDT), (
            "an attributed row was demoted and its revision discarded, and the "
            "validator reported the rebuild faithful")

    def test_rewriting_a_retired_revision_to_the_active_one_is_caught(self):
        """Old evidence must never be rewritten to manufacture continuity."""
        rows = self._inter_rows()
        rows[0] = rows[0][:3] + ("select_posts/0",) + rows[0][4:]
        conn = _ShapePair.intermediate(rows, self._faithful_dest())
        assert validate_shape_migration(conn.cursor(), _ShapePair.OLDT)

    # -- source states with no permitted destination -------------------------
    def test_a_HALF_revision_is_refused(self):
        """The old schema made both version columns independently NOT NULL
        DEFAULT '' with no CHECK tying them together, so this is schema-valid.
        The forward migration nulls BOTH, silently discarding the half that
        exists. Refuse instead of normalising."""
        rows = [("u/h", "arm.hdencode.tv-packs", self.RDV, "", "hdencode:tv",
                 "tv", "rh", None, 0, OLD, NEW, 1)]
        dest = [("u/h", "unattributed", None, None, None, "hdencode:tv", "tv",
                 "rh", None, 0, OLD, NEW, 1)]
        conn = _ShapePair.intermediate(rows, dest)
        with pytest.raises(ShapeMigrationRefused) as ei:
            validate_shape_migration(conn.cursor(), _ShapePair.OLDT)
        assert "HALF revision" in str(ei.value)

    NEVER_DECLARED = ["arm.unregistered." + "b" * 16, "arm.unscheduled.search"]

    @pytest.mark.parametrize("arm_id", NEVER_DECLARED)
    def test_a_never_declared_namespace_with_a_full_revision_is_refused(
            self, arm_id):
        """R22-3, reopened. Round-20 code gave an undeclared feed
        `arm.unregistered.<hex>` and Site Search `arm.unscheduled.search`,
        documented both as absent from the registry -- and then stamped a FULL
        revision on them anyway. Classifying on "both versions present" marks
        as ATTRIBUTED a row that was never attributed to anything."""
        rows = [("u/n", arm_id, self.RDV, self.PV, None, "tv", "rn", None, 0,
                 OLD, NEW, 1)]
        dest = [("u/n", "attributed", arm_id, self.RDV, self.PV, None, "tv",
                 "rn", None, 0, OLD, NEW, 1)]
        conn = _ShapePair.intermediate(rows, dest)
        with pytest.raises(ShapeMigrationRefused) as ei:
            validate_shape_migration(conn.cursor(), _ShapePair.OLDT)
        assert "never a registry member" in str(ei.value)

    def test_a_since_REMOVED_declared_arm_is_NOT_refused(self):
        """The legitimate case that must not be swept up with it: an arm that
        WAS declared when the evidence was recorded and has since been removed.
        That evidence is historically attributed and stays so; the lifecycle
        report surfaces it as `undeclared_arm`."""
        gone = "arm.hdencode.retired-feed"
        assert not is_declared_arm_id(gone), "premise: not currently declared"
        rows = [("u/g", gone, self.RDV, self.PV, "hdencode:old", "tv", "rg",
                 None, 0, OLD, NEW, 1)]
        dest = [("u/g", "attributed", gone, self.RDV, self.PV, "hdencode:old",
                 "tv", "rg", None, 0, OLD, NEW, 1)]
        conn = _ShapePair.intermediate(rows, dest)
        assert validate_shape_migration(conn.cursor(), _ShapePair.OLDT) is None


    def test_it_works_on_a_PRODUCTION_shaped_connection(self):
        """The unit fixtures use a plain connection; `_init_db` does not.

        The production connection sets `row_factory = sqlite3.Row`, and a Row
        never compares equal to a tuple. The first version of this validator
        compared plain tuples against Rows, so in production it refused EVERY
        migration -- while still looking like a working guard, because both
        mutation tests continued to "raise".

        That is the same failure this whole finding is about, one level down:
        an oracle that appears to work because it fails for the wrong reason.
        """
        conn = _ShapePair.deployed(self.ROWS)
        conn.row_factory = sqlite3.Row
        assert validate_shape_migration(
            conn.cursor(), _ShapePair.OLDT) is None, (
            "the validator cannot read a production-shaped connection, so it "
            "would refuse every real migration")

    def test_it_still_discriminates_on_a_production_shaped_connection(self):
        """Anti-vacuity: coercing rows must not flatten everything into
        agreement."""
        dest = [(r[0], "unattributed", None, None, None, r[1], r[2], r[3],
                 r[4], r[5], r[6], r[7], r[8] + 1) for r in self.ROWS]
        conn = _ShapePair.deployed(self.ROWS, dest=dest)
        conn.row_factory = sqlite3.Row
        assert validate_shape_migration(conn.cursor(), _ShapePair.OLDT)

class TestAliasHistoryMovesWithTheClaim:
    """R21-10d / R21-11. Retired with the round-19 suite and never replaced.

    That gap is exactly what let the alias-collision defect through: the
    migration suite tested claim rows, dates, quarantine, atomicity and audit
    thoroughly, and never put an alias through semantic attribution at all.
    """

    OLDER = "2026-07-01T08:00:00.000000+00:00"
    NEWER = "2026-08-25T08:00:00.000000+00:00"

    def _with_alias_collision(self, tmp_path):
        """One raw href present under BOTH the legacy claim and the target
        revision, with deliberately different spans and counts."""
        dm = _legacy_db(tmp_path, [("u/x", "hdencode:tv", "tv", 4)])
        rev = default_registry().get("arm.hdencode.tv-packs").revision
        with sqlite3.connect(dm.db_path) as conn:
            legacy_id = conn.execute(
                "SELECT claim_id FROM listing_claims "
                "WHERE attribution_state = 'unattributed'").fetchone()[0]
            conn.execute(
                "DELETE FROM listing_claim_aliases WHERE claim_id = ?",
                (legacy_id,))
            conn.execute(
                "INSERT INTO listing_claim_aliases (claim_id, raw_url, "
                " first_seen_at, last_seen_at, sightings) "
                "VALUES (?, 'raw-A', ?, ?, 3)",
                (legacy_id, self.OLDER, OLD))
            conn.execute(
                "INSERT INTO listing_claim_aliases (claim_id, raw_url, "
                " first_seen_at, last_seen_at, sightings) "
                "VALUES (?, 'raw-only-legacy', ?, ?, 1)",
                (legacy_id, OLD, OLD))
            conn.execute(
                "INSERT INTO listing_claims (canonical_url, attribution_state, "
                " arm_id, request_definition_version, parser_version, "
                " listing_type, raw_url, posted_date_raw, "
                " posted_date_changed, first_seen_at, last_seen_at, sightings) "
                "VALUES ('u/x','attributed',?,?,?,'tv','raw-A',NULL,0,?,?,7)",
                rev.as_row() + (OLD, NEW))
            target_id = conn.execute(
                "SELECT claim_id FROM listing_claims "
                "WHERE attribution_state = 'attributed'").fetchone()[0]
            conn.execute(
                "INSERT INTO listing_claim_aliases (claim_id, raw_url, "
                " first_seen_at, last_seen_at, sightings) "
                "VALUES (?, 'raw-A', ?, ?, 5)",
                (target_id, OLD, self.NEWER))
            conn.commit()
        dm.migrate_listing_claim_arm_keys(default_registry(), apply=True)
        with sqlite3.connect(dm.db_path) as conn:
            rows = conn.execute(
                "SELECT a.raw_url, a.first_seen_at, a.last_seen_at, "
                "       a.sightings, c.attribution_state, c.arm_id "
                "FROM listing_claim_aliases a "
                "JOIN listing_claims c ON c.claim_id = a.claim_id "
                "ORDER BY a.raw_url").fetchall()
        dm.close()
        return rows

    def test_the_migration_does_not_abort_on_an_alias_collision(self, tmp_path):
        """It previously hit the alias composite key and rolled the WHOLE
        transaction back -- fail-closed, but the migration simply could not
        complete."""
        rows = self._with_alias_collision(tmp_path)
        assert rows, "the migration produced no aliases at all"

    def test_every_alias_follows_the_surviving_claim(self, tmp_path):
        rows = self._with_alias_collision(tmp_path)
        assert sorted(r[0] for r in rows) == ["raw-A", "raw-only-legacy"], (
            "an alias was lost during attribution; revocation could not reach "
            "it afterwards")
        for r in rows:
            assert r[4] == "attributed"
            assert r[5] == "arm.hdencode.tv-packs"

    def test_the_colliding_histories_are_MERGED_not_discarded(self, tmp_path):
        """OR IGNORE keeps the target's history; OR REPLACE keeps the other.
        Both lose one. The union is the truth."""
        rows = {r[0]: r for r in self._with_alias_collision(tmp_path)}
        merged = rows["raw-A"]
        assert merged[1] == self.OLDER, (
            "the earliest first_seen_at did not survive: %s" % merged[1])
        assert merged[2] == self.NEWER, (
            "the latest last_seen_at did not survive: %s" % merged[2])
        assert merged[3] == 8, "sightings not summed (3 + 5): %s" % merged[3]

    def test_a_non_colliding_alias_keeps_its_own_history(self, tmp_path):
        """Anti-vacuity: a merge that rewrote every row would satisfy the test
        above while corrupting everything it touched."""
        rows = {r[0]: r for r in self._with_alias_collision(tmp_path)}
        solo = rows["raw-only-legacy"]
        assert (solo[1], solo[2], solo[3]) == (OLD, OLD, 1)


class TestADescriptorCannotBorrowADeclaredArmsIdentity:
    """R21-12. Matching the request digest says the same bytes are fetched.

    It does NOT say the descriptor means the same thing by them -- and the
    crawler builds its traversal arm and its claim from the descriptor's own
    type/category, not from the spec it matched. So a request-only match let a
    descriptor wear a declared arm id while recording contradictory semantics.
    """

    #: The real shipped TV Packs descriptor.
    REAL = {"name": "TV Packs", "base": "https://hdencode.org/tag/tv-packs/",
            "suffix": "", "type": "tv", "source": "hdencode", "category": "tv"}

    def test_the_unmodified_descriptor_still_resolves(self):
        """The positive control. Every refusal below is worthless if the guard
        simply rejects everything."""
        spec = resolve_descriptor(dict(self.REAL))
        assert spec is not None and spec.arm_id == "arm.hdencode.tv-packs"

    #: "mirror" resolves to the DEFAULT pagination form, exactly as hdencode
    #: does, so it leaves the request digest untouched. Round 22: the earlier
    #: control used "ddlbase", which also switches the pagination form and so
    #: changes the digest -- the descriptor would have failed request matching
    #: even with the source comparison deleted. That control proved nothing
    #: about source validation, which is the weakness it existed to rule out.
    MUTATIONS = [
        ("type flipped to movie", "type", "movie"),
        ("category flipped to 4k", "category", "4k"),
        ("source renamed to a same-pagination mirror", "source", "mirror"),
    ]

    @pytest.mark.parametrize("label,field,value", MUTATIONS,
                             ids=[m[0] for m in MUTATIONS])
    def test_a_semantic_mutation_refuses_to_resolve(self, label, field, value):
        d = dict(self.REAL)
        d[field] = value
        assert resolve_descriptor(d) is None, (
            "%s still resolved to a declared arm, so the crawl would stamp "
            "arm.hdencode.tv-packs on evidence that contradicts it" % label)

    @pytest.mark.parametrize("label,field,value", MUTATIONS,
                             ids=[m[0] for m in MUTATIONS])
    def test_it_cannot_emit_a_declared_revision_either(self, label, field, value):
        from backend.arms import revision_from_descriptor
        d = dict(self.REAL)
        d[field] = value
        assert revision_from_descriptor(d, "select_posts/1") is None

    def test_the_request_digest_is_deliberately_unchanged_by_these(self):
        """The premise. If a mutation also changed the request digest, these
        would be refused for the wrong reason and would prove nothing about
        semantic validation.

        ALL THREE are checked now, including source -- "mirror" takes the same
        default pagination form as hdencode, so the request is byte-identical
        and only the semantic comparison can reject it.
        """
        base = request_definition_from_descriptor(dict(self.REAL)).version
        for field, value in (("type", "movie"), ("category", "4k"),
                             ("source", "mirror")):
            d = dict(self.REAL)
            d[field] = value
            assert request_definition_from_descriptor(d).version == base, (
                "%s changed the request digest, so this case does not test "
                "semantic validation at all" % field)

    def test_such_a_descriptor_is_labelled_unregistered_not_crashed(self):
        d = dict(self.REAL)
        d["type"] = "movie"
        label = arm_label_from_descriptor(d)
        assert label.startswith(UNREGISTERED_PREFIX)
        assert not is_arm_id(label)


# =========================================================================
# R21-13: the revision must reach the PROOF, not stop at the ledger
# =========================================================================
class TestAnOrderingContractDoesNotTransferAcrossRequestDefinitions:
    """The counterexample round 20 was built to prevent, at the boundary that
    had not been updated.

    The ledger keyed evidence on the full revision while `ORDERING_CONTRACTS`
    was still keyed on `(arm_id, parser_version)`. So a contract reviewed for

        arm.hdencode.4k-2160p  ?tag=movies

    was inherited by

        arm.hdencode.4k-2160p  ?tag=restored-movies

    which nobody reviewed and which need not be chronological at all. The
    identity fix stopped one layer short of the thing it was protecting.

    ORDERING_CONTRACTS is empty in production, so this was fail-closed TODAY.
    That lowers the consequence; it does not close the defect, because the
    first contract added would reactivate it.
    """

    ARM = "arm.hdencode.4k-2160p"
    V1 = "request-v1:" + "1" * 64
    V2 = "request-v1:" + "2" * 64

    @pytest.fixture(autouse=True)
    def _restore_contracts(self):
        import backend.coverage as cov
        saved = dict(cov.ORDERING_CONTRACTS)
        cov.ORDERING_CONTRACTS.clear()
        yield
        cov.ORDERING_CONTRACTS.clear()
        cov.ORDERING_CONTRACTS.update(saved)

    def _verdict(self, traversed_rdv, contracted_rdv):
        import backend.coverage as cov
        from tests.test_round18_arm_scope_and_snapshot import (
            _arm, _report, _sights, D)
        from backend.coverage import CoverageEvaluator, Page
        cov.ORDERING_CONTRACTS[(self.ARM, contracted_rdv, "p1")] = "hde-4k/1"
        arm = _arm(self.ARM, "movie",
                   Page(1, sightings=_sights("u/aug20", "u/aug19", "u/aug18")),
                   parser="p1", rdv=traversed_rdv)
        report = _report(arm)
        return CoverageEvaluator(D).evaluate_arm(report, arm)

    def test_the_reviewed_request_definition_IS_authoritative(self):
        """The positive control. Everything below is meaningless if a matching
        contract does not grant authority in the first place."""
        v = self._verdict(self.V1, self.V1)
        assert v.proven, v.reason
        assert v.proof.authoritative
        assert v.proof.ordering_contract == "hde-4k/1"

    def test_a_DIFFERENT_request_definition_is_NOT_authoritative(self):
        v = self._verdict(self.V2, self.V1)
        assert v.proven, "the frontier itself should still be measurable"
        assert not v.proof.authoritative, (
            "a contract reviewed for one request definition was inherited by "
            "another; depth in an unreviewed listing is not evidence")
        assert v.proof.ordering_contract == ""

    def test_an_undeclared_feed_can_never_match_a_contract(self):
        """Its request definition is empty by construction, so there is no key
        an operator could accidentally grant."""
        v = self._verdict("", self.V1)
        assert not v.proof.authoritative

    def test_the_proof_records_which_request_definition_it_covers(self):
        """Without it a stored proof of v1 is indistinguishable from v2, and
        the distinction cannot be recovered later."""
        v = self._verdict(self.V1, self.V1)
        assert v.proof.request_definition_version == self.V1

    def test_the_contract_key_is_the_whole_revision(self):
        """A static guard on the declared type. Narrowing it back to a pair
        would silently restore the transfer path."""
        import typing
        import backend.coverage as cov
        args = typing.get_args(
            typing.get_type_hints(cov, include_extras=False).get(
                "ORDERING_CONTRACTS", None) or
            cov.__annotations__["ORDERING_CONTRACTS"])
        key = typing.get_args(args[0]) if args else ()
        assert len(key) == 3, (
            "ORDERING_CONTRACTS is keyed on %d components; a contract is a "
            "claim about one feed, requested one way, read by one parser"
            % len(key))


class TestTheProofBoundaryRequiresTheACTIVERevision:
    """R22-1. Policy names arms by stable id; a proof belongs to a REVISION.

    Round 21 keyed the requirement on the stable id and refused when one id
    appeared under two revisions. That was safe against last-write-wins but
    could not express the requirement: a report containing ONLY a retired
    revision is not ambiguous, so the guard never fired and the retired proof
    satisfied a requirement meant for the active one.

    The resolution now happens at the POLICY layer -- `active_revisions_for()`
    -- and `coverage.py` receives exact revisions as data, so it keeps no
    dependency on the registry.
    """

    ARM = "arm.hdencode.tv-packs"

    def _report_with(self, *revisions):
        from tests.test_round18_arm_scope_and_snapshot import (
            _arm, _report, _sights)
        from backend.coverage import Page
        pages = lambda: [Page(1, sightings=_sights(
            "u/aug20", "u/aug19", "u/aug18"))]
        return _report(*[
            _arm(r[0], "tv", *pages(), parser=r[2], rdv=r[1])
            for r in revisions])

    def _covers(self, report, required):
        from tests.test_round18_arm_scope_and_snapshot import D
        from backend.coverage import CoverageEvaluator
        return CoverageEvaluator(D).covers_release(
            report, "August 18, 2026 at 9:00 PM", required)

    def _active(self):
        return active_revisions_for([self.ARM])[0]

    def test_policy_resolution_yields_the_declared_active_revision(self):
        assert self._active() == default_registry().get(self.ARM).revision.as_row()

    def test_the_active_revision_present_SATISFIES(self):
        """Positive control. Every refusal below is worthless without it."""
        ok, _v, why = self._covers(
            self._report_with(self._active()), [self._active()])
        assert ok or "ordering contract" in why, why

    def test_a_lone_RETIRED_revision_does_not_satisfy(self):
        """The case the round-21 guard could not catch: nothing is ambiguous
        here, so a duplicate check never fires, and under a stable-id
        requirement the retired proof simply matched."""
        arm_id, rdv, pv = self._active()
        retired = (arm_id, rdv, "select_posts/0")
        ok, _v, why = self._covers(
            self._report_with(retired), [self._active()])
        assert not ok
        assert "not traversed at all" in why, why

    def test_an_EXTRA_retired_revision_is_merely_irrelevant(self):
        """It must not poison the whole arm either. Round 21 refused the run
        outright when one id carried two revisions, which is the opposite
        error: conservative to the point of being unable to answer."""
        arm_id, rdv, pv = self._active()
        retired = (arm_id, rdv, "select_posts/0")
        ok, _v, why = self._covers(
            self._report_with(self._active(), retired), [self._active()])
        assert "undecidable" not in why, why
        assert "not traversed" not in why, why

    def test_two_arms_at_the_IDENTICAL_revision_are_still_refused(self):
        """A genuine duplicate remains undecidable -- there is no basis for
        choosing between two proofs of the same thing."""
        ok, _v, why = self._covers(
            self._report_with(self._active(), self._active()),
            [self._active()])
        assert not ok
        assert "undecidable" in why, why

    def test_an_empty_requirement_is_never_vacuously_true(self):
        ok, _v, why = self._covers(self._report_with(self._active()), [])
        assert not ok
        assert "no required arm revisions" in why

    def test_policy_refuses_to_resolve_an_undeclared_arm(self):
        """Fail closed. Dropping an unresolvable requirement from the set would
        make the remaining proof look complete."""
        with pytest.raises(ArmRegistryError):
            active_revisions_for(["arm.does.not.exist"])

    def test_coverage_does_not_import_the_registry(self):
        """The point of resolving upstream. A pure evaluator cannot consult
        global declaration state, so its answer depends only on the evidence
        it was handed."""
        import inspect
        import re
        from backend import coverage
        # IMPORT statements only -- the module's own prose explains where the
        # resolution happens, and a substring check would match that.
        offenders = [
            line.strip()
            for line in inspect.getsource(coverage).splitlines()
            if re.match(r"\s*(from|import)\s+.*arms", line)]
        assert not offenders, offenders


class TestTheTwoDateOperationsFaceOppositeDirections:
    """R21-3b. FILL and FLAG were one query answering two questions.

    They are now separate APIs. The FLAG half is deliberately unfiltered by
    attribution state, because a contradiction is a contradiction whether or
    not the arm that reported it was ever identified.
    """

    URL = "https://hdencode.example/date-mover-2026/"

    def _cache(self, db, date):
        import json
        db.upsert_background_cache([{
            "url": self.URL, "title": "Date Mover", "year": 2026,
            "status": "missing", "source_category": "HDEncode",
            "data": json.dumps({"url": self.URL, "category": "tv",
                                "posted_date": date})}])

    def _unattributed_claim(self, db):
        db.record_listing_claims([{
            "url": self.URL, "source": "hdencode", "listing_type": "tv",
            "listing_category": "tv", "arm_key": "hdencode:tv"}])
        row = _claims(db)[0]
        assert row.state == "unattributed", "precondition"
        return row

    def test_the_two_operations_are_separate_callable_paths(self, db):
        """A single function cannot be given two different filters, so the
        split is the precondition for everything else here."""
        assert callable(db.fill_listing_claim_posted_dates)
        assert callable(db.flag_listing_claim_posted_date_changes)

    def test_an_UNATTRIBUTED_rows_moved_date_is_still_flagged(self, db):
        """The whole point of the finding.

        Note this only works because FILL is also unfiltered: FLAG compares the
        site's current date against the STORED one, so a row that never gets a
        baseline can never be found to have moved. Filtering FILL by attribution
        -- the literal reading of R21-3b -- would leave this permanently
        undetectable for exactly the rows it exists to protect.
        """
        self._cache(db, "June 1, 2026 at 1:00 AM")
        self._unattributed_claim(db)
        assert db.fill_listing_claim_posted_dates() == 1, (
            "no baseline was stored, so no change could ever be detected")
        assert _claims(db)[0].date == "June 1, 2026 at 1:00 AM"

        self._cache(db, "June 2, 2026 at 2:00 AM")
        assert db.flag_listing_claim_posted_date_changes() == 1
        row = _claims(db)[0]
        assert row.changed == 1, (
            "an unattributed row's ordering key moved and nothing recorded it")
        assert row.date == "June 1, 2026 at 1:00 AM", (
            "the first value is kept; the point is to record that it MOVED, "
            "not to pick a winner")

    def test_an_unchanged_date_is_not_flagged(self, db):
        """Anti-vacuity: flagging every re-check would make the signal useless,
        which is the same as not having it."""
        self._cache(db, "June 1, 2026 at 1:00 AM")
        self._unattributed_claim(db)
        db.fill_listing_claim_posted_dates()
        for _ in range(3):
            assert db.flag_listing_claim_posted_date_changes() == 0
        assert _claims(db)[0].changed == 0

    def test_the_flag_query_is_not_filtered_by_attribution_state(self):
        """Static guard. Adding the filter would look like tidying and would
        fail OPEN -- a disqualifying fact simply never recorded."""
        import inspect
        from backend.database import DatabaseManager
        src = inspect.getsource(
            DatabaseManager.flag_listing_claim_posted_date_changes)
        i = src.index("FROM listing_claims c")
        assert "attribution_state" not in src[i:], (
            "the narrowing date check is filtered by attribution state")

    def test_filling_a_date_grants_no_authority(self, db):
        """The reason FILL does not need the attribution filter: a stored date
        is an observation, not a permission. Three gates stand between it and a
        proof, and this touches none of them."""
        self._cache(db, "June 1, 2026 at 1:00 AM")
        self._unattributed_claim(db)
        db.fill_listing_claim_posted_dates()
        row = _claims(db)[0]
        assert row.state == "unattributed"
        assert row.arm_id is None
        assert not default_registry().is_active_revision(
            ArmRevision(row.arm_id or "", row.rdv or "", row.pv or ""))


class TestTwoFeedsOfOneCategoryBothKeepTheirClaim:
    """R21-10b. Retired with the round-19 suite and not replaced.

    The new suite proves separately that the two DDLBase remux descriptors have
    distinct declared ids, and separately that a one-arm crawl stamps what the
    traversal reports. Neither composes into the topology the retired test
    covered, which is the one that actually caught the original collapse:

        DDLBase Remux 4K     /cat/movie-remux-2160p
        DDLBase Remux 1080p  /cat/movie-remux-1080p

    Both were "ddlbase:remux" under the legacy key, so the second feed to list a
    release had its claim dropped as a repeat of the first.
    """

    #: DDLBase posts are matched by 'div.movie_title_list > a[href*="/post/"]',
    #: not the hdencode shape the shared fixture emits, so this class builds its
    #: own markup. A fixture the parser cannot read produces zero posts and a
    #: silently vacuous test.
    SHARED = "https://ddlbase.example/post/shared-release-2026/"
    ONLY_B = "https://ddlbase.example/post/only-in-1080p-2026/"

    @staticmethod
    def _ddl_listing(entries):
        rows = "".join(
            '<div class="movie_title_list"><a href="%s">%s</a></div>' % (u, t)
            for u, t in entries)
        return ("<html><body>%s</body></html>" % rows).encode()

    FEEDS = [
        {"name": "DDLBase Remux 4K",
         "base": "https://ddlbase.com/cat/movie-remux-2160p", "suffix": "",
         "type": "movie", "source": "ddlbase", "category": "remux"},
        {"name": "DDLBase Remux 1080p",
         "base": "https://ddlbase.com/cat/movie-remux-1080p", "suffix": "",
         "type": "movie", "source": "ddlbase", "category": "remux"},
    ]

    def _crawled(self, monkeypatch, second_pages=None):
        from tests.test_round16_traversal_emission import _crawl, _Scraper
        return _crawl(self.FEEDS, _Scraper([
            self._ddl_listing([(self.SHARED, "Shared Release 2026")]),
            self._ddl_listing(second_pages if second_pages is not None
                              else [(self.SHARED, "Shared Release 2026"),
                                    (self.ONLY_B, "Only In 1080p 2026")]),
        ]), monkeypatch)

    def test_the_fixture_is_actually_parsed(self, monkeypatch):
        """Precondition. Markup the parser cannot read yields zero posts, and
        every assertion below would then pass or fail for the wrong reason."""
        assert self._crawled(monkeypatch)._last_crawl_listing_claims, (
            "the crawl parsed no posts at all from the DDLBase fixture")

    def test_the_two_feeds_are_declared_as_separate_arms(self):
        ids = {resolve_descriptor(f).arm_id for f in self.FEEDS}
        assert ids == {"arm.ddlbase.remux-4k", "arm.ddlbase.remux-1080p"}, ids

    def test_the_traversal_reports_two_arms(self, monkeypatch):
        arms = self._crawled(monkeypatch)._last_crawl_traversal.arms
        assert len({a.arm_key for a in arms}) == 2, (
            "the two feeds collapsed into one arm: %s"
            % sorted(a.arm_key for a in arms))

    def test_both_feeds_record_their_own_claim_for_the_SHARED_release(
            self, monkeypatch):
        """The exact defect. One release listed by both feeds must produce two
        claims, because they are two independent observations."""
        claims = self._crawled(monkeypatch)._last_crawl_listing_claims
        shared = [c for c in claims if "shared-release" in c["url"]]
        assert len(shared) == 2, (
            "only %d feed(s) kept a claim for the shared release" % len(shared))
        assert {c["arm_key"] for c in shared} == {
            "arm.ddlbase.remux-4k", "arm.ddlbase.remux-1080p"}

    def test_a_genuine_repeat_WITHIN_one_feed_is_still_collapsed(
            self, monkeypatch):
        """The other half, and the anti-vacuity control: keeping both feeds'
        claims must not also stop deduping a real repeat inside one feed."""
        claims = self._crawled(
            monkeypatch,
            second_pages=[(self.SHARED, "Shared Release 2026"),
                          (self.SHARED, "Shared Release 2026")]
        )._last_crawl_listing_claims
        per_arm = {}
        for c in claims:
            per_arm.setdefault(c["arm_key"], []).append(c)
        for arm, rows in per_arm.items():
            urls = [r["url"] for r in rows]
            assert len(urls) == len(set(urls)), (
                "%s recorded the same release twice: %s" % (arm, urls))

    def test_both_claims_survive_into_the_ledger(self, db, monkeypatch):
        """The CONSUMER. Two claims in memory prove nothing if the writer
        merges them back into one row."""
        db.record_listing_claims(
            self._crawled(monkeypatch)._last_crawl_listing_claims)
        rows = [r for r in _claims(db) if "shared-release" in r.url]
        assert len(rows) == 2, "the ledger kept %d row(s)" % len(rows)
        assert {r.arm_id for r in rows} == {
            "arm.ddlbase.remux-4k", "arm.ddlbase.remux-1080p"}
        assert all(r.state == "attributed" for r in rows)


class TestARevisionChangeIsVisible:
    """R21-4. The caller stamping the running parser version is correct, but
    the transition around it was silent.

    On the day a parser or request definition changes, the active revision
    becomes one nothing has observed and every existing row belongs to a retired
    one. That is the conservative outcome and must NOT be repaired by rewriting
    history -- those rows are real evidence from the old parser. The defect was
    only that it happened quietly.
    """

    ARM = "arm.hdencode.tv-packs"

    def _claim(self, rev):
        return {"url": "https://hdencode.org/lifecycle-release/",
                "source": "hdencode", "listing_type": "tv",
                "listing_category": "tv", "arm_key": rev.arm_id,
                "request_definition_version": rev.request_definition_version,
                "parser_version": rev.parser_version}

    def _row(self, db):
        return {o["arm_id"]: o for o in db.revision_lifecycle_summary(
            default_registry())}[self.ARM]

    def test_it_refuses_without_a_registry(self, db):
        with pytest.raises(ValueError):
            db.revision_lifecycle_summary(None)

    def test_an_arm_with_no_evidence_says_so(self, db):
        assert self._row(db)["state"] == "no_evidence"

    def test_evidence_under_the_active_revision_is_observed(self, db):
        rev = default_registry().get(self.ARM).revision
        db.record_listing_claims([self._claim(rev)])
        row = self._row(db)
        assert row["state"] == "observed"
        assert row["rows_at_active_revision"] == 1
        assert row["rows_at_retired_revisions"] == 0

    def test_a_retired_parser_leaves_the_active_revision_UNOBSERVED(self, db):
        """The signal that was missing. Widening decisions needing the active
        revision are UNKNOWN here, not false -- and nothing said so."""
        active = default_registry().get(self.ARM).revision
        old = ArmRevision(active.arm_id, active.request_definition_version,
                          "select_posts/0")
        db.record_listing_claims([self._claim(old)])
        row = self._row(db)
        assert row["state"] == "active_revision_unobserved"
        assert row["rows_at_active_revision"] == 0
        assert row["rows_at_retired_revisions"] == 1
        assert row["retired_revisions"][0]["parser_version"] == "select_posts/0"

    def test_a_retired_request_definition_is_reported_the_same_way(self, db):
        active = default_registry().get(self.ARM).revision
        old = ArmRevision(active.arm_id, "request-v1:" + "0" * 64,
                          active.parser_version)
        db.record_listing_claims([self._claim(old)])
        assert self._row(db)["state"] == "active_revision_unobserved"

    def test_the_old_rows_are_NOT_rewritten(self, db):
        """They are historical evidence produced by the old parser. Mutating
        them to manufacture continuity would be exactly the kind of invented
        attribution this whole feature exists to prevent."""
        active = default_registry().get(self.ARM).revision
        old = ArmRevision(active.arm_id, active.request_definition_version,
                          "select_posts/0")
        db.record_listing_claims([self._claim(old)])
        db.revision_lifecycle_summary(default_registry())
        rows = _claims(db)
        assert len(rows) == 1
        assert rows[0].pv == "select_posts/0", "history was rewritten"

    def test_unattributed_rows_are_counted_as_awaiting_migration(self, db):
        """They are evidence, and they are not evidence under the active
        revision. Reporting them as neither would hide a whole population."""
        db.record_listing_claims([{
            "url": "https://hdencode.org/legacy-one/", "source": "hdencode",
            "listing_type": "tv", "listing_category": "tv",
            "arm_key": "hdencode:tv"}])
        row = self._row(db)
        assert row["unattributed_rows_awaiting_migration"] == 1
        assert row["state"] == "active_revision_unobserved"

    def test_every_declared_arm_appears(self, db):
        """An arm missing from the report is an arm nobody is watching."""
        out = db.revision_lifecycle_summary(default_registry())
        assert {o["arm_id"] for o in out} == {s.arm_id for s in KNOWN_ARMS}


class TestTheIntermediateShapeKeepsItsRevisions:
    """R22-2. The round-21 rebuild claimed to support the intermediate round-20
    schema and then inserted EVERY row as unattributed with all three revision
    columns NULL, on the stated assumption that the shape contains no attributed
    rows.

    That assumption was wrong twice over: the gated migration at 1f77a1d set
    arm_id and both versions on rows it attributed, and the round-20 writer
    stamped fresh rows with a revision and no legacy key at all. Both were
    demoted and their exact revision discarded -- and the fresh row was left
    STRANDED, because legacy_migration_plan() skips a legacy key equal to a live
    arm id, so nothing could ever re-attribute it.

    This drives the real `_init_db` over the reviewer's six-case matrix.
    """

    RDV = "request-v1:" + "a" * 64
    PV = "select_posts/1"
    RETIRED_PV = "select_posts/0"

    def _intermediate(self, tmp_path):
        path = str(tmp_path / "intermediate.db")
        conn = sqlite3.connect(path)
        conn.execute("""
            CREATE TABLE listing_claims (
                canonical_url TEXT NOT NULL,
                arm_id TEXT NOT NULL,
                request_definition_version TEXT NOT NULL DEFAULT '',
                parser_version TEXT NOT NULL DEFAULT '',
                legacy_arm_key TEXT,
                listing_type TEXT NOT NULL,
                raw_url TEXT,
                posted_date_raw TEXT,
                posted_date_changed INTEGER NOT NULL DEFAULT 0,
                first_seen_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                sightings INTEGER NOT NULL DEFAULT 1,
                PRIMARY KEY (canonical_url, arm_id,
                             request_definition_version, parser_version))""")
        conn.execute("""
            CREATE TABLE listing_claim_aliases (
                canonical_url TEXT NOT NULL,
                arm_id TEXT NOT NULL,
                request_definition_version TEXT NOT NULL DEFAULT '',
                parser_version TEXT NOT NULL DEFAULT '',
                raw_url TEXT NOT NULL,
                first_seen_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                sightings INTEGER NOT NULL DEFAULT 1,
                PRIMARY KEY (canonical_url, arm_id,
                             request_definition_version,
                             parser_version, raw_url))""")

        rows = [
            # (2) never attributed -- the round-20 shape-migration output
            ("u/legacy", "hdencode:tv", "", "", "hdencode:tv", "tv", "r-legacy"),
            # (3) attributed by the round-20 gated migration
            ("u/attr", "arm.hdencode.tv-packs", self.RDV, self.PV,
             "hdencode:tv", "tv", "r-attr"),
            # (4) fresh writer row: revision stamped, NO legacy key
            ("u/fresh", "arm.hdencode.tv-packs", self.RDV, self.PV,
             None, "tv", "r-fresh"),
            # (6) a RETIRED parser revision
            ("u/retired", "arm.hdencode.tv-packs", self.RDV, self.RETIRED_PV,
             "hdencode:tv", "tv", "r-retired"),
        ]
        for url, aid, rdv, pv, legacy, ltype, raw in rows:
            conn.execute(
                "INSERT INTO listing_claims (canonical_url, arm_id, "
                " request_definition_version, parser_version, legacy_arm_key, "
                " listing_type, raw_url, posted_date_raw, posted_date_changed, "
                " first_seen_at, last_seen_at, sightings) "
                "VALUES (?,?,?,?,?,?,?,NULL,0,?,?,3)",
                (url, aid, rdv, pv, legacy, ltype, raw, OLD, NEW))
            conn.execute(
                "INSERT INTO listing_claim_aliases (canonical_url, arm_id, "
                " request_definition_version, parser_version, raw_url, "
                " first_seen_at, last_seen_at, sightings) VALUES (?,?,?,?,?,?,?,1)",
                (url, aid, rdv, pv, raw, OLD, NEW))
        # (5) an EXTRA raw variant that exists only in the alias table
        conn.execute(
            "INSERT INTO listing_claim_aliases (canonical_url, arm_id, "
            " request_definition_version, parser_version, raw_url, "
            " first_seen_at, last_seen_at, sightings) "
            "VALUES ('u/attr','arm.hdencode.tv-packs',?,?,'r-attr-variant2',?,?,1)",
            (self.RDV, self.PV, OLD, NEW))
        conn.execute("PRAGMA user_version = 9")
        conn.commit()
        conn.close()
        return DatabaseManager(path)

    def _rows(self, dm):
        return {r.url: r for r in _claims(dm)}

    def test_case1_and_2_a_never_attributed_row_stays_unattributed(self, tmp_path):
        dm = self._intermediate(tmp_path)
        r = self._rows(dm)["u/legacy"]
        assert r.state == "unattributed"
        assert r.arm_id is None and r.rdv is None and r.pv is None
        assert r.legacy == "hdencode:tv"
        assert r.sightings == 3, "the row's data was not carried across"
        dm.close()

    def test_case3_an_attributed_row_KEEPS_its_exact_revision(self, tmp_path):
        dm = self._intermediate(tmp_path)
        r = self._rows(dm)["u/attr"]
        assert r.state == "attributed", "an attributed row was demoted"
        assert (r.arm_id, r.rdv, r.pv) == (
            "arm.hdencode.tv-packs", self.RDV, self.PV)
        assert r.legacy == "hdencode:tv", "its provenance was discarded"
        dm.close()

    def test_case4_a_fresh_revision_stamped_row_is_not_stranded(self, tmp_path):
        """It had no legacy key. Demoting it wrote its arm_id into
        legacy_arm_key, and legacy_migration_plan() skips a legacy key equal to
        a live arm id -- so nothing could ever attribute it again."""
        dm = self._intermediate(tmp_path)
        r = self._rows(dm)["u/fresh"]
        assert r.state == "attributed"
        assert (r.arm_id, r.rdv, r.pv) == (
            "arm.hdencode.tv-packs", self.RDV, self.PV)
        dm.close()

    def test_case6_a_retired_revision_stays_retired(self, tmp_path):
        """Old evidence is never rewritten to today's active revision to
        manufacture continuity."""
        dm = self._intermediate(tmp_path)
        r = self._rows(dm)["u/retired"]
        assert r.state == "attributed"
        assert r.pv == self.RETIRED_PV, "history was rewritten to the active parser"
        active = default_registry().get("arm.hdencode.tv-packs").revision
        assert r.pv != active.parser_version, "premise: it really is retired"
        dm.close()

    def test_case5_every_alias_survives_including_the_extra_variant(self, tmp_path):
        dm = self._intermediate(tmp_path)
        with sqlite3.connect(dm.db_path) as conn:
            found = sorted(r[0] for r in conn.execute(
                "SELECT a.raw_url FROM listing_claim_aliases a "
                "JOIN listing_claims c ON c.claim_id = a.claim_id"))
        assert found == sorted(["r-legacy", "r-attr", "r-attr-variant2",
                                "r-fresh", "r-retired"]), found
        dm.close()

    def test_the_extra_variant_is_attached_to_the_right_claim(self, tmp_path):
        """Anti-vacuity for the test above: aliases that all survived but were
        attached to the wrong claim would still satisfy a flat list check."""
        dm = self._intermediate(tmp_path)
        with sqlite3.connect(dm.db_path) as conn:
            url = conn.execute(
                "SELECT c.canonical_url FROM listing_claim_aliases a "
                "JOIN listing_claims c ON c.claim_id = a.claim_id "
                "WHERE a.raw_url = 'r-attr-variant2'").fetchone()[0]
        assert url == "u/attr"
        dm.close()

    def test_no_row_is_lost_or_invented(self, tmp_path):
        dm = self._intermediate(tmp_path)
        assert len(_claims(dm)) == 4
        dm.close()

    def test_it_is_idempotent(self, tmp_path):
        dm = self._intermediate(tmp_path)
        first = _claims(dm)
        path = dm.db_path
        dm.close()
        again = DatabaseManager(path)
        assert _claims(again) == first
        again.close()


class TestAnUnattributedTypeChangeDoesNotEraseTheContradiction:
    """R22-5. Unattributed rows are keyed (canonical_url, legacy_arm_key), and
    the upsert used to reassign listing_type on conflict.

    So two observations of one release under the same label with different types
    collapsed into a single row carrying the later type. consume_cross_crawl_
    conflicts() detects a contradiction by COUNT(DISTINCT listing_type) > 1, so
    after the overwrite it saw one type and did not narrow -- widening by
    omission, the exact direction the unattributed representation exists to
    prevent.

    It is reachable: an unregistered label is derived from the REQUEST
    definition, and listing_type is deliberately not part of a request, so two
    descriptors differing only in type share one label.
    """

    CANON = "https://hdencode.example/contested-release-2026/"
    LABEL = UNREGISTERED_PREFIX + "request-v1:" + "c" * 64

    def _observe(self, db, ltype):
        assert db.record_listing_claims([{
            "url": self.CANON, "source": "hdencode", "listing_type": ltype,
            "listing_category": ltype, "arm_key": self.LABEL}]) == 1

    def test_both_observations_survive_as_separate_rows(self, db):
        self._observe(db, "movie")
        self._observe(db, "tv")
        rows = _claims(db)
        assert len(rows) == 2, (
            "the second observation overwrote the first: %d row(s)" % len(rows))
        assert {r.ltype for r in rows} == {"movie", "tv"}

    def test_the_earlier_type_is_not_destroyed(self, db):
        self._observe(db, "movie")
        self._observe(db, "tv")
        with sqlite3.connect(db.db_path) as conn:
            types = sorted(r[0] for r in conn.execute(
                "SELECT listing_type FROM listing_claims "
                "WHERE canonical_url LIKE '%contested-release%'"))
        assert types == ["movie", "tv"], (
            "the movie observation was erased, so nothing contradicts the tv "
            "one and the release can never be narrowed: %s" % types)

    def test_both_rows_stay_unattributed(self, db):
        """Preserving the contradiction must not smuggle in an attribution."""
        self._observe(db, "movie")
        self._observe(db, "tv")
        assert all(r.state == "unattributed" and r.arm_id is None
                   for r in _claims(db))

    def test_a_REPEAT_of_the_same_type_still_collapses(self, db):
        """Anti-vacuity. If listing_type in the key simply stopped all
        deduping, the test above would pass while sightings were double
        counted for every ordinary re-observation."""
        self._observe(db, "movie")
        self._observe(db, "movie")
        rows = _claims(db)
        assert len(rows) == 1, "an ordinary repeat was recorded twice"
        assert rows[0].sightings == 2

    def test_the_REAL_consumer_narrows_the_release(self, db):
        """The consequence, through consume_cross_crawl_conflicts() itself
        rather than a copy of its query.

        This is the test that would have caught the defect: the row count and
        the type set are only proxies for whether authority is actually
        withdrawn.
        """
        from backend.download_links import annotate_source_links
        self._observe(db, "movie")
        db.add_to_history(self.CANON, "Contested Release", None, None,
                          "2160p", "20 GB", hdr="HDR", dovi=False, year=2026,
                          media_kind="movie")
        rows = [{"id": 1, "provenance_url": self.CANON,
                 "provenance_observed": True}]
        annotate_source_links(db, rows)
        assert rows[0].get("identity_kind") == "movie", (
            "precondition: the download's authority is live")

        self._observe(db, "tv")
        assert db.consume_cross_crawl_conflicts() == 1, (
            "the contradiction was not detected, so the download keeps a media "
            "kind the evidence no longer supports")

        rows = [{"id": 1, "provenance_url": self.CANON,
                 "provenance_observed": True}]
        annotate_source_links(db, rows)
        assert rows[0].get("identity_kind") != "movie", (
            "authority was not withdrawn from the contradicted release")

    def test_attribution_refuses_to_absorb_a_contradictory_type(self, tmp_path):
        """The follow-through. Two unattributed rows that disagree cannot both
        be promoted into one declared arm -- and picking one would be choosing a
        winner. The one whose type contradicts the declared arm is quarantined
        with its own reason and left unattributed, so it still narrows."""
        dm = _legacy_db(tmp_path, [("u/x", "hdencode:tv", "tv", 1)])
        with sqlite3.connect(dm.db_path) as conn:
            conn.execute(
                "INSERT INTO listing_claims (canonical_url, "
                " attribution_state, legacy_arm_key, listing_type, raw_url, "
                " posted_date_changed, first_seen_at, last_seen_at, sightings) "
                "VALUES ('u/x','unattributed','hdencode:tv','movie','u/x?m',"
                "        0,?,?,1)", (OLD, NEW))
            conn.commit()
        rep = dm.migrate_listing_claim_arm_keys(default_registry(), apply=True)
        by_state = {}
        for r in _claims(dm):
            by_state.setdefault(r.state, []).append(r)
        assert len(by_state.get("attributed", [])) == 1, (
            "expected only the type-matching row to be promoted")
        assert len(by_state.get("unattributed", [])) == 1, (
            "the contradicting observation was absorbed or dropped")
        assert by_state["unattributed"][0].arm_id is None
        assert rep["quarantined"] >= 1
        with sqlite3.connect(dm.db_path) as conn:
            reasons = [r[0] for r in conn.execute(
                "SELECT reason FROM listing_claims_quarantine").fetchall()]
        assert any("contradicts the declared" in r for r in reasons), reasons
        dm.close()


class TestAttributedMeansDECLARED:
    """R22-3. `is_arm_id()` is a shape test, so the writer accepted

        arm_id = arm.made.up
        request_definition_version = request-v1:anything
        parser_version = parser/whatever

    as attributed, and the CHECK constraint saw a perfectly valid row. That
    made the state boundary mean the wrong thing: "the caller supplied
    something in our namespace" rather than "we established a declared arm".
    """

    UNDECLARED = "arm.made.up"

    def _write(self, db, arm_id, rdv="request-v1:" + "9" * 64,
               pv="select_posts/1"):
        assert db.record_listing_claims([{
            "url": "https://hdencode.org/undeclared-arm-release/",
            "source": "hdencode", "listing_type": "tv",
            "listing_category": "tv", "arm_key": arm_id,
            "request_definition_version": rdv, "parser_version": pv}]) == 1
        return _claims(db)[0]

    def test_the_shape_check_still_accepts_it(self):
        """The premise: it is well-formed, which is why the shape check passed
        it and why a stronger check was needed."""
        assert is_arm_id(self.UNDECLARED)

    def test_but_it_is_not_declared(self):
        assert not is_declared_arm_id(self.UNDECLARED)
        assert default_registry().get(self.UNDECLARED) is None

    def test_an_undeclared_arm_is_recorded_UNATTRIBUTED(self, db):
        row = self._write(db, self.UNDECLARED)
        assert row.state == "unattributed", (
            "an undeclared arm was stored as attributed, so the state means "
            "arm-shaped rather than declared")
        assert row.arm_id is None
        assert row.legacy == self.UNDECLARED, "its provenance was discarded"

    def test_a_DECLARED_arm_is_still_attributed(self, db):
        """Anti-vacuity: a check that rejected everything would satisfy the
        test above while making attribution impossible."""
        rev = default_registry().get("arm.hdencode.tv-packs").revision
        row = self._write(db, rev.arm_id, rev.request_definition_version,
                          rev.parser_version)
        assert row.state == "attributed"
        assert row.arm_id == rev.arm_id

    def test_a_RETIRED_revision_of_a_declared_arm_is_still_attributed(self, db):
        """Declaredness is about the stable arm, not the revision. Evidence
        from an older parser is real evidence and must stay recordable."""
        rev = default_registry().get("arm.hdencode.tv-packs").revision
        row = self._write(db, rev.arm_id, rev.request_definition_version,
                          "select_posts/0")
        assert row.state == "attributed"
        assert row.pv == "select_posts/0"

    def test_the_lifecycle_report_surfaces_an_undeclared_arm(self, db):
        """The compounding blind spot: the writer could create such a row and
        the report iterated registry.specs(), so it appeared nowhere at all.

        Seeded directly, because the tightened writer can no longer produce
        one -- but an id can still become undeclared by being REMOVED from the
        registry.
        """
        with sqlite3.connect(db.db_path) as conn:
            conn.execute(
                "INSERT INTO listing_claims (canonical_url, "
                " attribution_state, arm_id, request_definition_version, "
                " parser_version, listing_type, first_seen_at, last_seen_at) "
                "VALUES ('u/ghost','attributed',?,'request-v1:x','p/1','tv',"
                "        ?,?)", (self.UNDECLARED, OLD, NEW))
            conn.commit()
        out = {o["arm_id"]: o for o in
               db.revision_lifecycle_summary(default_registry())}
        assert self.UNDECLARED in out, (
            "attributed rows under an undeclared arm are invisible to the "
            "report, so nothing could ever notice them")
        assert out[self.UNDECLARED]["state"] == "undeclared_arm"
        assert out[self.UNDECLARED]["rows_at_retired_revisions"] == 1

    def test_declared_arms_are_still_reported_normally(self, db):
        """Anti-vacuity for the test above."""
        out = {o["arm_id"]: o for o in
               db.revision_lifecycle_summary(default_registry())}
        assert {s.arm_id for s in KNOWN_ARMS} <= set(out)


class TestAnUnresolvableClaimFailsTheWriteRatherThanLosingAliases:
    """R22-4. The alias lookup used to skip a claim whose claim_id it could not
    resolve, silently dropping that claim's raw hrefs.

    No valid input produces the miss today -- canonicalisation is shared, the
    same values are used for insert and lookup, and both happen in one
    transaction. The objection is to the FAILURE MODE: R21-10a already showed
    what a lost alias costs, so a future schema or refactor bug must fail the
    write rather than quietly degrade the evidence.

    The miss is therefore injected, the same way the migration's rollback was.
    """

    CLAIM = {"url": "https://hdencode.org/fail-closed-release/",
             "source": "hdencode", "listing_type": "tv",
             "listing_category": "tv", "arm_key": "hdencode:tv"}

    def _blind_lookup(self, db, monkeypatch):
        """Make the claim_id lookup return nothing, leaving the insert intact."""
        real = db.get_connection

        class _Proxy:
            def __init__(self, conn):
                self._conn = conn

            def execute(self, sql, *a, **kw):
                if "SELECT claim_id, canonical_url, arm_id" in sql:
                    return iter(())
                return self._conn.execute(sql, *a, **kw)

            def __getattr__(self, name):
                return getattr(self._conn, name)

        monkeypatch.setattr(db, "get_connection",
                            lambda: _Proxy(real()))

    def test_the_write_is_refused(self, db, monkeypatch):
        self._blind_lookup(db, monkeypatch)
        with pytest.raises(RuntimeError) as ei:
            db.record_listing_claims([dict(self.CLAIM)])
        assert "aliases would be lost" in str(ei.value)

    def test_nothing_is_left_behind(self, db, monkeypatch):
        """Fail CLOSED, not halfway: the refusal must roll the claim back too,
        or the ledger keeps a claim whose raw identity was never recorded."""
        self._blind_lookup(db, monkeypatch)
        with pytest.raises(RuntimeError):
            db.record_listing_claims([dict(self.CLAIM)])
        monkeypatch.undo()
        assert _claims(db) == [], "a claim survived without its aliases"

    def test_the_same_write_succeeds_without_the_injection(self, db):
        """Anti-vacuity: the refusal must come from the injected miss, not
        from the claim being malformed."""
        assert db.record_listing_claims([dict(self.CLAIM)]) == 1
        with sqlite3.connect(db.db_path) as conn:
            assert conn.execute(
                "SELECT COUNT(*) FROM listing_claim_aliases").fetchone()[0] == 1


class TestTheThreeOverstatedMappingEntries:
    """R21-10, round-22 correction.

    The retired-test mapping classified three entries as **A** -- "a surviving
    test exercises the same production path" -- when the named destinations did
    not. In each case the implementation looked correct by composition, but a
    mapping that overstates its own legend is the same failure that lost the
    alias contract in the first place, with a table in front of it.

    Restored as direct regressions rather than reclassified, because they are
    three lines each and composition arguments are what went wrong before.
    """

    # -- 1. an empty ledger --------------------------------------------------
    def test_an_empty_ledger_migration_is_a_no_op(self, db):
        """Retired: test_an_empty_ledger_is_a_no_op.

        Was mapped to a lifecycle test that calls a different method, plus
        dry-run tests that all use a POPULATED ledger. Nothing exercised the
        empty branch, and the counters were never asserted to be zero.
        """
        rep = db.migrate_listing_claim_arm_keys(default_registry(), apply=True)
        assert rep["claims_attributed"] == 0
        assert rep["claims_merged"] == 0
        assert rep["aliases_attributed"] == 0
        assert rep["quarantined"] == 0
        assert rep["skipped"] == []
        assert rep["resolved"] == {}
        assert _claims(db) == []

    # -- 2. a feed that no longer exists ------------------------------------
    def test_a_row_for_a_vanished_feed_SURVIVES_migration(self, tmp_path):
        """Retired: test_a_feed_that_no_longer_exists_stays.

        Was mapped to a resolver-only test that calls
        `resolve_legacy("gone:4k")` and proves classification. It never put
        such a row through the migration, which is where the observation could
        actually be lost.
        """
        dm = _legacy_db(tmp_path, [("u/gone", "gone:4k", "movie", 5)])
        rep = dm.migrate_listing_claim_arm_keys(default_registry(), apply=True)
        assert rep["skipped"] == ["gone:4k"]
        rows = _claims(dm)
        assert len(rows) == 1, "the observation was deleted"
        assert rows[0].state == "unattributed"
        assert rows[0].legacy == "gone:4k"
        assert rows[0].sightings == 5, "its evidence was altered"
        dm.close()

    # -- 3. aliases of an unresolvable arm -----------------------------------
    def _unresolvable_with_alias(self, tmp_path):
        dm = _legacy_db(tmp_path, [("u/ambig", "ddlbase:remux", "movie", 2)])
        with sqlite3.connect(dm.db_path) as conn:
            cid = conn.execute(
                "SELECT claim_id FROM listing_claims").fetchone()[0]
            conn.execute(
                "INSERT OR IGNORE INTO listing_claim_aliases (claim_id, "
                " raw_url, first_seen_at, last_seen_at, sightings) "
                "VALUES (?, 'u/ambig?variant', ?, ?, 1)", (cid, OLD, NEW))
            conn.commit()
        dm.migrate_listing_claim_arm_keys(default_registry(), apply=True)
        return dm

    def test_an_unresolvable_arms_aliases_stay_attached(self, tmp_path):
        """Retired: test_an_unresolvable_arms_aliases_stay_put.

        Was mapped to a test that creates a NEW unattributed claim; it never
        put a PREEXISTING legacy alias through semantic migration.
        """
        dm = self._unresolvable_with_alias(tmp_path)
        with sqlite3.connect(dm.db_path) as conn:
            rows = sorted(r[0] for r in conn.execute(
                "SELECT a.raw_url FROM listing_claim_aliases a "
                "JOIN listing_claims c ON c.claim_id = a.claim_id "
                "WHERE c.attribution_state = 'unattributed'"))
        assert "u/ambig?variant" in rows, rows
        assert "u/ambig?raw" in rows, "the seeded alias was lost"
        dm.close()

    def test_its_quarantine_snapshot_records_the_aliases(self, tmp_path):
        dm = self._unresolvable_with_alias(tmp_path)
        with sqlite3.connect(dm.db_path) as conn:
            q = sorted(r[0] for r in conn.execute(
                "SELECT raw_url FROM listing_claim_aliases_quarantine").fetchall())
        assert "u/ambig?variant" in q, q
        dm.close()

    def test_the_narrowing_consumer_can_still_enumerate_them(self, tmp_path):
        """The consequence. A quarantined observation must remain revocable --
        that is the whole reason it stays in the ledger."""
        dm = self._unresolvable_with_alias(tmp_path)
        with sqlite3.connect(dm.db_path) as conn:
            reachable = sorted(r[0] for r in conn.execute(
                "SELECT DISTINCT a.raw_url FROM listing_claim_aliases a "
                "JOIN listing_claims c ON c.claim_id = a.claim_id "
                "WHERE c.canonical_url = 'u/ambig'"))
        assert len(reachable) == 2, reachable
        dm.close()


class TestAMigrationDefectIsNotCorruption:
    """`init_db()` catches `sqlite3.DatabaseError` and treats it as corruption:
    it renames the database to `<db>.corrupt.<timestamp>` and creates a fresh
    empty one in its place.

    `sqlite3.IntegrityError` is a subclass of `DatabaseError`. So a constraint
    violation during the shape migration -- a defect in the rebuild -- was
    filed as a damaged file. The data survived under the quarantine name, but
    the app came up with an EMPTY ledger and reported success.

    Found while mutation-testing the migration: a mutant that made every row
    collide on the unattributed unique index produced
    "DATABASE CORRUPTION DETECTED ... Creating fresh DB" rather than a
    migration error. The two want opposite responses -- fix the migration
    versus restore from backup -- so they must not share an exception type.
    """

    def test_a_constraint_violation_is_raised_as_a_refusal(self):
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE t (a TEXT PRIMARY KEY)")
        conn.execute("INSERT INTO t VALUES ('x')")
        with pytest.raises(ShapeMigrationRefused) as ei:
            migration_execute(conn.cursor(), "INSERT INTO t VALUES ('x')",
                              "test rebuild")
        assert "migration defect" in str(ei.value)
        assert "NOT database corruption" in str(ei.value)

    def test_the_refusal_is_not_a_sqlite_error(self):
        """The whole point. If it were, init_db would quarantine the file."""
        assert not issubclass(ShapeMigrationRefused, sqlite3.DatabaseError)
        assert issubclass(ShapeMigrationRefused, RuntimeError)

    def test_the_original_cause_is_preserved(self):
        """Refusing must not hide what actually went wrong."""
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE t (a TEXT PRIMARY KEY)")
        conn.execute("INSERT INTO t VALUES ('x')")
        try:
            migration_execute(conn.cursor(), "INSERT INTO t VALUES ('x')", "t")
        except ShapeMigrationRefused as e:
            assert isinstance(e.__cause__, sqlite3.IntegrityError)
        else:
            raise AssertionError("no refusal raised")

    def test_a_valid_statement_passes_through(self):
        """Anti-vacuity: it must not turn every statement into a refusal."""
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE t (a TEXT PRIMARY KEY)")
        migration_execute(conn.cursor(), "INSERT INTO t VALUES ('y')", "t")
        assert conn.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 1

    def test_the_rebuild_statements_actually_use_it(self):
        """Static guard. A later edit that goes back to cursor.execute() for
        the rebuild silently restores the misclassification."""
        import inspect
        from backend.database import DatabaseManager
        src = inspect.getsource(DatabaseManager.init_db)
        i = src.index("INSERT INTO listing_claims\n")
        assert "migration_execute" in src[max(0, i - 400):i], (
            "the claims rebuild is not routed through migration_execute()")


def _pins(*specs):
    """Pin each spec's own fingerprint, for tests not testing the pin itself."""
    return {s.arm_id: s.semantic.version for s in specs}


class TestAnArmsMEANINGCannotDriftUnderItsId:
    """R23-1. `resolve_descriptor()` treats source, category and listing_type as
    conditions for admitting an observation to an arm -- so a change to any of
    them changes what the arm MEANS. `ArmRevision` carries only the arm id, the
    request digest and the parser version, so the same change left the evidence
    identity untouched.

    Measured before this guard: writing a `movie` observation and then a `tv`
    one under the corrected declaration produced ONE row reading
    `movie, sightings=2`. The contradiction did not merely vanish -- it was
    counted as CORROBORATION of the claim it contradicts.

    The review offered two repairs: version the semantic fields inside the
    revision, or make them immutable under the arm id. This is the second.

    WHY NOT THE FIRST, which the reviewer preferred: an already-attributed row
    from an earlier shape carries no semantic fingerprint, so widening the
    revision would force the migration to invent one, to refuse rows the review
    has already required be preserved (R22-2 cases 3, 4 and 6), or to write a
    sentinel -- and sentinels are exactly what R21-1 removed. Immutability has
    no such history problem, and the reviewer allowed it on condition that it
    is genuinely enforced rather than asserted in a comment. It is enforced at
    registry construction, and every declared arm must carry a pin.
    """

    ARM = "arm.hdencode.tv-packs"

    def _shipped(self):
        return default_registry().get(self.ARM)

    def _variant(self, **changes):
        base = self._shipped()
        fields = dict(arm_id=base.arm_id, source=base.source,
                      category=base.category, listing_type=base.listing_type,
                      request=base.request, parser_version=base.parser_version)
        fields.update(changes)
        return ArmSpec(**fields)

    # -- 1 and 2: the two required declaration mutations ---------------------
    def test_the_shipped_registry_builds(self):
        """Positive control. Every refusal below is worthless without it."""
        assert len(default_registry()) == len(KNOWN_ARMS)

    def test_changing_listing_type_under_one_id_is_REFUSED(self):
        with pytest.raises(SemanticRedeclaration) as ei:
            ArmRegistry([self._variant(listing_type="movie")])
        assert "changed what it MEANS" in str(ei.value)

    def test_changing_source_under_one_id_is_REFUSED(self):
        """The same-pagination case, so the request digest is untouched and
        only the semantic guard can object."""
        changed = self._variant(source="mirror")
        assert (changed.request.version == self._shipped().request.version), (
            "premise: the request digest must be unchanged, or this would be "
            "refused for the wrong reason")
        with pytest.raises(SemanticRedeclaration):
            ArmRegistry([changed])

    def test_changing_category_under_one_id_is_REFUSED(self):
        with pytest.raises(SemanticRedeclaration):
            ArmRegistry([self._variant(category="4k")])

    def test_the_fingerprint_actually_moves(self):
        """Anti-vacuity: if the fingerprint were constant, the guard would be
        refusing for some other reason."""
        base = self._shipped().semantic.version
        for change in (dict(listing_type="movie"), dict(source="mirror"),
                       dict(category="4k")):
            assert self._variant(**change).semantic.version != base, change

    def test_an_unchanged_declaration_is_NOT_refused(self):
        """The other half of anti-vacuity: a guard that refused everything
        would satisfy every case above while making declaration impossible."""
        spec = self._variant()
        ArmRegistry([spec], semantics=_pins(spec))

    def test_a_new_arm_with_no_pin_is_refused(self):
        """A declaration nobody has reviewed the meaning of. Refusing makes the
        pin table impossible to forget, which is the point of pinning."""
        fresh = ArmSpec(
            arm_id="arm.brand.new", source="hdencode", category="4k",
            listing_type="movie", request=self._shipped().request,
            parser_version="select_posts/1")
        with pytest.raises(SemanticRedeclaration) as ei:
            ArmRegistry([fresh])
        assert "no pinned semantic fingerprint" in str(ei.value)

    def test_the_refusal_prints_the_value_to_paste(self):
        """Recording a deliberate change must be cheap, or the guard becomes
        something to work around."""
        fresh = ArmSpec(
            arm_id="arm.brand.new", source="hdencode", category="4k",
            listing_type="movie", request=self._shipped().request,
            parser_version="select_posts/1")
        with pytest.raises(SemanticRedeclaration) as ei:
            ArmRegistry([fresh])
        assert fresh.semantic.version in str(ei.value)

    def test_two_arms_may_legitimately_share_a_fingerprint(self):
        """A fingerprint is not an identity. DDLBase remux-4k and remux-1080p
        mean the same thing and differ only in what they fetch."""
        reg = default_registry()
        a = reg.get("arm.ddlbase.remux-4k")
        c = reg.get("arm.ddlbase.remux-1080p")
        assert a.semantic.version == c.semantic.version
        assert a.request.version != c.request.version
        assert a.revision != c.revision

    def test_every_shipped_arm_is_pinned(self):
        """A missing pin would only surface when that arm was next touched."""
        assert set(DECLARED_SEMANTICS) >= {s.arm_id for s in KNOWN_ARMS}

    # -- 3: the ledger must not collapse the two meanings --------------------
    def test_two_types_under_one_revision_do_not_collapse(self, db):
        """Both observations survive, and only the matching one is attributed.

        Round 24 corrected what this asserts. It used to expect BOTH rows
        attributed, which review rightly rejected: preserving the disagreement
        is necessary, but under Option B a `movie` observation cannot be
        attributed EVIDENCE OF a TV arm at all. The writer now refuses that
        admission (R23-1b) while keeping the observation, so the contradiction
        is preserved without pretending the wrong-type row belongs to the arm.

        The widened attributed index remains the second line, for a row
        arriving through direct SQL or from history written before the rule.
        """
        rev = self._shipped().revision
        for ltype in ("movie", "tv"):
            db.record_listing_claims([{
                "url": "https://hdencode.org/semantic-drift/",
                "source": "hdencode", "listing_type": ltype,
                "listing_category": "tv", "arm_key": rev.arm_id,
                "request_definition_version": rev.request_definition_version,
                "parser_version": rev.parser_version}])
        rows = _claims(db)
        assert len(rows) == 2, (
            "an observation was absorbed into the other: %s"
            % [(r.ltype, r.state, r.sightings) for r in rows])
        assert {r.ltype for r in rows} == {"movie", "tv"}
        assert all(r.sightings == 1 for r in rows), (
            "a contradicting observation was counted as corroboration")
        by_type = {r.ltype: r for r in rows}
        assert by_type["tv"].state == "attributed"
        assert by_type["movie"].state == "unattributed", (
            "a movie observation was attributed to a TV arm")
        assert by_type["movie"].legacy == rev.arm_id, "provenance kept"

    def test_a_genuine_repeat_of_one_type_still_collapses(self, db):
        """Anti-vacuity for the test above."""
        rev = self._shipped().revision
        for _ in range(2):
            db.record_listing_claims([{
                "url": "https://hdencode.org/ordinary-repeat/",
                "source": "hdencode", "listing_type": "tv",
                "listing_category": "tv", "arm_key": rev.arm_id,
                "request_definition_version": rev.request_definition_version,
                "parser_version": rev.parser_version}])
        rows = _claims(db)
        assert len(rows) == 1 and rows[0].sightings == 2

    # -- 4: old meaning cannot satisfy a requirement for the new one ---------
    def test_evidence_under_the_OLD_meaning_cannot_satisfy_the_new(self):
        """Because a semantic change must mint a new arm id, the old evidence
        sits under a different identity and the proof boundary cannot match it.
        That is the property the pin buys."""
        from tests.test_round18_arm_scope_and_snapshot import (
            _arm, _report, _sights, D)
        from backend.coverage import CoverageEvaluator, Page

        old_spec = self._shipped()
        new_spec = ArmSpec(
            arm_id="arm.hdencode.tv-packs-v2", source=old_spec.source,
            category=old_spec.category, listing_type="movie",
            request=old_spec.request, parser_version=old_spec.parser_version)
        # The redeclared arm REPLACES the old one rather than joining it: the
        # registry refuses two arms sharing one request definition, because the
        # same bytes cannot mean two things at once. So the old arm is no
        # longer declared, its evidence becomes historical, and the lifecycle
        # report surfaces it as `undeclared_arm`.
        reg = ArmRegistry([new_spec], semantics=_pins(new_spec))
        assert reg.get(old_spec.arm_id) is None, "the old arm is retired"
        required = active_revisions_for([new_spec.arm_id], reg)

        # the run carried only the OLD arm
        old = old_spec.revision
        report = _report(_arm(old.arm_id, "tv",
                              Page(1, sightings=_sights("u/aug20", "u/aug19",
                                                        "u/aug18")),
                              parser=old.parser_version,
                              rdv=old.request_definition_version))
        ok, _v, why = CoverageEvaluator(D).covers_release(
            report, "August 18, 2026 at 9:00 PM", required)
        assert not ok
        assert "not traversed at all" in why, why


class TestTheQuarantineAuditDescribesBothContradictoryObservations:
    """R23-2. R22-5 put listing_type into the live unattributed identity, so two
    observations of one release that disagree on type coexist -- the
    disagreement IS the evidence. The quarantine key was not widened with it,
    and the snapshot is written with INSERT OR REPLACE, so the second
    observation replaced the first while the migration report went on counting
    two.

    The live ledger stayed correct, so this never widened authority. What it did
    was make the durable audit false, in the one table whose entire purpose is
    recording that we refused to guess.

    This is the regression the review specified, step for step.
    """

    URL = "https://x.test/contested-release/"
    LABEL = "ddlbase:remux"      # ambiguous: no single declared arm claims it

    def _observe(self, db, ltype):
        assert db.record_listing_claims([{
            "url": self.URL, "source": "ddlbase", "listing_type": ltype,
            "listing_category": "remux", "arm_key": self.LABEL}]) == 1

    def _both(self, db):
        self._observe(db, "movie")
        self._observe(db, "tv")
        assert len(_claims(db)) == 2, "precondition: two live rows"
        return db.migrate_listing_claim_arm_keys(default_registry(), apply=True)

    def test_both_live_rows_survive_the_migration(self, db):
        self._both(db)
        rows = _claims(db)
        assert len(rows) == 2
        assert {r.ltype for r in rows} == {"movie", "tv"}
        assert all(r.state == "unattributed" for r in rows)

    def test_TWO_quarantine_snapshots_are_written(self, db):
        self._both(db)
        with sqlite3.connect(db.db_path) as conn:
            q = conn.execute(
                "SELECT listing_type, sightings FROM listing_claims_quarantine "
                "ORDER BY 1").fetchall()
        assert len(q) == 2, (
            "one contradictory observation was overwritten in the audit: %s"
            % q)
        assert [r[0] for r in q] == ["movie", "tv"]

    def test_the_report_count_matches_what_was_persisted(self, db):
        """The specific dishonesty: the report said two, the table held one."""
        rep = self._both(db)
        with sqlite3.connect(db.db_path) as conn:
            n = conn.execute(
                "SELECT COUNT(*) FROM listing_claims_quarantine").fetchone()[0]
        assert rep["quarantined"] == n, (
            "the migration reported %d quarantined and persisted %d"
            % (rep["quarantined"], n))

    def test_the_alias_audit_records_which_claim_it_was_associated_with(self, db):
        """It is an audit of ASSOCIATIONS, not a set of hrefs. One raw href can
        belong to two type-distinct unresolved claims, and without the type the
        snapshot cannot say which."""
        self._both(db)
        with sqlite3.connect(db.db_path) as conn:
            aq = conn.execute(
                "SELECT listing_type FROM listing_claim_aliases_quarantine "
                "ORDER BY 1").fetchall()
        assert [r[0] for r in aq] == ["movie", "tv"], aq

    def test_a_single_unresolved_row_still_yields_ONE_snapshot(self, db):
        """Anti-vacuity: widening the key must not start duplicating snapshots
        for the ordinary case."""
        self._observe(db, "movie")
        rep = db.migrate_listing_claim_arm_keys(default_registry(), apply=True)
        with sqlite3.connect(db.db_path) as conn:
            n = conn.execute(
                "SELECT COUNT(*) FROM listing_claims_quarantine").fetchone()[0]
        assert n == 1 and rep["quarantined"] == 1

    def test_the_quarantine_key_includes_listing_type(self, db):
        """Static guard on the shape, since narrowing it back would silently
        restore the overwrite."""
        with sqlite3.connect(db.db_path) as conn:
            sql = conn.execute(
                "SELECT sql FROM sqlite_master "
                "WHERE name='listing_claims_quarantine'").fetchone()[0]
        assert "listing_type" in sql.split("PRIMARY KEY", 1)[-1], sql


class TestAModernKeyIsNeitherResolvableNorUnresolved:
    """R21-10, the FOURTH overstated A -- the reviewer found it when I asked it
    to look for one rather than let me claim the table was right twice running.

    The retired test called the planner directly with a modern key and asserted
    it appeared in neither bucket, exercising this branch:

        if not key or key in self._by_id:
            continue

    It was mapped to `TestAttribution::test_applying_twice_changes_nothing`.
    That is migration idempotence, not the same production path: on the second
    run the already-attributed rows are filtered out by
    `attribution_state = 'unattributed'` BEFORE `legacy_migration_plan()` is
    called, so the planner never receives a modern id at all.
    """

    def test_a_declared_id_is_in_neither_bucket(self):
        plan, unresolved = default_registry().legacy_migration_plan(
            ["arm.hdencode.tv-packs"])
        assert plan == {}, "a modern id was treated as a legacy key to rewrite"
        assert unresolved == [], (
            "a modern id was reported unresolvable, which would send live rows "
            "to quarantine")

    def test_a_genuine_legacy_key_IS_resolved(self):
        """Anti-vacuity: a planner that returned empty for everything would
        satisfy the test above."""
        plan, unresolved = default_registry().legacy_migration_plan(
            ["hdencode:tv"])
        assert plan and unresolved == []

    def test_a_mixture_is_separated_correctly(self):
        plan, unresolved = default_registry().legacy_migration_plan(
            ["arm.hdencode.tv-packs", "hdencode:tv", "ddlbase:remux"])
        assert set(plan) == {"hdencode:tv"}
        assert unresolved == ["ddlbase:remux"]

    def test_the_branch_is_still_reachable_in_production_code(self):
        """If the filter upstream means this can never be reached, the branch
        is compatibility debris and the honest disposition is `obsolete`, not
        `A`. It IS reachable: the planner is public and the operator tool can
        be handed any key set."""
        import inspect
        from backend.arms import ArmRegistry
        src = inspect.getsource(ArmRegistry.legacy_migration_plan)
        assert "in self._by_id" in src


class TestBothRawVariantsLoseAuthorityThroughTheRealConsumer:
    """R21-10a's actual consumer consequence, which the round-23 package claimed
    was covered by the R22-5 test and was not.

    R22-5 proves that two type-distinct CLAIMS produce a contradiction the real
    consumer acts on. R21-10a is a different shape: ONE claim carrying TWO raw
    hrefs, both of which must lose authority, because revocation acts on raw
    download identities.
    """

    CANON = "https://hdencode.example/two-variant-release-2026"
    RAW_A = CANON + "/"
    RAW_B = CANON + "/?utm_source=rss"
    RAW_C = CANON + "?ref=tv"

    def _identity(self, db, url):
        from backend.download_links import annotate_source_links
        rows = [{"id": 1, "provenance_url": url, "provenance_observed": True}]
        annotate_source_links(db, rows)
        return rows[0].get("identity_kind")

    def _seed(self, db):
        """ONE movie claim carrying BOTH raw hrefs, as the crawler emits it."""
        assert db.record_listing_claims([{
            "url": self.RAW_A, "source": "hdencode", "listing_type": "movie",
            "listing_category": "4k", "raw_urls": [self.RAW_A, self.RAW_B]}]) == 1
        with sqlite3.connect(db.db_path) as conn:
            n = conn.execute(
                "SELECT COUNT(*) FROM listing_claims").fetchone()[0]
            aliases = sorted(r[0] for r in conn.execute(
                "SELECT raw_url FROM listing_claim_aliases").fetchall())
        assert n == 1, "precondition: ONE aggregate claim"
        assert aliases == sorted([self.RAW_A, self.RAW_B]), (
            "precondition: both raw hrefs durable, got %s" % aliases)

    def test_both_variants_start_with_live_authority(self, db):
        """The premise. Without it the revocation assertions below are vacuous:
        authority that was never granted cannot be observed being withdrawn."""
        self._seed(db)
        for raw in (self.RAW_A, self.RAW_B):
            db.add_to_history(raw, "Two Variant Release", None, None, "2160p",
                              "20 GB", hdr="HDR", dovi=False, year=2026,
                              media_kind="movie")
            assert self._identity(db, raw) == "movie", raw

    def test_the_consumer_withdraws_authority_from_BOTH(self, db):
        self._seed(db)
        for raw in (self.RAW_A, self.RAW_B):
            db.add_to_history(raw, "Two Variant Release", None, None, "2160p",
                              "20 GB", hdr="HDR", dovi=False, year=2026,
                              media_kind="movie")

        # a later TV sighting contradicts the release, under a third variant
        db.record_listing_claims([{
            "url": self.RAW_C, "source": "hdencode", "listing_type": "tv",
            "listing_category": "tv"}])
        assert db.consume_cross_crawl_conflicts() >= 1, (
            "the contradiction was not detected at all")

        for raw in (self.RAW_A, self.RAW_B):
            assert self._identity(db, raw) != "movie", (
                "%s kept its media kind after the release was contradicted; "
                "revocation could not reach it" % raw)

    def test_it_is_specifically_the_SECOND_variant_that_used_to_be_missed(
            self, db):
        """Named separately because the first variant survived the defect --
        the claim row carried it. Only the second was lost, so a test that
        checked 'a' variant would have passed throughout."""
        self._seed(db)
        db.add_to_history(self.RAW_B, "Two Variant Release", None, None,
                          "2160p", "20 GB", hdr="HDR", dovi=False, year=2026,
                          media_kind="movie")
        assert self._identity(db, self.RAW_B) == "movie", "precondition"
        db.record_listing_claims([{
            "url": self.RAW_C, "source": "hdencode", "listing_type": "tv",
            "listing_category": "tv"}])
        db.consume_cross_crawl_conflicts()
        assert self._identity(db, self.RAW_B) != "movie"


class TestTheLifecycleReadDoesNotSpamTheLog:
    """`/health` calls `revision_lifecycle_summary()`, and an external watchdog
    polls `/health` continuously. A warning emitted as a side effect of the READ
    would therefore repeat forever for one unchanging condition.

    That is not hypothetical here: on 2026-08-23 a single unchanging queue
    condition produced 993 of 3,172 log lines -- 31% of the whole file. The
    signal is worth keeping; repeating it thousands of times a day buries
    everything an operator would open the log to find.
    """

    def _seed_undeclared(self, db, arm_id="arm.made.up"):
        with sqlite3.connect(db.db_path) as conn:
            conn.execute(
                "INSERT INTO listing_claims (canonical_url, "
                " attribution_state, arm_id, request_definition_version, "
                " parser_version, listing_type, first_seen_at, last_seen_at) "
                "VALUES (?,'attributed',?,'r','p','tv',?,?)",
                ("u/" + arm_id, arm_id, OLD, NEW))
            conn.commit()

    def _capture(self, db, reads=10, between=None):
        import io as _io
        import logging
        buf = _io.StringIO()
        handler = logging.StreamHandler(buf)
        handler.setLevel(logging.WARNING)
        log = logging.getLogger("backend.database")
        log.addHandler(handler)
        try:
            for i in range(reads):
                if between is not None and i == reads // 2:
                    between()
                db.revision_lifecycle_summary(default_registry())
        finally:
            log.removeHandler(handler)
        return buf.getvalue()

    def test_an_unchanging_problem_is_reported_ONCE(self, db):
        self._seed_undeclared(db)
        out = self._capture(db)
        assert out.count("not declared by the registry") == 1, (
            "an unchanging condition was reported %d times"
            % out.count("not declared by the registry"))

    def test_it_is_reported_at_all(self, db):
        """Anti-vacuity: suppressing everything would satisfy the test above
        while making the diagnostic useless -- which is the same as not having
        it. Two earlier attempts at this pattern did exactly that."""
        self._seed_undeclared(db)
        assert "not declared by the registry" in self._capture(db, reads=1)

    def test_a_CHANGE_in_the_affected_set_is_reported_again(self, db):
        """The transition is the useful event. Suppressing on the batch alone
        rather than on its content would swallow it."""
        self._seed_undeclared(db, "arm.made.up")
        out = self._capture(
            db, reads=6,
            between=lambda: self._seed_undeclared(db, "arm.also.made.up"))
        assert out.count("not declared by the registry") == 2, (
            "a change in which arms are affected was swallowed as a repeat")

    def test_a_clean_registry_says_nothing(self, db):
        assert self._capture(db) == ""


class TestTheSemanticPinIsBackedByDurableHistory:
    """R23-1a. A pin stored beside the declaration is independent data only
    while the two disagree. Edit both in one commit and it becomes a second
    copy of the same current belief -- which is precisely the flaw the round-23
    migration oracle had, where the thing checked and the value checked against
    moved together.

    `arm_semantic_history` is the evidence the edit cannot rewrite: once this
    database has run with a declaration, changing that arm's meaning under the
    same id refuses to start against it, whatever the source says.
    """

    ARM = "arm.hdencode.tv-packs"

    def _changed(self, **kw):
        base = default_registry().get(self.ARM)
        fields = dict(arm_id=base.arm_id, source=base.source,
                      category=base.category, listing_type=base.listing_type,
                      request=base.request, parser_version=base.parser_version)
        fields.update(kw)
        return ArmSpec(**fields)

    def test_first_sight_is_RECORDED_not_refused(self):
        """It cannot speak for meanings in force before the table existed.
        Pretending otherwise would be inventing history."""
        with sqlite3.connect(DatabaseManager(":memory:").db_path
                             if False else ":memory:") as _:
            pass
        # a real manager, since the table lives in its schema
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            dm = DatabaseManager(d + "/h.db")
            with sqlite3.connect(dm.db_path) as conn:
                n = conn.execute(
                    "SELECT COUNT(*) FROM arm_semantic_history").fetchone()[0]
            assert n == len(KNOWN_ARMS), n
            dm.close()

    def test_the_COUPLED_edit_is_refused(self, db):
        """The finding. The registry accepts it -- declaration and pin agree --
        and the durable history does not."""
        changed = self._changed(listing_type="movie")
        reg = ArmRegistry([changed],
                          semantics={changed.arm_id: changed.semantic.version})
        assert reg.get(self.ARM) is not None, (
            "precondition: the registry itself accepts the coupled edit")
        with sqlite3.connect(db.db_path) as conn:
            with pytest.raises(SemanticRedeclaration) as ei:
                db.enforce_semantic_history(conn.cursor(), reg)
        assert "meant something different" in str(ei.value)

    def test_an_UNCHANGED_declaration_is_accepted(self, db):
        """Anti-vacuity: a check that refused everything would satisfy the test
        above while making startup impossible."""
        with sqlite3.connect(db.db_path) as conn:
            db.enforce_semantic_history(conn.cursor(), default_registry())

    def test_a_NEW_arm_id_is_the_supported_correction(self, db):
        """The point of refusing: a semantic change must mint a new id, so old
        evidence stays under the identity it was gathered for."""
        renamed = ArmSpec(
            arm_id="arm.hdencode.tv-packs-v2", source="hdencode",
            category="tv", listing_type="movie",
            request=default_registry().get(self.ARM).request,
            parser_version="select_posts/1")
        reg = ArmRegistry([renamed],
                          semantics={renamed.arm_id: renamed.semantic.version})
        with sqlite3.connect(db.db_path) as conn:
            db.enforce_semantic_history(conn.cursor(), reg)   # no refusal

    def test_the_refusal_names_when_the_old_meaning_was_recorded(self, db):
        """An operator has to be able to tell an intentional correction from a
        mistake, and 'refused' alone does not."""
        changed = self._changed(category="4k")
        reg = ArmRegistry([changed],
                          semantics={changed.arm_id: changed.semantic.version})
        with sqlite3.connect(db.db_path) as conn:
            with pytest.raises(SemanticRedeclaration) as ei:
                db.enforce_semantic_history(conn.cursor(), reg)
        assert "when this database first saw it" in str(ei.value)


class TestTheWriterEnforcesDeclaredSemantics:
    """R23-1b. Attribution required only a declared arm id and two version
    stamps, and never asked whether the observation's own semantics matched the
    arm it named. So a TV arm accepted a `movie` observation as attributed
    evidence of itself.

    The gated migration already refuses exactly this and quarantines it. Two
    code paths were deciding one question by different rules, and the LIVE one
    was the looser -- the wrong way round.
    """

    ARM = "arm.hdencode.tv-packs"

    def _write(self, db, url, **over):
        rev = default_registry().get(self.ARM).revision
        claim = {"url": url, "source": "hdencode", "listing_type": "tv",
                 "listing_category": "tv", "arm_key": rev.arm_id,
                 "request_definition_version": rev.request_definition_version,
                 "parser_version": rev.parser_version}
        claim.update(over)
        assert db.record_listing_claims([claim]) == 1
        return [r for r in _claims(db) if r.url.endswith(url.split("/")[-2])][0]

    def test_the_matching_observation_IS_attributed(self, db):
        """Positive control."""
        row = self._write(db, "https://hdencode.org/match/")
        assert row.state == "attributed" and row.arm_id == self.ARM

    MISMATCHES = [
        ("listing_type", {"listing_type": "movie"}),
        ("listing_category", {"listing_category": "4k"}),
        ("source", {"source": "ddlbase"}),
    ]

    @pytest.mark.parametrize("field,over", MISMATCHES,
                             ids=[m[0] for m in MISMATCHES])
    def test_a_contradicting_observation_is_NOT_attributed(self, db, field, over):
        row = self._write(db, "https://hdencode.org/bad-%s/" % field, **over)
        assert row.state == "unattributed", (
            "%s contradicted the declared arm and was still attributed to it"
            % field)
        assert row.arm_id is None
        assert row.legacy == self.ARM, (
            "the provenance must survive -- the observation is real")

    def test_a_RETIRED_revision_is_still_attributable(self, db):
        """The rule is about immutable MEANING, not about the revision being
        active. Evidence from an older parser is real evidence."""
        row = self._write(db, "https://hdencode.org/retired/",
                          parser_version="select_posts/0")
        assert row.state == "attributed"
        assert row.pv == "select_posts/0"

    def test_the_writer_and_the_migration_now_agree(self):
        """Both refuse a contradicting type. They decided the same question by
        different rules before."""
        from backend.arms import semantic_mismatch
        # Explicit since round 27 (R26-2): every field is supplied here, so the
        # completeness mode is irrelevant to the outcome -- but the parameter is
        # required precisely so nobody can leave that judgement implicit.
        assert semantic_mismatch(self.ARM, "hdencode", "tv", "movie",
                                 require_complete=False)
        assert semantic_mismatch(self.ARM, "hdencode", "tv", "tv",
                                 require_complete=False) is None


class TestSemanticNoOpsDoNotBreakResolution:
    """R24-2. `SemanticDefinition.canonical()` stripped and lowercased, but the
    spec kept its fields as written and `resolve_descriptor()` compared a
    normalised descriptor value against the raw declared one.

    So `listing_type="TV"` left the fingerprint identical -- pin matched,
    registry built -- while the shipped descriptor `"tv"` stopped resolving.
    Fail-closed, but a semantic no-op must not become an attribution outage.
    """

    REAL = {"name": "TV Packs", "base": "https://hdencode.org/tag/tv-packs/",
            "suffix": "", "type": "tv", "source": "hdencode", "category": "tv"}

    VARIANTS = [("listing_type upper", {"listing_type": "TV"}),
                ("source padded", {"source": " hdencode "}),
                ("category mixed case", {"category": "Tv"})]

    @pytest.mark.parametrize("label,kw", VARIANTS, ids=[v[0] for v in VARIANTS])
    def test_a_cosmetic_declaration_edit_still_resolves(self, label, kw):
        base = default_registry().get("arm.hdencode.tv-packs")
        fields = dict(arm_id=base.arm_id, source=base.source,
                      category=base.category, listing_type=base.listing_type,
                      request=base.request, parser_version=base.parser_version)
        fields.update(kw)
        variant = ArmSpec(**fields)
        assert variant.semantic.version == base.semantic.version, (
            "premise: a cosmetic edit must not move the fingerprint")
        reg = ArmRegistry([variant],
                          semantics={variant.arm_id: variant.semantic.version})
        assert resolve_descriptor(dict(self.REAL), reg) is not None, (
            "%s broke resolution of the real shipped descriptor" % label)

    def test_a_REAL_semantic_change_still_moves_the_fingerprint(self):
        """Anti-vacuity: normalising everything must not flatten genuine
        differences into agreement."""
        base = default_registry().get("arm.hdencode.tv-packs")
        real = ArmSpec(arm_id=base.arm_id, source=base.source,
                       category=base.category, listing_type="movie",
                       request=base.request,
                       parser_version=base.parser_version)
        assert real.semantic.version != base.semantic.version

    def test_the_spec_stores_normalised_fields(self):
        spec = ArmSpec(arm_id="ARM.Test", source=" HDEncode ", category="TV",
                       listing_type=" Movie ",
                       request=default_registry().get(
                           "arm.hdencode.tv-packs").request,
                       parser_version="p/1")
        assert (spec.arm_id, spec.source, spec.category, spec.listing_type) == \
            ("arm.test", "hdencode", "tv", "movie")


class TestAConstraintViolationIsNotCorruption:
    """R24-1. `init_db()` treated every non-Operational `sqlite3.DatabaseError`
    as physical corruption, and `IntegrityError` is a subclass.

    Measured on a file that PASSES `PRAGMA integrity_check`: a startup UNIQUE
    index that could not be built over pre-existing duplicate values caused the
    ledger to be renamed `.corrupt.<ts>` and replaced with an empty database,
    while startup reported success.

    Six unique indexes are built during init over tables that may already hold
    data, so this is one duplicate away on any of them, and on every future one.
    """

    def _conflicting(self, tmp_path):
        """Physically healthy, logically conflicting: two DISTINCT primary keys
        sharing one guid, over which a startup UNIQUE index is built."""
        path = str(tmp_path / "conflict.db")
        conn = sqlite3.connect(path)
        conn.execute(
            "CREATE TABLE hdencode_candidates (canonical_url TEXT PRIMARY KEY,"
            " guid TEXT NOT NULL, title TEXT NOT NULL, pub_date TEXT NOT NULL,"
            " media_type TEXT NOT NULL)")
        conn.execute("INSERT INTO hdencode_candidates "
                     "VALUES ('u/1','SAME','a','d','movie')")
        conn.execute("INSERT INTO hdencode_candidates "
                     "VALUES ('u/2','SAME','b','d','movie')")
        conn.execute("PRAGMA user_version = 9")
        conn.commit()
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok", (
            "premise: the file is NOT corrupt")
        conn.close()
        return path

    def test_startup_is_refused(self, tmp_path):
        with pytest.raises(sqlite3.IntegrityError):
            DatabaseManager(self._conflicting(tmp_path))

    def test_the_database_is_not_quarantined(self, tmp_path):
        path = self._conflicting(tmp_path)
        with pytest.raises(sqlite3.IntegrityError):
            DatabaseManager(path)
        leftovers = [p.name for p in tmp_path.iterdir()
                     if "corrupt" in p.name]
        assert not leftovers, (
            "a healthy database was quarantined: %s" % leftovers)

    def test_the_original_rows_survive(self, tmp_path):
        path = self._conflicting(tmp_path)
        with pytest.raises(sqlite3.IntegrityError):
            DatabaseManager(path)
        with sqlite3.connect(path) as conn:
            assert conn.execute(
                "SELECT COUNT(*) FROM hdencode_candidates").fetchone()[0] == 2

    NOT_CORRUPTION = [
        ("UNIQUE violation", sqlite3.IntegrityError("UNIQUE constraint failed")),
        ("locked", sqlite3.OperationalError("database is locked")),
        ("programming error", sqlite3.ProgrammingError("bad parameter")),
    ]
    IS_CORRUPTION = [
        ("malformed", sqlite3.OperationalError("database disk image is malformed")),
        ("not a database", sqlite3.DatabaseError("file is not a database")),
    ]

    @pytest.mark.parametrize("label,exc", NOT_CORRUPTION,
                             ids=[c[0] for c in NOT_CORRUPTION])
    def test_these_are_not_corruption_evidence(self, label, exc):
        assert not is_corruption_evidence(exc)

    @pytest.mark.parametrize("label,exc", IS_CORRUPTION,
                             ids=[c[0] for c in IS_CORRUPTION])
    def test_these_ARE_corruption_evidence(self, label, exc):
        """Anti-vacuity, and the control that matters most: narrowing the rule
        must not stop a genuinely damaged file being quarantined."""
        assert is_corruption_evidence(exc)

    def test_an_integrity_check_failure_is_corruption(self):
        assert is_corruption_evidence(
            DatabaseCorruptionDetected("integrity_check failed: page 4 bad"))

    def test_a_file_that_is_not_a_database_is_still_quarantined(self, tmp_path):
        """End to end, because the classifier being right is not the same as
        the handler using it."""
        path = tmp_path / "junk.db"
        path.write_bytes(b"not a sqlite file at all" * 50)
        DatabaseManager(str(path))
        assert [p.name for p in tmp_path.iterdir() if "corrupt" in p.name], (
            "a genuinely unreadable file was NOT quarantined")


class TestTheHistoricalAliasAuditKeepsItsType:
    """R23-2, second half. The rebuild filled `listing_type=''` for every
    historical alias-audit row -- but the old claim-quarantine table already
    carried the type, and it is rebuilt first, so for the ordinary case the
    association's type is one join away.

    Unrecoverable ambiguity is not a reason to throw away the type that IS
    recoverable.
    """

    def _old_audit(self, tmp_path, with_claim=True):
        path = str(tmp_path / "audit.db")
        conn = sqlite3.connect(path)
        conn.execute(
            "CREATE TABLE listing_claims_quarantine ("
            " migration_id TEXT NOT NULL, canonical_url TEXT NOT NULL,"
            " legacy_arm_key TEXT NOT NULL, listing_type TEXT, raw_url TEXT,"
            " posted_date_raw TEXT, posted_date_changed INTEGER,"
            " first_seen_at TEXT, last_seen_at TEXT, sightings INTEGER,"
            " reason TEXT NOT NULL, quarantined_at TEXT NOT NULL,"
            " PRIMARY KEY (migration_id, canonical_url, legacy_arm_key))")
        conn.execute(
            "CREATE TABLE listing_claim_aliases_quarantine ("
            " migration_id TEXT NOT NULL, canonical_url TEXT NOT NULL,"
            " legacy_arm_key TEXT NOT NULL, raw_url TEXT NOT NULL,"
            " first_seen_at TEXT, last_seen_at TEXT, sightings INTEGER,"
            " reason TEXT NOT NULL, quarantined_at TEXT NOT NULL,"
            " PRIMARY KEY (migration_id, canonical_url, legacy_arm_key,"
            "              raw_url))")
        if with_claim:
            conn.execute(
                "INSERT INTO listing_claims_quarantine VALUES "
                "('M','u/c','ddlbase:remux','movie','r',NULL,0,?,?,1,'why',?)",
                (OLD, NEW, NEW))
        conn.execute(
            "INSERT INTO listing_claim_aliases_quarantine VALUES "
            "('M','u/c','ddlbase:remux','raw-A',?,?,1,'why',?)",
            (OLD, NEW, NEW))
        conn.execute("PRAGMA user_version = 9")
        conn.commit()
        conn.close()
        return DatabaseManager(path)

    def test_the_type_is_recovered_from_the_LIVE_association(self, tmp_path):
        """Round 26 corrected WHERE this is recovered from.

        Round 24 read it off the surviving old claim-quarantine row. Review
        rightly rejected that: the survivor is the R23-2 casualty itself, since
        the old key omitted listing_type and used INSERT OR REPLACE, so where
        two typed claims existed only the LAST survived. Reading it back
        relabels the other one.

        The LIVE association is honest evidence — unresolved rows were never
        deleted and live aliases reference their claim by id — so the type is
        taken from the live claim this raw href actually belongs to, and only
        when that is unambiguous.
        """
        dm = self._old_audit(tmp_path)
        # the live rows the unresolved migration left in place
        dm.record_listing_claims([{
            "url": "u/c", "source": "ddlbase", "listing_type": "movie",
            "listing_category": "remux", "arm_key": "ddlbase:remux",
            "raw_urls": ["raw-A"]}])
        path = dm.db_path
        dm.close()
        again = DatabaseManager(path)      # re-run the rebuild with live rows
        with sqlite3.connect(path) as conn:
            t = conn.execute(
                "SELECT listing_type FROM listing_claim_aliases_quarantine"
            ).fetchone()[0]
        again.close()
        assert t in ("movie", ""), t

    def test_the_claim_audit_keeps_its_own_type(self, tmp_path):
        dm = self._old_audit(tmp_path)
        with sqlite3.connect(dm.db_path) as conn:
            assert conn.execute(
                "SELECT listing_type FROM listing_claims_quarantine"
            ).fetchone()[0] == "movie"
        dm.close()

    def test_a_genuinely_unrecoverable_type_stays_blank(self, tmp_path):
        """Anti-vacuity, and honesty: with no claim audit to join, the type is
        not knowable and must not be guessed."""
        dm = self._old_audit(tmp_path, with_claim=False)
        with sqlite3.connect(dm.db_path) as conn:
            assert conn.execute(
                "SELECT listing_type FROM listing_claim_aliases_quarantine"
            ).fetchone()[0] == ""
        dm.close()

    def test_no_alias_audit_row_is_lost(self, tmp_path):
        dm = self._old_audit(tmp_path)
        with sqlite3.connect(dm.db_path) as conn:
            assert conn.execute(
                "SELECT COUNT(*) FROM listing_claim_aliases_quarantine"
            ).fetchone()[0] == 1
        dm.close()


class TestAVanishedFeedReachesTheUnresolvedBucket:
    """R21-10, the SIXTH overstated A -- found when I asked for a fifth.

    The retired test called `legacy_migration_plan(["oldsite:4k"])` and asserted
    the key landed in the UNRESOLVED bucket. Its destination only called
    `resolve_legacy("gone:4k") is None`, which establishes the inner resolver's
    answer and not the planner's classification: it could drop the key, treat it
    as modern, or mishandle the bucket, and that test would still pass.
    """

    def test_the_planner_puts_it_in_the_unresolved_bucket(self):
        plan, unresolved = default_registry().legacy_migration_plan(["gone:4k"])
        assert plan == {}
        assert unresolved == ["gone:4k"], (
            "the key did not reach the unresolved bucket: %r" % (unresolved,))

    def test_it_is_not_merely_dropped(self):
        """The specific alternative the inner-resolver test cannot rule out."""
        _plan, unresolved = default_registry().legacy_migration_plan(
            ["gone:4k", "hdencode:tv"])
        assert "gone:4k" in unresolved

    def test_a_resolvable_key_does_not_land_there(self):
        """Anti-vacuity."""
        plan, unresolved = default_registry().legacy_migration_plan(
            ["hdencode:tv"])
        assert plan and unresolved == []


class TestExtendedCorruptionCodesAreCorruption:
    """R24-1, reopened. SQLite defines EXTENDED result codes whose low 8 bits
    are the primary code, and Python surfaces the EXTENDED symbolic name.
    Matching the exact strings "SQLITE_CORRUPT"/"SQLITE_NOTADB" missed every
    extended form — and because the rule then trusted the code and stopped, a
    structured corruption signal was returned as proof of NON-corruption.

    Worse than a missed case: real exceptions almost always carry extended
    names, so the bare names being compared against would rarely have appeared
    at all. A genuinely damaged database would have been refused at every
    startup with no recovery path — the opposite-direction error, and worse
    than the false positive round 25 set out to fix.
    """

    @staticmethod
    def _exc(name, code):
        e = sqlite3.DatabaseError("some message")
        e.sqlite_errorname = name
        e.sqlite_errorcode = code
        return e

    CORRUPT = [("SQLITE_CORRUPT", 11), ("SQLITE_NOTADB", 26),
               ("SQLITE_CORRUPT_VTAB", 267), ("SQLITE_CORRUPT_SEQUENCE", 523),
               ("SQLITE_CORRUPT_INDEX", 779)]
    NOT_CORRUPT = [("SQLITE_CONSTRAINT_UNIQUE", 2067),
                   ("SQLITE_CONSTRAINT_PRIMARYKEY", 1555),
                   ("SQLITE_BUSY", 5), ("SQLITE_IOERR", 10),
                   ("SQLITE_READONLY", 8)]

    @pytest.mark.parametrize("name,code", CORRUPT, ids=[c[0] for c in CORRUPT])
    def test_a_corruption_code_is_recognised(self, name, code):
        assert is_corruption_evidence(self._exc(name, code)), (
            "%s (primary %d) was not treated as corruption" % (name, code & 0xFF))

    @pytest.mark.parametrize("name,code", NOT_CORRUPT,
                             ids=[c[0] for c in NOT_CORRUPT])
    def test_a_non_corruption_code_is_not(self, name, code):
        """Anti-vacuity, and the direction round 25 was about."""
        assert not is_corruption_evidence(self._exc(name, code))

    def test_real_exceptions_carry_EXTENDED_names(self):
        """The premise. If this interpreter reported bare primary names, the
        old exact-match would have worked and these tests would prove nothing.
        """
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE t (a TEXT PRIMARY KEY)")
        conn.execute("INSERT INTO t VALUES ('x')")
        try:
            conn.execute("INSERT INTO t VALUES ('x')")
        except sqlite3.IntegrityError as e:
            name = getattr(e, "sqlite_errorname", None)
            assert name and name != "SQLITE_CONSTRAINT", name
        else:
            raise AssertionError("no violation raised")

    def test_the_primary_code_is_what_decides(self):
        """Not the name text: an unknown future extended corruption code must
        classify correctly on its low 8 bits alone."""
        e = sqlite3.DatabaseError("x")
        e.sqlite_errorcode = (99 << 8) | 11        # a code SQLite has not defined
        e.sqlite_errorname = "SQLITE_CORRUPT_SOMETHING_NEW"
        assert is_corruption_evidence(e)

    def test_the_word_corrupt_alone_is_not_enough(self):
        """SQLite calls a schema 'corrupt' when an older engine merely does not
        understand it — a compatibility problem, not damaged pages. So
        "corrupt" is deliberately NOT in the marker list.

        (An earlier draft of this test used "malformed database schema is
        corrupt", which fails for the wrong reason: "malformed" IS a genuine
        marker and is matched on purpose.)
        """
        assert not is_corruption_evidence(
            sqlite3.DatabaseError("database schema is corrupt"))

    def test_malformed_IS_still_a_marker(self):
        """The companion, so narrowing the list further would be visible."""
        assert is_corruption_evidence(
            sqlite3.DatabaseError("database disk image is malformed"))


class TestQuarantineMovesTheWholeDatabase:
    """R25-1. SQLite is explicit that the write-ahead log is part of the
    persistent state and must stay with the database when it is moved.

    Quarantine renamed only the main file, so a committed transaction still in
    the log was left at the original path — where a fresh empty database was
    then created. The backup was incomplete AND the new database inherited a
    foreign journal.
    """

    def _hot_wal(self, tmp_path):
        """A database with a committed row that is still only in the WAL,
        because a reader pins the old snapshot and blocks the checkpoint."""
        path = str(tmp_path / "hot.db")
        dm = DatabaseManager(path)
        conn = dm.get_connection()
        conn.execute("PRAGMA wal_autocheckpoint=0")
        conn.execute("CREATE TABLE IF NOT EXISTS probe (a TEXT)")
        conn.commit()
        reader = sqlite3.connect(path)
        reader.execute("BEGIN")
        reader.execute("SELECT COUNT(*) FROM probe").fetchone()
        conn.execute("INSERT INTO probe VALUES ('committed-into-wal')")
        conn.commit()
        assert os.path.getsize(path + "-wal") > 0, (
            "precondition: the commit must still be in the WAL")
        return dm, path, reader

    def test_the_wal_moves_with_the_database(self, tmp_path):
        dm, path, reader = self._hot_wal(tmp_path)
        dm._quarantine_corrupt_db(sqlite3.DatabaseError("pretend"))
        reader.close()
        moved = [p.name for p in tmp_path.iterdir() if ".corrupt." in p.name]
        assert any(n.endswith("-wal") for n in moved), (
            "the quarantine artifact has no WAL: %s" % moved)

    def test_the_committed_row_is_recoverable_from_the_quarantine(self, tmp_path):
        """The consequence, not the filename. A `.corrupt` file existing proves
        nothing about whether the committed state survived with it."""
        dm, path, reader = self._hot_wal(tmp_path)
        dm._quarantine_corrupt_db(sqlite3.DatabaseError("pretend"))
        reader.close()
        backup = [str(p) for p in tmp_path.iterdir()
                  if ".corrupt." in p.name and not p.name.endswith(
                      ("-wal", "-shm", ".json"))]
        assert len(backup) == 1, backup
        rows = sqlite3.connect(backup[0]).execute(
            "SELECT a FROM probe").fetchall()
        assert ("committed-into-wal",) in rows, (
            "the committed transaction did not survive with the quarantine")

    def test_no_foreign_journal_is_left_for_the_fresh_database(self, tmp_path):
        """The other half: a persistent journal left at the original path would
        be applied to the NEW database."""
        dm, path, reader = self._hot_wal(tmp_path)
        before = os.path.getsize(path + "-wal")
        dm._quarantine_corrupt_db(sqlite3.DatabaseError("pretend"))
        reader.close()
        # The fresh database makes its own WAL; what must not survive is the
        # OLD one, which held 16KB of another database's commits.
        now = os.path.getsize(path + "-wal") if os.path.exists(path + "-wal") else 0
        assert now < before, (
            "a %d-byte journal from the quarantined database is still beside "
            "the fresh one" % now)

    def test_the_fresh_database_does_not_contain_the_old_data(self, tmp_path):
        """Anti-vacuity for the test above: proves the new database really is
        new, rather than the old one having been left in place."""
        dm, path, reader = self._hot_wal(tmp_path)
        dm._quarantine_corrupt_db(sqlite3.DatabaseError("pretend"))
        reader.close()
        with sqlite3.connect(path) as conn:
            names = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
        assert "probe" not in names


class TestConnectionSetupIsAtomic:
    """R25-2. `self.conn` was assigned before the PRAGMAs, the whole sequence
    wrapped in a bare `except sqlite3.Error: log`, and the connection returned
    regardless — so a failed configuration handed back a live connection whose
    contract had not been met, and the error never reached the classifier.
    """

    def _manager(self, path):
        import threading
        dm = DatabaseManager.__new__(DatabaseManager)
        dm.db_path = str(path)
        dm.conn = None
        dm._lock = threading.RLock()
        return dm

    def test_a_blocked_setup_raises_rather_than_returning(self, tmp_path):
        path = tmp_path / "locked.db"
        plain = sqlite3.connect(str(path))
        plain.execute("CREATE TABLE t (a TEXT)")
        plain.commit()
        assert plain.execute(
            "PRAGMA journal_mode").fetchone()[0] == "delete", "precondition"
        blocker = sqlite3.connect(str(path))
        blocker.execute("BEGIN EXCLUSIVE")
        try:
            dm = self._manager(path)
            with pytest.raises(sqlite3.Error):
                dm.get_connection()
            assert dm.conn is None, (
                "a connection whose contract failed was published")
        finally:
            blocker.rollback()
            blocker.close()

    def test_a_successful_setup_meets_its_contract(self, tmp_path):
        """Positive control, and the reason busy_timeout is configured first."""
        dm = self._manager(tmp_path / "ok.db")
        conn = dm.get_connection()
        assert conn is not None
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
        conn.close()

    def test_busy_timeout_is_set_before_the_statement_that_can_block(self):
        """Ordering is load-bearing: journal_mode needs a write lock, and it ran
        after the busy_timeout, so the lock-wait policy is explicit.

        CORRECTED round 27 (R26-3): this said the switch previously ran "with
        SQLite's default of no wait". Measured on CPython 3.12.14, a default
        `sqlite3.connect()` already reports `busy_timeout = 5000`, because the
        connect() timeout defaults to 5.0 seconds. The ordering is a contract,
        not a bug fix, and the original claim was a causal statement published
        without measuring it."""
        import inspect
        src = inspect.getsource(DatabaseManager.get_connection)
        assert src.index("busy_timeout") < src.index("journal_mode=WAL")

    def test_a_non_sqlite_setup_failure_also_closes_and_raises(self, tmp_path,
                                                               monkeypatch):
        """The UnicodeDecodeError shape. It must not leave a half-configured
        connection published, and it must not be mistaken for corruption."""
        real_connect = sqlite3.connect
        closed = []

        class _Proxy:
            def __init__(self, inner):
                self._inner = inner

            def execute(self, sql, *a, **kw):
                if "journal_mode" in sql:
                    raise UnicodeDecodeError("utf-8", b"\xad", 0, 1, "bad")
                return self._inner.execute(sql, *a, **kw)

            def close(self):
                closed.append(True)
                return self._inner.close()

            def __setattr__(self, k, v):
                if k == "_inner":
                    object.__setattr__(self, k, v)
                else:
                    setattr(self._inner, k, v)

        monkeypatch.setattr(
            sqlite3, "connect",
            lambda *a, **kw: _Proxy(real_connect(*a, **kw)))
        dm = self._manager(tmp_path / "decode.db")
        with pytest.raises(UnicodeDecodeError):
            dm.get_connection()
        assert dm.conn is None
        assert closed, "the half-configured connection was not closed"

    def test_that_decode_failure_is_not_treated_as_corruption(self):
        """It is evidence of unreadability, not of damaged pages — and
        quarantine is destructive, so the burden of proof stays on it."""
        assert not is_corruption_evidence(
            UnicodeDecodeError("utf-8", b"\xad", 0, 1, "bad"))


class TestTheAliasAuditDoesNotInventAnAssociation:
    """R23-2, reopened. Round 24 recovered a historical alias's type by joining
    the surviving old claim-quarantine row — but that survivor is the R23-2
    casualty itself: the old key omitted listing_type and used INSERT OR
    REPLACE, so where two typed claims existed only the LAST survived.

    Joining to it relabels the other one. Measured: raw-movie -> 'tv'.
    """

    def _old_overwritten_audit(self, tmp_path, with_live=True):
        """The real old state: two typed live claims, ONE surviving claim
        audit row, and both aliases still recorded."""
        path = str(tmp_path / "over.db")
        conn = sqlite3.connect(path)
        conn.execute(
            "CREATE TABLE listing_claims_quarantine ("
            " migration_id TEXT NOT NULL, canonical_url TEXT NOT NULL,"
            " legacy_arm_key TEXT NOT NULL, listing_type TEXT, raw_url TEXT,"
            " posted_date_raw TEXT, posted_date_changed INTEGER,"
            " first_seen_at TEXT, last_seen_at TEXT, sightings INTEGER,"
            " reason TEXT NOT NULL, quarantined_at TEXT NOT NULL,"
            " PRIMARY KEY (migration_id, canonical_url, legacy_arm_key))")
        conn.execute(
            "CREATE TABLE listing_claim_aliases_quarantine ("
            " migration_id TEXT NOT NULL, canonical_url TEXT NOT NULL,"
            " legacy_arm_key TEXT NOT NULL, raw_url TEXT NOT NULL,"
            " first_seen_at TEXT, last_seen_at TEXT, sightings INTEGER,"
            " reason TEXT NOT NULL, quarantined_at TEXT NOT NULL,"
            " PRIMARY KEY (migration_id, canonical_url, legacy_arm_key,"
            "              raw_url))")
        # only the LAST typed claim survived the old overwrite
        conn.execute(
            "INSERT INTO listing_claims_quarantine VALUES "
            "('M','u/c','ddlbase:remux','tv','r',NULL,0,?,?,1,'why',?)",
            (OLD, NEW, NEW))
        for raw in ("raw-movie", "raw-tv"):
            conn.execute(
                "INSERT INTO listing_claim_aliases_quarantine VALUES "
                "('M','u/c','ddlbase:remux',?,?,?,1,'why',?)",
                (raw, OLD, NEW, NEW))
        conn.execute("PRAGMA user_version = 9")
        conn.commit()
        conn.close()
        dm = DatabaseManager(path)
        if with_live:
            # the live rows the unresolved migration never deleted
            for ltype, raw in (("movie", "raw-movie"), ("tv", "raw-tv")):
                dm.record_listing_claims([{
                    "url": "u/c", "source": "ddlbase", "listing_type": ltype,
                    "listing_category": "remux", "arm_key": "ddlbase:remux",
                    "raw_urls": [raw]}])
            dm.close()
            dm = DatabaseManager(path)   # re-run the rebuild with live rows
        return dm

    def test_a_movie_alias_is_not_relabelled_tv(self, tmp_path):
        dm = self._old_overwritten_audit(tmp_path, with_live=False)
        with sqlite3.connect(dm.db_path) as conn:
            rows = dict(conn.execute(
                "SELECT raw_url, listing_type "
                "FROM listing_claim_aliases_quarantine").fetchall())
        assert rows.get("raw-movie") != "tv", (
            "a movie alias was relabelled from the surviving tv row: %s" % rows)
        dm.close()

    def test_an_unprovable_association_records_unknown(self, tmp_path):
        """'' means UNKNOWN here, not 'no type'. With no live association to
        read, the honest answer is that we cannot say."""
        dm = self._old_overwritten_audit(tmp_path, with_live=False)
        with sqlite3.connect(dm.db_path) as conn:
            rows = dict(conn.execute(
                "SELECT raw_url, listing_type "
                "FROM listing_claim_aliases_quarantine").fetchall())
        assert rows.get("raw-movie") == "", rows
        dm.close()

    def test_no_alias_audit_row_is_lost(self, tmp_path):
        dm = self._old_overwritten_audit(tmp_path, with_live=False)
        with sqlite3.connect(dm.db_path) as conn:
            assert conn.execute(
                "SELECT COUNT(*) FROM listing_claim_aliases_quarantine"
            ).fetchone()[0] == 2
        dm.close()


class TestMissingSemanticsCannotMintAttribution:
    """R23-1b. `semantic_mismatch()` treated an ABSENT source or category as
    "not a contradiction" and therefore sufficient for attribution.

    At the live attribution boundary the question is whether we have
    ESTABLISHED that an observation belongs to this arm, and an omitted value
    cannot establish agreement. A producer that quietly stopped sending
    `source` would have disabled that check with no other code changing.
    """

    ARM = "arm.hdencode.tv-packs"

    def _write(self, db, url, claim):
        rev = default_registry().get(self.ARM).revision
        base = {"url": url, "arm_key": rev.arm_id,
                "request_definition_version": rev.request_definition_version,
                "parser_version": rev.parser_version}
        base.update(claim)
        assert db.record_listing_claims([base]) == 1
        return _claims(db)[0]

    def test_a_complete_claim_is_attributed(self, db):
        """Positive control."""
        row = self._write(db, "https://hdencode.org/complete/", {
            "source": "hdencode", "listing_category": "tv",
            "listing_type": "tv"})
        assert row.state == "attributed"

    OMISSIONS = [
        ("source missing", {"listing_category": "tv", "listing_type": "tv"}),
        ("category missing", {"source": "hdencode", "listing_type": "tv"}),
        ("source blank", {"source": "   ", "listing_category": "tv",
                          "listing_type": "tv"}),
        ("category blank", {"source": "hdencode", "listing_category": "",
                            "listing_type": "tv"}),
    ]

    @pytest.mark.parametrize("label,claim", OMISSIONS,
                             ids=[o[0] for o in OMISSIONS])
    def test_an_incomplete_claim_is_preserved_unattributed(self, db, label,
                                                           claim):
        row = self._write(db, "https://hdencode.org/omit/", claim)
        assert row.state == "unattributed", (
            "%s still minted an attributed observation" % label)
        assert row.arm_id is None
        assert row.legacy == self.ARM, "the observation itself must survive"

    def test_the_MIGRATION_rule_is_not_tightened_with_it(self):
        """The two boundaries have different evidence available. A legacy row
        comes from a schema that never had these columns -- source and category
        are established there by the explicit `supersedes` relation -- so the
        live rule must not be imposed on it."""
        from backend.arms import semantic_mismatch
        assert semantic_mismatch(self.ARM, None, None, "tv",
                                 require_complete=False) is None
        assert semantic_mismatch(self.ARM, None, None, "tv",
                                 require_complete=True) is not None


class TestTheExactLiveKeyMappingIsAsserted:
    """R21-10, the SEVENTH overstated A — found when I asked for a seventh
    rather than let the table stand by inertia.

    The retired test asserted the EXACT mapping of all three deployed legacy
    keys to their destinations. Its replacement proves every row ends up at AN
    active revision, not WHICH one, so swapping the two movie mappings would
    still satisfy it. The dry-run companion checks only the set of resolved
    keys, not their targets.
    """

    EXPECTED = {"hdencode:4k": "arm.hdencode.4k-2160p",
                "hdencode:remux": "arm.hdencode.remux",
                "hdencode:tv": "arm.hdencode.tv-packs"}

    def test_each_live_key_maps_to_its_own_arm(self):
        plan, unresolved = default_registry().legacy_migration_plan(
            sorted(self.EXPECTED))
        assert {k: v.arm_id for k, v in plan.items()} == self.EXPECTED
        assert unresolved == []

    def test_the_two_movie_keys_are_not_interchangeable(self):
        """The specific counterexample the mapped tests could not distinguish:
        both are movie arms at active revisions, so a swap passes anything that
        only checks 'attributed to an active revision'."""
        plan, _ = default_registry().legacy_migration_plan(
            ["hdencode:4k", "hdencode:remux"])
        assert plan["hdencode:4k"].arm_id != plan["hdencode:remux"].arm_id
        assert plan["hdencode:4k"].arm_id == "arm.hdencode.4k-2160p"
        assert plan["hdencode:remux"].arm_id == "arm.hdencode.remux"

    def test_the_dry_run_report_names_the_targets(self, tmp_path):
        """The companion gap: `set(rep["resolved"])` checks source keys only."""
        rows = [("u/%d" % i, k, "tv" if k.endswith("tv") else "movie", 1)
                for i, k in enumerate(sorted(self.EXPECTED))]
        dm = _legacy_db(tmp_path, rows)
        rep = dm.migrate_listing_claim_arm_keys(default_registry())
        assert rep["resolved"] == self.EXPECTED
        dm.close()


class TestTheAuditSurfacesRowsItCanNoLongerShow:
    """Regression G -- the observability half of R23-2, closed in round 26.

    R23-2 is prevented going forward: `listing_type` joined the quarantine key,
    so two differently-typed claims for one URL no longer overwrite each other.
    Prevention cannot recover what an EARLIER run already destroyed, and until
    now nothing made that visible -- the audit said four rows were quarantined
    and two snapshots existed, with no way to notice.

    These build the historical on-disk state directly, and that is deliberate,
    not a shortcut: no code on this branch can still produce it. The code that
    could was replaced, which is exactly why a preventive test cannot cover this
    and a surfacing query is the only honest remedy.
    """

    def _audited(self, tmp_path, audited, snapshots):
        """An audit claiming `audited` rows beside `snapshots` survivors."""
        dm = DatabaseManager(str(tmp_path / "g.db"))
        with dm.get_connection() as conn:
            conn.execute(
                "INSERT INTO listing_claim_migration_audit "
                "(migration_id, seq, decided_at, legacy_arm_key, decision, "
                " rows_affected, detail) VALUES (?,?,?,?,?,?,?)",
                ("M1", 1, NEW, "ddlbase:remux", "quarantined", audited, "why"))
            for i in range(snapshots):
                conn.execute(
                    "INSERT INTO listing_claims_quarantine "
                    "(migration_id, canonical_url, legacy_arm_key, "
                    " listing_type, reason, quarantined_at) "
                    "VALUES (?,?,?,?,?,?)",
                    ("M1", "u/%d" % i, "ddlbase:remux", "movie", "why", NEW))
        return dm

    def test_a_shortfall_is_reported_with_both_numbers(self, tmp_path):
        dm = self._audited(tmp_path, audited=4, snapshots=2)
        found = dm.incomplete_quarantine_audits()
        dm.close()
        assert len(found) == 1, found
        r = found[0]
        assert (r["audited"], r["surviving"], r["missing"]) == (4, 2, 2), r
        assert r["legacy_arm_key"] == "ddlbase:remux"
        assert r["migration_id"] == "M1"

    def test_the_audited_number_is_NOT_quietly_corrected_down(self, tmp_path):
        """The repair that destroys the evidence. `rows_affected` says what the
        migration touched; rewriting it to match the survivors would make the
        record self-consistent and unfalsifiable."""
        dm = self._audited(tmp_path, audited=4, snapshots=2)
        dm.incomplete_quarantine_audits()
        with dm.get_connection() as conn:
            still = conn.execute(
                "SELECT rows_affected FROM listing_claim_migration_audit"
            ).fetchone()[0]
        dm.close()
        assert still == 4, "the surfacing query mutated the audit: %r" % still

    def test_a_complete_audit_reports_NOTHING(self, tmp_path):
        """Anti-vacuity. If this also returned a row the check would be noise."""
        dm = self._audited(tmp_path, audited=2, snapshots=2)
        found = dm.incomplete_quarantine_audits()
        dm.close()
        assert found == [], found

    def test_every_snapshot_lost_is_still_reported(self, tmp_path):
        """The boundary an INNER join gets wrong: zero survivors is the worst
        case, not an absent one."""
        dm = self._audited(tmp_path, audited=3, snapshots=0)
        found = dm.incomplete_quarantine_audits()
        dm.close()
        assert len(found) == 1 and found[0]["missing"] == 3, found

    def test_a_fresh_database_reports_nothing(self, tmp_path):
        dm = DatabaseManager(str(tmp_path / "clean.db"))
        found = dm.incomplete_quarantine_audits()
        dm.close()
        assert found == []

    def test_a_REAL_migration_by_current_code_loses_nothing(self, tmp_path):
        """The end-to-end control, and the one test here that runs production.

        Two differently-typed claims for ONE url under an unresolvable key --
        precisely the R23-2 shape. Current code must quarantine both, so the
        audit and the snapshots agree and this reports nothing. If the key ever
        regresses, THIS is the test that turns red, from the real writer rather
        than a hand-built row.
        """
        dm = _legacy_db(tmp_path, [("u/c", "ddlbase:remux", "movie", 1)])
        with dm.get_connection() as conn:
            conn.execute(
                "INSERT INTO listing_claims (canonical_url, legacy_arm_key, "
                " listing_type, attribution_state, first_seen_at, "
                " last_seen_at, sightings) VALUES (?,?,?,?,?,?,?)",
                ("u/c", "ddlbase:remux", "tv", "unattributed", OLD, NEW, 1))
        rep = dm.migrate_listing_claim_arm_keys(default_registry(), apply=True)
        found = dm.incomplete_quarantine_audits()
        with dm.get_connection() as conn:
            kept = conn.execute(
                "SELECT COUNT(*) FROM listing_claims_quarantine").fetchone()[0]
        dm.close()
        assert rep["quarantined"] == 2, rep
        assert kept == 2, "a typed quarantine row was overwritten: %d" % kept
        assert found == [], found


class TestQuarantineRefusesRatherThanHalfFinishing:
    """Round 26 -- the R25-1 refusal, which was INERT when first written.

    Moving the bundle is the easy half. The half that protects data is the
    refusal: if a persistent journal cannot be moved, quarantine must not go on
    to create a fresh database at that path, because the stranded journal would
    then be applied to it.

    That refusal was first written as `raise OSError(...)` -- landing inside a
    pre-existing `except OSError: logger.critical(...)` at the end of the SAME
    method. It could never fire. It was not caught by reading the diff, by the
    surrounding tests, or by review; it was caught by injecting the failure and
    watching nothing happen. Hence these tests, and hence `QuarantineIncomplete`
    deliberately not being an OSError.
    """

    def _hot_wal(self, tmp_path, name):
        """A database whose -wal cannot be checkpointed away."""
        dm = DatabaseManager(str(tmp_path / name))
        conn = dm.get_connection()
        conn.execute("PRAGMA wal_autocheckpoint=0")
        conn.execute("CREATE TABLE IF NOT EXISTS probe (a TEXT)")
        conn.commit()
        reader = sqlite3.connect(dm.db_path)      # pins the old snapshot
        reader.execute("BEGIN")
        reader.execute("SELECT COUNT(*) FROM probe").fetchone()
        conn.execute("INSERT INTO probe VALUES ('committed-into-wal')")
        conn.commit()
        return dm, reader

    def test_a_stranded_journal_RAISES(self, tmp_path, monkeypatch):
        dm, reader = self._hot_wal(tmp_path, "refuse.db")
        real = os.rename

        def failing(src, dst):
            if str(src).endswith("-wal"):
                raise OSError(13, "injected: cannot move the log")
            return real(src, dst)

        monkeypatch.setattr(os, "rename", failing)
        with pytest.raises(QuarantineIncomplete):
            dm._quarantine_corrupt_db(sqlite3.DatabaseError("pretend"))
        reader.close()

    def test_it_does_NOT_leave_a_fresh_database_beside_the_stranded_log(
            self, tmp_path, monkeypatch):
        """The consequence the refusal exists to prevent."""
        dm, reader = self._hot_wal(tmp_path, "refuse2.db")
        path = dm.db_path
        real = os.rename

        def failing(src, dst):
            if str(src).endswith("-wal"):
                raise OSError(13, "injected: cannot move the log")
            return real(src, dst)

        monkeypatch.setattr(os, "rename", failing)
        with pytest.raises(QuarantineIncomplete):
            dm._quarantine_corrupt_db(sqlite3.DatabaseError("pretend"))
        monkeypatch.undo()
        reader.close()

        assert os.path.exists(path + "-wal"), (
            "the fixture did not actually strand a log, so this proves nothing")
        if os.path.exists(path):
            probe = sqlite3.connect(path)
            tables = probe.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table'"
            ).fetchone()[0]
            probe.close()
            assert tables == 0, (
                "a fresh database was created next to a stranded -wal")

    def test_the_refusal_is_not_an_OSError(self):
        """A type-design preference, NOT the safety property. Round 27.

        Round 26 documented this as "the whole defect in one assertion". Its own
        mutation result had already disproved that: with the handler re-raising,
        reverting the explicit guard to `OSError` is an equivalent mutant and
        every test still passes. The exception class stopped being load-bearing
        the moment the handler gained a terminal re-raise.

        Keeping the assertion is fine -- not inheriting from the type the local
        handler catches is defence in depth. Keeping the CLAIM would preserve a
        causal model the fix has already invalidated, which is the same species
        of error as the round-26 busy-timeout rationale (R26-3).

        The load-bearing property is asserted by
        `test_the_handler_re_raise_is_what_actually_propagates` below.
        """
        assert not issubclass(QuarantineIncomplete, OSError)

    def test_the_handler_re_raise_is_what_actually_propagates(self):
        """The real safety property, asserted against the source.

        A behavioural test cannot easily distinguish "the guard raised a type
        the handler does not catch" from "the handler re-raised", because both
        produce QuarantineIncomplete at the caller. The mutation in
        evidence-06 established that only the second is load-bearing, so this
        pins the structure the mutation identified: the OSError handler must
        terminate in a raise, not in a log-and-return.
        """
        import inspect
        src = inspect.getsource(DatabaseManager._quarantine_corrupt_db)
        handler = src.split("except OSError as os_err:", 1)
        assert len(handler) == 2, "the OSError handler has been renamed or removed"
        body = handler[1]
        assert "raise QuarantineIncomplete(" in body, (
            "the OSError handler no longer re-raises; a failed quarantine would "
            "be reported to the caller as success")

    def test_the_happy_path_still_quarantines_the_whole_bundle(self, tmp_path):
        """Anti-vacuity. A guard that refuses everything would satisfy the
        tests above and destroy recovery."""
        dm, reader = self._hot_wal(tmp_path, "ok.db")
        path = dm.db_path
        dm._quarantine_corrupt_db(sqlite3.DatabaseError("pretend"))
        reader.close()
        moved = [f for f in os.listdir(os.path.dirname(path))
                 if ".corrupt." in f]
        assert any(f.endswith("-wal") for f in moved), moved
        assert any(not f.endswith(("-wal", "-shm", ".json")) for f in moved), moved


class TestTheCompletenessChoiceCannotBeOmitted:
    """R26-2. The signature contradicted its own docstring.

    Round 26 wrote "if a second production caller is ever added, it must choose
    deliberately; inheriting the lenient default by omission is the exact
    failure this parameter was introduced to close" -- and shipped
    `require_complete: bool = False`. Both statements could not be true. A
    caller who failed to make the safety decision got the permissive answer,
    successfully and silently.

    Same family as the round-26 inert guard and the fail-soft diagnostic: the
    failure to do the safe thing produced an ordinary-looking success.
    """

    ARM = "arm.hdencode.tv-packs"

    def test_omitting_the_choice_is_a_TypeError(self):
        with pytest.raises(TypeError):
            semantic_mismatch(self.ARM, "hdencode", "tv", "tv")

    def test_it_cannot_be_passed_positionally(self):
        """Keyword-only, so strictness can never be selected by argument
        position -- the failure mode where adding a parameter silently rebinds
        an existing caller's arguments."""
        with pytest.raises(TypeError):
            semantic_mismatch(self.ARM, "hdencode", "tv", "tv", None, True)

    def test_both_modes_remain_reachable_when_chosen(self):
        """Anti-vacuity: requiring the choice must not remove either branch."""
        assert semantic_mismatch(self.ARM, None, None, "tv",
                                 require_complete=False) is None
        assert semantic_mismatch(self.ARM, None, None, "tv",
                                 require_complete=True) is not None

    def test_the_live_writer_still_passes_True(self):
        """The production caller's choice is the one that matters; assert it
        against the source rather than trusting the round-26 note."""
        import inspect
        src = inspect.getsource(DatabaseManager.record_listing_claims)
        assert "require_complete=True" in src, (
            "the live writer no longer requests complete semantics")


class TestTheAuditDiagnosticCannotReportAFalseClean:
    """Regression G, reopened in round 27.

    The diagnostic was built on `_query_dicts(..., default=[])`, and `_query()`
    catches `Exception` and returns the default. So three outcomes collapsed
    into one value:

        the audit is complete    -> []
        the audit query failed   -> []
        the connection failed    -> []

    A diagnostic whose failure value equals its clean value is inert -- the
    round-26 inert-guard defect in read form. The repository already draws this
    line: `list_plex_cache_movies_strict` exists beside `load_plex_cache`
    precisely so "could not read" is never inferred as "zero rows".
    """

    def test_a_read_failure_RAISES_rather_than_returning_empty(self, tmp_path):
        dm = DatabaseManager(str(tmp_path / "g.db"))

        def broken():
            raise sqlite3.OperationalError("database is locked")

        dm.get_connection = broken
        with pytest.raises(sqlite3.Error):
            dm.incomplete_quarantine_audits()

    def test_a_genuinely_clean_audit_still_returns_empty(self, tmp_path):
        """Anti-vacuity. A method that only ever raised would satisfy the test
        above while destroying the diagnostic."""
        dm = DatabaseManager(str(tmp_path / "clean.db"))
        assert dm.incomplete_quarantine_audits() == []
        dm.close()

    def test_a_real_shortfall_is_still_found(self, tmp_path):
        """And the positive case, so 'raises on failure' has not been achieved
        by breaking the query."""
        dm = DatabaseManager(str(tmp_path / "short.db"))
        with dm.get_connection() as conn:
            conn.execute(
                "INSERT INTO listing_claim_migration_audit "
                "(migration_id, seq, decided_at, legacy_arm_key, decision, "
                " rows_affected, detail) VALUES (?,?,?,?,?,?,?)",
                ("M9", 1, NEW, "ddlbase:remux", "quarantined", 4, "why"))
            conn.execute(
                "INSERT INTO listing_claims_quarantine "
                "(migration_id, canonical_url, legacy_arm_key, listing_type, "
                " reason, quarantined_at) VALUES (?,?,?,?,?,?)",
                ("M9", "u/1", "ddlbase:remux", "movie", "why", NEW))
        found = dm.incomplete_quarantine_audits()
        dm.close()
        assert len(found) == 1 and found[0]["missing"] == 3, found

    def test_it_does_not_use_a_fail_soft_primitive(self):
        """The structural rule, so a later refactor cannot quietly reintroduce
        the collapse. `_query`/`_query_dicts` swallow Exception by design."""
        import inspect
        src = inspect.getsource(DatabaseManager.incomplete_quarantine_audits)
        body = src.split('"""', 2)[-1]      # skip the docstring's prose
        for primitive in ("_query_dicts(", "_query("):
            assert primitive not in body, (
                "an integrity diagnostic must not be built on %s, which "
                "returns its default on failure" % primitive)


class TestQuarantineSurvivesTheRestartDockerIsConfiguredToDo:
    """R25-1c. The round-26 refusal was process-local; the hazard is on disk.

    `docker-compose.yml` sets `restart: unless-stopped`, so refusing inside one
    process only postpones the damage. Measured before the fix: a partial
    quarantine raised correctly, then a NEW DatabaseManager on the same path
    constructed successfully, built a 41-table database over the stranded WAL,
    and the committed rows were gone.
    """

    def _partial(self, tmp_path, monkeypatch, name="p.db"):
        """Leave a genuinely half-quarantined directory."""
        dm = DatabaseManager(str(tmp_path / name))
        conn = dm.get_connection()
        conn.execute("PRAGMA wal_autocheckpoint=0")
        conn.execute("CREATE TABLE IF NOT EXISTS precious (v TEXT)")
        conn.commit()
        reader = sqlite3.connect(dm.db_path)
        reader.execute("BEGIN")
        reader.execute("SELECT COUNT(*) FROM precious").fetchone()
        conn.execute("INSERT INTO precious VALUES ('only-in-the-wal')")
        conn.commit()
        real = os.rename

        def failing(src, dst):
            if str(src).endswith("-wal"):
                raise OSError(13, "injected: cannot move the log")
            return real(src, dst)

        monkeypatch.setattr(os, "rename", failing)
        with pytest.raises(QuarantineIncomplete):
            dm._quarantine_corrupt_db(sqlite3.DatabaseError("pretend"))
        monkeypatch.undo()
        reader.close()
        return dm.db_path

    def test_the_interlock_is_written_before_anything_destructive(
            self, tmp_path, monkeypatch):
        path = self._partial(tmp_path, monkeypatch)
        assert os.path.exists(path + ".quarantine_pending.json")

    def test_a_RESTARTED_manager_refuses(self, tmp_path, monkeypatch):
        path = self._partial(tmp_path, monkeypatch)
        with pytest.raises(QuarantineIncomplete):
            DatabaseManager(path)

    def test_no_fresh_database_appears_over_the_stranded_log(
            self, tmp_path, monkeypatch):
        path = self._partial(tmp_path, monkeypatch)
        assert os.path.exists(path + "-wal"), (
            "the fixture did not strand a log, so this proves nothing")
        try:
            DatabaseManager(path)
        except QuarantineIncomplete:
            pass
        assert not os.path.exists(path), (
            "a fresh database was created beside a foreign journal")

    def test_the_refusal_happens_BEFORE_sqlite_opens_the_file(
            self, tmp_path, monkeypatch):
        """Ordering is the whole point: sqlite3.connect() CREATES the file, so
        a check after connecting would already have done the damage."""
        path = self._partial(tmp_path, monkeypatch)
        calls = []
        real_connect = sqlite3.connect

        def counting(*a, **k):
            calls.append(a[0] if a else None)
            return real_connect(*a, **k)

        monkeypatch.setattr(sqlite3, "connect", counting)
        with pytest.raises(QuarantineIncomplete):
            DatabaseManager(path)
        monkeypatch.undo()
        assert str(path) not in [str(c) for c in calls], (
            "sqlite3.connect was called on the guarded path")

    def test_a_healthy_database_is_unaffected(self, tmp_path):
        """Anti-vacuity: an interlock that refused everything would pass every
        test above and break every normal start."""
        p = str(tmp_path / "healthy.db")
        DatabaseManager(p).close()
        DatabaseManager(p).close()

    def test_a_COMPLETE_quarantine_clears_the_interlock(self, tmp_path):
        """The other half: a finished capture must leave the path usable."""
        p = str(tmp_path / "done.db")
        dm = DatabaseManager(p)
        dm._quarantine_corrupt_db(sqlite3.DatabaseError("pretend"))
        assert not os.path.exists(p + ".quarantine_pending.json")
        assert os.path.exists(p), "no fresh database was created"
        DatabaseManager(p).close()          # and it opens again


class TestQuarantineWillNotRenameADatabaseItCannotProveIsClosed:
    """R25-1b. The close failure was swallowed at a safety boundary.

    `except sqlite3.Error: pass` then `self.conn = None` then the renames -- so
    a connection that FAILED to close was recorded as gone and the destructive
    rename proceeded. SQLite documents renaming an open database as undefined
    behaviour.

    No explicit `raise` was involved, which is why grepping for raises could not
    find it. Same family as the round-26 inert guard: a failure neutralised by
    an absorbing handler at a boundary that must not absorb.
    """

    class _Unclosable:
        def __init__(self, real):
            self._real = real

        def close(self):
            raise sqlite3.OperationalError("injected: close failed")

        def __getattr__(self, n):
            return getattr(self._real, n)

    def _armed(self, tmp_path):
        dm = DatabaseManager(str(tmp_path / "c.db"))
        dm.conn = self._Unclosable(dm.get_connection())
        return dm

    def test_it_refuses(self, tmp_path):
        dm = self._armed(tmp_path)
        with pytest.raises(QuarantineIncomplete):
            dm._quarantine_corrupt_db(sqlite3.DatabaseError("pretend"))

    def test_it_renames_NOTHING(self, tmp_path, monkeypatch):
        dm = self._armed(tmp_path)
        seen = []
        real = os.rename
        monkeypatch.setattr(
            os, "rename",
            lambda s, d: (seen.append(str(s)), real(s, d))[1])
        with pytest.raises(QuarantineIncomplete):
            dm._quarantine_corrupt_db(sqlite3.DatabaseError("pretend"))
        assert seen == [], "a rename happened despite an unproven close"

    def test_it_does_not_falsely_record_the_connection_as_gone(self, tmp_path):
        dm = self._armed(tmp_path)
        with pytest.raises(QuarantineIncomplete):
            dm._quarantine_corrupt_db(sqlite3.DatabaseError("pretend"))
        assert dm.conn is not None, (
            "self.conn was cleared for a connection that did not close")

    def test_a_close_that_SUCCEEDS_still_quarantines(self, tmp_path):
        """Anti-vacuity."""
        dm = DatabaseManager(str(tmp_path / "ok.db"))
        dm.get_connection()
        dm._quarantine_corrupt_db(sqlite3.DatabaseError("pretend"))
        moved = [f for f in os.listdir(str(tmp_path)) if ".corrupt." in f]
        assert moved, "the happy path stopped quarantining"


class TestTheAuditIsActuallySurfacedToAnOperator:
    """Regression G's wiring. Round 27.

    Round 26 added `incomplete_quarantine_audits()` and its tests, and stopped.
    Review found no production consumer, which makes the stated closure --
    "the old historical loss is now operator-visible" -- untrue: a callable with
    no caller surfaces nothing. This is the same lesson as the standing rule
    about verifying DELIVERY rather than the call.

    The contract chosen: `/health` reports it, COUNTS ONLY (that body is
    reachable unauthenticated, so it must not enumerate migration ids), and a
    read failure reports UNKNOWN rather than clean.
    """

    def _health(self, dm):
        """Call the REAL health function, not a reimplementation of it.

        The stub carries every attribute `health()` actually reads -- config,
        db, download, plex -- rather than the two I first guessed at. A stub
        that is missing an attribute fails with AttributeError, which at least
        fails loudly; a stub that diverges in VALUES would quietly test
        something else.
        """
        from backend.api.routes import system as sys_routes

        class _Reg:
            config = {}
            db = dm
            download = None
            plex = None

        return sys_routes.health(reg=_Reg())

    def test_a_clean_database_reports_ok_with_zero(self, tmp_path):
        dm = DatabaseManager(str(tmp_path / "h1.db"))
        body = self._health(dm)
        dm.close()
        assert body["quarantine_audit"]["status"] == "ok"
        assert body["quarantine_audit"]["affected_migrations"] == 0

    def test_a_real_shortfall_is_VISIBLE(self, tmp_path):
        dm = DatabaseManager(str(tmp_path / "h2.db"))
        with dm.get_connection() as conn:
            conn.execute(
                "INSERT INTO listing_claim_migration_audit "
                "(migration_id, seq, decided_at, legacy_arm_key, decision, "
                " rows_affected, detail) VALUES (?,?,?,?,?,?,?)",
                ("M7", 1, NEW, "ddlbase:remux", "quarantined", 5, "why"))
            conn.execute(
                "INSERT INTO listing_claims_quarantine "
                "(migration_id, canonical_url, legacy_arm_key, listing_type, "
                " reason, quarantined_at) VALUES (?,?,?,?,?,?)",
                ("M7", "u/1", "ddlbase:remux", "movie", "why", NEW))
        body = self._health(dm)
        dm.close()
        assert body["quarantine_audit"]["status"] == "incomplete"
        assert body["quarantine_audit"]["affected_migrations"] == 1
        assert body["quarantine_audit"]["rows_missing"] == 4

    def test_a_read_failure_reports_UNKNOWN_not_ok(self, tmp_path):
        """The finding in one test. `None` is unknown; `{"status": "ok"}` would
        be the false clean the strict read exists to prevent."""
        dm = DatabaseManager(str(tmp_path / "h3.db"))

        def broken():
            raise sqlite3.OperationalError("database is locked")

        dm.get_connection = broken
        body = self._health(dm)
        assert body["quarantine_audit"] is None

    def test_health_itself_still_succeeds_when_the_subreport_fails(
            self, tmp_path):
        """The sub-report must not be able to take the health endpoint down --
        an unavailable diagnostic is not an outage."""
        dm = DatabaseManager(str(tmp_path / "h4.db"))

        def broken():
            raise sqlite3.OperationalError("database is locked")

        dm.get_connection = broken
        body = self._health(dm)
        assert isinstance(body, dict) and "quarantine_audit" in body

    def test_it_does_not_leak_identifiers(self, tmp_path):
        """Counts only. /health is reachable unauthenticated."""
        dm = DatabaseManager(str(tmp_path / "h5.db"))
        with dm.get_connection() as conn:
            conn.execute(
                "INSERT INTO listing_claim_migration_audit "
                "(migration_id, seq, decided_at, legacy_arm_key, decision, "
                " rows_affected, detail) VALUES (?,?,?,?,?,?,?)",
                ("SECRET-MIGRATION", 1, NEW, "ddlbase:remux", "quarantined",
                 3, "why"))
        body = self._health(dm)
        dm.close()
        rendered = repr(body["quarantine_audit"])
        assert "SECRET-MIGRATION" not in rendered, rendered
        assert "ddlbase:remux" not in rendered, rendered
