"""Version-count badges: 1,032 movies in this library have more than one file.

Two rules carry the weight, and both are the same rule the DV labeler learned:

  * a count with no label produces no Kometa overlay, so the badge vanishes
    silently and looks exactly like "this movie has one version" -- hence the
    5+ catch-all;
  * a title with no cached row is UNKNOWN, not "one version", and unknown must
    never authorise removing a badge.
"""
from unittest.mock import MagicMock

import pytest

from backend.rename.version_labeler import (
    MAX_EXACT_VERSIONS, VERSION_LABELS, count_versions, reconcile_movie_versions,
    sync_version_labels, version_label)


def _movie(rating_key, labels=()):
    mv = MagicMock()
    mv.ratingKey = rating_key
    objs = []
    for t in labels:
        lo = MagicMock(); lo.tag = t; objs.append(lo)
    mv.labels = objs
    return mv


class TestTheLabelForACount:
    def test_a_single_version_gets_no_badge(self):
        """Badging all 15,250 movies would make the badge noise. It exists to
        flag the 1,032 exceptions."""
        assert version_label(1) is None
        assert version_label(0) is None

    def test_the_counts_that_actually_occur(self):
        # Live data: 983 twos, 48 threes, 1 four.
        assert version_label(2) == "2 Versions"
        assert version_label(3) == "3 Versions"
        assert version_label(4) == "4 Versions"

    def test_beyond_the_exact_range_collapses_rather_than_vanishing(self):
        """THE guard. Kometa needs one overlay block per label, so a count with
        no label renders nothing — and a movie that silently loses its badge is
        indistinguishable from one that never had duplicates."""
        assert version_label(5) == "5+ Versions"
        assert version_label(9) == "5+ Versions"
        assert version_label(150) == "5+ Versions"

    def test_every_label_it_can_emit_is_in_the_closed_set(self):
        """The removal set must cover everything the add path can produce, or a
        stale badge could never be cleaned up."""
        emitted = {version_label(n) for n in range(2, 40)}
        assert emitted - {None} <= set(VERSION_LABELS)

    def test_junk_counts_do_not_produce_a_label(self):
        assert version_label(None) is None
        assert version_label("2") is None


class TestCounting:
    def test_it_counts_rows_per_title(self):
        rows = [{"rating_key": "10"}, {"rating_key": "10"}, {"rating_key": "11"}]
        assert count_versions(rows) == {"10": 2, "11": 1}

    def test_rows_with_no_rating_key_are_dropped_not_lumped_together(self):
        # Counting them into one bucket would invent a multi-version movie out
        # of unrelated orphans.
        rows = [{"rating_key": None}, {"rating_key": ""}, {"rating_key": "7"}]
        assert count_versions(rows) == {"7": 1}

    def test_keys_are_normalised_so_int_and_str_do_not_split(self):
        assert count_versions([{"rating_key": 5}, {"rating_key": "5"}]) == {"5": 2}

    def test_empty_input_is_empty_not_an_error(self):
        assert count_versions([]) == {} and count_versions(None) == {}


class TestReconcile:
    def test_it_badges_a_multi_version_movie(self):
        pm = MagicMock()
        res = reconcile_movie_versions(_movie(1), {"1": 3}, pm, dry_run=False)
        assert res["added"] == ["3 Versions"] and res["removed"] == []
        pm.add_label.assert_called_once_with(1, "3 Versions")

    def test_it_leaves_a_single_version_movie_alone(self):
        pm = MagicMock()
        res = reconcile_movie_versions(_movie(1), {"1": 1}, pm, dry_run=False)
        assert res["added"] == [] and res["removed"] == []
        pm.add_label.assert_not_called()

    def test_it_corrects_a_stale_count(self):
        """A version was deleted, so the badge must follow — that is what makes
        the label worth managing rather than applying once."""
        pm = MagicMock()
        res = reconcile_movie_versions(_movie(1, ["3 Versions"]), {"1": 2}, pm,
                                       dry_run=False)
        assert res["added"] == ["2 Versions"] and res["removed"] == ["3 Versions"]

    def test_it_removes_the_badge_when_the_duplicate_is_gone(self):
        pm = MagicMock()
        res = reconcile_movie_versions(_movie(1, ["2 Versions"]), {"1": 1}, pm,
                                       dry_run=False)
        assert res["removed"] == ["2 Versions"] and res["added"] == []

    def test_an_UNCACHED_title_is_left_completely_alone(self):
        """THE safety test. No cached row is UNKNOWN, not "one version". Reading
        a cache gap as evidence would strip a correct badge from every title the
        cache happens to be missing — the same failure the DV labeler's
        may_remove rules exist to prevent."""
        pm = MagicMock()
        res = reconcile_movie_versions(_movie(99, ["2 Versions"]), {}, pm,
                                       dry_run=False)
        assert res["removed"] == [] and res["added"] == []
        assert res["count"] is None
        pm.remove_label.assert_not_called()

    def test_it_never_touches_a_label_it_does_not_manage(self):
        """'DV FEL' and the owner's own labels are not this module's business."""
        pm = MagicMock()
        res = reconcile_movie_versions(
            _movie(1, ["DV FEL", "Christmas", "2 Versions"]), {"1": 2}, pm,
            dry_run=False)
        assert res["added"] == [] and res["removed"] == []
        pm.remove_label.assert_not_called()

    def test_dry_run_writes_nothing(self):
        pm = MagicMock()
        res = reconcile_movie_versions(_movie(1, ["3 Versions"]), {"1": 2}, pm,
                                       dry_run=True)
        assert res["added"] == ["2 Versions"] and res["removed"] == ["3 Versions"]
        pm.add_label.assert_not_called()
        pm.remove_label.assert_not_called()


