"""Two dv_scan rows naming ONE file must not let row ORDER pick the DV layer.

Several rows normalize onto a single key whenever the same file was recorded
under a drive letter and its UNC share, under different separators or case, or
simply stored twice under two spellings. Every index in dv_labeler used to be
built with a last-write-wins assignment in a loop, and get_dv_scans() orders
``last_seen_at DESC`` -- which is not a tie-break, because a bulk rescan stamps
thousands of rows within one second (the live table: 6,948 rows, 5 distinct
timestamps, 5,975 sharing one). The winner is whichever row the sorter emits
first among equals, and that shifts whenever rows are added: 346 keys have a
real layer on one row and a failed scan on the other, and the count resolving
wrongly measured 335 and then 0 on the same database twenty minutes apart.

Permutation invariance is therefore the property under test, not a nicety: every
case below is asserted in BOTH orders, because the old code passes one order of
each pair. A test that fed only one order would pass unchanged against the bug.
"""
from unittest.mock import MagicMock

from backend.rename.dv_labeler import (
    LAYER_CONFLICT, LAYER_DETECTION_FAILED, _index_by_normalized_path,
    build_index, build_index_and_paths, is_authoritative, pick_layer,
    reconcile_movie, sync_labels)

VOCAB = {"fel": "DV FEL", "mel": "DV MEL", "profile8": "DV8", "profile5": "DV5"}

#: One file, two spellings that differ only in separator and case — the exact
#: shape of the 118 dv_scan rows that are one file stored twice.
PATH_A = r"Y:\Movies\Alpha (2001)\Alpha.mkv"
PATH_B = "Y:/movies/alpha (2001)/alpha.mkv"
NORM = "y:/movies/alpha (2001)/alpha.mkv"


def _rows(*layers_and_paths):
    return [{"path": p, "dv_layer": lay} for lay, p in layers_and_paths]


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


def test_both_spellings_really_do_collide():
    """Positive control for every test in this file.

    If normalization stopped collapsing these two paths, every assertion below
    would pass trivially against two independent keys and prove nothing.
    """
    idx = build_index(_rows(("fel", PATH_A), ("mel", PATH_B)), mappings=[])
    assert list(idx) == [NORM], "the two spellings no longer collide"


def test_a_failed_rescan_cannot_erase_a_real_layer_in_either_order():
    """`unknown` is a FAILED detection, so it can never outvote a real finding.

    The live shape: 346 colliding keys carry a real layer on one row and a
    failed scan on the other (180 badge-producing), and 0 carry two different
    real layers. Under last-write-wins the two orders disagree — the ('fel',
    'unknown') order yields 'unknown' — so asserting both is what makes the
    test discriminating.

    Note what the damage actually is: `unknown` is non-destructive at the
    removal step, so it does not STRIP a badge. It fails to add or converge
    one. Measured at the consumer, an adverse row order applies 0 labels where
    a favourable one applies 72 across 30 titles.
    """
    for rows in (_rows(("fel", PATH_A), ("unknown", PATH_B)),
                 _rows(("unknown", PATH_A), ("fel", PATH_B))):
        idx, _, conflicts = _index_by_normalized_path(rows, mappings=[])
        assert idx[NORM] == "fel", f"row order decided the layer: {rows}"
        assert conflicts == {}, "a failure is not a disagreement"


def test_none_is_authoritative_and_survives_a_failed_rescan():
    """'none' means the detector RAN and found no Dolby Vision.

    It is evidence, unlike 'unknown', so a later failure must not downgrade it
    to "not scanned" — that would make an authoritative no-DV file oscillate
    between labelled and unlabelled as scans succeed and fail.
    """
    for rows in (_rows(("none", PATH_A), ("unknown", PATH_B)),
                 _rows(("unknown", PATH_A), ("none", PATH_B))):
        idx, _, conflicts = _index_by_normalized_path(rows, mappings=[])
        assert idx[NORM] == "none"
        assert conflicts == {}


def test_two_different_real_layers_are_a_conflict_in_either_order():
    """Two positive claims about ONE file contradict each other.

    _LAYER_RANK must NOT arbitrate here. It ranks the PARTS of one title, where
    "any part proving DV proves it for the title" holds; applied to two
    observations of a single file it would launder a disagreement into a
    confident 'fel'. The conflict gets its OWN value, LAYER_CONFLICT, rather
    than reusing 'unknown' -- see test_a_conflict_is_not_the_same_state_as_a
    _failed_scan for why that distinction is load-bearing -- and the layers are
    returned separately so the disagreement is not silent.
    """
    for rows in (_rows(("fel", PATH_A), ("mel", PATH_B)),
                 _rows(("mel", PATH_A), ("fel", PATH_B))):
        idx, _, conflicts = _index_by_normalized_path(rows, mappings=[])
        assert idx[NORM] == LAYER_CONFLICT, \
            f"a contradiction became a verdict: {rows}"
        assert conflicts == {NORM: ["fel", "mel"]}, "the conflict was not reported"


