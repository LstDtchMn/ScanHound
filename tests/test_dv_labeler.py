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


def test_sync_labels_backwrite_uses_original_row_path(monkeypatch):
    """Correctness: the back-write must use the *original* dv_scan row path
    (not the normalized form) for whichever row matched the movie's file."""
    from backend.rename import dv_labeler as L
    from unittest.mock import MagicMock

    rows = [{"path": r"Y:\Movies\A\f.mkv", "dv_layer": "fel"}]

    class _DB:
        def get_dv_scans(self, **kw):
            return rows
        upsert_dv_scan = MagicMock(return_value=True)

    pm = MagicMock()
    lib = MagicMock()
    mv = _movie(42, ["Y:/Movies/A/f.mkv"], [])
    lib.all.return_value = [mv]
    pm.get_library_section.return_value = lib
    db = _DB()

    res = L.sync_labels(db, pm, {"movie_libs": ["Movies"]}, dry_run=False)

    assert res["matched"] == 1
    db.upsert_dv_scan.assert_called_once_with(
        r"Y:\Movies\A\f.mkv", "fel", rating_key="42", source="scan")


def test_sync_labels_normalize_path_is_on_o_rows_not_o_movies_x_rows(monkeypatch):
    """Complexity guard: normalize_path must be called ~O(rows) total for a
    sync (built once), NOT O(movies * rows) (re-scanned per matched movie).

    2 movies x 3 rows: a naive per-lookup linear rescan would call
    normalize_path at least 3 (index build) + 3 (movie1 back-write scan)
    + 3 (movie2 back-write scan) = 9+ times, on top of per-movie path
    normalization for pick_layer/back-write matching. The fixed O(1)
    dict-lookup version only normalizes each row once during index build
    (3) plus a small constant number of times per movie file for matching
    (2 movies x 1 file x 2 lookups = 4) = 7 total -- it must NOT scale with
    rows-per-movie the way the naive version does.
    """
    from backend.rename import dv_labeler as L

    rows = [
        {"path": "Y:/Movies/A/f1.mkv", "dv_layer": "fel"},
        {"path": "Y:/Movies/B/f2.mkv", "dv_layer": "mel"},
        {"path": "Y:/Movies/C/f3.mkv", "dv_layer": "profile8"},
    ]

    class _DB:
        def get_dv_scans(self, **kw):
            return rows
        def upsert_dv_scan(self, *a, **k):
            return True

    pm = MagicMock()
    lib = MagicMock()
    mv1 = _movie(1, ["Y:/Movies/A/f1.mkv"], [])
    mv2 = _movie(2, ["Y:/Movies/B/f2.mkv"], [])
    lib.all.return_value = [mv1, mv2]
    pm.get_library_section.return_value = lib
    db = _DB()

    call_count = {"n": 0}
    real_normalize = L.normalize_path

    def spy(path, mappings=None):
        call_count["n"] += 1
        return real_normalize(path, mappings)

    monkeypatch.setattr(L, "normalize_path", spy)

    res = L.sync_labels(db, pm, {"movie_libs": ["Movies"]}, dry_run=False)

    assert res["matched"] == 2
    # naive O(movies*rows) would re-scan+re-normalize all 3 rows per matched
    # movie for the back-write lookup: 3 (index) + 2*3 (per-movie rescans)
    # = 9, on top of movie-file normalization. Fixed version does index
    # build (3, once) + a small constant per movie file (2 movies x 2
    # lookups = 4) = 7 -- independent of row count. Assert well under the
    # naive floor of 9 rescans alone (excluding movie-file normalization).
    assert call_count["n"] <= 7, (
        f"normalize_path called {call_count['n']} times; expected O(rows) "
        "not O(movies*rows)")


