"""A partial Plex load must never be treated as authoritative.

Covers three audit-pass-2 findings in backend/plex_service.py, all of which end
at the same place: something less than a full picture of Plex is accepted as
complete, and then ``save_plex_cache(full_replace=True)`` prunes every cached
row the partial picture didn't contain.

- SH-H12 (plex_service.py:245/302) — a library whose section won't resolve is
  skipped without marking the content type incomplete.
- SH-H13 (plex_service.py:163/675) — a cache holding only ONE content type is
  accepted as a complete index, and check_cache_status calls it valid.
- SH-M25 (plex_service.py:707) — the new-content probe cannot tell "nothing
  new" from "the probe failed", and asserts the cache is valid either way.

Read the POSITIVE CONTROLS first. A "fix" that simply stopped full-replacing,
or that always invalidated the cache, would pass every failure test in this
file — the controls are what rule those out.
"""

import time
from unittest.mock import MagicMock

from backend.database import DatabaseManager
from backend.plex_service import PlexService


# ── Helpers ──────────────────────────────────────────────────────────

def _make_service(config=None, db=None, plex_manager=None, connected=True):
    pm = plex_manager or MagicMock()
    # Explicit: a bare MagicMock's is_connected is truthy, which would let the
    # new-content probe run in tests that mean to isolate from it.
    pm.is_connected = connected
    return PlexService(config=config or {}, db=db or MagicMock(), plex_manager=pm)


def _mock_movie(title, rating_key, res="1080", imdb=None, size=5 * 1024 ** 3):
    """A Plex movie object with exactly one media/one part.

    The cache key plex_service builds from this is
    f"{rating_key}_{rating_key * 10}_0" — seed tests use that form directly.
    """
    stream = MagicMock()
    part = MagicMock()
    part.size = size
    part.file = f"/movies/{title}.mkv"
    part.videoStreams.return_value = [stream]
    media = MagicMock()
    media.id = rating_key * 10
    media.videoResolution = res
    media.parts = [part]
    guid = MagicMock()
    guid.id = f"imdb://{imdb or f'tt{rating_key:07d}'}"
    movie = MagicMock()
    movie.title = title
    movie.year = 2020
    movie.ratingKey = rating_key
    movie.originalLanguage = "en"
    movie.guids = [guid]
    movie.media = [media]
    return movie


def _mock_show(title, rating_key, season_key):
    stream = MagicMock()
    part = MagicMock()
    part.size = 2 * 1024 ** 3
    part.videoStreams.return_value = [stream]
    media = MagicMock()
    media.videoResolution = "1080"
    media.parts = [part]
    episode = MagicMock()
    episode.media = [media]
    season = MagicMock()
    season.index = 1
    season.ratingKey = season_key
    season.episodes.return_value = [episode]
    guid = MagicMock()
    guid.id = f"imdb://tt{rating_key:07d}"
    show = MagicMock()
    show.title = title
    show.year = 2020
    show.ratingKey = rating_key
    show.originalLanguage = "en"
    show.guids = [guid]
    show.seasons.return_value = [season]
    return show


def _movie_section(*movies):
    lib = MagicMock()
    lib.type = "movie"
    lib.all.return_value = list(movies)
    return lib


def _tv_section(*shows):
    lib = MagicMock()
    lib.type = "show"
    lib.all.return_value = list(shows)
    return lib


def _sections(mapping):
    """get_library_section side effect: name → section, or None to fail."""
    return lambda name: mapping.get(name)


def _saves(db, mode):
    return [c for c in db.save_plex_cache.call_args_list if c.args[1] == mode]


def _full_replaced(db, mode):
    """Did any save for `mode` request the content-type-wide prune?"""
    return [c for c in _saves(db, mode)
            if c.kwargs.get("full_replace") or (len(c.args) > 3 and c.args[3])]


# ======================================================================
# SH-H12 — an unresolvable library must make the load incomplete
# ======================================================================

