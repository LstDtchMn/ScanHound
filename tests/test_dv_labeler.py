from unittest.mock import MagicMock
from backend.rename.dv_labeler import (
    MANAGED, desired_label, pick_layer, reconcile_movie, build_index)

VOCAB = {"fel": "DV FEL", "mel": "DV MEL", "profile8": "DV P8", "profile5": "DV P5"}


def _movie(rk, files, labels):
    mv = MagicMock()
    mv.ratingKey = rk
    lab_objs = []
    for t in labels:
        lo = MagicMock(); lo.tag = t; lab_objs.append(lo)
    mv.labels = lab_objs
    medias = []
    for f in files:
        part = MagicMock(); part.file = f
        m = MagicMock(); m.parts = [part]; medias.append(m)
    mv.media = medias
    return mv


def test_desired_label_maps_and_ignores_none():
    assert desired_label("fel", VOCAB) == "DV FEL"
    assert desired_label("none", VOCAB) is None
    assert desired_label("unknown", VOCAB) is None
    assert desired_label(None, VOCAB) is None


def test_pick_layer_tie_break_rank():
    idx = {"y:/a.mkv": "profile5", "y:/b.mkv": "fel", "y:/c.mkv": "mel"}
    assert pick_layer(["y:/a.mkv", "y:/b.mkv", "y:/c.mkv"], idx) == "fel"
    assert pick_layer(["y:/a.mkv", "y:/c.mkv"], idx) == "mel"
    assert pick_layer(["y:/a.mkv"], idx) == "profile5"
    assert pick_layer(["y:/none.mkv"], idx) is None


def test_reconcile_add_when_none():
    idx = {"y:/a.mkv": "fel"}
    pm = MagicMock()
    mv = _movie(1, ["Y:/a.mkv"], [])
    res = reconcile_movie(mv, idx, VOCAB, pm, dry_run=False)
    assert res["added"] == ["DV FEL"] and res["removed"] == []
    pm.add_label.assert_called_once_with(1, "DV FEL")


def test_reconcile_swaps_stale_managed():
    idx = {"y:/a.mkv": "fel"}
    pm = MagicMock()
    mv = _movie(1, ["Y:/a.mkv"], ["DV MEL"])
    res = reconcile_movie(mv, idx, VOCAB, pm, dry_run=False)
    assert res["added"] == ["DV FEL"] and res["removed"] == ["DV MEL"]


def test_reconcile_never_touches_non_managed():
    idx = {"y:/a.mkv": "fel"}
    pm = MagicMock()
    mv = _movie(1, ["Y:/a.mkv"], ["DV Cut", "DV FEL"])  # already correct
    res = reconcile_movie(mv, idx, VOCAB, pm, dry_run=False)
    assert res["added"] == [] and res["removed"] == []   # idempotent
    pm.remove_label.assert_not_called()                  # DV Cut survives


def test_reconcile_unmatched_removes_stale_managed_only():
    idx = {}  # movie's path not in index
    pm = MagicMock()
    mv = _movie(1, ["Y:/a.mkv"], ["DV FEL", "DV Cut"])
    res = reconcile_movie(mv, idx, VOCAB, pm, dry_run=False)
    assert res["removed"] == ["DV FEL"] and res["added"] == []
    pm.remove_label.assert_called_once_with(1, "DV FEL")  # DV Cut untouched


def test_reconcile_multipart_tie_break():
    idx = {"y:/a.mkv": "mel", "y:/b.mkv": "fel"}
    pm = MagicMock()
    mv = _movie(1, ["Y:/a.mkv", "Y:/b.mkv"], [])
    res = reconcile_movie(mv, idx, VOCAB, pm, dry_run=False)
    assert res["added"] == ["DV FEL"]  # fel outranks mel


def test_dry_run_writes_nothing():
    idx = {"y:/a.mkv": "fel"}
    pm = MagicMock()
    mv = _movie(1, ["Y:/a.mkv"], ["DV MEL"])
    res = reconcile_movie(mv, idx, VOCAB, pm, dry_run=True)
    assert res["added"] == ["DV FEL"] and res["removed"] == ["DV MEL"]
    pm.add_label.assert_not_called()
    pm.remove_label.assert_not_called()


def test_build_index_normalizes():
    rows = [{"path": r"Y:\Movies\A\f.mkv", "dv_layer": "fel"}]
    idx = build_index(rows, mappings=[])
    assert idx == {"y:/movies/a/f.mkv": "fel"}


def test_sync_labels_finally_emits_done_on_plex_failure(monkeypatch):
    from backend.rename import dv_labeler as L

    class _DB:
        def get_dv_scans(self, **kw): return [{"path": "Y:/a.mkv", "dv_layer": "fel"}]
        def upsert_dv_scan(self, *a, **k): return True

    class _PM:
        def get_library_section(self, name):
            raise RuntimeError("plex dropped")

    # should NOT raise; per-lib failure is swallowed -> empty movie set
    res = L.sync_labels(_DB(), _PM(), {"movie_libs": ["Movies"]}, dry_run=True)
    assert res["total"] == 0 and res["matched"] == 0


def test_sync_labels_dry_run_no_writes():
    from backend.rename import dv_labeler as L
    from unittest.mock import MagicMock

    class _DB:
        def get_dv_scans(self, **kw): return [{"path": "Y:/a.mkv", "dv_layer": "fel"}]
        upsert_dv_scan = MagicMock(return_value=True)

    pm = MagicMock()
    lib = MagicMock()
    mv = _movie(1, ["Y:/a.mkv"], ["DV MEL"])
    lib.all.return_value = [mv]
    pm.get_library_section.return_value = lib
    db = _DB()
    res = L.sync_labels(db, pm, {"movie_libs": ["Movies"]}, dry_run=True)
    assert res["added"] == 1 and res["removed"] == 1
    pm.add_label.assert_not_called()
    db.upsert_dv_scan.assert_not_called()  # no back-write in dry_run
