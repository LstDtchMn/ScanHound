"""The downloads views claim two things about every row they render:
"first grabbed <when>" and "this link goes to that release's page".

These tests hold those two claims to the data, because both fail silently and
convincingly when wrong: a date that quietly means "last grabbed" still renders
a plausible date, and a link resolved from an ambiguous package name still
renders a working link -- to the wrong release.
"""
import pytest

from backend.download_links import annotate_source_links


@pytest.fixture
def db(tmp_path):
    from backend.database import DatabaseManager
    return DatabaseManager(str(tmp_path / "links.db"))


def _first_grab(db, url):
    row = db._query("SELECT date_added, last_grabbed_at FROM downloads WHERE url = ?",
                    (url,), one=True, default=None)
    return (row[0], row[1])


class TestFirstGrabDate:
    def test_a_regrab_does_not_move_the_first_grab_date(self, db):
        """The whole label rests on this. date_added is second-resolution, so
        calling add_to_history twice in the same second would agree no matter
        what the ON CONFLICT clause did -- the date is pinned to a known past
        value first, so a reset to CURRENT_TIMESTAMP is unmissable."""
        url = "https://example.test/a-release"
        db.add_to_history(url, "A Release", package_name="A.Release.2026")
        db._mutate("UPDATE downloads SET date_added = ?, last_grabbed_at = ? WHERE url = ?",
                   ("2020-01-01 00:00:00", "2020-01-01 00:00:00", url))

        db.add_to_history(url, "A Release", package_name="A.Release.2026")

        date_added, last_grabbed = _first_grab(db, url)
        assert date_added == "2020-01-01 00:00:00", "the regrab overwrote the first-grab date"
        # Positive control: without this, the assertion above would also pass if
        # the second add_to_history had silently done nothing at all.
        assert last_grabbed != "2020-01-01 00:00:00", "the regrab never touched the row"

    def test_the_reported_first_grab_date_is_the_pinned_one(self, db):
        url = "https://example.test/dated"
        db.add_to_history(url, "Dated", package_name="Dated.2026")
        db._mutate("UPDATE downloads SET date_added = ? WHERE url = ?",
                   ("2021-06-05 12:00:00", url))

        links = db.get_download_source_links(["Dated.2026"])

        assert links["Dated.2026"]["first_grabbed_at"] == "2021-06-05 12:00:00"


class TestSourceLinkResolution:
    def test_an_unambiguous_name_maps_to_its_release(self, db):
        db.add_to_history("https://example.test/only", "Only", package_name="Only.2026")

        links = db.get_download_source_links(["Only.2026"])

        assert links["Only.2026"]["source_url"] == "https://example.test/only"

    def test_a_name_used_by_two_different_releases_maps_to_neither(self, db):
        """The safety property. Either url would render as a working link, so a
        wrong guess is indistinguishable from a right one at the UI."""
        db.add_to_history("https://example.test/one", "Dup", package_name="Same.Name.2026")
        db.add_to_history("https://example.test/two", "Dup", package_name="Same.Name.2026")

        links = db.get_download_source_links(["Same.Name.2026"])

        assert "Same.Name.2026" not in links

    def test_a_regrab_of_one_release_still_maps(self, db):
        """Contrast with the test above: a regrab reuses the same url, so the
        name still resolves to exactly one release and must NOT be suppressed."""
        url = "https://example.test/regrabbed"
        db.add_to_history(url, "Regrabbed", package_name="Regrabbed.2026")
        db.add_to_history(url, "Regrabbed", package_name="Regrabbed.2026")

        links = db.get_download_source_links(["Regrabbed.2026"])

        assert links["Regrabbed.2026"]["source_url"] == url

    def test_jd_confirmed_name_resolves_too(self, db):
        """JD sanitizes punctuation, so the name on a live row is often the
        confirmed one rather than the package_name recorded at grab time."""
        url = "https://example.test/confirmed"
        db.add_to_history(url, "Confirmed", package_name="Original.Name.2026")
        db._mutate("UPDATE downloads SET jd_confirmed_name = ? WHERE url = ?",
                   ("JD Sanitized Name 2026", url))

        links = db.get_download_source_links(["JD Sanitized Name 2026"])

        assert links["JD Sanitized Name 2026"]["source_url"] == url

    def test_an_unknown_name_is_absent(self, db):
        db.add_to_history("https://example.test/known", "Known", package_name="Known.2026")

        assert db.get_download_source_links(["Never.Seen.2026"]) == {}

    def test_empty_and_missing_names_are_ignored(self, db):
        assert db.get_download_source_links([]) == {}
        assert db.get_download_source_links(None) == {}
        assert db.get_download_source_links([None, ""]) == {}

    def test_more_names_than_one_chunk(self, db):
        """The name list is chunked to bound bind variables; a release in a
        later chunk must resolve exactly like one in the first."""
        db.add_to_history("https://example.test/late", "Late", package_name="Late.2026")
        names = [f"Filler.{i}" for i in range(700)] + ["Late.2026"]

        links = db.get_download_source_links(names)

        assert links["Late.2026"]["source_url"] == "https://example.test/late"


class TestSchema:
    def test_the_lookup_indexes_exist_on_a_fresh_database(self, db):
        """`package_name` and `jd_confirmed_name` are added by migration, not by
        the CREATE TABLE -- a fresh `downloads` is (url, title, date_added).
        Indexing them before that migration raises "no such column" and takes
        startup down with it, and a fresh database is precisely where it breaks.
        Constructing the fixture at all is half the assertion."""
        names = {r["name"] for r in db._query_dicts(
            "SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = 'downloads'")}

        assert "idx_downloads_package_name" in names
        assert "idx_downloads_jd_confirmed_name" in names


class TestAnnotation:
    def test_both_keys_are_set_even_when_unresolved(self, db):
        """Consistent shape across the REST poll and the WebSocket push. An
        unresolved row must still carry both keys, or the two transports
        disagree and the page blanks a link it just rendered."""
        rows = [{"name": "Never.Seen.2026", "state": "downloading"}]

        annotate_source_links(db, rows)

        assert rows[0]["source_url"] is None
        assert rows[0]["first_grabbed_at"] is None

    def test_resolved_rows_carry_the_link(self, db):
        db.add_to_history("https://example.test/live", "Live", package_name="Live.2026")
        rows = [{"name": "Live.2026", "state": "downloading"}]

        annotate_source_links(db, rows)

        assert rows[0]["source_url"] == "https://example.test/live"

    def test_a_lookup_failure_does_not_break_the_download_list(self):
        """Decoration must never take down the live progress view, nor the
        poller loop that broadcasts it."""
        class Exploding:
            def get_download_source_links(self, names):
                raise RuntimeError("db is gone")

        rows = [{"name": "Whatever", "state": "downloading"}]

        annotate_source_links(Exploding(), rows)

        assert rows[0]["source_url"] is None
        assert rows[0]["state"] == "downloading"

    def test_no_db_is_tolerated(self):
        rows = [{"name": "Whatever"}]
        annotate_source_links(None, rows)
        assert rows[0]["first_grabbed_at"] is None