class TestUnresolvedLibraryBlocksFullReplace:

    def test_unresolved_movie_library_blocks_movies_full_replace(self):
        # "Movies 4K" fails to resolve (get_library_section swallows the
        # timeout / NotFound / auth blip and returns None). _movies then holds
        # only the 1080p library, and a full_replace would delete every cached
        # 4K row — the exact reported damage.
        db = MagicMock()
        pm = MagicMock()
        pm.get_library_section.side_effect = _sections({
            "Movies 1080p": _movie_section(_mock_movie("Movie A", 1)),
            "Movies 4K": None,
            "TV": _tv_section(_mock_show("Show A", 100, 200)),
        })
        svc = _make_service(
            config={"movie_libs": ["Movies 1080p", "Movies 4K"], "tv_libs": ["TV"]},
            db=db, plex_manager=pm,
        )

        svc.load_libraries()

        assert not _full_replaced(db, "Movies"), (
            "A movie library that failed to resolve must not full_replace the cache"
        )
        # DISAGREEING CASE: an implementation that marked the whole load
        # incomplete (or that just deleted full_replace=True outright) would
        # also pass the assertion above. TV loaded cleanly here, so its prune
        # must still happen — this pins the flag to the right content type.
        tv_replaced = _full_replaced(db, "TV Shows")
        assert len(tv_replaced) == 1
        assert tv_replaced[0].kwargs.get("full_replace") is True

    def test_unresolved_tv_library_blocks_tv_full_replace(self):
        db = MagicMock()
        pm = MagicMock()
        pm.get_library_section.side_effect = _sections({
            "Movies": _movie_section(_mock_movie("Movie A", 1)),
            "TV 1080p": _tv_section(_mock_show("Show A", 100, 200)),
            "TV 4K": None,
        })
        svc = _make_service(
            config={"movie_libs": ["Movies"], "tv_libs": ["TV 1080p", "TV 4K"]},
            db=db, plex_manager=pm,
        )

        svc.load_libraries()

        assert not _full_replaced(db, "TV Shows"), (
            "A TV library that failed to resolve must not full_replace the cache"
        )
        # Mirror of the movie case: movies were complete, so they still prune.
        movie_replaced = _full_replaced(db, "Movies")
        assert len(movie_replaced) == 1
        assert movie_replaced[0].kwargs.get("full_replace") is True

    def test_healthy_multi_library_load_still_full_replaces_with_all_items(self):
        # POSITIVE CONTROL. Both libraries resolve, so pruning must still run —
        # and the payload must carry BOTH libraries' items, since the prune is
        # content-type-wide and anything missing from it gets deleted.
        db = MagicMock()
        pm = MagicMock()
        pm.get_library_section.side_effect = _sections({
            "Movies 1080p": _movie_section(_mock_movie("Movie A", 1)),
            "Movies 4K": _movie_section(_mock_movie("Movie B", 2, res="4k")),
        })
        svc = _make_service(
            config={"movie_libs": ["Movies 1080p", "Movies 4K"], "tv_libs": []},
            db=db, plex_manager=pm,
        )

        svc.load_libraries()

        movie_saves = _saves(db, "Movies")
        assert len(movie_saves) == 1
        assert movie_saves[0].kwargs.get("full_replace") is True
        saved_libs = {i["library_name"] for i in movie_saves[0].args[0]}
        assert saved_libs == {"Movies 1080p", "Movies 4K"}