def test_none_versus_a_real_layer_is_also_a_conflict():
    """Both operands are authoritative, so this is a genuine contradiction —
    not the benign `<layer> vs unknown` case. Treating 'none' as merely absent
    evidence here would let one bad row quietly delete a correct FEL badge."""
    for rows in (_rows(("fel", PATH_A), ("none", PATH_B)),
                 _rows(("none", PATH_A), ("fel", PATH_B))):
        idx, _, conflicts = _index_by_normalized_path(rows, mappings=[])
        assert idx[NORM] == LAYER_CONFLICT
        assert conflicts == {NORM: ["fel", "none"]}


def test_norm_to_path_follows_the_winning_row():
    """The raw path must come from the row the LAYER came from.

    norm_to_path feeds the rating_key back-annotation, which addresses a row by
    its raw path string. Taking the layer from one row and the path from
    another would annotate a different row than the one that produced the
    verdict — a bug no assertion about the layer alone can see.
    """
    idx, norm_to_path, _ = _index_by_normalized_path(
        _rows(("unknown", PATH_A), ("fel", PATH_B)), mappings=[])
    assert idx[NORM] == "fel"
    assert norm_to_path[NORM] == PATH_B, "path came from the losing row"

    idx, norm_to_path, _ = _index_by_normalized_path(
        _rows(("fel", PATH_A), ("unknown", PATH_B)), mappings=[])
    assert idx[NORM] == "fel"
    assert norm_to_path[NORM] == PATH_A, "path came from the losing row"


def test_conflict_never_removes_a_label_under_full_reconcile():
    """The destructive mode is where a conflict must be inert.

    Guards a subtly wrong alternative: OMITTING the conflicting key from the
    index instead of mapping it to 'unknown'. Both look identical for a
    multi-part title, but for a single-part title an absent key makes
    pick_layer return None ("not our title"), and under additive_only=False
    that path REMOVES managed labels. Mapping to 'unknown' sets may_remove
    False in every mode. Only a full reconcile with pre-existing labels tells
    the two designs apart.
    """
    idx = build_index(_rows(("fel", PATH_A), ("mel", PATH_B)), mappings=[])
    pm = MagicMock()
    mv = _movie(7, [PATH_B], ["DV FEL", "DV7", "DV"])

    res = reconcile_movie(mv, idx, VOCAB, pm, dry_run=False, mappings=[],
                          additive_only=False)

    assert res["removed"] == [], "a contradiction stripped a managed label"
    assert res["added"] == [], "a contradiction invented a label"
    assert res["matched"] is False
    pm.remove_label.assert_not_called()


def test_conflict_never_back_writes_a_rating_key():
    """A contradiction must not persist Plex identity onto either row.

    matched=False is what gates the back-write in sync_labels, so this asserts
    the whole chain rather than the flag: the conflict reaches the database
    layer as no call at all.
    """
    rows = _rows(("fel", PATH_A), ("mel", PATH_B))

    class _DB:
        def get_dv_scans(self, **kw):
            return rows
        upsert_dv_scan = MagicMock(return_value=True)
        annotate_dv_scan_rating_key = MagicMock(return_value=True)

    pm = MagicMock()
    lib = MagicMock()
    lib.all.return_value = [_movie(42, [PATH_B], [])]
    pm.get_library_section.return_value = lib
    db = _DB()

    res = sync_labels(db, pm, {"movie_libs": ["Movies"]}, dry_run=False,
                      mappings=[])

    assert res["matched"] == 0
    assert res["layer_conflicts"] == 1, "the conflict was not counted"
    db.annotate_dv_scan_rating_key.assert_not_called()
    db.upsert_dv_scan.assert_not_called()


