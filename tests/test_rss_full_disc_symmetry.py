"""#191 — the full-disc rule must mean the same thing on both discovery paths.

The listing path already excludes [BD] releases. If RSS ingests them, the two
paths disagree about what "discovered" means, and every listing-vs-RSS
comparison inherits a fixed invisible offset — the same failure shape as the two
divergent URL canonicalisers, which reported a healthy pipeline as 0% acquired.
"""

import sqlite3
import types

import pytest

from backend.database import DatabaseManager
from backend.release_policy import (
    REASON_LISTING_FULL_DISC,
    REASON_RSS_FULL_DISC,
    is_full_disc_title,
)


# ─────────────────────── one predicate, both paths ──────────────────────────

class TestOneSharedPredicate:
    def test_listing_and_rss_import_the_SAME_function(self):
        """Not an equivalent copy — the same object. A copy can drift; this
        cannot."""
        from backend import hdencode_rss_service, scanner_service
        assert scanner_service.is_full_disc_title is is_full_disc_title
        assert hdencode_rss_service.is_full_disc_title is is_full_disc_title

    @pytest.mark.parametrize("title", [
        "[BD]Sorority House Massacre 1986 1080p Blu-ray AVC DTS-HD MA 2.0",
        "[bd] Some Film 2020",
        "[ BD ] Spaced Brackets 2021",
        "  [BD]Leading Whitespace 2019",
    ])
    def test_full_disc_titles_match(self, title):
        assert is_full_disc_title(title)

    @pytest.mark.parametrize("title", [
        "BD Movie Title 2020 1080p",          # no brackets
        "Some BDRip Movie 2020",              # substring, not prefix
        "Blade 1998 2160p UHD BluRay",
        "A Film [BD] Mentioned Late 2020",    # not a prefix
        "", None,
    ])
    def test_ordinary_releases_do_not_match(self, title):
        assert not is_full_disc_title(title)


# ──────────────────────── the RSS ingest boundary ───────────────────────────

class FakeEntry:
    def __init__(self, title, link, pub_date="2026-08-01T12:00:00+00:00"):
        self.title, self.link, self.pub_date = title, link, pub_date

    def as_database_row(self):
        return {"title": self.title, "link": self.link, "pub_date": self.pub_date}


def make_service(skip=True):
    """A bare service object — we exercise the partition, not the HTTP stack."""
    from backend.hdencode_rss_service import HDEncodeRSSService
    svc = object.__new__(HDEncodeRSSService)
    svc.config = {"hdencode_skip_full_disc": skip}
    return svc


ENTRIES = [
    FakeEntry("Normal Film 2026 2160p", "https://hdencode.org/normal/"),
    FakeEntry("[BD]Disc Rip 2026 Blu-ray AVC", "https://hdencode.org/disc/"),
    FakeEntry("Another Encode 2026 1080p", "https://hdencode.org/another/"),
]


class TestRssPartition:
    def test_full_disc_entries_are_held_back(self):
        ingestable, excluded = make_service()._split_full_disc(ENTRIES)
        assert [e.title for e in ingestable] == ["Normal Film 2026 2160p",
                                                 "Another Encode 2026 1080p"]
        assert [e.title for e in excluded] == ["[BD]Disc Rip 2026 Blu-ray AVC"]

    def test_disabling_the_policy_ingests_everything(self):
        ingestable, excluded = make_service(skip=False)._split_full_disc(ENTRIES)
        assert len(ingestable) == 3 and excluded == []

    def test_the_setting_is_read_per_poll(self):
        """A settings change must take effect without a restart, matching the
        listing path's behaviour."""
        svc = make_service()
        assert len(svc._split_full_disc(ENTRIES)[1]) == 1
        svc.config["hdencode_skip_full_disc"] = False
        assert svc._split_full_disc(ENTRIES)[1] == []


class TestDepthIsNotFiltered:
    def test_depth_spans_ALL_entries_including_excluded_ones(self):
        """Depth describes hdencode.org's publication window, not our policy.
        Measuring it over only what we kept would understate the window and
        corrupt the coverage-margin figures the promotion gate depends on."""
        from backend.hdencode_rss_service import _observed_depth_seconds

        entries = [
            FakeEntry("Normal A", "https://hdencode.org/a/", "2026-08-01T12:00:00+00:00"),
            FakeEntry("[BD]Oldest", "https://hdencode.org/b/", "2026-08-01T06:00:00+00:00"),
        ]
        full = _observed_depth_seconds(entries)
        kept_only = _observed_depth_seconds(make_service()._split_full_disc(entries)[0])
        assert full == 6 * 3600
        assert kept_only == 0            # what filtering first would have produced
        assert full != kept_only         # ...which is why the call site must not


# ─────────────────────── shared identity in the store ───────────────────────

@pytest.fixture
def db(tmp_path):
    return DatabaseManager(str(tmp_path / "sym.db"))


class TestExclusionStoreIdentity:
    def test_same_release_from_both_paths_is_one_row(self, db):
        """THE BUG CLASS. The RSS canonicaliser keeps a trailing slash and the
        listing one strips it. If the store did not canonicalise at its own
        boundary, the same release would occupy two rows and every join across
        them would return nothing — exactly what produced '0 of 100 acquired'."""
        db.record_policy_exclusions([{
            "url": "https://hdencode.org/disc-rip-2026/",   # RSS form (slash)
            "source": "hdencode", "title": "[BD]Disc Rip 2026",
            "reason": REASON_RSS_FULL_DISC}])
        db.record_policy_exclusions([{
            "url": "https://hdencode.org/disc-rip-2026",    # listing form (none)
            "source": "hdencode", "title": "[BD]Disc Rip 2026",
            "reason": REASON_LISTING_FULL_DISC}])
        assert db.count_policy_exclusions("hdencode") == 1

    def test_the_two_reasons_are_distinguishable(self, db):
        """The paths must agree on WHAT is excluded without pretending to be the
        same writer — an audit has to be able to tell which route caught it."""
        assert REASON_RSS_FULL_DISC != REASON_LISTING_FULL_DISC
        db.record_policy_exclusions([{
            "url": "https://hdencode.org/x/", "source": "hdencode",
            "title": "[BD]X", "reason": REASON_RSS_FULL_DISC}])
        conn = sqlite3.connect(db.db_path)
        reason = conn.execute(
            "SELECT policy_reason FROM listing_policy_exclusions").fetchone()[0]
        conn.close()
        assert reason == REASON_RSS_FULL_DISC