class TestUnresolvedLibraryAgainstRealDatabase:
    """Consumer-level proof: what actually survives in plex_cache."""

    def _seed(self, tmp_path):
        db = DatabaseManager(db_path=str(tmp_path / "plex.db"))
        db.save_plex_cache(
            [
                {"key": "1_10_0", "clean_title": "movie a", "original_title": "Movie A",
                 "year": 2020, "res": "1080p", "size": 5.0, "imdb_id": "tt0000001",
                 "rating_key": 1, "media_id": 10, "library_name": "Movies 1080p"},
                {"key": "2_20_0", "clean_title": "movie b", "original_title": "Movie B",
                 "year": 2020, "res": "4K", "size": 50.0, "imdb_id": "tt0000002",
                 "rating_key": 2, "media_id": 20, "library_name": "Movies 4K"},
            ],
            "Movies", full_replace=False,
        )
        return db

    def test_healthy_load_prunes_stale_rows_and_keeps_both_libraries(self, tmp_path):
        # POSITIVE CONTROL, end to end. A stale row that Plex no longer has
        # must still be pruned by a healthy load — this is the behaviour the
        # fix has to preserve, and the one a "never full_replace" fix breaks.
        db = self._seed(tmp_path)
        db.save_plex_cache(
            [{"key": "999_9990_0", "clean_title": "deleted movie",
              "original_title": "Deleted Movie", "year": 2019, "res": "1080p",
              "size": 4.0, "imdb_id": "tt0000999", "rating_key": 999,
              "media_id": 9990, "library_name": "Movies 1080p"}],
            "Movies", full_replace=False,
        )
        assert {r["key"] for r in db.load_plex_cache("Movies")} == {
            "1_10_0", "2_20_0", "999_9990_0"}

        pm = MagicMock()
        pm.get_library_section.side_effect = _sections({
            "Movies 1080p": _movie_section(_mock_movie("Movie A", 1)),
            "Movies 4K": _movie_section(_mock_movie("Movie B", 2, res="4k")),
        })
        svc = _make_service(
            config={"movie_libs": ["Movies 1080p", "Movies 4K"], "tv_libs": []},
            db=db, plex_manager=pm,
        )

        svc.load_libraries()

        remaining = {r["key"] for r in db.load_plex_cache("Movies")}
        assert remaining == {"1_10_0", "2_20_0"}, (
            "A healthy full load must still prune rows Plex no longer has"
        )
        db.close()

    def test_unresolved_library_does_not_delete_the_other_librarys_rows(self, tmp_path):
        # The reported damage, measured on real storage: "Movies 4K" fails to
        # resolve, and every 4K row is deleted from plex_cache.
        db = self._seed(tmp_path)
        pm = MagicMock()
        pm.get_library_section.side_effect = _sections({
            "Movies 1080p": _movie_section(_mock_movie("Movie A", 1)),
            "Movies 4K": None,
        })
        svc = _make_service(
            config={"movie_libs": ["Movies 1080p", "Movies 4K"], "tv_libs": []},
            db=db, plex_manager=pm,
        )

        svc.load_libraries()

        remaining = {r["key"] for r in db.load_plex_cache("Movies")}
        assert "2_20_0" in remaining, (
            "The unreadable library's cached rows must survive, not be pruned"
        )
        assert remaining == {"1_10_0", "2_20_0"}
        db.close()


# ======================================================================
# SH-H13 — a single-content-type cache is not a complete index
# ======================================================================