def test_a_real_layer_still_labels_normally():
    """Negative control. Without this, every assertion above would still pass
    if the collapse had been broken to return 'unknown' for everything."""
    rows = _rows(("fel", PATH_A), ("unknown", PATH_B))

    class _DB:
        def get_dv_scans(self, **kw):
            return rows
        upsert_dv_scan = MagicMock(return_value=True)
        annotate_dv_scan_rating_key = MagicMock(return_value=True)

    pm = MagicMock()
    lib = MagicMock()
    lib.all.return_value = [_movie(42, [PATH_B], [])]
    pm.get_library_section.return_value = lib
    db = _DB()

    res = sync_labels(db, pm, {"movie_libs": ["Movies"]}, dry_run=False,
                      mappings=[])

    assert res["matched"] == 1
    assert res["layer_conflicts"] == 0
    assert res["added"] == 3                       # DV FEL + DV7 + DV
    # And it addresses the row the LAYER came from, not the other spelling.
    db.annotate_dv_scan_rating_key.assert_called_once_with(PATH_A, "42")


def test_rows_that_do_not_collide_are_untouched():
    """Regression control for the ~99% of keys with a single row.

    A NULL layer must stay NULL and an 'unknown' must stay 'unknown': the
    collapse distinguishes them so a non-colliding key yields exactly what it
    did before this change.
    """
    rows = [
        {"path": "Y:/m/fel.mkv", "dv_layer": "fel"},
        {"path": "Y:/m/none.mkv", "dv_layer": "none"},
        {"path": "Y:/m/failed.mkv", "dv_layer": "unknown"},
        {"path": "Y:/m/null.mkv", "dv_layer": None},
        {"path": "", "dv_layer": "fel"},           # unusable path, dropped
    ]
    idx, norm_to_path, conflicts = _index_by_normalized_path(rows, mappings=[])

    assert idx == {"y:/m/fel.mkv": "fel", "y:/m/none.mkv": "none",
                   "y:/m/failed.mkv": "unknown", "y:/m/null.mkv": None}
    assert norm_to_path["y:/m/fel.mkv"] == "Y:/m/fel.mkv"
    assert conflicts == {}


def test_build_index_and_paths_still_returns_two_values():
    """Its two public callers unpack a pair; the conflicts are a third value on
    the private helper precisely so this signature did not have to change."""
    idx, norm_to_path = build_index_and_paths(
        _rows(("fel", PATH_A), ("unknown", PATH_B)), mappings=[])
    assert idx == {NORM: "fel"}
    assert norm_to_path == {NORM: PATH_A}


#: A second part of the same title, on its own normalized key.
SIBLING = "Y:/Movies/Alpha (2001)/Alpha-pt2.mkv"
SIBLING_NORM = "y:/movies/alpha (2001)/alpha-pt2.mkv"


def test_a_conflict_is_not_the_same_state_as_a_failed_scan():
    """Both are non-authoritative, and they are non-authoritative DIFFERENTLY.

    'unknown' means "no evidence for this part" and is meant to lose to a
    sibling's positive finding. A conflict means "evidence that contradicts
    itself" and must not. Collapsing the second onto the first is what created
    the multipart hole, so the distinctness is worth pinning.
    """
    assert LAYER_CONFLICT != LAYER_DETECTION_FAILED
    assert is_authoritative(LAYER_CONFLICT) is False
    assert is_authoritative(LAYER_DETECTION_FAILED) is False
    assert is_authoritative("none") is True


def test_ordinary_unknown_is_still_beaten_by_a_positive_sibling():
    """REGRESSION CONTROL for the fix to the multipart hole.

    "One part proving DV proves it for the title" is deliberate and predates
    this work. If the conflict fix had been implemented by making pick_layer
    pessimistic about `unknown` too, this is the test that would have caught
    it, and the whole existing suite depends on the behaviour.
    """
    idx = {"y:/a.mkv": LAYER_DETECTION_FAILED, "y:/b.mkv": "fel"}
    assert pick_layer(["y:/a.mkv", "y:/b.mkv"], idx) == "fel"
    assert pick_layer(["y:/b.mkv", "y:/a.mkv"], idx) == "fel"


def test_conflict_beats_a_positive_sibling_at_the_title_level():
    """A contradicting part poisons the title, in either part order.

    pick_layer's rank loop runs before its `unknown` handling, so a conflict
    reported as `unknown` was simply invisible whenever any sibling part had a
    positive layer. Profile 5 is the sibling on purpose: it is the LOWEST rank,
    so the contradicting file (fel or mel) would outrank it if the truth were
    known -- acting on the sibling is therefore not merely incomplete, it is
    likely wrong.
    """
    idx = {NORM: LAYER_CONFLICT, SIBLING_NORM: "profile5"}
    assert pick_layer([NORM, SIBLING_NORM], idx) == LAYER_CONFLICT
    assert pick_layer([SIBLING_NORM, NORM], idx) == LAYER_CONFLICT
    # And with a sibling that outranks nothing in question:
    idx_fel = {NORM: LAYER_CONFLICT, SIBLING_NORM: "fel"}
    assert pick_layer([NORM, SIBLING_NORM], idx_fel) == LAYER_CONFLICT
    assert pick_layer([SIBLING_NORM, NORM], idx_fel) == LAYER_CONFLICT


