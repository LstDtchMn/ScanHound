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


def test_additive_only_converges_conflicting_label_when_path_is_matched():
    """A positive path match is sufficient to replace a stale managed label.

    The unattended safety rule still protects unmatched movies; it must not
    preserve a known-wrong FEL/MEL/P5/P8 label after an authoritative rescan.
    """
    idx = {"y:/a.mkv": "fel"}
    pm = MagicMock()
    mv = _movie(1, ["Y:/a.mkv"], ["DV MEL"])
    res = reconcile_movie(mv, idx, VOCAB, pm, dry_run=False, additive_only=True)
    assert res["added"] == ["DV FEL"]
    assert res["removed"] == ["DV MEL"]
    pm.remove_label.assert_called_once_with(1, "DV MEL")


def test_additive_only_removes_stale_label_for_authoritative_none_match():
    """A matched no-DV scan is evidence; an unmatched movie remains protected."""
    idx = {"y:/a.mkv": "none"}
    pm = MagicMock()
    mv = _movie(1, ["Y:/a.mkv"], ["DV FEL"])

    res = reconcile_movie(mv, idx, VOCAB, pm, dry_run=False, additive_only=True)

    assert res["matched"] is True
    assert res["added"] == []
    assert res["removed"] == ["DV FEL"]
    pm.remove_label.assert_called_once_with(1, "DV FEL")


def test_full_reconcile_still_removes_by_default():
    """Regression guard: the manual sync path must be unchanged."""
    idx = {}
    pm = MagicMock()
    mv = _movie(1, ["Y:/a.mkv"], ["DV FEL"])
    res = reconcile_movie(mv, idx, VOCAB, pm, dry_run=False)
    assert res["removed"] == ["DV FEL"]


# ── 'unknown' is a FAILED detection, not a finding ──────────────────────────
# The suite already pinned the authoritative cases ('fel' converges a stale
# label, 'none' removes one, no match preserves). It never pinned the failure
# case -- and that was the one that stripped labels: desired_label('unknown')
# is None, so the removal loop subtracted nothing and took every managed DV
# label off the title during the unattended hourly sync. A single unreadable
# file on a network mount was enough.

def test_additive_only_keeps_label_when_layer_is_unknown():
    idx = {"y:/a.mkv": "unknown"}  # detection FAILED for this file
    pm = MagicMock()
    mv = _movie(1, ["Y:/a.mkv"], ["DV FEL"])

    res = reconcile_movie(mv, idx, VOCAB, pm, dry_run=False, additive_only=True)

    assert res["removed"] == []
    assert res["added"] == []
    pm.remove_label.assert_not_called()


def test_unknown_is_not_reported_as_a_match():
    """sync_labels gates its rating_key back-write on `matched`; re-persisting
    an 'unknown' row on every pass is what made one failure sticky instead of
    letting the next host run retry it."""
    idx = {"y:/a.mkv": "unknown"}
    pm = MagicMock()
    mv = _movie(1, ["Y:/a.mkv"], ["DV FEL"])

    res = reconcile_movie(mv, idx, VOCAB, pm, dry_run=False, additive_only=True)

    assert res["matched"] is False
    assert res["layer"] == "unknown"  # still reported for diagnostics


def test_unknown_never_removes_even_in_a_full_reconcile():
    """REVERSED after peer review, and the reviewer was right.

    The first cut kept 'unknown' destructive outside additive_only and pinned
    that as a "negative control" -- which contradicted this module's own
    stated invariant that a failed detection is not evidence. A manual full
    reconcile may reconcile KNOWN evidence; it cannot convert a failed
    classification into proof of absence.
    """
    idx = {"y:/a.mkv": "unknown"}
    pm = MagicMock()
    mv = _movie(1, ["Y:/a.mkv"], ["DV FEL"])

    res = reconcile_movie(mv, idx, VOCAB, pm, dry_run=False, additive_only=False)

    assert res["removed"] == []
    pm.remove_label.assert_not_called()


