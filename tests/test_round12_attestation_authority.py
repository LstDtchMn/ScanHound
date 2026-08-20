"""Round 12, M12-1: who is ENTITLED to attest a classification as clean.

The round-11 fix made `category_attested` a real three-state gate, and that part
is right. What it did not do is establish who may write the middle state. The
background scanner computed:

    _clean = _last_crawl_seen_urls - _last_crawl_conflicted_urls
    db.attest_scan_categories(_clean)

which promotes "this crawl observed no contradiction" into "this release was
checked and is clean". Those are only the same claim when the crawl actually
covered every listing that could have contradicted it.

The real background crawl covers nothing of the sort. It is bounded
(`background_scan_pages`, default 3), it runs with `early_stop=True`, and its
category arms are individually switchable via `background_scan_categories` --
so the TV arm can be absent from the crawl entirely.

The existing tests could not see this: they call `attest_scan_categories()`
directly, which presupposes the entitlement instead of testing it.

These tests drive the REAL `BackgroundScanner.scan_once()` authority decision.
"""
import json
import pytest

from backend.database import DatabaseManager
from backend.background_scanner import BackgroundScanner
from tests.test_background_scanner import _FakeScanner, _FakeRegistry

URL = "https://hdencode.example/the-release-2026-2160p/"


@pytest.fixture(autouse=True)
def _reset_cache():
    def _clear():
        try:
            dm = DatabaseManager()
            dm.clear_background_cache()
            dm.close()
        except Exception:
            pass
    _clear()
    yield
    _clear()


@pytest.fixture
def db():
    dm = DatabaseManager()
    yield dm
    dm.close()


def _legacy_row(db, url=URL, category="4k"):
    """A row exactly as the pre-conflict-detection crawler wrote it: a recorded
    category, and no attestation of any kind."""
    db.upsert_background_cache([{
        "url": url, "title": "The Release", "year": 2026,
        "status": "missing", "source_category": "HDEncode",
        "data": json.dumps({"url": url, "category": category}),
    }])


def _run(db, scanner, **cfg):
    base = {"background_scan_sources": ["HDEncode"], "background_scan_pages": 3}
    base.update(cfg)
    BackgroundScanner(_FakeRegistry(base, scanner, db)).scan_once()


def _saw(url=URL, conflicted=(), *, early_stopped=False, termination="complete",
         attests=False, types=("movie", "tv"), page_errors=0):
    """A finished crawl, described by the verdict fields the gate reads.

    Defaults describe the PRODUCTION cycle: it does not claim attesting coverage,
    because it is bounded and early-stopping. `attests=True` with full type
    coverage describes the dedicated conflict-aware crawl that may certify.
    """
    s = _FakeScanner()
    s._last_crawl_seen_urls = {url}
    s._last_crawl_conflicted_urls = set(conflicted)
    s._last_crawl_early_stopped = early_stopped
    s._last_crawl_termination = termination
    s._last_crawl_attests_coverage = attests
    s._last_crawl_types_covered = set(types)
    s._last_crawl_page_errors = page_errors
    return s


class TestAttestationRequiresCoverageThatRulesOutContradiction:

    def test_a_crawl_with_the_tv_arm_switched_off_must_not_attest(self, db):
        """THE CONFIG-REACHABLE FALSE POSITIVE.

        `background_scan_categories=['4k']` makes `_build_sources` emit only the
        4K movie arm. No TV listing is ever fetched, so a contradiction cannot
        be observed even in principle. Attesting here converts "we did not look"
        into "we looked and it is clean", and that is what later authorizes a
        destructive Keep-best."""
        _legacy_row(db)
        _run(db, _saw(), background_scan_categories=["4k"])
        assert db.get_scan_category(URL) is None

    def test_an_early_stopped_crawl_must_not_attest(self, db):
        """early_stop=True is hard-coded in the production path, so the crawl
        stops at the cached frontier. It never reached the pages where a
        contradicting listing would live."""
        _legacy_row(db)
        _run(db, _saw(early_stopped=True, termination="early_stopped"))
        assert db.get_scan_category(URL) is None

    def test_a_crawl_with_page_errors_must_not_attest(self, db):
        _legacy_row(db)
        _run(db, _saw(termination="page_errors"))
        assert db.get_scan_category(URL) is None

    def test_a_cancelled_crawl_must_not_attest(self, db):
        _legacy_row(db)
        _run(db, _saw(termination="cancelled"))
        assert db.get_scan_category(URL) is None

    def test_a_conflict_is_still_recorded_by_a_partial_crawl(self, db):
        """The asymmetry that makes this safe rather than merely strict:
        DISCOVERING a contradiction is valid evidence from any crawl, because it
        is a positive observation. Only the negative assertion needs coverage."""
        _legacy_row(db)
        _run(db, _saw(conflicted=[URL], early_stopped=True,
                      termination="early_stopped"))
        assert db.get_scan_category(URL) is None
        row = db.get_background_cache()[0]
        assert json.loads(row["data"]).get("category_conflict") is True