class TestCachePathRequiresConfiguredTypes:

    def test_movies_only_cache_with_tv_configured_falls_back_to_full_load(self):
        db = MagicMock()
        db.load_plex_cache.side_effect = lambda mode: (
            [{"clean_title": "movie a", "res": "1080p", "imdb_id": "tt1", "rating_key": 1}]
            if mode == "Movies" else []
        )
        pm = MagicMock()
        pm.get_library_section.side_effect = _sections({
            "Movies": _movie_section(_mock_movie("Movie A", 1)),
            "TV": _tv_section(_mock_show("Show A", 100, 200)),
        })
        svc = _make_service(
            config={"movie_libs": ["Movies"], "tv_libs": ["TV"]},
            db=db, plex_manager=pm,
        )

        svc.load_libraries(use_cache=True)

        assert pm.get_library_section.called, (
            "A cache with no TV rows while TV libraries are configured must not "
            "short-circuit the load"
        )
        assert svc.plex_tv, "The fallback full load must actually populate TV"
        assert svc.stats["tv_seasons"] == 1

    def test_complete_cache_is_still_used_without_touching_plex(self):
        # POSITIVE CONTROL. The cache fast path is the whole point of
        # use_cache=True; a fix that always fell through to a live load would
        # pass the previous test and destroy this one.
        cached_movies = [{"clean_title": "movie a", "res": "4K",
                          "imdb_id": "tt1", "rating_key": 1}]
        cached_tv = [{"clean_title": "show a", "season": 1,
                      "imdb_id": "tt2", "rating_key": 2}]
        db = MagicMock()
        db.load_plex_cache.side_effect = lambda mode: (
            cached_movies if mode == "Movies" else cached_tv)
        pm = MagicMock()
        svc = _make_service(
            config={"movie_libs": ["Movies"], "tv_libs": ["TV"]},
            db=db, plex_manager=pm,
        )

        svc.load_libraries(use_cache=True)

        assert svc.plex_movies == cached_movies
        assert svc.plex_tv == cached_tv
        assert not pm.get_library_section.called
        assert not db.save_plex_cache.called

    def test_movies_only_cache_is_used_when_no_tv_libraries_configured(self):
        # DISAGREEING CASE: an implementation that demanded both content types
        # unconditionally would fall through to a full load here. A user with
        # no TV libraries has a legitimately movies-only cache.
        cached_movies = [{"clean_title": "movie a", "res": "4K",
                          "imdb_id": "tt1", "rating_key": 1}]
        db = MagicMock()
        db.load_plex_cache.side_effect = lambda mode: (
            cached_movies if mode == "Movies" else [])
        pm = MagicMock()
        svc = _make_service(
            config={"movie_libs": ["Movies"], "tv_libs": []},
            db=db, plex_manager=pm,
        )

        svc.load_libraries(use_cache=True)

        assert svc.plex_movies == cached_movies
        assert not pm.get_library_section.called

    def test_legacy_known_tv_libraries_key_also_counts_as_configured(self):
        # tv_libs is empty but the legacy key is populated — load_libraries
        # already falls back to it for the full load, so cache coverage has to
        # honour the same fallback or the two disagree about what's configured.
        db = MagicMock()
        db.load_plex_cache.side_effect = lambda mode: (
            [{"clean_title": "movie a", "res": "4K", "imdb_id": "tt1", "rating_key": 1}]
            if mode == "Movies" else []
        )
        pm = MagicMock()
        pm.get_library_section.side_effect = _sections({
            "Movies": _movie_section(_mock_movie("Movie A", 1)),
            "TV": _tv_section(_mock_show("Show A", 100, 200)),
        })
        svc = _make_service(
            config={"movie_libs": ["Movies"], "tv_libs": [],
                    "known_tv_libraries": ["TV"]},
            db=db, plex_manager=pm,
        )

        svc.load_libraries(use_cache=True)

        assert pm.get_library_section.called
        assert svc.plex_tv


class TestCheckCacheStatusRequiresConfiguredTypes:

    def _db(self, timestamps):
        db = MagicMock()
        db.get_plex_cache_max_timestamp.return_value = timestamps
        return db

    def test_invalid_when_configured_tv_type_has_no_rows(self):
        now = time.time()
        svc = _make_service(
            config={"cache_duration": 4, "movie_libs": ["Movies"], "tv_libs": ["TV"]},
            db=self._db({"Movies": now}), connected=False,  # isolate from the probe
        )

        valid, msg = svc.check_cache_status()

        assert valid is False
        assert "TV Shows" in msg

    def test_invalid_when_configured_movie_type_has_no_rows(self):
        now = time.time()
        svc = _make_service(
            config={"cache_duration": 4, "movie_libs": ["Movies"], "tv_libs": ["TV"]},
            db=self._db({"TV Shows": now}), connected=False,
        )

        valid, msg = svc.check_cache_status()

        assert valid is False
        assert "Movies" in msg

    def test_valid_when_every_configured_type_is_present_and_fresh(self):
        # POSITIVE CONTROL. A fix that returned False whenever a type was
        # missing from `timestamps` without consulting the config would still
        # pass both tests above; this is what stops it.
        now = time.time()
        svc = _make_service(
            config={"cache_duration": 4, "movie_libs": ["Movies"], "tv_libs": ["TV"]},
            db=self._db({"Movies": now, "TV Shows": now}), connected=False,
        )

        valid, msg = svc.check_cache_status()

        assert valid is True
        assert msg == ""

    def test_valid_when_the_absent_type_is_not_configured(self):
        # DISAGREEING CASE: movies-only setup, movies-only cache. Still valid.
        now = time.time()
        svc = _make_service(
            config={"cache_duration": 4, "movie_libs": ["Movies"], "tv_libs": []},
            db=self._db({"Movies": now}), connected=False,
        )

        valid, msg = svc.check_cache_status()

        assert valid is True
        assert msg == ""


# ======================================================================
# SH-M25 — "no new content" must not be indistinguishable from a failure
# ======================================================================

