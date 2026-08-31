"""Parity between the TWO independent readiness implementations.

ScanHound computes qualification readiness twice, on purpose:

  * `DatabaseManager.get_hdencode_rss_readiness` — the app's own view;
  * `05_shadow_evidence.py` — an independent, read-only, DB-derived view.

The collector consumes the second and cross-checks it against the first. That
independence is the entire value of the corroboration, so the two must NOT be
refactored to call each other. What they must do is AGREE.

Round 3 showed why this file has to exist: a defect was fixed in the app-side
implementation while the mirror kept the old behaviour, and because the mirror
is what the collector's mandatory-stop logic actually reads, the production
alert path would still have fired. Nothing caught it but reading the collector
by hand.

These are black-box fixtures: one synthetic database, both implementations, and
an assertion of agreement on every field the gate consumes.
"""

import json
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

from backend.database import DatabaseManager

EVIDENCE_SCRIPT = (Path(__file__).resolve().parents[1] / "docs"
                   / "feature-pack-review" / "qualification" / "scripts"
                   / "05_shadow_evidence.py")

#: Every field the collector or the gate reads. Agreement on these is the
#: contract; anything else may legitimately differ between the two views.
GATE_FIELDS = ("successful_cycles", "relevant_misses", "recovery_cycles",
               "request_reduction_pct", "window_start_at")


def cycle(db, *, completed_at, misses=0, outcome="success", rss=2, listing=10,
          recovery=False):
    """Record a cycle, and the miss ROWS backing any count it claims.

    UPDATED 2026-08-19, same reason as the helper in
    test_rss_qualification_window.py. The app-side gate re-derives misses from
    the rows and treats a count with no rows as `count_without_rows`, counting
    nothing. A fixture that sets `relevant_miss_count` and stops is therefore
    not "a cycle with N misses" -- it is a cycle with corrupt evidence.

    RESIDUAL DIVERGENCE, stated rather than hidden: the app validates the count
    against the rows; the mirror sums the stored count. They agree whenever a
    cycle's count matches its rows, which is every consistent cycle. They
    disagree exactly when the evidence contradicts itself -- and there the app
    also raises an integrity finding and blocks, so the gate is closed either
    way, but the two views report different NUMBERS. This fixture no longer
    exercises that case; whether the mirror should adopt the row-validated
    accounting is a real question and is not settled here.
    """
    cycle_uuid = str(uuid.uuid4())
    db.record_hdencode_shadow_comparison(
        cycle_uuid=cycle_uuid,
        started_at=completed_at, completed_at=completed_at,
        metrics={"normal_feeds_complete": True, "rss_requests": rss,
                 "listing_requests": listing, "rss_count": 10,
                 "listing_count": 10, "duplicate_count": 10,
                 "feed_only_count": 0, "listing_only_count": 0,
                 "relevant_miss_count": misses,
                 "normal_feed_outcomes": {"movies_all": "changed",
                                          "tv_all": "changed"},
                 "request_reduction_pct": 80.0, "outcome": outcome},
        catchup_used=recovery, restart_recovery=False)
    if misses:
        conn = db.get_connection()
        for i in range(misses):
            conn.execute(
                "INSERT OR REPLACE INTO hdencode_shadow_misses "
                "(cycle_uuid, canonical_url, title, status, media_type) "
                "VALUES (?,?,?,?,?)",
                (cycle_uuid, "https://hdencode.org/%s-%d" % (cycle_uuid[:8], i),
                 "T", "missing", "movie"))
        conn.commit()
    return cycle_uuid


def feeds_healthy(db, when):
    with db.transaction() as conn:
        for feed in ("movies_all", "tv_all"):
            conn.execute(
                "INSERT INTO hdencode_feed_state (feed_key, feed_url, "
                "last_status, consecutive_failures, last_checked_at) "
                "VALUES (?,?,304,0,?)", (feed, f"https://hdencode.org/{feed}/", when))


def mirror_readiness(db_path):
    """Run the independent implementation exactly as the collector does."""
    p = subprocess.run(
        [sys.executable, str(EVIDENCE_SCRIPT), "--db", str(db_path),
         "--evidence-dir", str(Path(db_path).parent)],
        capture_output=True, text=True)
    assert p.stdout.strip().startswith("{"), (
        f"evidence script produced no JSON:\n{p.stdout[:600]}\n{p.stderr[:600]}")
    return json.loads(p.stdout)["readiness"]


def assert_agree(app, mirror, *, note=""):
    diffs = []
    for key in GATE_FIELDS:
        a, m = app.get(key), mirror.get(key)
        if isinstance(a, float) or isinstance(m, float):
            same = abs((a or 0) - (m or 0)) < 0.01
        else:
            same = a == m
        if not same:
            diffs.append(f"{key}: app={a!r} mirror={m!r}")
    assert not diffs, f"gate-field disagreement {note}:\n  " + "\n  ".join(diffs)