def test_full_reconcile_still_removes_for_an_authoritative_none():
    """The behaviour the guard above must NOT break: a real 'no DV' finding
    still strips a stale label in either mode."""
    idx = {"y:/a.mkv": "none"}
    pm = MagicMock()
    mv = _movie(1, ["Y:/a.mkv"], ["DV FEL"])

    res = reconcile_movie(mv, idx, VOCAB, pm, dry_run=False, additive_only=False)

    assert res["removed"] == ["DV FEL"]


def test_full_reconcile_still_removes_for_an_unmatched_movie():
    """And the pre-existing full-reconcile policy for a title with no scan row
    at all is unchanged -- that is a coverage decision, not a failed one."""
    pm = MagicMock()
    mv = _movie(1, ["Y:/a.mkv"], ["DV FEL"])

    res = reconcile_movie(mv, {}, VOCAB, pm, dry_run=False, additive_only=False)

    assert res["removed"] == ["DV FEL"]


# ── multipart aggregation: incomplete coverage is never proof of absence ────

class TestMultipartAggregate:
    """A title with several parts must not have its labels deleted because of
    filesystem ordering or a part nobody scanned."""

    def test_none_plus_unknown_is_unknown_in_both_orders(self):
        idx = {"y:/a.mkv": "none", "y:/b.mkv": "unknown"}
        assert pick_layer(["y:/a.mkv", "y:/b.mkv"], idx) == "unknown"
        assert pick_layer(["y:/b.mkv", "y:/a.mkv"], idx) == "unknown"

    def test_none_plus_an_unscanned_part_is_unknown(self):
        idx = {"y:/a.mkv": "none"}
        assert pick_layer(["y:/a.mkv", "y:/missing.mkv"], idx) == "unknown"

    def test_every_part_none_is_authoritative_none(self):
        idx = {"y:/a.mkv": "none", "y:/b.mkv": "none"}
        assert pick_layer(["y:/a.mkv", "y:/b.mkv"], idx) == "none"

    def test_a_positive_finding_wins_over_an_unknown_sibling(self):
        idx = {"y:/a.mkv": "fel", "y:/b.mkv": "unknown"}
        assert pick_layer(["y:/a.mkv", "y:/b.mkv"], idx) == "fel"

    def test_no_matched_part_at_all_is_still_no_match(self):
        assert pick_layer(["y:/a.mkv", "y:/b.mkv"], {}) is None

    def test_a_mixed_title_keeps_its_label_end_to_end(self):
        # the consequence that matters: ordering must not delete a badge
        idx = {"y:/a.mkv": "none", "y:/b.mkv": "unknown"}
        pm = MagicMock()
        mv = _movie(1, ["Y:/a.mkv", "Y:/b.mkv"], ["DV FEL"])
        res = reconcile_movie(mv, idx, VOCAB, pm, dry_run=False,
                              additive_only=False)
        assert res["removed"] == []
        pm.remove_label.assert_not_called()


# --- disaster recovery: rebuilt Plex database -------------------------------
#
# Jesse asked (2026-08-09) what happens to the DV labels if he loses his Plex
# database. Labels live ONLY in Plex -- dv_file_tagging is false, so nothing is
# written into the video files -- so a fresh Plex starts with zero DV labels.
#
# The answer that matters is whether they can be REBUILT from dv_scan without
# re-reading 730 video files. That rests entirely on the labeler matching by
# normalized PATH rather than by Plex's internal ids, because a rebuilt database
# assigns brand-new ratingKeys to everything.
#
# These tests are written so they FAIL if that ever stops being true: the
# dv_scan rows carry rating_key values that deliberately DISAGREE with the
# movies' new ratingKeys. Under path matching the labels are restored anyway;
# under any id-based matching nothing would match at all.

