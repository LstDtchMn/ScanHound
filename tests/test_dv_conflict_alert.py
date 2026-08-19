"""The DV conflict alert must fire once per distinct set, and must DELIVER.

A file whose two dv_scan rows claim different Dolby Vision layers is left
strictly alone by the labeler -- no badge added, none removed -- so it is
invisible in every ordinary count the sync reports. The alert is the only thing
that says so, and the unattended hourly sync is where it matters because nobody
is reading the log.

Two properties are therefore load-bearing.

DEDUP, which fails in both directions:

  * no dedup           -> an alert every hour, forever, for a state that does
                          not self-heal (the rows keep disagreeing until a
                          rescan resolves them);
  * dedup on the COUNT -> silence on precisely the pass where one file resolves
                          and a different one starts conflicting.

DELIVERY: on this deployment every outbound notification channel is
unconfigured (headless container disables plyer; Discord/Slack/Pushover/generic
webhook all unset), so the notification manager's channel list is empty and a
send through it is a no-op. An alert that is *called* but never *delivered*
reads as coverage -- the same shape as the backup script that sat unrunnable
for two days because of its file extension. The in-app websocket is the channel
that works without configuration, so it must fire even with no notifier at all.
"""
from unittest.mock import MagicMock

import pytest

from backend.app_service import AppService

A = "C:/4K Drives/4K Columbo/Movies 2/Alpha (2001).mkv"
B = "C:/4K Drives/4K Columbo/Movies 2/Beta (2002).mkv"


@pytest.fixture(autouse=True)
def ws(monkeypatch):
    """The in-app websocket — the channel that delivers today.

    Autouse: the alert broadcasts on it unconditionally, and letting the real
    one through would exercise the app's event loop rather than this method.
    """
    fake = MagicMock()
    monkeypatch.setattr("backend.api.ws.ws_manager", fake)
    return fake


@pytest.fixture(autouse=True)
def bridge(monkeypatch):
    """The registry's NotificationBridge — the configured outbound path.

    Deliberately NOT AppService.notification_manager: that instance is built
    bare and nothing calls configure_from_dict on it, so it can never hold a
    channel. Asserting against it would have proved the alert was called while
    saying nothing about whether it could ever arrive.
    """
    fake = MagicMock()
    monkeypatch.setattr("backend.api.dependencies.registry._notification_bridge",
                        fake, raising=False)
    return fake


def _svc():
    """An AppService with only what the alert touches — no real startup."""
    return object.__new__(AppService)


def _result(*paths):
    return {"matched": 1, "added": 0, "layer_conflicts": len(paths),
            "layer_conflict_paths": sorted(paths)}


# --- delivery -------------------------------------------------------------

def test_alert_reaches_the_in_app_channel(ws):
    """THE delivery test, and the reason the websocket path exists at all."""
    _svc()._alert_dv_layer_conflicts(_result(A, B))

    ws.broadcast_sync.assert_called_once()
    payload = ws.broadcast_sync.call_args[0][0]
    assert payload["type"] == "notification"
    assert payload["data"]["priority"] == "high"
    assert "2 file(s)" in payload["data"]["body"]
    assert A in payload["data"]["body"], "the alert does not name the files"


def test_alert_also_goes_to_the_configured_bridge(bridge):
    """The forward-looking half: a webhook added later must carry this."""
    _svc()._alert_dv_layer_conflicts(_result(A))
    bridge.send.assert_called_once()
    type_name, title, body, data = bridge.send.call_args[0]
    assert type_name == "error"
    assert A in body
    assert data["count"] == 1


# --- dedup ----------------------------------------------------------------

def test_alert_fires_when_a_conflict_first_appears(ws):
    svc = _svc()
    svc._alert_dv_layer_conflicts(_result(A))
    assert ws.broadcast_sync.call_count == 1


