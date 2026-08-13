"""What authorises a source link on a LIVE download row, and what the date means.

Peer review Finding 1 rejected the previous answer. Links used to be resolved by
JDownloader package NAME, refusing only when two ScanHound releases shared one.
That is a closed-world guard: poll_results() enumerates JDownloader's ENTIRE
package list, so a package added by hand has no history row, collides with
nothing, and was handed a confident link to an unrelated release.

Name matching is gone from this path. A row gets a link because
`provenance_url` was recorded -- its file-host links matched links ScanHound
recorded submitting -- or it gets none.

The peer's required cases are here: an unproven package whose name matches
history must resolve to NOTHING, with a positive control beside it so the first
assertion cannot pass merely because resolution is broken.
"""
import pytest

from backend.download_links import annotate_source_links

A = "https://source.example/release-A"


@pytest.fixture
def db(tmp_path):
    from backend.database import DatabaseManager
    return DatabaseManager(str(tmp_path / "links.db"))


class TestFindingOne:
    def test_a_name_match_without_provenance_gets_nothing(self, db):
        """THE regression. The package calls itself exactly what a real release
        is called, and that must now buy it nothing at all."""
        db.add_to_history(A, "A Release", package_name="A.Release.2026")
        rows = [{"name": "A.Release.2026", "state": "downloading", "provenance_url": None}]

        annotate_source_links(db, rows)

        assert rows[0]["source_url"] is None
        assert rows[0]["first_seen_at"] is None

    def test_the_same_name_WITH_provenance_resolves(self, db):
        """Positive control. Without it, the test above would also pass if
        annotation were broken outright."""
        db.add_to_history(A, "A Release", package_name="A.Release.2026")
        rows = [{"name": "A.Release.2026", "state": "downloading", "provenance_url": A}]

        annotate_source_links(db, rows)

        assert rows[0]["source_url"] == A

    def test_an_unproven_row_BESIDE_a_proven_one_still_gets_nothing(self, db):
        """The case that actually exercises the guard.

        annotate_source_links returns early when NO row carries provenance, so a
        list of only-unproven rows never reaches the per-row resolution at all --
        a test using one passes for the wrong reason. Found by mutation: a
        name-matching fallback reintroduced into that loop was NOT caught until
        this case existed, because the early return short-circuited it. A live
        JDownloader list is exactly this mixed shape: our packages beside
        whatever else the user added.
        """
        db.add_to_history(A, "A Release", package_name="A.Release.2026")
        other = "https://source.example/release-B"
        db.add_to_history(other, "B Release", package_name="B.Release.2026")
        rows = [
            {"name": "B.Release.2026", "provenance_url": other},   # proven
            {"name": "A.Release.2026", "provenance_url": None},    # name matches, unproven
        ]

        annotate_source_links(db, rows)

        assert rows[0]["source_url"] == other, "the proven row lost its link"
        assert rows[1]["source_url"] is None, "a name match bought an unproven row a link"

    def test_a_foreign_package_is_unaffected_by_history(self, db):
        """A hand-added package shares nothing with ScanHound: no provenance, and
        a name that happens to collide changes nothing."""
        db.add_to_history(A, "A Release", package_name="Movie (2026) [2160p]")
        rows = [{"name": "Movie (2026) [2160p]", "state": "downloading"}]

        annotate_source_links(db, rows)

        assert rows[0]["source_url"] is None


class TestFirstSeenDate:
    def test_the_date_comes_from_the_PROVEN_release(self, db):
        db.add_to_history(A, "A Release", package_name="A.Release.2026")
        db._mutate("UPDATE downloads SET date_added = ? WHERE url = ?",
                   ("2021-06-05 12:00:00", A))
        rows = [{"name": "whatever", "provenance_url": A}]

        annotate_source_links(db, rows)

        assert rows[0]["first_seen_at"] == "2021-06-05 12:00:00"

    def test_a_regrab_does_not_move_it(self, db):
        """date_added is second-resolution, so two writes in one second would
        agree no matter what ON CONFLICT did -- pin it to a known past value
        first, then a reset to CURRENT_TIMESTAMP is unmissable."""
        db.add_to_history(A, "A Release", package_name="A.Release.2026")
        db._mutate("UPDATE downloads SET date_added = ?, last_grabbed_at = ? WHERE url = ?",
                   ("2020-01-01 00:00:00", "2020-01-01 00:00:00", A))

        db.add_to_history(A, "A Release", package_name="A.Release.2026")

        row = db._query_dicts(
            "SELECT date_added, last_grabbed_at FROM downloads WHERE url = ?", (A,))[0]
        assert row["date_added"] == "2020-01-01 00:00:00", "the regrab moved the date"
        # Positive control: without this the assertion above would also pass if
        # the second write had silently done nothing at all.
        assert row["last_grabbed_at"] != "2020-01-01 00:00:00", "the regrab never wrote"

    def test_a_proven_release_with_no_history_row_still_links(self, db):
        """Provenance authorises the LINK; the date is separate information that
        may simply be absent. Withholding the link too would hide a package we
        can actually prove is ours."""
        rows = [{"name": "x", "provenance_url": "https://source.example/never-in-history"}]

        annotate_source_links(db, rows)

        assert rows[0]["source_url"] == "https://source.example/never-in-history"
        assert rows[0]["first_seen_at"] is None


class TestAnnotationShape:
    def test_both_keys_are_always_set(self, db):
        """Consistent shape across the REST poll and the WebSocket push. A
        consumer that has to distinguish missing from None will guess wrong."""
        rows = [{"name": "unproven", "state": "downloading"}]

        annotate_source_links(db, rows)

        assert rows[0]["source_url"] is None
        assert rows[0]["first_seen_at"] is None

    def test_a_lookup_failure_does_not_break_the_download_list(self):
        """Decoration must never take down the live progress view, nor the
        poller loop that broadcasts it."""
        class Exploding:
            def get_release_first_seen(self, urls):
                raise RuntimeError("db is gone")

        rows = [{"name": "x", "state": "downloading", "provenance_url": A}]

        annotate_source_links(Exploding(), rows)

        assert rows[0]["source_url"] is None
        assert rows[0]["first_seen_at"] is None
        assert rows[0]["state"] == "downloading"

    def test_no_db_is_tolerated(self):
        rows = [{"name": "x", "provenance_url": A}]
        annotate_source_links(None, rows)
        assert rows[0]["source_url"] is None

    def test_the_retired_name_resolver_is_gone(self, db):
        """Deleted rather than left unused: a name-based resolver sitting in the
        codebase is a loaded gun for the next caller who reaches for it."""
        assert not hasattr(db, "get_download_source_links")
