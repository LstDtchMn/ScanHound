"""The reveal-success reset, wired and proven to change the cooldown.

WHY THIS FILE EXISTS. `observe_reveal_success()` was written with the right body, the
right docstring, and NO PRODUCTION CALLER -- the fifth "signal nothing consumes" of
this effort. Three test files already called it directly, which is exactly why its
absence survived: testing the method proves the method, not the wiring.

So this file asserts two different things, and the distinction is the whole point:

  * that the reset is REACHED from the real scrape path (the wiring), and
  * that reaching it CHANGES THE NEXT COOLDOWN (the consequence).

A test that only checked `stall_streak == 0` would pass against a coordinator whose
escalation ignored the streak entirely. The cooldown duration is the axis the bug is
on, so that is what gets asserted.
"""
from __future__ import annotations

import threading

import pytest

from backend.scrape_outcome import ScrapeCode


# ─────────────────────────────────────────────────────────────────────────────
# The consequence: escalation must come back DOWN after a success
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture()
def coordinator(tmp_path):
    from backend.database import DatabaseManager
    from backend.hdencode_coordinator import HDEncodeTrafficCoordinator
    db = DatabaseManager(str(tmp_path / "c.db"))
    # __init__ takes no arguments; state arrives via configure(). The coordinator is
    # a process-wide singleton in production, so a FRESH instance is built here
    # rather than reconfiguring the shared one, which would leak streak state into
    # every other test in the run.
    coord = HDEncodeTrafficCoordinator()
    coord.configure({"base_url": "https://hdencode.org"}, db)
    yield coord
    db.close()


class _NoJitter:
    """Deterministic stand-in for `random`, so cooldowns are exact.

    observe_reveal_stall applies +/-10% jitter. My first version of this file
    asserted `second > first` as a positive control WITH the jitter live -- which,
    if escalation were flat, would still have passed roughly half the time on noise
    alone. A control that can pass by coin flip is not a control. The production
    signature already accepts an injectable rng; using it makes both the escalation
    and the reset assertions exact rather than approximate.
    """

    @staticmethod
    def uniform(_lo, _hi):
        return 0.0


def _stall_seconds(coord):
    """Observe one stall and return the cooldown it chose, in seconds."""
    before = coord.reveal_telemetry()
    coord.observe_reveal_stall("reveal_verification_stalled", rng=_NoJitter())
    after = coord.reveal_telemetry()
    assert after["stall_streak"] == before["stall_streak"] + 1
    return after["last_cooldown_seconds"]


def test_success_brings_the_next_cooldown_back_down(coordinator):
    """THE WRONG ANSWER: the 4th stall still draws the ceiling after a success.

    This is the behaviour the missing caller destroyed. Asserted as a comparison
    between two real escalation steps, so an implementation that returns a constant
    cooldown fails it.
    """
    first = _stall_seconds(coordinator)
    second = _stall_seconds(coordinator)
    third = _stall_seconds(coordinator)

    # Positive control: escalation must actually escalate, or the rest of this test
    # is measuring nothing. Pinned to the documented 1h -> 2h -> 4h multipliers, so
    # a flattened curve fails here instead of hiding behind an inequality.
    assert (first, second, third) == (3600, 7200, 14400), (first, second, third)

    coordinator.observe_reveal_success()
    assert coordinator.reveal_telemetry()["stall_streak"] == 0

    after_success = _stall_seconds(coordinator)
    assert after_success == first == 3600, (
        f"a stall after a successful reveal drew {after_success}s; the first-stall "
        f"value is {first}s, so escalation did not reset")
    assert after_success < third


def test_success_is_recorded_as_telemetry_not_just_state(coordinator):
    coordinator.observe_reveal_stall("reveal_verification_stalled")
    assert coordinator.reveal_telemetry()["last_success_at"] is None
    coordinator.observe_reveal_success()
    tel = coordinator.reveal_telemetry()
    assert tel["last_success_at"] is not None
    assert tel["successes"] == 1