def test_rebuilt_plex_restores_labels_from_dv_scan_by_path():
    # dv_scan rows as they survive in ScanHound's own database. The stale
    # rating_key column is the point: these are the ids from BEFORE the loss.
    rows = [
        {"path": "Y:/Movie 1 (14TB)/4K DV/Alpha (2001).mkv", "dv_layer": "fel",
         "rating_key": 111},
        {"path": "Y:/Movie 1 (14TB)/4K DV/Beta (2002).mkv", "dv_layer": "mel",
         "rating_key": 222},
    ]
    idx = build_index(rows, None)

    # The rebuilt library: same files on disk, BRAND-NEW ratingKeys, and no
    # labels at all -- the state a fresh Plex scan produces.
    pm = MagicMock()
    alpha = _movie(90001, ["Y:/Movie 1 (14TB)/4K DV/Alpha (2001).mkv"], [])
    beta = _movie(90002, ["Y:/Movie 1 (14TB)/4K DV/Beta (2002).mkv"], [])

    r1 = reconcile_movie(alpha, idx, VOCAB, pm, dry_run=False)
    r2 = reconcile_movie(beta, idx, VOCAB, pm, dry_run=False)

    assert r1["added"] == ["DV FEL"], "FEL label not restored after a Plex rebuild"
    assert r2["added"] == ["DV MEL"], "MEL label not restored after a Plex rebuild"
    # Written against the NEW ids, proving the stale rating_key column is unused
    # for both matching and writing.
    pm.add_label.assert_any_call(90001, "DV FEL")
    pm.add_label.assert_any_call(90002, "DV MEL")
    assert r1["removed"] == [] and r2["removed"] == []


def test_rebuilt_plex_recovery_works_under_additive_only():
    # The AUTOMATIC sync path runs additive_only=True. Recovery must work there
    # too, not only under the manual full reconcile -- otherwise the documented
    # recovery would need a mode Jesse has to know to select.
    rows = [{"path": "Y:/Movie 2 (8TB)/4K DV/Gamma (2003).mkv", "dv_layer": "profile8",
             "rating_key": 333}]
    idx = build_index(rows, None)
    pm = MagicMock()
    mv = _movie(90003, ["Y:/Movie 2 (8TB)/4K DV/Gamma (2003).mkv"], [])

    res = reconcile_movie(mv, idx, VOCAB, pm, dry_run=False, additive_only=True)

    assert res["added"] == ["DV P8"]
    pm.add_label.assert_called_once_with(90003, "DV P8")


def test_rebuilt_plex_does_not_touch_unmanaged_user_labels():
    # A restored library may already carry the user's OWN labels (collections,
    # favourites, and the 'DV Cut'-style names a prefix wildcard once deleted).
    # Recovery must add the managed label WITHOUT disturbing any of them.
    rows = [{"path": "Y:/Movie 1 (14TB)/4K DV/Delta (2004).mkv", "dv_layer": "fel"}]
    idx = build_index(rows, None)
    pm = MagicMock()
    mv = _movie(90004, ["Y:/Movie 1 (14TB)/4K DV/Delta (2004).mkv"],
                ["DV Cut", "Christmas", "Favorites"])

    res = reconcile_movie(mv, idx, VOCAB, pm, dry_run=False)

    assert res["added"] == ["DV FEL"]
    assert res["removed"] == [], "recovery removed a label it does not manage"
    pm.remove_label.assert_not_called()


def test_recovery_cannot_invent_labels_for_unscanned_movies():
    # The negative control. If a movie has no dv_scan row -- e.g. it was added
    # after the last scan -- recovery must leave it unlabelled rather than
    # guessing. Without this, the three tests above would pass even if
    # reconcile_movie labelled everything it saw.
    idx = build_index([{"path": "Y:/Movie 1 (14TB)/4K DV/Alpha (2001).mkv",
                        "dv_layer": "fel"}], None)
    pm = MagicMock()
    stranger = _movie(90005, ["Y:/Movie 9 (1TB)/4K DV/Unscanned (2026).mkv"], [])

    res = reconcile_movie(stranger, idx, VOCAB, pm, dry_run=False,
                          additive_only=True)

    assert res["added"] == [] and res["removed"] == []
    pm.add_label.assert_not_called()
