"""Version-count badges: 1,029 movies in this library have more than one
PLEX VERSION -- which is not the same as more than one file.

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
        flag the 1,029 exceptions."""
        assert version_label(1) is None
        assert version_label(0) is None

    def test_the_counts_that_actually_occur(self):
        # Live data: 983 twos, 45 threes, 1 four.
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
    def test_it_counts_DISTINCT_MEDIA_not_rows(self):
        """plex_cache stores one row per PART, not per version: multipart media
        gets separate cache keys but REUSES the media_id. Counting rows reported
        a one-version two-part film as "2 Versions".

        Six live titles are in that shape -- Friday the 13th: The New Blood is 2
        rows with 1 media_id and should carry NO badge; Lawrence of Arabia is 3
        rows with 2 and should read "2 Versions" (peer review H1)."""
        rows = [
            {"rating_key": "1", "media_id": "10"},
            {"rating_key": "1", "media_id": "10"},   # second PART of one version
            {"rating_key": "1", "media_id": "11"},
            {"rating_key": "2", "media_id": "20"},
        ]
        assert count_versions(rows) == {"1": 2, "2": 1}

    def test_a_multipart_single_version_gets_no_badge_at_all(self):
        """The live Friday the 13th case, end to end through the label."""
        rows = [{"rating_key": "9", "media_id": "90"},
                {"rating_key": "9", "media_id": "90"}]
        assert count_versions(rows) == {"9": 1}
        assert version_label(count_versions(rows)["9"]) is None

    def test_a_row_with_no_media_id_makes_its_TITLE_unknown(self):
        """Parts and versions become indistinguishable for that title, and an
        unknown count must touch nothing rather than be guessed. Dropping just
        the row would silently undercount instead."""
        rows = [{"rating_key": "1", "media_id": "10"},
                {"rating_key": "1", "media_id": None},
                {"rating_key": "2", "media_id": "20"}]
        counts = count_versions(rows)
        assert "1" not in counts, "a title with an unattributable row was counted anyway"
        assert counts == {"2": 1}

    def test_rows_with_no_rating_key_are_dropped_not_lumped_together(self):
        # Counting them into one bucket would invent a multi-version movie out
        # of unrelated orphans.
        rows = [{"rating_key": None, "media_id": "1"}, {"rating_key": "", "media_id": "2"},
                {"rating_key": "7", "media_id": "3"}]
        assert count_versions(rows) == {"7": 1}

    def test_keys_are_normalised_so_int_and_str_do_not_split(self):
        assert count_versions([{"rating_key": 5, "media_id": "a"},
                               {"rating_key": "5", "media_id": "b"}]) == {"5": 2}

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
        db.list_plex_cache_movies_strict.return_value = [
            {"rating_key": "1", "media_id": "a"},       # 2 distinct versions
            {"rating_key": "1", "media_id": "b"},
            {"rating_key": "2", "media_id": "c"},       # 1 version
        ]
        pm = self._pm([_movie(1), _movie(2)])
        out = sync_version_labels(db, pm, {"movie_libs": ["Movies"]}, dry_run=False)
        assert out["total"] == 2
        assert out["added_attempted"] == 1 and out["removed_attempted"] == 0
        assert out["multi_version"] == 1
        assert out["unknown"] == 0

    def test_uncached_titles_are_counted_separately_from_single_version(self):
        """Folding them together would hide a broken cache behind a plausible
        number."""
        db = MagicMock()
        db.list_plex_cache_movies_strict.return_value = [{"rating_key": "1", "media_id": "a"}]
        pm = self._pm([_movie(1), _movie(404)])
        out = sync_version_labels(db, pm, {"movie_libs": ["Movies"]}, dry_run=True)
        assert out["unknown"] == 1

    def test_a_failing_library_does_not_abort_the_others(self):
        db = MagicMock()
        db.list_plex_cache_movies_strict.return_value = [{"rating_key": "1", "media_id": "a"},
                                                  {"rating_key": "1", "media_id": "b"}]
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
        there stacks overlapping labels. Most of these 1,029 movies already
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


