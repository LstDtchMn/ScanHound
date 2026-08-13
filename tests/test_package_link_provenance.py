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