class TestNewContentProbeFailsClosed:

    def _service(self, pm, recently_added=None, raises=None):
        now = time.time()
        db = MagicMock()
        db.get_plex_cache_max_timestamp.return_value = {"Movies": now, "TV Shows": now}
        pm.is_connected = True
        if raises is not None:
            pm.get_recently_added.side_effect = raises
        else:
            pm.get_recently_added.return_value = recently_added
        return _make_service(
            config={"cache_duration": 4, "movie_libs": ["Movies"], "tv_libs": ["TV"],
                    "plex_invalidate_on_new_content": True},
            db=db, plex_manager=pm,
        )

    def test_empty_result_with_unreachable_libraries_invalidates_cache(self):
        # Plex is mid-restart: get_recently_added swallows the per-library
        # errors and returns []. Nothing was actually searched, so "no new
        # content" is not an answer.
        pm = MagicMock()
        pm.get_library_section.return_value = None
        svc = self._service(pm, recently_added=[])

        valid, msg = svc.check_cache_status()

        assert valid is False
        assert msg, "An invalid cache must carry a reason, not an empty message"
        assert "verify" in msg.lower()

    def test_empty_result_with_reachable_libraries_keeps_cache_valid(self):
        # POSITIVE CONTROL, and the one that matters most here: Plex is up and
        # genuinely has nothing new. Failing closed on THIS would force a full
        # Plex reload on every incremental scan.
        pm = MagicMock()
        pm.get_library_section.return_value = MagicMock()  # section resolves
        svc = self._service(pm, recently_added=[])

        valid, msg = svc.check_cache_status()

        assert valid is True
        assert msg == ""

    def test_reachability_check_stops_at_the_first_resolving_library(self):
        # DISAGREEING CASE: a check that required EVERY configured library to
        # resolve would invalidate the cache here, even though the server
        # clearly answered — one renamed library would then force a reload on
        # every scan forever.
        pm = MagicMock()
        pm.get_library_section.side_effect = _sections({"Movies": MagicMock(), "TV": None})
        svc = self._service(pm, recently_added=[])

        valid, msg = svc.check_cache_status()

        assert valid is True
        assert msg == ""

    def test_new_items_still_invalidate_cache(self):
        # POSITIVE CONTROL for the detection path itself — unchanged behaviour.
        pm = MagicMock()
        pm.get_library_section.return_value = MagicMock()
        svc = self._service(pm, recently_added=[MagicMock(), MagicMock(), MagicMock()])

        valid, msg = svc.check_cache_status()

        assert valid is False
        assert "3 new item(s)" in msg

    def test_probe_exception_invalidates_cache_instead_of_being_swallowed(self):
        pm = MagicMock()
        pm.get_library_section.return_value = MagicMock()
        svc = self._service(pm, raises=RuntimeError("plex unreachable"))

        valid, msg = svc.check_cache_status()

        assert valid is False
        assert "plex unreachable" in msg

    def test_none_result_is_treated_as_unknown(self):
        # Forward compatibility with the other half of the suggested fix
        # (giving plex_manager.get_recently_added an Optional return). None
        # must never be read as "nothing new" if that lands later.
        pm = MagicMock()
        pm.get_library_section.return_value = MagicMock()
        svc = self._service(pm, recently_added=None)

        valid, msg = svc.check_cache_status()

        assert valid is False
        assert "verify" in msg.lower()

    def test_probe_is_skipped_entirely_when_invalidation_is_disabled(self):
        # DISAGREEING CASE: the corroboration must live inside the existing
        # opt-out, not above it. plex_invalidate_on_new_content=False means the
        # operator asked for no probe at all — an unreachable Plex must not
        # invalidate the cache through the back door.
        now = time.time()
        db = MagicMock()
        db.get_plex_cache_max_timestamp.return_value = {"Movies": now, "TV Shows": now}
        pm = MagicMock()
        pm.is_connected = True
        pm.get_library_section.return_value = None
        svc = _make_service(
            config={"cache_duration": 4, "movie_libs": ["Movies"], "tv_libs": ["TV"],
                    "plex_invalidate_on_new_content": False},
            db=db, plex_manager=pm,
        )

        valid, msg = svc.check_cache_status()

        assert valid is True
        assert msg == ""
        assert not pm.get_recently_added.called