def test_alert_does_not_repeat_for_the_same_set(ws, bridge):
    """The whole point of the dedup: this state persists until a rescan."""
    svc = _svc()
    for _ in range(5):
        svc._alert_dv_layer_conflicts(_result(A))
    assert ws.broadcast_sync.call_count == 1, \
        "an unresolvable state alerted on every pass"
    assert bridge.send.call_count == 1


def test_alert_fires_again_when_a_DIFFERENT_file_conflicts(ws):
    """Same COUNT, different SET — the case a count-based guard misses.

    The discriminating test. Both passes report exactly one conflict, so a
    guard comparing counts stays silent on the second even though the conflict
    is now on a completely different file.
    """
    svc = _svc()
    svc._alert_dv_layer_conflicts(_result(A))
    svc._alert_dv_layer_conflicts(_result(B))
    assert ws.broadcast_sync.call_count == 2, \
        "a new conflict on a different file was silently swallowed"


def test_alert_fires_when_the_set_grows(ws):
    svc = _svc()
    svc._alert_dv_layer_conflicts(_result(A))
    svc._alert_dv_layer_conflicts(_result(A, B))
    assert ws.broadcast_sync.call_count == 2


def test_no_alert_when_there_are_no_conflicts(ws, bridge):
    """Negative control. Without it every test above would still pass if the
    alert fired unconditionally on any result at all."""
    svc = _svc()
    svc._alert_dv_layer_conflicts(_result())
    svc._alert_dv_layer_conflicts(_result())
    ws.broadcast_sync.assert_not_called()
    bridge.send.assert_not_called()


def test_resolving_a_conflict_is_silent_but_rearms(ws):
    """Clearing does not alert, but it must reset the dedup: the same file
    conflicting again after a rescan fixed it is news."""
    svc = _svc()
    svc._alert_dv_layer_conflicts(_result(A))
    svc._alert_dv_layer_conflicts(_result())            # resolved
    assert ws.broadcast_sync.call_count == 1
    svc._alert_dv_layer_conflicts(_result(A))           # regressed
    assert ws.broadcast_sync.call_count == 2


# --- neither channel may take the sync down -------------------------------

def test_a_dead_notifier_cannot_break_the_sync(ws, bridge):
    """The label work has already succeeded when this runs.

    A notifier that raises must not propagate, or a broken channel would stop
    the watermark advancing and re-run the whole sync forever -- the shape of
    the 2026-08-12 logging-as-hard-dependency outage.
    """
    bridge.send.side_effect = RuntimeError("webhook down")
    _svc()._alert_dv_layer_conflicts(_result(A))        # must not raise
    assert ws.broadcast_sync.call_count == 1, \
        "a dead notifier suppressed the channel that does work"


def test_a_broken_websocket_cannot_break_the_sync(ws, bridge):
    """Symmetric: neither channel may take the sync down, and a failure on one
    must not skip the other."""
    ws.broadcast_sync.side_effect = RuntimeError("no event loop")
    _svc()._alert_dv_layer_conflicts(_result(A))        # must not raise
    assert bridge.send.call_count == 1


def test_a_missing_bridge_is_survivable(ws, monkeypatch):
    """Startup order is not guaranteed; the registry may hold no bridge yet."""
    monkeypatch.setattr("backend.api.dependencies.registry._notification_bridge",
                        None, raising=False)
    _svc()._alert_dv_layer_conflicts(_result(A))        # must not raise
    assert ws.broadcast_sync.call_count == 1


# --- the MANUAL sync path -------------------------------------------------
# The scheduled sync has the alert above. A manual run has only its one-line
# summary, so that summary is the only place it can report a skipped title.

def _summary(matched=1, added=0, removed=0, conflicts=None, dry_run=False):
    from backend.api.routes.rename import dv_sync_summary_body
    result = {"matched": matched, "added": added, "removed": removed}
    if conflicts is not None:
        result["layer_conflicts"] = conflicts
    return dv_sync_summary_body(result, dry_run)


