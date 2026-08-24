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
import pathlib
import re
import sqlite3

import pytest

from backend.arms import (KNOWN_ARMS, SEARCH_CATEGORY, ArmRegistry,
                          ArmRegistryError, ArmRevision, ArmSpec,
                          PaginationForm, RequestDefinition, build_page_url,
                          arm_id_from_descriptor, default_registry,
                          request_definition_from_descriptor,
                          resolve_descriptor)
from backend.database import DatabaseManager

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


def _claims(dm):
    with sqlite3.connect(dm.db_path) as conn:
        return conn.execute(
            "SELECT canonical_url, arm_id, request_definition_version, "
            "       parser_version, legacy_arm_key, sightings, "
            "       posted_date_raw, posted_date_changed, first_seen_at, "
            "       last_seen_at FROM listing_claims ORDER BY 1,2").fetchall()


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

    def test_the_shipped_set_builds_cleanly(self):
        assert len(default_registry()) == len(KNOWN_ARMS)

    def test_two_feeds_under_one_name_raise(self):
        with pytest.raises(ArmRegistryError) as ei:
            ArmRegistry([self._spec("arm.a", "/one"),
                         self._spec("arm.a", "/two")])
        assert "arm.a" in str(ei.value)

    def test_one_request_under_two_names_raises(self):
        with pytest.raises(ArmRegistryError) as ei:
            ArmRegistry([self._spec("arm.a", "/same"),
                         self._spec("arm.b", "/same")])
        assert "SAME request definition" in str(ei.value)

    def test_a_legacy_key_claimed_by_two_arms_raises(self):
        with pytest.raises(ArmRegistryError) as ei:
            ArmRegistry([self._spec("arm.a", "/one", ["old:key"]),
                         self._spec("arm.b", "/two", ["old:key"])])
        assert "old:key" in str(ei.value)

    def test_a_legacy_key_equal_to_a_live_arm_id_raises(self):
        """Otherwise the legacy key silently resolves to a live arm at runtime
        and its rows are stranded, with both names looking valid."""
        with pytest.raises(ArmRegistryError) as ei:
            ArmRegistry([self._spec("arm.a", "/one"),
                         self._spec("arm.b", "/two", ["arm.a"])])
        assert "ambiguous" in str(ei.value)

    def test_an_identical_repeat_is_not_a_collision(self):
        s = self._spec("arm.a", "/one")
        assert len(ArmRegistry([s, s])) == 1


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
        produced = {arm_id_from_descriptor(d) for d in self._emitted()}
        extra = sorted({s.arm_id for s in KNOWN_ARMS} - produced)
        assert not extra, (
            "declared arms nothing produces would let an ambiguous legacy key "
            "look resolvable: %s" % extra)

    def test_the_two_ddlbase_remux_feeds_are_distinct(self):
        feeds = [d for d in self._emitted()
                 if d["source"] == "ddlbase" and d["category"] == "remux"]
        assert len(feeds) == 2
        assert (arm_id_from_descriptor(feeds[0])
                != arm_id_from_descriptor(feeds[1]))

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
        aid = arm_id_from_descriptor(d)
        assert default_registry().get(aid) is None

    def test_an_undeclared_feed_gets_an_id_instead_of_crashing(self):
        """A feed added to _build_sources and not declared must not take the
        crawl down; it must simply be unable to prove anything."""
        aid = arm_id_from_descriptor(
            {"base": "https://example.org/new/", "suffix": "",
             "source": "hdencode", "category": "4k"})
        assert aid.startswith("arm.unregistered.")
        assert default_registry().get(aid) is None


