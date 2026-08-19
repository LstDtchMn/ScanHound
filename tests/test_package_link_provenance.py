"""Provenance: a live JDownloader package is ours only if we can prove it.

Peer review Finding 1. The previous resolver matched a package to a release by
display NAME and refused only when two ScanHound releases shared that name. That
is a closed-world guard: poll_results() enumerates JDownloader's entire package
list, so a package added by hand contributes no history row, collides with
nothing, and received a confident link to an unrelated release.

The property under test is therefore not "the right release resolves". It is
"an unproven package resolves to NOTHING" -- and it needs a positive control
beside it, because a resolver that is simply broken would also pass the first
half.
"""
import pytest

from backend.database import DatabaseManager

A = "https://source.example/release-A"
B = "https://source.example/release-B"


@pytest.fixture
def db(tmp_path):
    return DatabaseManager(str(tmp_path / "prov.db"))


class TestFindingOne:
    def test_a_package_we_never_sent_resolves_to_nothing(self, db):
        """THE bug. An external package's links were never recorded, so there is
        nothing to resolve it to -- regardless of what it calls itself."""
        db.record_submitted_links(A, ["https://host.example/a1"])

        assert db.resolve_release_by_links(["https://host.example/stranger"]) is None

    def test_a_package_we_did_send_resolves(self, db):
        """Positive control. Without this, the test above would also pass if
        resolution were broken outright."""
        db.record_submitted_links(A, ["https://host.example/a1",
                                      "https://host.example/a2"])

        assert db.resolve_release_by_links(["https://host.example/a2"]) == A

    def test_a_name_collision_is_now_irrelevant(self, db):
        """The old resolver's failure mode, restated: identical display names no
        longer participate in the decision at all, because names are not
        evidence. Only the links are."""
        db.record_submitted_links(A, ["https://host.example/a1"])

        # An external package calling itself exactly what release A is called.
        assert db.resolve_release_by_links(["https://host.example/foreign"]) is None


class TestAmbiguity:
    def test_links_spanning_two_releases_resolve_to_nothing(self, db):
        """A hand-built package can mix links from two releases. There is no
        honest answer to "which release is this", so there is no answer."""
        db.record_submitted_links(A, ["https://host.example/a1"])
        db.record_submitted_links(B, ["https://host.example/b1"])

        assert db.resolve_release_by_links(
            ["https://host.example/a1", "https://host.example/b1"]) is None

    def test_one_shared_link_between_releases_resolves_to_nothing(self, db):
        """Two releases can legitimately reference the same host link (a regrab
        recorded under a different release URL). Ambiguous is ambiguous."""
        db.record_submitted_links(A, ["https://host.example/shared"])
        db.record_submitted_links(B, ["https://host.example/shared"])

        assert db.resolve_release_by_links(["https://host.example/shared"]) is None

    def test_ambiguity_is_detected_across_chunk_boundaries(self, db):
        """The lookup is chunked to bound bind variables; two candidates landing
        in different chunks must still be seen as ambiguous rather than the last
        chunk silently winning."""
        db.record_submitted_links(A, ["https://host.example/a1"])
        db.record_submitted_links(B, ["https://host.example/b1"])
        filler = ["https://host.example/pad-%d" % i for i in range(400)]

        assert db.resolve_release_by_links(
            ["https://host.example/a1"] + filler + ["https://host.example/b1"]) is None


