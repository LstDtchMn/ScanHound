"""Audit pass 2 — scan-pipeline regressions in the background scanner and the
scanner API routes.

Four confirmed findings, all of which fail SILENTLY today:

* #4  a cancelled scan is reported complete, stamps last_scan_time and
      auto-grabs items that were never matched against Plex;
* #15 the auto-grab completion counter always broadcasts grabbed=0;
* #7  rss_primary skips the HDEncode listing without setting purge_safe=False,
      so every cycle purges a cache nothing refreshed;
* #6  the RSS shadow comparison records outcome='success' for a cycle whose
      listing crawl fetched nothing, and that counts as promotion evidence.

Each fix is paired with a POSITIVE CONTROL (the healthy path must still do the
thing) so a fix that simply disables the feature cannot pass.
"""
import threading
from types import SimpleNamespace

import pytest

from backend.background_scanner import BackgroundScanner
from backend.database import DatabaseManager


# ── shared doubles ────────────────────────────────────────────────────

class _Item:
    """Minimal stand-in for MediaItem (only __dict__ attrs are serialized)."""

    def __init__(self, url, title="A Movie In Plex", status="missing",
                 year=2024):
        self.url = url
        self.title = title
        self.status = status
        self.year = year


class _WS:
    def __init__(self):
        self.sent = []

    def broadcast_sync(self, payload):
        self.sent.append(payload)

    def types(self):
        return [p.get("type") for p in self.sent]

    def data_for(self, kind):
        for payload in self.sent:
            if payload.get("type") == kind:
                return payload.get("data")
        return None


class _Notifications:
    def __init__(self):
        self.calls = []

    def notify_scan_complete(self, total, missing, upgrades):
        self.calls.append((total, missing, upgrades))


class _AutoGrab:
    def __init__(self, result, enabled=True):
        self.enabled = enabled
        self._result = result
        self.calls = []

    def process_items(self, items):
        self.calls.append(list(items))
        return self._result


class _RouteScanner:
    """ScannerService stand-in for the route's _run_scan.

    ``on_run`` lets a test simulate something happening DURING the scan — e.g.
    the operator posting /scan/stop, which is what really sets the route's
    "stopping" state.
    """

    def __init__(self, items, *, stop_scan_flag=False, on_run=None):
        self._items = items
        self.stop_scan_flag = stop_scan_flag
        self._on_run = on_run
        self._slot = threading.Lock()

    def try_acquire_scan(self):
        return self._slot.acquire(blocking=False)

    def release_scan(self):
        try:
            self._slot.release()
        except RuntimeError:
            pass

    @property
    def scan_in_progress(self):
        return self._slot.locked()

    def set_progress_callback(self, fn):
        pass

    def set_log_callback(self, fn):
        pass

    def run_scan(self, **kwargs):
        if self._on_run is not None:
            self._on_run()
        return list(self._items)


class _RouteRegistry:
    def __init__(self, scanner, *, auto_grab=None, notifications=None,
                 config=None):
        self.scanner = scanner
        self.auto_grab = auto_grab
        self.notifications = notifications
        self.config = config if config is not None else {}
        self.backend = SimpleNamespace(save_config=lambda: None)


@pytest.fixture
def route_env(monkeypatch):
    """Patch the route's ws_manager and reset its module-global scan state."""
    from backend.api.routes import scanner as scanner_route

    ws = _WS()
    monkeypatch.setattr(scanner_route, "ws_manager", ws)
    with scanner_route._scan_lock:
        scanner_route._scan_state.update({
            "state": "idle", "progress": 0.0, "phase": "",
            "scanned": 0, "total": 0, "holds_slot": False,
        })
    yield ws, scanner_route
    with scanner_route._scan_lock:
        scanner_route._scan_state.update({
            "state": "idle", "progress": 0.0, "phase": "",
            "holds_slot": False,
        })


# ── finding #4 — a cancelled scan must not be reported complete ───────