class TestTheDeclaredPaginationIsWhatTheCrawlerBuilds:
    """Pagination lives in the request definition because one of the four
    branches DROPS the query suffix. A digest that ignored it would call two
    genuinely different feeds identical."""

    @staticmethod
    def _crawler_url(base, suffix, source_id, page):
        # Verbatim from ScannerService._crawl_pages.
        if page == 1:
            return "%s%s" % (base, suffix)
        if source_id == "ddlbase":
            return "%s/page/%d%s" % (base, page, suffix)
        elif source_id == "adithd":
            return "%spage/%d/" % (base, page)
        else:
            return "%spage/%d/%s" % (base, page, suffix)

    CASES = [
        ("https://hdencode.org/quality/2160p/", "?tag=movies", "hdencode"),
        ("https://ddlbase.com/cat/movie-remux-2160p", "", "ddlbase"),
        ("https://adit-hd.com/forums/tv-packs/", "", "adithd"),
    ]

    @pytest.mark.parametrize("base,suffix,source", CASES)
    @pytest.mark.parametrize("page", [1, 2, 7])
    def test_declared_matches_built(self, base, suffix, source, page):
        rd = request_definition_from_descriptor(
            {"base": base, "suffix": suffix, "source": source})
        assert build_page_url(rd, base, page) == self._crawler_url(
            base, suffix, source, page)

    def test_adithd_really_does_drop_the_suffix(self):
        """If it did not, the declared enum would be pointless and every check
        above would pass for the wrong reason."""
        base = "https://adit-hd.com/forums/tv-packs/"
        assert self._crawler_url(base, "?x=1", "adithd", 2) == base + "page/2/"
        assert "?x=1" not in self._crawler_url(base, "?x=1", "adithd", 2)

    def test_the_wrong_form_would_be_caught(self):
        """Anti-vacuity: swapping the declared form must change the answer."""
        base = "https://adit-hd.com/forums/tv-packs/"
        rd = request_definition_from_descriptor(
            {"base": base, "suffix": "?x=1", "source": "adithd"})
        wrong = RequestDefinition(
            method=rd.method, scheme=rd.scheme, host=rd.host, port=rd.port,
            path=rd.path, query_suffix=rd.query_suffix,
            pagination=PaginationForm.BASE_PAGE_N_SLASH_SUFFIX)
        assert (build_page_url(wrong, base, 2)
                != self._crawler_url(base, "?x=1", "adithd", 2))


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
        by_url = {r[0]: r for r in rows}
        for url, key, ltype, n in self.ROWS:
            assert by_url[url][5] == n, "sightings changed"
            assert by_url[url][8] == OLD and by_url[url][9] == NEW
        dm.close()

    def test_the_legacy_key_is_carried_across_verbatim(self, tmp_path):
        dm = _legacy_db(tmp_path, self.ROWS)
        for r in _claims(dm):
            assert r[1] == r[4], "arm_id is not the legacy key verbatim"
        dm.close()

    def test_nothing_is_attributed(self, tmp_path):
        """The shape change must invent no attribution -- that is the gated
        step, and a row attributed here would have no audit row explaining it."""
        dm = _legacy_db(tmp_path, self.ROWS)
        assert all(r[2] == "" and r[3] == "" for r in _claims(dm))
        dm.close()

    def test_the_key_is_now_the_revision_triple(self, tmp_path):
        dm = _legacy_db(tmp_path, self.ROWS)
        with sqlite3.connect(dm.db_path) as conn:
            pk = [r[1] for r in conn.execute(
                "PRAGMA table_info(listing_claims)").fetchall() if r[5]]
        assert pk == ["canonical_url", "arm_id",
                      "request_definition_version", "parser_version"]
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
            assert reg.is_active_revision(ArmRevision(r[1], r[2], r[3])), r
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
        rows = {r[0]: r for r in _claims(dm)}
        assert "u/keep" in rows, "the quarantined row was deleted"
        assert rows["u/keep"][1] == "ddlbase:remux"
        assert rows["u/keep"][2] == "", "it acquired an attribution"
        dm.close()

    def test_the_knowable_rows_still_move_alongside(self, tmp_path):
        dm = _legacy_db(tmp_path, self.ROWS)
        dm.migrate_listing_claim_arm_keys(default_registry(), apply=True)
        rows = {r[0]: r for r in _claims(dm)}
        assert rows["u/move"][1] == "arm.hdencode.tv-packs"
        assert rows["u/move"][2] != ""
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
                "INSERT INTO listing_claims (canonical_url, arm_id, "
                " request_definition_version, parser_version, legacy_arm_key, "
                " listing_type, raw_url, posted_date_raw, posted_date_changed, "
                " first_seen_at, last_seen_at, sightings) "
                "VALUES ('u/x',?,?,?,NULL,'tv','u/x?raw',?,0,?,?,7)",
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
        assert rows[0][5] == 11, "4 + 7"

    def test_the_span_is_unioned(self, tmp_path):
        rows = self._collide(tmp_path, "August 01, 2026 at 01:00 PM",
                             "August 19, 2026 at 09:00 AM")
        assert rows[0][8] == OLD and rows[0][9] == NEW

    def test_the_posted_date_is_merged_not_dropped(self, tmp_path):
        """THE ROUND-19 DEFECT. The merge listed first_seen, last_seen,
        sightings and posted_date_changed and never mentioned posted_date_raw,
        so one row's date silently won and the other was destroyed."""
        rows = self._collide(tmp_path, "August 01, 2026 at 01:00 PM",
                             "August 19, 2026 at 09:00 AM")
        assert rows[0][6] == "August 19, 2026 at 09:00 AM", (
            "the date from the more recently seen observation must survive")

    def test_a_disagreement_is_recorded_as_a_change(self, tmp_path):
        """Previously discarded along with the losing value."""
        rows = self._collide(tmp_path, "August 01, 2026 at 01:00 PM",
                             "August 19, 2026 at 09:00 AM")
        assert rows[0][7] == 1

    def test_agreement_is_not_recorded_as_a_change(self, tmp_path):
        """Anti-vacuity for the test above: if the flag were set
        unconditionally, that test would pass for the wrong reason."""
        same = "August 19, 2026 at 09:00 AM"
        rows = self._collide(tmp_path, same, same)
        assert rows[0][7] == 0
        assert rows[0][6] == same

    def test_a_null_date_never_beats_a_real_one(self, tmp_path):
        rows = self._collide(tmp_path, "August 01, 2026 at 01:00 PM", None)
        assert rows[0][6] == "August 01, 2026 at 01:00 PM"

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
        assert rows[0][6] == "August 19, 2026 at 09:00 AM"
        assert rows[0][7] == 1
        assert rows[0][9] == NEW, "last_seen_at must still be the later of the two"


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
        assert (row[1], row[2], row[3]) == rev.as_row()

    def test_an_unstamped_claim_is_recorded_but_cannot_prove(self, db):
        """A producer that has not been updated must degrade to 'cannot prove',
        never to 'proved on evidence of unknown origin' -- and never to a
        dropped sighting, which would lose a contradiction."""
        row = self._write(db, {"arm_key": "arm.hdencode.tv-packs"})
        assert row[1] == "arm.hdencode.tv-packs"
        assert row[2] == "" and row[3] == ""
        assert not default_registry().is_active_revision(
            ArmRevision(row[1], row[2], row[3]))

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
        assert all(r[5] == 1 for r in rows), "sightings bled across revisions"


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