class TestRetraction:
    """Absence of observation is not an observation of absence.

    Both cases below hand `provenance_url=None` to the same write. They must do
    OPPOSITE things, and which one happens is decided solely by whether the
    caller actually managed to look. Collapsing them was the blocker: a proof
    outlived the evidence for it, and the UI kept pointing at a release the
    resolver had already stopped authorising.
    """

    L = "https://host.example/shared-link"

    def _stored(self, db, uuid):
        rows = db._query_dicts(
            "SELECT provenance_url FROM download_results WHERE package_uuid = ?", (uuid,))
        return rows[0]["provenance_url"] if rows else "<no row>"

    def _prove(self, db, uuid="u1"):
        db.record_submitted_links(A, [self.L])
        resolved = db.resolve_release_by_links([self.L])
        assert resolved == A, "setup failed: the link should resolve to A"
        db.upsert_download_result("Pkg", package_uuid=uuid,
                                  provenance_url=resolved, provenance_observed=True)
        assert self._stored(db, uuid) == A, "setup failed: the proof was not stored"

    def test_an_OBSERVED_ambiguous_result_retracts_the_proof(self, db):
        """The blocker. Release B later records the same host link, so the
        resolver stops having an honest answer -- and the stored one must go."""
        self._prove(db)
        db.record_submitted_links(B, [self.L])
        assert db.resolve_release_by_links([self.L]) is None, "should now be ambiguous"

        db.upsert_download_result("Pkg", package_uuid="u1",
                                  provenance_url=None, provenance_observed=True)

        assert self._stored(db, "u1") is None, "a stale proof outlived its evidence"

    def test_an_UNOBSERVED_poll_preserves_the_proof(self, db):
        """The other half. JDownloader's link query failed, so this poll knows
        nothing -- it must not be read as 'there is no longer an answer'."""
        self._prove(db)

        db.upsert_download_result("Pkg", package_uuid="u1",
                                  provenance_url=None, provenance_observed=False)

        assert self._stored(db, "u1") == A, "an unobserved poll erased a valid proof"

    def test_an_observed_proof_replaces_an_earlier_different_one(self, db):
        """Not only retraction: a package whose links genuinely now prove a
        different release must follow the evidence."""
        self._prove(db)
        db.record_submitted_links(B, ["https://host.example/only-b"])

        db.upsert_download_result("Pkg", package_uuid="u1",
                                  provenance_url=B, provenance_observed=True)

        assert self._stored(db, "u1") == B

    def test_an_unobserved_write_cannot_overwrite_even_WITH_a_url(self, db):
        """The invariant must be structural, not a convention.

        The unobserved branch ignores the passed value entirely rather than
        COALESCEing it. With COALESCE, a caller passing observed=False together
        with a url would still replace the stored proof -- so "unobserved
        preserves" held only because the one production caller never emits that
        combination. Found by mutation: restoring COALESCE broke nothing, because
        no test passed the contradictory pair. An invariant nothing tests is a
        convention, and conventions are what the next caller breaks.
        """
        self._prove(db)

        db.upsert_download_result("Pkg", package_uuid="u1",
                                  provenance_url=B, provenance_observed=False)

        assert self._stored(db, "u1") == A, "an unobserved write overwrote a stored proof"

    def test_an_insert_records_provenance_without_needing_observation(self, db):
        """A brand-new row has nothing to preserve, so the value stands as given
        regardless of the flag -- the COALESCE branch only matters on UPDATE."""
        db.upsert_download_result("Fresh", package_uuid="u2",
                                  provenance_url=A, provenance_observed=True)

        assert self._stored(db, "u2") == A


class TestRecording:
    def test_a_regrab_re_affirms_rather_than_duplicating(self, db):
        db.record_submitted_links(A, ["https://host.example/a1"])
        db.record_submitted_links(A, ["https://host.example/a1"])

        rows = db._query_dicts(
            "SELECT COUNT(*) AS n FROM download_package_links WHERE url = ?", (A,))
        assert rows[0]["n"] == 1
        assert db.resolve_release_by_links(["https://host.example/a1"]) == A

    def test_empty_input_records_nothing(self, db):
        assert db.record_submitted_links(A, []) == 0
        assert db.record_submitted_links(A, None) == 0
        assert db.record_submitted_links("", ["https://host.example/a1"]) == 0
        assert db.resolve_release_by_links([]) is None
        assert db.resolve_release_by_links(None) is None

    def test_falsy_links_are_skipped(self, db):
        db.record_submitted_links(A, ["", None, "https://host.example/a1"])

        rows = db._query_dicts(
            "SELECT COUNT(*) AS n FROM download_package_links WHERE url = ?", (A,))
        assert rows[0]["n"] == 1

    def test_the_table_exists_on_a_fresh_database(self, db):
        """Constructing the fixture is half the assertion -- the schema path has
        already broken once this session by ordering an index before its column."""
        names = {r["name"] for r in db._query_dicts(
            "SELECT name FROM sqlite_master WHERE type = 'table'")}
        idx = {r["name"] for r in db._query_dicts(
            "SELECT name FROM sqlite_master WHERE type = 'index'")}

        assert "download_package_links" in names
        assert "idx_package_links_link" in idx