class TestTheSyncIsActuallyREACHABLE:
    """A module with tests and no caller ships nothing.

    This suite's other tests build their own inputs, so every one of them would
    still pass if `sync_version_labels` were never invoked by anything -- which
    was true for two days: 23 green tests, a live dry-run finding 1,029 movies,
    and zero badges on any poster.
    """

    def _pass_source(self):
        import ast
        import inspect
        import textwrap
        from backend.app_service import AppService
        return ast.parse(textwrap.dedent(inspect.getsource(AppService._run_maintenance_pass)))

    def test_the_maintenance_pass_calls_it(self):
        import ast
        calls = [n for n in ast.walk(self._pass_source())
                 if isinstance(n, ast.Call)
                 and isinstance(n.func, ast.Attribute)
                 and n.func.attr == "sync_version_labels"]
        assert calls, "nothing in the maintenance pass calls sync_version_labels"

    # The two lifecycle assertions that used to live here are gone on purpose.
    # They were source-shape tests for RUNTIME properties, and the review showed
    # both of its findings satisfied them: a baseline-only first pass still
    # "calls" the sync, and a partly-failed sync still assigns the watermark
    # after it. One of them then broke against correct code the moment the
    # assignment moved inside an `if result["complete"]` guard — a test that
    # tracks the shape of the code rather than what it does.
    #
    # TestTheWatermarkLifecycle below covers the same ground behaviourally.
    # What stays here is the single tripwire for the original regression:
    # nobody calls it at all.


class TestTheWatermarkLifecycle:
    """BEHAVIOURAL, not source-shape. The review's point: the AST tests prove a
    caller exists, and both of its findings satisfied them anyway — a
    baseline-only first pass never runs, and a partly-failed sync consumes the
    generation. Those are runtime properties, so they are tested at runtime.

    Drives the real `_run_maintenance_pass` block by calling it on a stub
    AppService, so the thing under test is the shipped code path.
    """

    def _svc(self, monkeypatch, latest, sync_result):
        """An AppService whose only live parts are the version-badge block."""
        from backend.app_service import AppService
        from backend.rename import version_labeler

        svc = AppService.__new__(AppService)
        svc.config = {"plex_enabled": True, "movie_libs": ["Movies"]}
        svc.db = MagicMock()
        svc.db.get_latest_plex_cache_at.return_value = latest

        calls = []

        def _fake_sync(db, pm, config, **kw):
            calls.append(True)
            return sync_result
        monkeypatch.setattr(version_labeler, "sync_version_labels", _fake_sync)

        # Plex present unless a test says otherwise.
        from backend.api import dependencies
        reg = MagicMock()
        reg._plex_service.plex_manager = MagicMock()
        monkeypatch.setattr(dependencies, "registry", reg)
        return svc, calls, reg

    OK = {"total": 3, "added": 1, "removed": 0, "multi_version": 1, "unknown": 0,
          "lib_failures": 0, "title_failures": 0, "write_failures": 0, "complete": True}
    PARTIAL = dict(OK, complete=False, write_failures=7)

    def _run(self, svc):
        from backend.app_service import AppService
        # Only the version block matters; the rest of the pass is tolerant of a
        # stub service because every section is individually try/except'd.
        AppService._run_maintenance_pass(svc)

    def test_the_EXISTING_generation_runs_on_the_first_pass(self, monkeypatch):
        """M1. A baseline-only first pass leaves the badges at zero until the
        cache happens to be rewritten — the same 'unreachable' failure one step
        later."""
        svc, calls, _ = self._svc(monkeypatch, 1000.0, self.OK)
        self._run(svc)
        assert calls, "the first pass skipped an already-populated cache"
        assert svc._last_version_cache_at == 1000.0

    def test_the_same_generation_does_not_run_twice(self, monkeypatch):
        svc, calls, _ = self._svc(monkeypatch, 1000.0, self.OK)
        self._run(svc)
        self._run(svc)
        assert len(calls) == 1, "an unchanged generation was reconciled again"

    def test_a_NEW_generation_runs_again(self, monkeypatch):
        """The positive control: without it, 'never run' would satisfy the test
        above and the feature would be dead."""
        svc, calls, _ = self._svc(monkeypatch, 1000.0, self.OK)
        self._run(svc)
        svc.db.get_latest_plex_cache_at.return_value = 2000.0
        self._run(svc)
        assert len(calls) == 2
        assert svc._last_version_cache_at == 2000.0

    def test_an_INCOMPLETE_sync_does_not_consume_the_generation(self, monkeypatch):
        """M2. Every failure inside the sync is caught so one bad title cannot
        abandon the rest, which makes 'returned' a useless success signal."""
        svc, calls, _ = self._svc(monkeypatch, 1000.0, self.PARTIAL)
        self._run(svc)
        assert calls, "the sync did not run at all"
        assert svc._last_version_cache_at is None, (
            "an incomplete pass advanced the watermark")

    def test_it_RETRIES_the_same_generation_after_an_incomplete_pass(self, monkeypatch):
        svc, calls, _ = self._svc(monkeypatch, 1000.0, self.PARTIAL)
        self._run(svc)
        self._run(svc)
        assert len(calls) == 2, "a failed generation was never retried"

    def test_a_successful_retry_finally_advances_it(self, monkeypatch):
        from backend.rename import version_labeler
        svc, calls, _ = self._svc(monkeypatch, 1000.0, self.PARTIAL)
        self._run(svc)
        assert svc._last_version_cache_at is None
        monkeypatch.setattr(version_labeler, "sync_version_labels",
                            lambda db, pm, config, **kw: self.OK)
        self._run(svc)
        assert svc._last_version_cache_at == 1000.0

    def test_plex_unavailable_leaves_the_generation_pending(self, monkeypatch):
        svc, calls, reg = self._svc(monkeypatch, 1000.0, self.OK)
        reg._plex_service = None
        self._run(svc)
        assert not calls, "the sync ran without Plex"
        assert svc._last_version_cache_at is None

    def test_a_RAISING_sync_leaves_the_generation_pending(self, monkeypatch):
        from backend.rename import version_labeler

        def _boom(db, pm, config, **kw):
            raise RuntimeError("plex exploded")
        monkeypatch.setattr(version_labeler, "sync_version_labels", _boom)
        svc, _, _ = self._svc(monkeypatch, 1000.0, self.OK)
        monkeypatch.setattr(version_labeler, "sync_version_labels", _boom)
        self._run(svc)
        assert svc._last_version_cache_at is None