def test_success_without_any_prior_stall_is_harmless(coordinator):
    coordinator.observe_reveal_success()
    assert coordinator.reveal_telemetry()["stall_streak"] == 0
    # And the first stall after it still draws the FIRST step, not step 2.
    first = _stall_seconds(coordinator)
    coordinator.observe_reveal_success()
    assert _stall_seconds(coordinator) == first


# ─────────────────────────────────────────────────────────────────────────────
# The wiring: the real scrape path must reach it
# ─────────────────────────────────────────────────────────────────────────────

def test_delivering_links_calls_the_reset_on_the_real_scrape_path(monkeypatch):
    """THE WRONG ANSWER: a successful HDEncode scrape leaves the streak untouched.

    Drives the actual `if links:` branch of scrape_links rather than calling the
    coordinator directly -- the distinction that let the missing caller survive
    three test files.
    """
    from backend import download_service as mod
    from backend.download_service import DownloadService

    calls = []

    class _FakeCoordinator:
        def observe_reveal_success(self):
            calls.append("success")

        def observe_reveal_stall(self, reason_code="reveal_verification_stalled"):
            calls.append("stall")
            return None

    monkeypatch.setattr(mod, "get_hdencode_coordinator", lambda: _FakeCoordinator())
    # STATEFUL ON PURPOSE. scrape_links extracts links TWICE: once to check for
    # already-visible links (which returns before any reveal) and again after the
    # click. A monkeypatch that always returns links satisfies the FIRST call, so
    # the post-click branch never runs -- which is exactly how this test caught that
    # I had wired the reset to only one of the two success paths.
    extract_calls = []

    def _extract(page, keyword):
        extract_calls.append(keyword)
        return [] if len(extract_calls) == 1 else ["https://rapidgator.net/file/x/y.rar"]

    monkeypatch.setattr(mod, "_extract_requested_host_links", _extract)
    monkeypatch.setattr(mod, "_WebDriverWait",
                        lambda driver, timeout: type("W", (), {"until": lambda s, c: True})())
    monkeypatch.setattr(mod, "record_scrape_outcome", lambda *a, **k: None)
    # _By and _EC are None when Selenium is absent, and the post-click link wait is
    # wrapped in `except Exception`, so `_By.XPATH` raised and the function returned a
    # diagnostic BEFORE the second extraction -- silently, looking exactly like "no
    # links appeared". The existing HDEncode tests patch both for the same reason.
    monkeypatch.setattr(mod, "_By", type("By", (), {"XPATH": "xpath"}))
    monkeypatch.setattr(mod, "_EC", type("EC", (), {
        "presence_of_element_located": staticmethod(lambda locator: locator)}))

    svc = DownloadService.__new__(DownloadService)
    svc.config = {"base_url": "https://hdencode.org", "hdencode_enabled": True}
    svc._log = lambda *a, **k: None
    svc._driver_lock = threading.RLock()
    svc._scrape_count_lock = threading.Lock()
    svc._scrapes_done = threading.Condition(svc._scrape_count_lock)
    svc._active_scrapes = 0
    svc._last_reveal_tier = "links-control"

    class _Btn:
        def get_attribute(self, _):
            return "Get Links"
        text = "Get Links"
        tag_name = "input"

        def click(self):
            return None

    class _Driver:
        page_source = "<html></html>"
        title = "A Movie"
        current_url = "https://hdencode.org/a-movie-2160p/"

        def execute_script(self, *a, **k):
            return None

    svc._navigate_with_diagnostic = lambda url, tag=None: (_Driver(), None)
    svc._wait_past_cloudflare = lambda driver, source_kind=None: None
    svc._find_reveal_control = lambda *a, **k: _Btn()

    result = svc.scrape_links("https://hdencode.org/a-movie-2160p/", "Rapidgator")

    assert len(extract_calls) == 2, (
        "the already-visible check must have run first and found nothing, so this "
        "test exercises the POST-CLICK success path")
    assert list(result) == ["https://rapidgator.net/file/x/y.rar"]
    assert calls == ["success"], (
        "a successful HDEncode reveal must notify the coordinator exactly once; "
        f"got {calls!r}")