class TestCancelledScanIsNotComplete:
    def test_cancelled_scan_does_not_complete_or_auto_grab(self, route_env):
        """A scan stopped mid-flight returns items that never reached
        _match_against_plex — every one reads MISSING whether or not it is in
        Plex. It must not stamp last_scan_time, notify, or auto-grab."""
        ws, route = route_env
        auto_grab = _AutoGrab(SimpleNamespace(grabbed=1, failed=0, evaluated=1))
        notifications = _Notifications()
        reg = _RouteRegistry(
            _RouteScanner([_Item("https://x/a")], stop_scan_flag=True),
            auto_grab=auto_grab, notifications=notifications, config={})

        route._run_scan(reg, route.ScanRequest())

        assert "scan:complete" not in ws.types()
        assert "scan:cancelled" in ws.types()
        assert "autograb:started" not in ws.types()
        assert "autograb:complete" not in ws.types()
        assert auto_grab.calls == []
        assert notifications.calls == []
        assert "last_scan_time" not in reg.config

    def test_cancelled_scan_still_publishes_its_partial_results(self, route_env):
        """The operator asked to stop; they should still see what was found.
        The fix must suppress the completion side effects, not the results."""
        ws, route = route_env
        reg = _RouteRegistry(
            _RouteScanner([_Item("https://x/a")], stop_scan_flag=True))

        route._run_scan(reg, route.ScanRequest())

        assert "scan:result" in ws.types()
        assert len(route.get_last_scan_items()) == 1
        cancelled = ws.data_for("scan:cancelled")
        assert cancelled["partial"] is True
        assert cancelled["total"] == 1

    def test_completed_scan_still_completes_and_auto_grabs(self, route_env):
        """POSITIVE CONTROL. A scan that was never stopped must still broadcast
        scan:complete, stamp last_scan_time, send the completion notification
        and run auto-grab — a fix that just disables auto-grab would otherwise
        pass the failure-only test above."""
        ws, route = route_env
        auto_grab = _AutoGrab(SimpleNamespace(grabbed=1, failed=0, evaluated=1))
        notifications = _Notifications()
        items = [_Item("https://x/a")]
        reg = _RouteRegistry(
            _RouteScanner(items, stop_scan_flag=False),
            auto_grab=auto_grab, notifications=notifications, config={})

        route._run_scan(reg, route.ScanRequest())

        assert "scan:complete" in ws.types()
        assert "scan:cancelled" not in ws.types()
        assert ws.data_for("scan:complete")["total"] == 1
        assert notifications.calls == [(1, 1, 0)]
        assert "last_scan_time" in reg.config
        assert len(auto_grab.calls) == 1
        assert [i.url for i in auto_grab.calls[0]] == ["https://x/a"]

    def test_stop_posted_mid_scan_is_detected_without_the_scanner_flag(
            self, route_env):
        """DISAGREEING CASE for "read only scanner.stop_scan_flag".

        POST /scan/stop sets BOTH the scanner flag and the route's "stopping"
        state. Here only the route state is set, so an implementation that
        reads just one of the two signals passes the first test and fails this
        one.
        """
        ws, route = route_env
        auto_grab = _AutoGrab(SimpleNamespace(grabbed=1, failed=0, evaluated=1))

        def _operator_presses_stop():
            with route._scan_lock:
                route._scan_state["state"] = "stopping"

        reg = _RouteRegistry(
            _RouteScanner([_Item("https://x/a")], stop_scan_flag=False,
                          on_run=_operator_presses_stop),
            auto_grab=auto_grab, config={})

        route._run_scan(reg, route.ScanRequest())

        assert "scan:cancelled" in ws.types()
        assert "scan:complete" not in ws.types()
        assert auto_grab.calls == []

    def test_non_bool_stop_attribute_is_not_a_cancellation(self, route_env):
        """DISAGREEING CASE for a bare truth test on the attribute.

        The real ScannerService returns a bool from stop_scan_flag (it wraps a
        threading.Event). Mock-based callers auto-create a truthy attribute
        that is not a bool and must not be read as "the operator pressed Stop"
        — otherwise every scan driven by a test double looks cancelled.
        """
        ws, route = route_env
        scanner = _RouteScanner([_Item("https://x/a")])
        scanner.stop_scan_flag = object()  # truthy, but not a bool
        reg = _RouteRegistry(scanner, config={})

        route._run_scan(reg, route.ScanRequest())

        assert "scan:complete" in ws.types()
        assert "scan:cancelled" not in ws.types()


# ── finding #15 — the auto-grab completion counter ────────────────────