class TestTheGateStillLetsAQualifiedCrawlThrough:
    """POSITIVE CONTROLS.

    Without these the suite cannot tell "correctly stricter" from "permanently
    broken": deleting the attestation call outright, or any gate that never fires,
    would satisfy every negative test above. Each of these must FAIL if the gate
    is made unconditionally closed.
    """

    def test_a_full_coverage_attesting_crawl_does_attest(self, db):
        _legacy_row(db)
        _run(db, _saw(attests=True))
        assert db.get_scan_category(URL) == "4k"

    def test_and_that_attestation_reaches_the_media_kind_resolver(self, db):
        """The consumer, not just the component: attestation only matters because
        it is what lets the server answer with a media kind at all."""
        from backend.download_service import DownloadService
        _legacy_row(db)
        _run(db, _saw(attests=True))
        service = DownloadService.__new__(DownloadService)
        service.db = db
        assert service.verified_media_kind(URL, "4k") == "movie"

    def test_coverage_of_only_the_movie_arms_is_not_enough(self, db):
        """Discriminates the TYPE-coverage rule specifically: everything else about
        this crawl qualifies, and it still must not attest."""
        _legacy_row(db)
        _run(db, _saw(attests=True, types=("movie",)))
        assert db.get_scan_category(URL) is None



class TestARescanNeverDestroysAttestability:
    """Round 12 M12-3, and the sharper half the mapping pass surfaced.

    `attest_scan_categories` used to skip any row that merely HAD the
    `category_attested` key, truth or not. A rescan wrote it falsey, so one
    rescan permanently disabled the media kind for that release -- no later crawl,
    however thorough, could ever restore it."""

    def test_a_row_carrying_a_false_attestation_can_still_be_attested(self, db):
        db.upsert_background_cache([{
            "url": URL, "title": "The Release", "year": 2026,
            "status": "missing", "source_category": "HDEncode",
            "data": json.dumps({"url": URL, "category": "4k",
                                "category_attested": False}),
        }])
        assert db.attest_scan_categories([URL]) == 1
        assert db.get_scan_category(URL) == "4k"

    def test_a_rescan_carries_the_attestation_instead_of_dropping_it(self):
        from backend.api.routes.scanner import rescan_classification
        carried = rescan_classification({"data": json.dumps({
            "category": "4k", "category_attested": True})})
        assert carried.category_attested is True

    def test_a_rescan_never_invents_an_attestation(self):
        from backend.api.routes.scanner import rescan_classification
        carried = rescan_classification({"data": json.dumps({"category": "4k"})})
        assert carried.category_attested is False


def _kind_of(db, url=URL):
    row = db._query("SELECT media_kind FROM downloads WHERE url = ?",
                    (url,), one=True, default=None)
    return dict(row).get("media_kind") if row else None


class TestARevocationFailureIsNotSwallowed:
    """Round 12 M12-2, driven through the REAL scan_once sequence.

    The old code caught any exception here and logged it as bookkeeping. After
    M1a it is not bookkeeping: a failed revocation means the withdrawn permission
    is still authoritative on downloads.media_kind, which is what the destructive
    identity actually reads. Losing that fact is fail-OPEN."""

    def _seed(self, db):
        db.add_to_history(URL, "The Release", None, "2160p", "20 GB",
                          hdr="HDR", dovi=False, year=2026, media_kind="movie")
        _legacy_row(db)

    def test_a_failed_revocation_is_held_and_retried_next_cycle(self, db, monkeypatch):
        self._seed(db)
        assert _kind_of(db) == "movie", "seed did not take"
        scanner = _saw(conflicted=[URL])
        bs = BackgroundScanner(_FakeRegistry(
            {"background_scan_sources": ["HDEncode"], "background_scan_pages": 3},
            scanner, db))

        def boom(*a, **k):
            raise RuntimeError("injected: database is locked")
        monkeypatch.setattr(
            db, "record_classification_conflicts_and_retract_kinds", boom)
        bs.scan_once()

        # The transaction rolled back, so the stale kind IS still there -- that is
        # exactly why the fact must not be dropped.
        assert _kind_of(db) == "movie"
        assert URL in bs._pending_revocations, (
            "a failed revocation was swallowed; the conflict is now forgotten "
            "while the permission it invalidates stays live")

        monkeypatch.undo()
        bs.scan_once()
        assert _kind_of(db) is None, "retry did not withdraw the stale authority"
        assert URL not in bs._pending_revocations

    def test_the_retraction_precedes_the_cache_mark_in_one_transaction(self, db):
        """Ordering is the safety property: if only ONE half could survive, it
        must be the erase. A missing or unreadable cache row must never block it."""
        db.add_to_history(URL, "The Release", None, "2160p", "20 GB",
                          hdr="HDR", dovi=False, year=2026, media_kind="movie")
        # NO cache row at all for this URL.
        retracted, marked = db.record_classification_conflicts_and_retract_kinds(
            [URL], reason="test")
        assert retracted == 1, "the erase must not depend on a readable cache row"
        assert marked == 0
        assert _kind_of(db) is None
