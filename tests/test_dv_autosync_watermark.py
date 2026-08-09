"""The DV auto-sync watermark must survive a failed label sync.

Found by adversarial review on 2026-08-09. `_maybe_dv_auto_sync` assigned
`_last_dv_scan_at = latest` BEFORE attempting the sync, so a scan generation was
consumed even when no labels were written. Two paths, both silent:

  1. Plex not initialized (`pm is None`) -- plausible when the maintenance pass
     runs shortly after a container start. The old code logged "skipping this
     pass" with the watermark already advanced.
  2. `sync_labels()` raising -- swallowed by the caller's "non-fatal" handler.

Either way the DV detection was correct and the labels never reached Plex, with
no retry until a LATER scan advanced the watermark again. That is the same
consumer-never-sees-it class the review series keeps surfacing.

Each test asserts BOTH halves -- that the retry happens AND that the watermark
is unchanged -- because asserting only the retry would pass on an implementation
that re-syncs every pass regardless.
"""
from unittest.mock import MagicMock, patch


class _Svc:
    """Minimal stand-in exposing only what the auto-sync block touches.

    The real AppService constructor pulls in a database, config and thread
    machinery none of which this branch uses. Binding the unbound function
    exercises the PRODUCTION code path rather than a reimplementation of it.
    """

    def __init__(self, latest, previous):
        self.db = MagicMock()
        self.db.get_latest_dv_scan_at.return_value = latest
        self.config = {"dv_auto_sync_enabled": True}
        self._last_dv_scan_at = previous
        # Present so the unrelated earlier blocks in the same method no-op.
        self.db.list_plex_cache_movies.return_value = []


def _run(svc):
    """Invoke the REAL production path against *svc*.

    The DV auto-sync is not its own method -- it is one block inside
    `_run_maintenance_pass`, so that is what has to be driven. Every earlier
    block in that method is individually wrapped in try/except and logs
    "non-fatal", so they harmlessly no-op against a MagicMock db rather than
    needing to be stubbed out one by one. Driving the real method is the point:
    a reimplementation of the branch would not have caught this bug.
    """
    from backend.app_service import AppService
    fn = getattr(AppService, "_run_maintenance_pass", None)
    assert fn is not None, (
        "AppService._run_maintenance_pass is gone; find where the DV auto-sync "
        "block moved to and update this test")
    return fn(svc)


def _registry_with(pm):
    reg = MagicMock()
    reg._plex_service = MagicMock()
    reg._plex_service.plex_manager = pm
    return reg


def test_watermark_not_advanced_when_plex_is_unavailable():
    svc = _Svc(latest="2026-08-09 11:00:00", previous="2026-08-01 00:00:00")
    with patch("backend.api.dependencies.registry", _registry_with(None)):
        _run(svc)
    assert svc._last_dv_scan_at == "2026-08-01 00:00:00", (
        "watermark advanced despite Plex being unavailable -- this scan's "
        "labels would never be applied")


def test_watermark_not_advanced_when_sync_raises():
    svc = _Svc(latest="2026-08-09 11:00:00", previous="2026-08-01 00:00:00")
    pm = MagicMock()
    with patch("backend.api.dependencies.registry", _registry_with(pm)), \
         patch("backend.rename.dv_labeler.sync_labels",
               side_effect=RuntimeError("Plex refused the connection")):
        try:
            _run(svc)
        except RuntimeError:
            pass          # the production caller swallows this as non-fatal
    assert svc._last_dv_scan_at == "2026-08-01 00:00:00", (
        "watermark advanced despite the label sync failing")


def test_watermark_advances_on_success():
    """Positive control. Without it the two tests above would also pass on an
    implementation that never advances the watermark at all -- which would
    re-walk the entire Plex library on every maintenance pass forever."""
    svc = _Svc(latest="2026-08-09 11:00:00", previous="2026-08-01 00:00:00")
    pm = MagicMock()
    with patch("backend.api.dependencies.registry", _registry_with(pm)), \
         patch("backend.rename.dv_labeler.sync_labels",
               return_value={"matched": 5, "added": 3}) as sync:
        _run(svc)
    sync.assert_called_once()
    assert svc._last_dv_scan_at == "2026-08-09 11:00:00", (
        "watermark did not advance after a successful sync -- every pass would "
        "redundantly reconcile the whole library")


def test_failed_pass_retries_on_the_next_pass():
    """The point of the fix: a transient failure must self-heal.

    Asserts the retry AND that the second attempt is what advances the
    watermark, so a fix that merely retried without ever acknowledging would
    not pass.
    """
    svc = _Svc(latest="2026-08-09 11:00:00", previous="2026-08-01 00:00:00")

    # Pass 1: Plex unavailable.
    with patch("backend.api.dependencies.registry", _registry_with(None)):
        _run(svc)
    assert svc._last_dv_scan_at == "2026-08-01 00:00:00"

    # Pass 2: Plex is back. The same pending generation must be picked up.
    pm = MagicMock()
    with patch("backend.api.dependencies.registry", _registry_with(pm)), \
         patch("backend.rename.dv_labeler.sync_labels",
               return_value={"matched": 5, "added": 3}) as sync:
        _run(svc)
    sync.assert_called_once(), "the pending generation was not retried"
    assert svc._last_dv_scan_at == "2026-08-09 11:00:00"


def test_baseline_pass_still_syncs_nothing():
    """Pre-existing deliberate behaviour, pinned so the fix does not change it:
    the first pass after startup records a baseline only, so restarting the app
    never kicks off a full-library label walk."""
    svc = _Svc(latest="2026-08-09 11:00:00", previous=None)
    del svc._last_dv_scan_at          # simulate "attribute not yet set"
    pm = MagicMock()
    with patch("backend.api.dependencies.registry", _registry_with(pm)), \
         patch("backend.rename.dv_labeler.sync_labels") as sync:
        _run(svc)
    sync.assert_not_called()
    assert svc._last_dv_scan_at == "2026-08-09 11:00:00"