def test_manual_sync_summary_reports_skipped_titles():
    """Without this the run looks like it had nothing to do.

    A conflicted title moves NONE of matched/added/removed, so "3 matched, 0
    added, 0 removed" is what both a clean no-op and a run that silently
    skipped files look like.
    """
    body = _summary(matched=3, conflicts=2)
    assert "2 file(s) skipped" in body
    assert "contradicting scan records" in body


def test_manual_sync_summary_stays_quiet_with_no_conflicts():
    """Negative control: the clause must not appear for the ordinary case."""
    body = _summary(matched=3, added=5, removed=1, conflicts=0)
    assert "skipped" not in body
    assert body == "3 matched, 5 added, 1 removed"


def test_manual_sync_summary_survives_a_result_with_no_conflict_key():
    """Backward compatibility: a caller or test double predating the key must
    not crash the notification that reports the sync succeeded."""
    assert _summary(matched=1, added=2, removed=3) == "1 matched, 2 added, 3 removed"


def test_manual_sync_summary_marks_a_dry_run():
    assert _summary(dry_run=True).endswith("(dry run)")
    assert _summary(conflicts=1, dry_run=True).endswith("(dry run)")
    assert "1 file(s) skipped" in _summary(conflicts=1, dry_run=True)


# --- DURABLE DISCOVERY ----------------------------------------------------
# The alert is an EVENT and its delivery is best-effort: the in-app broadcast
# reaches whoever is connected at that instant and raises nothing if that is
# nobody. Since the alert also marks the set "seen" before delivering, a
# conflict appearing while no client is open would be announced to an empty
# room and never mentioned again (peer review round 2, M1).
#
# The answer is that a conflict is not an event at all, it is a property the
# rows currently have -- so it is recomputed on demand and stays discoverable
# no matter what happened to the notification.

CONFLICT_ROWS = [
    {"path": r"Y:\Movies\Alpha (2001)\Alpha.mkv", "dv_layer": "fel"},
    {"path": "Y:/movies/alpha (2001)/alpha.mkv", "dv_layer": "mel"},
]


def _conflicts(rows=None, **kw):
    from backend.rename.dv_labeler import current_conflicts
    return current_conflicts(CONFLICT_ROWS if rows is None else rows,
                             mappings=[], **kw)


def test_unresolved_conflict_is_discoverable_after_the_alert_reached_nobody(
        ws, monkeypatch):
    """THE regression test for M1, and the reason current state is derived.

    Sequence that used to lose a conflict permanently:
      1. conflict appears; the sync alerts
      2. zero clients connected -- the broadcast targets nobody, no exception
      3. no outbound channel configured -- the bridge cannot reach anyone
      4. the set is already marked seen, so later passes return early
      5. nobody ever learns
    Step 5 is what this pins: after all of that, the conflict is still there to
    be found.
    """
    monkeypatch.setattr("backend.api.dependencies.registry._notification_bridge",
                        None, raising=False)          # no notifier at all
    svc = object.__new__(AppService)
    result = {"matched": 0, "added": 0, "layer_conflicts": 1,
              "layer_conflict_paths": [CONFLICT_ROWS[0]["path"]]}
    svc._alert_dv_layer_conflicts(result)                   # announced to nobody
    svc._alert_dv_layer_conflicts(result)                   # deduped, silent

    assert ws.broadcast_sync.call_count == 1, "sanity: the event fired once"

    # ...and the state is still discoverable, which is the whole point.
    status = _conflicts()
    assert status["count"] == 1
    assert status["sample"][0]["layers"] == ["fel", "mel"]


def test_discovery_does_not_depend_on_the_alert_having_run_at_all():
    """Current state is derived from rows, so it needs no prior notification,
    no stored flag, and no live process that happened to observe it."""
    assert _conflicts()["count"] == 1