class TestAutoGrabCompletionCount:
    def test_autograb_complete_reports_the_reports_counts(self, route_env):
        """process_items returns an AutoGrabReport, so the old
        isinstance(grabbed, int) check always broadcast 0."""
        from backend.auto_grab_service import AutoGrabReport

        ws, route = route_env
        report = AutoGrabReport(evaluated=3, grabbed=2, failed=1)
        # 3 items but only 2 grabbed, so an implementation that reports
        # len(items) or `total` instead of report.grabbed disagrees here.
        items = [_Item(f"https://x/{n}") for n in range(3)]
        reg = _RouteRegistry(_RouteScanner(items),
                             auto_grab=_AutoGrab(report), config={})

        route._run_scan(reg, route.ScanRequest())

        data = ws.data_for("autograb:complete")
        assert data["grabbed"] == 2
        assert data["failed"] == 1
        assert data["evaluated"] == 3
        assert data["total"] == 3

    def test_autograb_complete_reports_zero_when_nothing_qualified(
            self, route_env):
        """DISAGREEING CASE. A "fix" that hardcodes a non-zero count, or that
        reports len(items), passes the test above and fails this one."""
        from backend.auto_grab_service import AutoGrabReport

        ws, route = route_env
        items = [_Item(f"https://x/{n}") for n in range(3)]
        reg = _RouteRegistry(
            _RouteScanner(items),
            auto_grab=_AutoGrab(AutoGrabReport(evaluated=3, grabbed=0)),
            config={})

        route._run_scan(reg, route.ScanRequest())

        data = ws.data_for("autograb:complete")
        assert data["grabbed"] == 0
        assert data["evaluated"] == 3

    def test_autograb_complete_accepts_a_plain_int_return(self, route_env):
        """The int form is what the old code was written for; keep honouring
        it so a stub/legacy service is reported truthfully rather than as 0."""
        ws, route = route_env
        items = [_Item("https://x/a")]
        reg = _RouteRegistry(_RouteScanner(items),
                             auto_grab=_AutoGrab(5), config={})

        route._run_scan(reg, route.ScanRequest())

        assert ws.data_for("autograb:complete")["grabbed"] == 5


# ── background-scanner doubles ────────────────────────────────────────

class _BgScanner:
    def __init__(self, *, items=None, seen=None, early_stopped=False,
                 requests=0):
        self.calls = []
        self._items = list(items or [])
        self._last_crawl_seen_urls = set(seen or ())
        self._last_crawl_early_stopped = early_stopped
        self._last_crawl_request_count = requests
        self.scrapers = SimpleNamespace(_detail=None)

    def try_acquire_scan(self):
        return True

    def release_scan(self):
        return None

    def run_scan(self, **kwargs):
        self.calls.append(kwargs)
        return list(self._items)

    def rematch_cache(self):
        return 0


class _BgRegistry:
    def __init__(self, config, scanner, db):
        self.config = config
        self.scanner = scanner
        self.db = db
        self.backend = SimpleNamespace(save_config=lambda: None)
        self.lifespan_generation = 1

    def owns_lifespan(self, generation):
        return generation == self.lifespan_generation


def _seed_aged_row(db, url="https://hdencode.org/aged/"):
    db.upsert_background_cache([{
        "url": url, "title": "Aged", "year": 2020, "status": "missing",
        "source_category": "HDEncode", "data": "{}",
    }])
    db._mutate(
        "UPDATE background_scan_cache SET last_seen_at = "
        "datetime('now','-30 days') WHERE url = ?", (url,))


def _patch_candidate_service(monkeypatch):
    monkeypatch.setattr(
        "backend.hdencode_candidate_service."
        "HDEncodeCandidateService.classify_pending",
        lambda self, **kwargs: {"processed": 0, "states": {}},
    )


def _patch_poll_cycle(monkeypatch, cycle):
    monkeypatch.setattr(
        "backend.hdencode_rss_service.HDEncodeRSSService.poll_cycle",
        lambda self, **kwargs: dict(cycle),
    )


# ── finding #7 — rss_primary skip must block the purge ────────────────

class TestRssPrimarySkipBlocksPurge:
    def _config(self, mode, **extra):
        cfg = {
            "background_scan_enabled": True,
            "background_scan_sources": ["HDEncode"],
            "background_scan_pages": 3,
            "background_scan_retain_days": 7,
            "hdencode_enabled": True,
            "hdencode_discovery_mode": mode,
        }
        cfg.update(extra)
        return cfg

    def test_rss_primary_skip_does_not_purge_the_cache(self, tmp_path,
                                                       monkeypatch):
        """The listing was never visited, so nothing refreshed last_seen —
        purging would age the entire HDEncode cache out over retain_days."""
        db = DatabaseManager(str(tmp_path / "crawler.db"))
        _seed_aged_row(db)
        _patch_candidate_service(monkeypatch)
        _patch_poll_cycle(monkeypatch, {
            "mode": "rss_primary", "fallback_qualified": False, "feeds": [],
        })
        # early_stopped False on purpose: if the row survived because of the
        # early-stop guard the test would pass for the wrong reason.
        scanner = _BgScanner(early_stopped=False)

        BackgroundScanner(
            _BgRegistry(self._config("rss_primary"), scanner, db)).scan_once()

        assert scanner.calls == []          # listing really was skipped
        assert db.count_background_cache() == 1
        db.close()

    def test_rss_primary_one_page_fallback_does_not_purge_the_cache(
            self, tmp_path, monkeypatch):
        """The qualified fallback crawls ONE page against a configured three —
        a deliberate partial visit, so its seen-set cannot justify aging out
        rows that only appear deeper in the listing."""
        db = DatabaseManager(str(tmp_path / "crawler.db"))
        _seed_aged_row(db)
        _patch_candidate_service(monkeypatch)
        _patch_poll_cycle(monkeypatch, {
            "mode": "rss_primary", "fallback_qualified": True, "feeds": [],
        })
        scanner = _BgScanner(seen={"https://hdencode.org/new/"},
                             early_stopped=False)

        BackgroundScanner(_BgRegistry(
            self._config("rss_primary",
                         hdencode_rss_listing_fallback_enabled=True),
            scanner, db)).scan_once()

        assert [c["pages"] for c in scanner.calls] == [1]
        assert db.count_background_cache() == 1
        db.close()

    def test_full_listing_crawl_still_purges(self, tmp_path):
        """POSITIVE CONTROL. A complete crawl in ordinary listing mode must
        still age out rows the site no longer lists — a fix that just sets
        purge_safe=False everywhere would pass both tests above and fail this
        one, silently disabling retention forever."""
        db = DatabaseManager(str(tmp_path / "crawler.db"))
        _seed_aged_row(db)
        # Seen-set deliberately excludes the aged URL: if it were touched the
        # row would survive the purge and the control would prove nothing.
        scanner = _BgScanner(seen={"https://hdencode.org/new/"},
                             early_stopped=False)

        BackgroundScanner(
            _BgRegistry(self._config("listing"), scanner, db)).scan_once()

        assert db.count_background_cache() == 0
        db.close()