def test_sync_labels_normalize_calls_dont_scale_with_movie_count(monkeypatch):
    """Stronger complexity guard: holding rows fixed and growing the number
    of matched movies must NOT multiply normalize_path calls by len(rows).
    Under the old O(movies*rows) back-write scan, going from 2 to 20 movies
    (all matching row 0) would roughly 10x the call count for the back-write
    portion. Under the fix, growing movies only adds a small constant per
    movie (independent of len(rows))."""
    from backend.rename import dv_labeler as L

    rows = [{"path": f"Y:/Movies/{i}/f.mkv", "dv_layer": "fel"} for i in range(50)]

    class _DB:
        def get_dv_scans(self, **kw):
            return rows
        def upsert_dv_scan(self, *a, **k):
            return True

    def _make_movies(n):
        return [_movie(i, [f"Y:/Movies/{i}/f.mkv"], []) for i in range(n)]

    def run(n_movies):
        pm = MagicMock()
        lib = MagicMock()
        lib.all.return_value = _make_movies(n_movies)
        pm.get_library_section.return_value = lib
        db = _DB()

        count = {"n": 0}
        real_normalize = L.normalize_path

        def spy(path, mappings=None):
            count["n"] += 1
            return real_normalize(path, mappings)

        monkeypatch.setattr(L, "normalize_path", spy)
        res = L.sync_labels(db, pm, {"movie_libs": ["Movies"]}, dry_run=False)
        monkeypatch.undo()
        return res, count["n"]

    res_small, calls_small = run(2)
    res_large, calls_large = run(20)

    assert res_small["matched"] == 2
    assert res_large["matched"] == 20

    # index build over 50 rows dominates both; the DELTA from 2->20 movies
    # (18 extra movies) must be small (a few calls per movie), not
    # proportional to len(rows)=50 per extra movie (which would be 900+).
    delta = calls_large - calls_small
    assert delta <= 18 * 4, (
        f"normalize_path call delta for +18 movies was {delta}; "
        "suggests back-write is re-scanning all rows per movie")


# --- additive-only mode (the scheduled auto-sync) ---------------------------
# The whole point: an unattended sync must never REMOVE a managed label. A
# movie whose path can't be matched on a given run yields desired=None, which
# in full-reconcile mode strips its labels — on a timer, one transient
# matching failure would wipe DV labels library-wide (and with them the Kometa
# FEL/MEL overlays that key on those labels).

def test_additive_only_keeps_label_when_unmatched():
    idx = {}  # nothing matches this run -> desired is None
    pm = MagicMock()
    mv = _movie(1, ["Y:/a.mkv"], ["DV FEL"])
    res = reconcile_movie(mv, idx, VOCAB, pm, dry_run=False, additive_only=True)
    assert res["removed"] == []
    pm.remove_label.assert_not_called()


def test_additive_only_still_adds_missing_label():
    idx = {"y:/a.mkv": "fel"}
    pm = MagicMock()
    mv = _movie(1, ["Y:/a.mkv"], [])
    res = reconcile_movie(mv, idx, VOCAB, pm, dry_run=False, additive_only=True)
    assert res["added"] == ["DV FEL"]
    pm.add_label.assert_called_once_with(1, "DV FEL")


def test_additive_only_adds_without_removing_stale():
    """A wrong-but-managed label is left in place; cleaning it up stays a
    deliberate manual-sync decision."""
    idx = {"y:/a.mkv": "fel"}
    pm = MagicMock()
    mv = _movie(1, ["Y:/a.mkv"], ["DV MEL"])
    res = reconcile_movie(mv, idx, VOCAB, pm, dry_run=False, additive_only=True)
    assert res["added"] == ["DV FEL"]
    assert res["removed"] == []
    pm.remove_label.assert_not_called()


def test_full_reconcile_still_removes_by_default():
    """Regression guard: the manual sync path must be unchanged."""
    idx = {}
    pm = MagicMock()
    mv = _movie(1, ["Y:/a.mkv"], ["DV FEL"])
    res = reconcile_movie(mv, idx, VOCAB, pm, dry_run=False)
    assert res["removed"] == ["DV FEL"]
