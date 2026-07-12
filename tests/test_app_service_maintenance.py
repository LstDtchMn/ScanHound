"""Tests for AppService._run_maintenance_pass's conflict-analysis backfill wiring."""
from unittest.mock import MagicMock, patch

from backend.app_service import AppService


def test_maintenance_pass_calls_analyze_pending_conflicts():
    svc = AppService.__new__(AppService)  # bypass __init__'s heavy service wiring
    svc.db = MagicMock()
    svc.config = MagicMock()
    svc.config.get.side_effect = lambda k, d=None: d
    # _run_maintenance_pass also runs trash-sweep and pipeline-reconcile in
    # their own try/except blocks — explicitly no-op them so this test only
    # exercises (and can't be accidentally affected by) the conflict-analysis
    # block, and never touches the real filesystem via fileops.sweep_trash.
    with patch("backend.rename.fileops.sweep_trash", return_value={}), \
         patch("backend.pipeline_service.reconcile_batch", return_value=0), \
         patch("backend.rename.conflict_analyzer.analyze_pending_conflicts", return_value=3) as analyze_mock:
        svc._run_maintenance_pass()
    analyze_mock.assert_called_once_with(svc.db, limit=50, path_mappings=None)