# Relative, so a boundary that was valid when written cannot silently become
# a future (and therefore refused) timestamp later.
_NOW = __import__("datetime").datetime.now(__import__("datetime").timezone.utc)
_TD = __import__("datetime").timedelta
NOW_ISH = (_NOW - _TD(hours=3)).isoformat()
OLD = (_NOW - _TD(days=10)).isoformat()
INSIDE = (_NOW - _TD(hours=2)).isoformat()


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "parity.db"
    yield DatabaseManager(str(path)), path


class TestParity:
    """One synthetic database, both implementations, every gate field."""

    def test_no_window_at_all(self, db):
        """The round-3 case. Old misses present, no window started: BOTH must
        report zero, or the collector fires a rollback from void evidence."""
        mgr, path = db
        cycle(mgr, completed_at=OLD, misses=101, outcome="relevant_miss")
        cycle(mgr, completed_at=OLD)
        app = mgr.get_hdencode_rss_readiness()
        mirror = mirror_readiness(path)
        assert_agree(app, mirror, note="(no window)")
        assert app["relevant_misses"] == mirror["relevant_misses"] == 0
        assert "qualification_window_not_started" in app["reasons"]
        assert "qualification_window_not_started" in mirror["reasons"]

    def test_old_misses_only_with_a_started_window(self, db):
        """History outside the window must not enter either view."""
        mgr, path = db
        cycle(mgr, completed_at=OLD, misses=101, outcome="relevant_miss")
        mgr.start_qualification_window(NOW_ISH)
        assert_agree(mgr.get_hdencode_rss_readiness(window_start_at=NOW_ISH),
                     mirror_readiness(path), note="(old misses only)")

    def test_a_miss_INSIDE_the_current_window(self, db):
        """Both must still see it — the suppression is only for no-window."""
        mgr, path = db
        cycle(mgr, completed_at=OLD, misses=101, outcome="relevant_miss")
        mgr.start_qualification_window(NOW_ISH)
        cycle(mgr, completed_at=INSIDE, misses=1,
              outcome="relevant_miss")
        app = mgr.get_hdencode_rss_readiness(window_start_at=NOW_ISH)
        mirror = mirror_readiness(path)
        assert_agree(app, mirror, note="(miss inside window)")
        assert app["relevant_misses"] == mirror["relevant_misses"] == 1

    def test_unhealthy_feeds(self, db):
        mgr, path = db
        mgr.start_qualification_window(NOW_ISH)
        cycle(mgr, completed_at=INSIDE)
        assert_agree(mgr.get_hdencode_rss_readiness(window_start_at=NOW_ISH),
                     mirror_readiness(path), note="(unhealthy feeds)")

    def test_insufficient_duration(self, db):
        mgr, path = db
        mgr.start_qualification_window(NOW_ISH)
        for i in range(25):
            cycle(mgr, completed_at=(_NOW - _TD(hours=2, minutes=i)).isoformat())
        app = mgr.get_hdencode_rss_readiness(window_start_at=NOW_ISH)
        mirror = mirror_readiness(path)
        assert_agree(app, mirror, note="(insufficient duration)")
        assert not app["ready"] and not mirror["ready"]

    def test_a_window_that_would_pass_on_duration_and_cycles(self, db):
        mgr, path = db
        mgr.start_qualification_window(NOW_ISH)
        for i in range(25):
            cycle(mgr, completed_at=(_NOW - _TD(hours=2, minutes=i * 5)).isoformat(),
                  recovery=(i == 0))
        feeds_healthy(mgr, _NOW.isoformat())
        assert_agree(mgr.get_hdencode_rss_readiness(window_start_at=NOW_ISH),
                     mirror_readiness(path), note="(full window)")

    def test_a_malformed_boundary(self, db):
        """Both must fail closed the same way, not one of them silently
        aggregating everything."""
        mgr, path = db
        cycle(mgr, completed_at=OLD, misses=5, outcome="relevant_miss")
        app = mgr.get_hdencode_rss_readiness(window_start_at="not-a-timestamp")
        mirror = mirror_readiness(path)
        assert_agree(app, mirror, note="(malformed boundary)")
        assert app["relevant_misses"] == mirror["relevant_misses"] == 0

    def test_a_future_boundary(self, db):
        mgr, path = db
        cycle(mgr, completed_at=OLD, misses=5, outcome="relevant_miss")
        app = mgr.get_hdencode_rss_readiness(window_start_at=(_NOW + _TD(days=365)).isoformat())
        mirror = mirror_readiness(path)
        assert_agree(app, mirror, note="(future boundary)")
        assert app["relevant_misses"] == mirror["relevant_misses"] == 0


class TestTheMirrorReadsPersistedState:
    def test_the_mirror_picks_up_the_boundary_with_no_flag(self, db):
        """The boundary is durable state, so the mirror finds it without being
        handed anything — removing the file/flag edit surface entirely."""
        mgr, path = db
        cycle(mgr, completed_at=OLD, misses=101, outcome="relevant_miss")
        mgr.start_qualification_window(NOW_ISH)
        cycle(mgr, completed_at=INSIDE)
        mirror = mirror_readiness(path)
        assert mirror["window_start_at"] == NOW_ISH
        assert mirror["successful_cycles"] == 1
        assert mirror["relevant_misses"] == 0