class TestSync:
    def _pm(self, movies):
        pm = MagicMock()
        lib = MagicMock()
        lib.all.return_value = movies
        pm.get_library_section.return_value = lib
        return pm

    def test_it_reports_what_it_did(self):
        db = MagicMock()
        db.list_plex_cache_movies.return_value = [
            {"rating_key": "1"}, {"rating_key": "1"},   # 2 versions
            {"rating_key": "2"},                        # 1 version
        ]
        pm = self._pm([_movie(1), _movie(2)])
        out = sync_version_labels(db, pm, {"movie_libs": ["Movies"]}, dry_run=False)
        assert out["total"] == 2
        assert out["added"] == 1 and out["removed"] == 0
        assert out["multi_version"] == 1
        assert out["unknown"] == 0

    def test_uncached_titles_are_counted_separately_from_single_version(self):
        """Folding them together would hide a broken cache behind a plausible
        number."""
        db = MagicMock()
        db.list_plex_cache_movies.return_value = [{"rating_key": "1"}]
        pm = self._pm([_movie(1), _movie(404)])
        out = sync_version_labels(db, pm, {"movie_libs": ["Movies"]}, dry_run=True)
        assert out["unknown"] == 1

    def test_a_failing_library_does_not_abort_the_others(self):
        db = MagicMock()
        db.list_plex_cache_movies.return_value = [{"rating_key": "1"}, {"rating_key": "1"}]
        pm = MagicMock()
        good = MagicMock(); good.all.return_value = [_movie(1)]
        pm.get_library_section.side_effect = [RuntimeError("offline"), good]
        out = sync_version_labels(db, pm, {"movie_libs": ["Bad", "Good"]}, dry_run=True)
        assert out["total"] == 1 and out["multi_version"] == 1


class TestTheKometaConfigCoversEveryLabel:
    """A label with no overlay block renders NOTHING, and a poster that silently
    loses its badge looks exactly like a movie with one version. The label set
    and the config must therefore stay in lockstep — this is the assertion that
    makes the 5+ catch-all worth having."""

    def _overlays(self):
        import pathlib
        import yaml
        doc = yaml.safe_load(
            pathlib.Path("docs/kometa/version_badges.yml").read_text(encoding="utf-8"))
        return doc["overlays"]

    def test_every_emittable_label_has_an_overlay_block(self):
        gated = {block["plex_search"]["all"]["label"]
                 for block in self._overlays().values()}
        missing = set(VERSION_LABELS) - gated
        assert not missing, f"labels ScanHound can apply with no Kometa block: {missing}"

    def test_the_config_gates_nothing_scanhound_never_applies(self):
        """The other direction: a block for a label nothing emits is dead config
        that will quietly rot."""
        gated = {block["plex_search"]["all"]["label"]
                 for block in self._overlays().values()}
        assert gated - set(VERSION_LABELS) == set()

    def test_the_badges_do_not_draw_over_the_DV_badges(self):
        """dv_badges.yml draws every block top-LEFT and warns that a second one
        there stacks overlapping labels. Most of these 1,032 movies already
        carry a DV or HDR10 badge, so a collision would be the common case."""
        for name, block in self._overlays().items():
            o = block["overlay"]
            assert o["horizontal_align"] == "right", (
                f"{name} draws on the left, where the DV badge already is")


def test_it_shares_no_labels_with_the_dv_labeler():
    """The two closed sets must not overlap. If they did, whichever sync ran
    second would strip the other's badge -- the RETIRED_LABELS trap, which is
    why these labels are NOT in MANAGED."""
    from backend.rename.dv_labeler import MANAGED
    assert VERSION_LABELS & MANAGED == set()
