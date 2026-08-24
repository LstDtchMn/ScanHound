"""Round 20, M19-3: an ordinary crawl must never migrate evidence rows.

`scan_once()` used to call `migrate_listing_claim_arm_keys()` lazily, just
before the first new-shape claim write of the process. The reviewer ruled
against it on two grounds and both are load-bearing:

  * a migration that REWRITES existing evidence must not be a side effect of a
    crawl. It made the first real execution land at an operationally
    surprising moment and turned a dark rollout into an unannounced data
    rewrite.
  * the merge it would have run is defective (M19-2) and would fire on the
    FIRST run, not in some rare rollback case, because
    `backfill_listing_claim_posted_dates` writes today's date onto the new-arm
    row while the legacy row holds an older one.

THE ASSERTION THAT MATTERS IS `call_count == 0`.

A test that merely checks the scan completes, or that claims were recorded,
passes whether or not the migration ran — it ran and completed successfully
before this change. Only counting the calls can tell the two apart. This is
the same shape as the standing lesson that a test which cannot distinguish the
fix from its absence is not a test.
"""
from unittest.mock import MagicMock

import pytest

from backend.background_scanner import BackgroundScanner
from tests.test_background_scanner import _FakeRegistry, _FakeScanner
from backend.database import DatabaseManager

URL = "https://hdencode.org/some-release-2026-2160p/"


def _claim(url, ltype="movie", category="4k"):
    return {"url": url, "source": "hdencode", "listing_type": ltype,
            "listing_category": category, "arm_key": "hdencode:4k:2160p"}


@pytest.fixture
def db(tmp_path):
    dm = DatabaseManager(str(tmp_path / "r20disarm.db"))
    yield dm
    dm.close()


def _armed_scanner():
    scanner = _FakeScanner()
    scanner._last_crawl_seen_urls = {URL}
    scanner._last_crawl_conflicted_urls = set()
    scanner._last_crawl_early_stopped = True
    scanner._last_crawl_termination = "early_stopped"
    scanner._last_crawl_attests_coverage = False
    scanner._last_crawl_types_covered = set()
    scanner._last_crawl_listing_claims = [_claim(URL)]
    return scanner


def _scan(scanner, db):
    BackgroundScanner(_FakeRegistry(
        {"background_scan_sources": ["HDEncode"], "background_scan_pages": 3},
        scanner, db)).scan_once()


class TestACrawlNeverMigrates:

    def test_the_migration_is_not_called_even_once(self, db, monkeypatch):
        """The whole point. Spy on the method and count."""
        spy = MagicMock(wraps=db.migrate_listing_claim_arm_keys)
        monkeypatch.setattr(db, "migrate_listing_claim_arm_keys", spy)
        _scan(_armed_scanner(), db)
        assert spy.call_count == 0, (
            "an ordinary crawl invoked the ledger migration %d time(s); "
            "rewriting evidence rows must be an explicit operator step"
            % spy.call_count)

    def test_claims_are_still_recorded(self, db):
        """The positive control. Disarming the migration must not disarm the
        thing the crawl is actually for -- a scan that silently stopped
        recording claims would also pass the assertion above."""
        _scan(_armed_scanner(), db)
        assert len(db.get_listing_claims(URL)) == 1

    def test_a_scan_with_no_claims_still_does_not_migrate(self, db, monkeypatch):
        """The old call sat behind `if _claims:`, so a claimless cycle already
        skipped it. Pinned so a future refactor cannot reintroduce the call on
        the path that happens to be less tested."""
        spy = MagicMock(wraps=db.migrate_listing_claim_arm_keys)
        monkeypatch.setattr(db, "migrate_listing_claim_arm_keys", spy)
        scanner = _armed_scanner()
        scanner._last_crawl_listing_claims = []
        _scan(scanner, db)
        assert spy.call_count == 0

    def test_repeated_scans_never_migrate(self, db, monkeypatch):
        """The old guard was a once-per-process flag. Its absence must not mean
        'migrates on every cycle instead'."""
        spy = MagicMock(wraps=db.migrate_listing_claim_arm_keys)
        monkeypatch.setattr(db, "migrate_listing_claim_arm_keys", spy)
        for _ in range(3):
            _scan(_armed_scanner(), db)
        assert spy.call_count == 0


class TestTheMigrationStillExistsForTheOperatorTool:
    """Disarmed, not deleted. The corrected migration ships as an explicit
    step; removing the method would strand that work."""

    def test_the_method_is_still_callable(self, db):
        from backend.arms import default_registry
        out = db.migrate_listing_claim_arm_keys(default_registry())
        # Round 20 renamed the report: "moved" described rekeying a string,
        # which is no longer what happens -- rows are ATTRIBUTED to a revision,
        # or quarantined. `applied` is present so a caller cannot mistake the
        # dry-run default for a completed migration.
        assert set(out) >= {"claims_attributed", "claims_merged",
                            "aliases_attributed", "quarantined", "skipped",
                            "applied", "migration_id"}
        assert out["applied"] is False, "the operator tool must default to dry run"

    def test_background_scanner_no_longer_imports_the_registry(self):
        """The import was removed with the call. If it comes back, something
        reintroduced a caller."""
        import inspect
        import backend.background_scanner as bs
        src = inspect.getsource(bs)
        assert "migrate_listing_claim_arm_keys(" not in src.replace(
            "# migrate_listing_claim_arm_keys() here, lazily, immediately", ""), (
            "background_scanner references the migration again")