# ── finding #6 — the shadow comparison's listing (control) arm ────────

class TestShadowListingArmCompleteness:
    FEEDS = [
        {"feed": "movies_all", "outcome": "changed"},
        {"feed": "tv_all", "outcome": "not_modified"},
    ]
    URL = "https://hdencode.org/a/"

    def _run(self, tmp_path, monkeypatch, scanner):
        db = DatabaseManager(str(tmp_path / "crawler.db"))
        _patch_candidate_service(monkeypatch)
        _patch_poll_cycle(monkeypatch, {
            "mode": "rss_shadow",
            "fallback_qualified": False,
            "feeds": list(self.FEEDS),
            "candidate_urls": [self.URL],
            "requests": 2,
        })
        cfg = {
            "background_scan_enabled": True,
            "background_scan_sources": ["HDEncode"],
            "background_scan_pages": 3,
            "background_scan_retain_days": 7,
            "hdencode_enabled": True,
            "hdencode_discovery_mode": "rss_shadow",
        }
        BackgroundScanner(_BgRegistry(cfg, scanner, db)).scan_once()
        summary = db.get_hdencode_shadow_summary()
        db.close()
        return summary

    def test_blocked_listing_crawl_is_not_promotion_evidence(
            self, tmp_path, monkeypatch):
        """A crawl that read no listing page (Cloudflare 403 / connection
        failure) returns items=[] with err=None and a non-zero request count.
        Recorded as outcome='success' it stretched the observation window and
        inflated the request reduction — the gate meant to prove RSS misses
        nothing was being fed cycles where the control arm was broken."""
        summary = self._run(tmp_path, monkeypatch, _BgScanner(
            items=[], seen=set(), early_stopped=True, requests=5))

        assert summary["latest"]["outcome"] != "success"
        assert summary["latest"]["normal_feeds_complete"] == 0
        # Verified at the CONSUMER: the readiness query must not count it.
        assert summary["successful_cycles"] == 0

    def test_healthy_listing_crawl_still_counts_as_evidence(
            self, tmp_path, monkeypatch):
        """POSITIVE CONTROL. A crawl that really read the listing must still
        record a usable comparison — a fix that marks every cycle incomplete
        would stall the promotion gate forever."""
        summary = self._run(tmp_path, monkeypatch, _BgScanner(
            items=[_Item(self.URL)], seen={self.URL},
            early_stopped=False, requests=5))

        assert summary["latest"]["outcome"] == "success"
        assert summary["latest"]["normal_feeds_complete"] == 1
        assert summary["successful_cycles"] == 1

    def test_early_stop_at_cached_content_still_counts_as_evidence(
            self, tmp_path, monkeypatch):
        """DISAGREEING CASE for `listing_incomplete = err or early_stopped`.

        The background crawl runs with early_stop=True, so reaching
        already-cached content sets _last_crawl_early_stopped on a perfectly
        healthy cycle — the same flag a block sets. Only the seen-set tells
        the two apart, and an implementation keyed on early_stopped passes the
        blocked test above while wrongly discarding this one.
        """
        summary = self._run(tmp_path, monkeypatch, _BgScanner(
            items=[_Item(self.URL)], seen={self.URL},
            early_stopped=True, requests=5))

        assert summary["latest"]["outcome"] == "success"
        assert summary["successful_cycles"] == 1