class TestCompletenessIsEarnedByTheWRITES:
    """The lifecycle tests above feed a canned sync result, so they prove the
    CALLER honours `complete` — not that the sync ever sets it correctly.

    Found by mutation: removing the write-failure counter, and dropping
    write_failures from the `complete` expression, both left the suite green.
    These drive the real sync with a Plex that rejects writes.
    """

    def _pm(self, movies, add_raises=False, lib_raises=False):
        pm = MagicMock()
        if lib_raises:
            pm.get_library_section.side_effect = RuntimeError("library offline")
        else:
            lib = MagicMock()
            lib.all.return_value = movies
            pm.get_library_section.return_value = lib
        if add_raises:
            pm.add_label.side_effect = RuntimeError("plex rejected the write")
        return pm

    def _db(self):
        db = MagicMock()
        db.list_plex_cache_movies_strict.return_value = [
            {"rating_key": "1", "media_id": "a"},
            {"rating_key": "1", "media_id": "b"},   # 2 versions -> wants a badge
        ]
        return db

    def test_a_rejected_label_write_makes_the_pass_INCOMPLETE(self):
        out = sync_version_labels(self._db(), self._pm([_movie(1)], add_raises=True),
                                  {"movie_libs": ["Movies"]})
        assert out["write_failures"] == 1
        assert out["complete"] is False, "a rejected write still reported complete"

    def test_reconcile_reports_the_failed_write_rather_than_only_logging_it(self):
        pm = MagicMock()
        pm.add_label.side_effect = RuntimeError("nope")
        res = reconcile_movie_versions(_movie(1), {"1": 2}, pm, dry_run=False)
        assert res["failed"] == 1
        # ...and still reports what it INTENDED, so the caller can log both.
        assert res["added"] == ["2 Versions"]

    def test_a_library_that_will_not_enumerate_makes_the_pass_INCOMPLETE(self):
        """Its titles were never reconciled at all, so the generation is not
        done — even though nothing raised at the title level."""
        out = sync_version_labels(self._db(), self._pm([], lib_raises=True),
                                  {"movie_libs": ["Movies"]})
        assert out["lib_failures"] == 1
        assert out["complete"] is False

    def test_a_title_that_raises_makes_the_pass_INCOMPLETE(self):
        # Raise on `.labels`, which reconcile reads -- NOT on `.ratingKey`,
        # which the library WALK reads first and would be booked as a library
        # failure instead. The distinction is the point of the two counters.
        bad = MagicMock()
        bad.ratingKey = 1
        type(bad).labels = property(lambda _: (_ for _ in ()).throw(RuntimeError("boom")))
        pm = self._pm([bad])
        out = sync_version_labels(self._db(), pm, {"movie_libs": ["Movies"]})
        assert out["title_failures"] == 1
        assert out["complete"] is False

    def test_a_clean_pass_IS_complete(self):
        """The positive control. Without it, `complete: False` always would
        satisfy every assertion above and the badges would never persist."""
        out = sync_version_labels(self._db(), self._pm([_movie(1)]),
                                  {"movie_libs": ["Movies"]})
        assert out["write_failures"] == 0 and out["lib_failures"] == 0
        assert out["title_failures"] == 0
        assert out["complete"] is True
        assert out["added_attempted"] == 1