def test_conflicted_part_stops_removal_on_a_multipart_title():
    """The reviewer's counterexample, end to end, in a DESTRUCTIVE reconcile.

    One part's rows say fel and mel; a clean sibling part says profile5. Before
    the fix the title resolved to profile5, which is authoritative, so the
    labeler would add DV5/DV and REMOVE the DV FEL/DV7 the contradicting part
    may well justify. Both part orders, because the old failure was order-shaped.
    """
    rows = _rows(("fel", PATH_A), ("mel", PATH_B), ("profile5", SIBLING))
    idx = build_index(rows, mappings=[])
    assert idx[NORM] == LAYER_CONFLICT and idx[SIBLING_NORM] == "profile5"

    for files in ([PATH_B, SIBLING], [SIBLING, PATH_B]):
        pm = MagicMock()
        mv = _movie(11, files, ["DV FEL", "DV7", "DV"])
        res = reconcile_movie(mv, idx, VOCAB, pm, dry_run=False, mappings=[],
                              additive_only=False)
        assert res["layer"] == LAYER_CONFLICT, f"order {files} lost the conflict"
        assert res["removed"] == [], "a masked conflict stripped a label"
        assert res["added"] == [], "a masked conflict invented a label"
        assert res["matched"] is False
        pm.remove_label.assert_not_called()
        pm.add_label.assert_not_called()


def test_conflicted_part_blocks_the_rating_key_back_write_on_a_multipart_title():
    """Same shape, checked at the database boundary rather than the flag.

    The back-write loop annotates the first movie path merely PRESENT in the
    index, so a clean sibling making the title authoritative was enough to
    stamp a rating_key onto the contradicting path.
    """
    rows = _rows(("fel", PATH_A), ("mel", PATH_B), ("profile5", SIBLING))

    class _DB:
        def get_dv_scans(self, **kw):
            return rows
        upsert_dv_scan = MagicMock(return_value=True)
        annotate_dv_scan_rating_key = MagicMock(return_value=True)

    for files in ([PATH_B, SIBLING], [SIBLING, PATH_B]):
        pm = MagicMock()
        lib = MagicMock()
        lib.all.return_value = [_movie(11, files, [])]
        pm.get_library_section.return_value = lib
        db = _DB()
        db.annotate_dv_scan_rating_key.reset_mock()

        res = sync_labels(db, pm, {"movie_libs": ["Movies"]}, dry_run=False,
                          mappings=[])

        assert res["matched"] == 0, f"order {files} matched a conflicted title"
        assert res["layer_conflicts"] == 1
        db.annotate_dv_scan_rating_key.assert_not_called()


def test_a_clean_multipart_title_is_unaffected():
    """Negative control for the four tests above.

    Without it they would all still pass if the conflict state had been made so
    sticky that ordinary multi-version titles stopped being labelled at all --
    and 1,029 of this library's movies have more than one version.
    """
    rows = _rows(("mel", "Y:/m/one.mkv"), ("fel", "Y:/m/two.mkv"))
    idx = build_index(rows, mappings=[])
    pm = MagicMock()
    mv = _movie(12, ["Y:/m/one.mkv", "Y:/m/two.mkv"], [])

    res = reconcile_movie(mv, idx, VOCAB, pm, dry_run=False, mappings=[],
                          additive_only=False)

    assert res["layer"] == "fel", "fel must still outrank mel across parts"
    assert set(res["added"]) == {"DV FEL", "DV7", "DV"}
    assert res["matched"] is True


def test_seed_baseline_collapse_is_order_independent_too():
    """The seed index had the same last-write-wins shape.

    Its only consumer is the dry-run discrepancy report, so an order-dependent
    pick there makes that report disagree with itself across two runs over
    unchanged data — the kind of drift that gets read as a real change.
    """
    seed_rows = [{"path": PATH_A, "seed_layer": "unknown"},
                 {"path": PATH_B, "seed_layer": "fel"}]
    idx, _, conflicts = _index_by_normalized_path(
        seed_rows, mappings=[], layer_key="seed_layer")
    assert idx == {NORM: "fel"} and conflicts == {}

    seed_rows.reverse()
    idx, _, conflicts = _index_by_normalized_path(
        seed_rows, mappings=[], layer_key="seed_layer")
    assert idx == {NORM: "fel"} and conflicts == {}