def test_no_conflicts_reports_a_clean_empty_state():
    """Negative control: a clean library must not show 'needs attention'."""
    clean = [{"path": "Y:/m/a.mkv", "dv_layer": "fel"},
             {"path": "Y:/m/b.mkv", "dv_layer": "unknown"}]
    status = _conflicts(clean)
    assert status["count"] == 0
    assert status["sample"] == [] and status["truncated"] is False


def test_conflict_status_is_bounded_but_the_count_is_exact():
    """The wire gets a sample; the count stays truthful (review L1)."""
    rows = []
    for i in range(40):
        # Two spellings of one file: separators and case, as in live data.
        rows.append({"path": f"Y:/m/f{i}.mkv", "dv_layer": "fel"})
        rows.append({"path": f"Y:\\M\\F{i}.MKV", "dv_layer": "mel"})
    status = _conflicts(rows, sample=25)
    assert status["count"] == 40, "the count must not be capped"
    assert len(status["sample"]) == 25
    assert status["truncated"] is True


def test_wire_payload_caps_the_conflict_path_list():
    """The completion event goes to every client on every manual sync."""
    from backend.api.routes.rename import wire_safe_sync_result
    from backend.rename.dv_labeler import CONFLICT_SAMPLE

    paths = [f"Y:/m/f{i}.mkv" for i in range(CONFLICT_SAMPLE + 10)]
    out = wire_safe_sync_result(
        {"matched": 1, "layer_conflicts": len(paths),
         "layer_conflict_paths": list(paths)})

    assert len(out["layer_conflict_paths"]) == CONFLICT_SAMPLE
    assert out["layer_conflict_paths_truncated"] is True
    assert out["layer_conflicts"] == len(paths), "the exact count must survive"


def test_wire_payload_leaves_a_small_list_alone_and_says_so():
    from backend.api.routes.rename import wire_safe_sync_result
    out = wire_safe_sync_result(
        {"matched": 1, "layer_conflicts": 2,
         "layer_conflict_paths": ["a", "b"]})
    assert out["layer_conflict_paths"] == ["a", "b"]
    assert out["layer_conflict_paths_truncated"] is False


def test_dv_conflicts_endpoint_derives_current_state():
    """The endpoint the frontend refreshes on reconnect and panel-open.

    Narrow by design: it must read only path+dv_layer, never the paged
    inventory, because it is called on every reconnect and every panel open.
    """
    from backend.api.routes.rename import dv_conflicts

    class _DB:
        def __init__(self):
            self.narrow_calls = 0

        def get_dv_layer_rows(self, source="scan"):
            self.narrow_calls += 1
            assert source == "scan"
            return [{"path": r"C:\Movies\Alpha (2001).mkv", "dv_layer": "fel"},
                    {"path": "C:/movies/alpha (2001).mkv", "dv_layer": "mel"}]

        def get_dv_scans(self, *a, **k):
            raise AssertionError("must not read the paged inventory")

        def count_dv_scans_by_layer(self, *a, **k):
            raise AssertionError("must not read inventory counts")

    class _Reg:
        db = _DB()

    reg = _Reg()
    out = dv_conflicts(reg)

    assert out["count"] == 1, "the two spellings are one file, and they disagree"
    assert out["sample"][0]["layers"] == ["fel", "mel"]
    assert out["truncated"] is False
    assert reg.db.narrow_calls == 1


def test_dv_conflicts_endpoint_is_safe_before_the_db_exists():
    """Startup order is not guaranteed; a panel open must not 500."""
    from backend.api.routes.rename import dv_conflicts

    class _Reg:
        db = None

    assert dv_conflicts(_Reg()) == {"count": 0, "sample": [], "truncated": False}


def test_wire_payload_survives_an_error_result():
    """The finally block broadcasts whatever it has, including None or an error
    dict from the exception path. Trimming must not become a second failure."""
    from backend.api.routes.rename import wire_safe_sync_result
    assert wire_safe_sync_result(None) is None
    assert wire_safe_sync_result({"error": "boom"}) == {"error": "boom"}