class TestAnEmptyReadIsNotASuccessfulRead:
    """Round 2's finding, in one sentence: the code had learned that a returned
    WRITE is not a successful write, but not the input rule — an empty or absent
    READ is not a successful read.

    Both paths below fail SOFT in production and so bypassed every counter:
    `get_library_section()` catches its own errors and returns None, and
    `list_plex_cache_movies()` turns a DB error into `[]`. Either one let the
    sync report complete and consume the cache generation without reconciling it.
    """

    def _db_ok(self):
        db = MagicMock()
        db.list_plex_cache_movies_strict.return_value = [
            {"rating_key": "1", "media_id": "a"},
            {"rating_key": "1", "media_id": "b"},
        ]
        return db

    def _pm(self, movies):
        pm = MagicMock()
        lib = MagicMock()
        lib.all.return_value = movies
        pm.get_library_section.return_value = lib
        return pm

    def test_a_library_that_RESOLVES_TO_NONE_is_incomplete(self):
        """Mechanism A. The round-1 test made the mock RAISE, which only proved
        the except branch. Production PlexManager catches its own connect and
        lookup failures and returns None — so this is what a real outage looks
        like, and `if not lib: continue` treated it as a harmless skip."""
        pm = MagicMock()
        pm.get_library_section.return_value = None
        out = sync_version_labels(self._db_ok(), pm, {"movie_libs": ["Movies"]})
        assert out["lib_failures"] == 1
        assert out["complete"] is False, "an unreadable library still earned complete"

    def test_a_FAILED_CACHE_READ_is_incomplete(self):
        """Mechanism B. A DB error made every live movie 'unknown', which the
        reconciler correctly leaves alone — so nothing raised, no counter moved,
        and the pass called itself complete while the badges went stale."""
        db = MagicMock()
        db.list_plex_cache_movies_strict.side_effect = RuntimeError("db unavailable")
        out = sync_version_labels(db, self._pm([_movie(1)]), {"movie_libs": ["Movies"]})
        assert out["cache_failures"] == 1
        assert out["complete"] is False
        # ...and it must not have guessed: no label was touched.
        assert out["added_attempted"] == 0 and out["removed_attempted"] == 0

    def test_a_GENUINELY_empty_cache_is_still_complete(self):
        """The distinction that matters. An empty table is a valid answer; only
        a failed read is not. Without this, 'always incomplete' would satisfy
        the test above and the watermark could never advance."""
        db = MagicMock()
        db.list_plex_cache_movies_strict.return_value = []
        out = sync_version_labels(db, self._pm([_movie(1)]), {"movie_libs": ["Movies"]})
        assert out["cache_failures"] == 0
        assert out["complete"] is True
        assert out["unknown"] == 1     # the movie is unknown, which is correct

    def test_a_genuinely_empty_LIBRARY_is_still_complete(self):
        """Same distinction on the other path: a library that resolves and
        contains nothing is not a failure."""
        out = sync_version_labels(self._db_ok(), self._pm([]), {"movie_libs": ["Movies"]})
        assert out["lib_failures"] == 0
        assert out["complete"] is True

    def test_the_strict_reader_is_preferred_over_the_soft_one(self):
        """A db exposing both must be read strictly, or the fix is inert."""
        db = self._db_ok()
        db.list_plex_cache_movies.return_value = []      # the soft path
        sync_version_labels(db, self._pm([_movie(1)]), {"movie_libs": ["Movies"]})
        db.list_plex_cache_movies_strict.assert_called_once()
        db.list_plex_cache_movies.assert_not_called()