def test_already_visible_links_also_reset_the_streak(monkeypatch):
    """THE SECOND success path, which I had missed.

    `scrape_links` returns early when file-host links are already on the page, with
    no reveal at all. I wired the reset to the post-click branch only; the wiring
    test above found it because links appeared while the coordinator stayed
    untouched. A diff read would have called it done.

    This path counts because the rule is "HDEncode served links", not "the reveal
    control worked" -- otherwise a source that serves fine keeps an inflated streak
    and the next genuine stall still draws the ceiling, i.e. the ratchet is only
    half-fixed.
    """
    from backend import download_service as mod
    from backend.download_service import DownloadService

    calls = []

    class _FakeCoordinator:
        def observe_reveal_success(self):
            calls.append("success")

        def observe_reveal_stall(self, reason_code="reveal_verification_stalled"):
            calls.append("stall")
            return None

    monkeypatch.setattr(mod, "get_hdencode_coordinator", lambda: _FakeCoordinator())
    monkeypatch.setattr(mod, "_extract_requested_host_links",
                        lambda page, keyword: ["https://rapidgator.net/file/v/z.rar"])

    svc = DownloadService.__new__(DownloadService)
    svc.config = {"base_url": "https://hdencode.org", "hdencode_enabled": True}
    svc._log = lambda *a, **k: None
    svc._driver_lock = threading.RLock()
    svc._scrape_count_lock = threading.Lock()
    svc._scrapes_done = threading.Condition(svc._scrape_count_lock)
    svc._active_scrapes = 0

    class _Driver:
        page_source = "<html></html>"
        title = "A Movie"
        current_url = "https://hdencode.org/a-movie-2160p/"

    svc._navigate_with_diagnostic = lambda url, tag=None: (_Driver(), None)
    svc._wait_past_cloudflare = lambda driver, source_kind=None: None

    def _no_reveal_needed(*a, **k):
        raise AssertionError("the reveal control must not be sought when links are "
                             "already visible")
    svc._find_reveal_control = _no_reveal_needed

    result = svc.scrape_links("https://hdencode.org/a-movie-2160p/", "Rapidgator")
    assert list(result) == ["https://rapidgator.net/file/v/z.rar"]
    assert calls == ["success"]


def test_a_direct_file_host_does_not_report_hdencode_health(monkeypatch):
    """The reset must not fire for a URL that is not HDEncode.

    Pairs with the round-7 dispatch fix: a Rapidgator URL used to travel the HDEncode
    reveal path, so its outcome touched HDEncode's health either way.
    """
    from backend import download_service as mod
    from backend.download_service import DownloadService

    calls = []

    class _FakeCoordinator:
        def observe_reveal_success(self):
            calls.append("success")

        def observe_reveal_stall(self, reason_code="reveal_verification_stalled"):
            calls.append("stall")
            return None

    monkeypatch.setattr(mod, "get_hdencode_coordinator", lambda: _FakeCoordinator())

    svc = DownloadService.__new__(DownloadService)
    svc.config = {"base_url": "https://hdencode.org", "hdencode_enabled": True}
    svc._log = lambda *a, **k: None
    svc._driver_lock = threading.RLock()
    svc._scrape_count_lock = threading.Lock()
    svc._active_scrapes = 0

    result = svc.scrape_links("https://rapidgator.net/file/abc/x.rar", "Rapidgator")
    # THE PROPERTY UNDER TEST is `calls == []`. The diagnostic assertion that used to
    # sit here was incidental and became wrong on round 8, when a supported direct
    # host started returning itself instead of an empty result -- so it is replaced
    # with the passthrough, and the escalation assertion is left as the point.
    assert list(result) == ["https://rapidgator.net/file/abc/x.rar"]
    assert calls == [], "a direct file host must not touch HDEncode's escalation state"
