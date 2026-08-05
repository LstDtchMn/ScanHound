"""A partial Plex load must not become the complete live authority (SH-P1).

``test_plex_partial_load_authority.py`` proved the DISK half: an incomplete load
no longer full-replaces plex_cache, so the unreadable library's rows survive.
That is only half the guarantee. ``plex_movies``/``plex_tv`` are what
``_build_plex_index`` turns into the matcher's ONLY view of the library, and the
partial load was still assigned to them — so a library that failed to read had
its rows sitting safely in SQLite while every one of its titles disappeared from
the index. Measured before the fix, with the real matcher and the real
AutoGrabService driving:

    plex_movies (live authority): ['alpha one']       # 'zulu nine' gone
    cache rows on disk: ['1_10_0', '2_20_0']          # but still cached
    STATUS of a title in the FAILED library: ScanStatus.MISSING
    AUTOGRAB grabbed: 1
    download_item called: [call(url='http://x/zulu-nine', ...)]

So this file deliberately does NOT stop at plex_service's own boundary. It drives
``ScannerService._match_against_plex`` with a real ``MatchingEngine`` and then
feeds the resulting items to a real ``AutoGrabService``, because "the cached rows
were not deleted" was already true while the bug was live.

Read the POSITIVE CONTROLS and DISAGREEING CASES first. Two wrong fixes pass
every failure assertion here on their own:
  * "union the whole cache into the index every time" — caught by
    test_title_deleted_from_the_healthy_library_stays_missing_and_is_grabbed;
  * "never stamp / always reset the freshness timestamp" — caught by
    test_complete_load_advances_the_freshness_stamp and
    test_scanner_suppresses_reload_after_a_complete_load.
"""

import asyncio
import time
from unittest.mock import MagicMock

import pytest

from backend.auto_grab_service import AutoGrabService
from backend.database import DatabaseManager
from backend.matching import MatchingEngine, clear_fuzzy_cache
from backend.plex_service import PlexService
from backend.scanner_service import MediaItem, ScanStatus, ScannerService

from tests.conftest import MockApp


# ── Plex object doubles ──────────────────────────────────────────────

class _Stream:
    """A plain SDR video stream.

    Deliberately NOT a MagicMock. ``PlexService._check_dovi`` probes with
    ``hasattr(stream, 'DOViProfile')``, and a MagicMock answers True to every
    attribute name, so every mocked movie comes back as Dolby Vision and
    ``plex_info`` assertions become meaningless.
    """
    colorPrimaries = None
    DOVIPresent = "false"
    displayTitle = ""
    title = ""
    profile = ""
    codec = "h264"
    _data: dict = {}


def _mock_movie(title, rating_key, res="1080", size_gb=5.0, broken=False):
    """A Plex movie with one media/one part.

    The cache key plex_service derives is f"{rating_key}_{rating_key * 10}_0";
    the seeded cache rows below use that form directly.

    broken=True gives the media NO parts, so ``_extract_movie_data`` returns None
    while ``movie.media`` stays truthy — the item-level extraction failure of
    SH-H07, which degrades its library without failing the whole thing.
    """
    part = MagicMock()
    part.size = int(size_gb * 1024 ** 3)
    part.file = f"/movies/{title}.mkv"
    part.videoStreams.return_value = [_Stream()]
    media = MagicMock()
    media.id = rating_key * 10
    media.videoResolution = res
    media.parts = [] if broken else [part]
    guid = MagicMock()
    guid.id = f"imdb://tt{rating_key:07d}"
    movie = MagicMock()
    movie.title = title
    movie.year = 2020
    movie.ratingKey = rating_key
    movie.originalLanguage = "en"
    movie.guids = [guid]
    movie.media = [media]
    return movie


def _mock_show(title, rating_key, season_key, broken=False):
    part = MagicMock()
    part.size = 2 * 1024 ** 3
    part.videoStreams.return_value = [_Stream()]
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
    if broken:
        show.seasons.side_effect = RuntimeError("connection reset mid-show")
    else:
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


def _plex_manager(sections):
    """get_library_section side effect: name → section, or None to fail."""
    pm = MagicMock()
    pm.is_connected = True
    pm.get_library_section.side_effect = lambda name: sections.get(name)
    return pm


# ── Cache seeding ────────────────────────────────────────────────────

# One row per title, tagged with the library that owns it. Keys match the form
# _mock_movie produces so a live row and its cached copy are the SAME identity.
ALPHA = {"key": "1_10_0", "clean_title": "alpha one", "original_title": "Alpha One",
         "year": 2020, "res": "1080p", "size": 5.0, "imdb_id": "tt0000001",
         "rating_key": 1, "media_id": 10, "library_name": "Movies 1080p"}
BRAVO = {"key": "3_30_0", "clean_title": "bravo two", "original_title": "Bravo Two",
         "year": 2020, "res": "1080p", "size": 5.0, "imdb_id": "tt0000003",
         "rating_key": 3, "media_id": 30, "library_name": "Movies 1080p"}
ZULU = {"key": "2_20_0", "clean_title": "zulu nine", "original_title": "Zulu Nine",
        "year": 2020, "res": "4K", "size": 50.0, "imdb_id": "tt0000002",
        "rating_key": 2, "media_id": 20, "library_name": "Movies 4K"}


def _seed_movies(tmp_path, rows, name="plex.db"):
    db = DatabaseManager(db_path=str(tmp_path / name))
    db.save_plex_cache([dict(r) for r in rows], "Movies", full_replace=False)
    return db


def _config(**over):
    """Real defaults plus the auto-grab settings the consumer tests need."""
    from backend.config import get_default_config
    cfg = get_default_config()
    cfg.update({
        "movie_libs": ["Movies 1080p", "Movies 4K"],
        "tv_libs": [],
        "auto_grab_enabled": True,
        # Narrow to MISSING only: the question these tests ask is whether an
        # owned title is reported as absent, and a broader status set would let
        # an UPGRADE verdict be scored as a pass.
        "auto_grab_statuses": "missing",
        "auto_grab_min_rating": 0.0,
        "auto_grab_min_votes": 0,
    })
    cfg.update(over)
    return cfg


# ── Real consumers ───────────────────────────────────────────────────

def _scanner(plex, db, cfg):
    """A ScannerService wired to a REAL MatchingEngine, not a mock.

    The whole point of this file is that the previous round asserted on
    plex_service's own outputs. A mocked matching engine would reproduce that
    mistake: it returns whatever the test told it to, regardless of what is
    actually in plex_index.
    """
    return ScannerService(config=cfg, db=db, scrapers=MagicMock(),
                          matching=MatchingEngine(MockApp(cfg)), plex_service=plex)


def _item(title, imdb, res="4K", size="50 GB", url=None):
    return MediaItem(id=f"i_{imdb}", title=title, year=2020, resolution=res,
                     size=size, imdb_id=imdb,
                     url=url or f"http://example/{imdb}",
                     web_data={"imdb_id": imdb, "size": size})


def _match(plex, db, cfg, items):
    svc = _scanner(plex, db, cfg)
    svc.items = list(items)
    completed = asyncio.run(svc._match_against_plex("Deep Scan"))
    return svc.items, completed


def _auto_grab(cfg, items):
    """Run the REAL AutoGrabService. Returns (report, download_service mock)."""
    dl = MagicMock()
    dl.download_item.return_value = {"success": True, "method": "jdownloader"}
    report = AutoGrabService(cfg, dl).process_items(items)
    return report, dl


def _grabbed_urls(dl):
    return [c.kwargs["url"] for c in dl.download_item.call_args_list]


@pytest.fixture(autouse=True)
def _clean_fuzzy_cache():
    # The fuzzy matcher memoizes on a module-level LRU shared by every test.
    clear_fuzzy_cache()
    yield
    clear_fuzzy_cache()


# ======================================================================
# FINAL CONSUMER — an owned title in an unreadable library
# ======================================================================

class TestFailedLibraryTitleStaysOwned:
    """Drives the real matcher and the real auto-grab, not plex_service alone."""

    def _partial_load(self, tmp_path, healthy_movies=(ALPHA,)):
        """'Movies 4K' fails to resolve; 'Movies 1080p' loads cleanly."""
        db = _seed_movies(tmp_path, [ALPHA, ZULU])
        live = [_mock_movie(r["original_title"], r["rating_key"]) for r in healthy_movies]
        pm = _plex_manager({
            "Movies 1080p": _movie_section(*live),
            "Movies 4K": None,
        })
        cfg = _config()
        plex = PlexService(config=cfg, db=db, plex_manager=pm)
        plex.load_libraries()
        return plex, db, cfg

    def test_title_in_failed_library_is_in_library_not_missing(self, tmp_path):
        plex, db, cfg = self._partial_load(tmp_path)

        items, completed = _match(plex, db, cfg, [_item("Zulu Nine", "tt0000002")])

        assert completed is True
        assert items[0].status == ScanStatus.IN_LIBRARY, (
            "A title whose library failed to read is still owned; the matcher "
            f"reported {items[0].status} (plex_info={items[0].plex_info!r})"
        )
        # Assert the payload too, not just the status: a fix that inserted a
        # bare placeholder row would satisfy the status check while telling the
        # user nothing about what they own.
        assert "4K" in items[0].plex_info
        assert "50" in items[0].plex_info
        db.close()

    def test_title_in_failed_library_is_not_auto_grabbed(self, tmp_path):
        plex, db, cfg = self._partial_load(tmp_path)

        items, _ = _match(plex, db, cfg, [_item("Zulu Nine", "tt0000002")])
        report, dl = _auto_grab(cfg, items)

        assert report.grabbed == 0, "Auto-grab re-downloaded a title already in Plex"
        assert _grabbed_urls(dl) == []
        assert report.skipped_status == 1
        db.close()

    def test_healthy_library_title_is_still_owned_in_the_same_load(self, tmp_path):
        # The library that DID load must keep working — restoring the failed one
        # must not disturb it.
        plex, db, cfg = self._partial_load(tmp_path)

        items, _ = _match(
            plex, db, cfg,
            [_item("Alpha One", "tt0000001", res="1080p", size="5 GB")])

        assert items[0].status == ScanStatus.IN_LIBRARY
        db.close()

    def test_genuinely_absent_title_is_still_missing_and_still_grabbed(self, tmp_path):
        # POSITIVE CONTROL, and the one that matters most: the fix must not have
        # turned "owned" into a blanket answer. A release in NEITHER library and
        # NOT in the cache has to stay MISSING and reach auto-grab, otherwise
        # auto-grab is silently dead and every test above passes anyway.
        plex, db, cfg = self._partial_load(tmp_path)

        items, _ = _match(plex, db, cfg,
                          [_item("Yankee Kilo Riverbend", "tt0009999")])
        report, dl = _auto_grab(cfg, items)

        assert items[0].status == ScanStatus.MISSING
        assert report.grabbed == 1
        assert _grabbed_urls(dl) == ["http://example/tt0009999"]
        db.close()

    def test_title_deleted_from_the_healthy_library_stays_missing_and_is_grabbed(
            self, tmp_path):
        # DISAGREEING CASE. The cheap fix — "on any incomplete load, union the
        # whole plex_cache into the index" — passes every assertion above. It
        # fails here.
        #
        # 'Movies 1080p' read successfully and no longer contains Alpha One (the
        # user deleted it); 'Movies 4K' failed. The successful library's live
        # answer is authoritative, so Alpha One must stay MISSING and be
        # grabbable, while Zulu Nine (from the FAILED library) is restored.
        db = _seed_movies(tmp_path, [ALPHA, BRAVO, ZULU])
        pm = _plex_manager({
            # Alpha One is gone from the library that read fine.
            "Movies 1080p": _movie_section(_mock_movie("Bravo Two", 3)),
            "Movies 4K": None,
        })
        cfg = _config()
        plex = PlexService(config=cfg, db=db, plex_manager=pm)
        plex.load_libraries()

        items, _ = _match(plex, db, cfg, [
            _item("Alpha One", "tt0000001", res="1080p", size="5 GB",
                  url="http://example/alpha"),
            _item("Zulu Nine", "tt0000002", url="http://example/zulu"),
        ])
        report, dl = _auto_grab(cfg, items)

        by_imdb = {i.imdb_id: i for i in items}
        assert by_imdb["tt0000001"].status == ScanStatus.MISSING, (
            "A title deleted from a library that read successfully must not be "
            "resurrected from cache — only the UNREADABLE library is restored"
        )
        assert by_imdb["tt0000002"].status == ScanStatus.IN_LIBRARY
        assert _grabbed_urls(dl) == ["http://example/alpha"]
        assert report.grabbed == 1
        db.close()

    def test_every_library_failing_restores_the_whole_cache(self, tmp_path):
        # The degenerate case the old code handled worst: no library resolves,
        # so the index was empty and EVERY title read Missing at once.
        db = _seed_movies(tmp_path, [ALPHA, ZULU])
        pm = _plex_manager({"Movies 1080p": None, "Movies 4K": None})
        cfg = _config()
        plex = PlexService(config=cfg, db=db, plex_manager=pm)
        plex.load_libraries()

        items, _ = _match(plex, db, cfg, [
            _item("Alpha One", "tt0000001", res="1080p", size="5 GB"),
            _item("Zulu Nine", "tt0000002"),
        ])
        report, dl = _auto_grab(cfg, items)

        assert [i.status for i in items] == [
            ScanStatus.IN_LIBRARY, ScanStatus.IN_LIBRARY]
        assert report.grabbed == 0
        assert _grabbed_urls(dl) == []
        db.close()


# ======================================================================
# Freshness — an incomplete load is not a fresh load
# ======================================================================

class TestIncompleteLoadFreshness:

    def _load(self, tmp_path, fail_4k, name="fresh.db"):
        db = _seed_movies(tmp_path, [ALPHA, ZULU], name=name)
        pm = _plex_manager({
            "Movies 1080p": _movie_section(_mock_movie("Alpha One", 1)),
            "Movies 4K": (None if fail_4k
                          else _movie_section(_mock_movie("Zulu Nine", 2, res="4k",
                                                          size_gb=50.0))),
        })
        cfg = _config()
        plex = PlexService(config=cfg, db=db, plex_manager=pm)
        plex.load_libraries()
        return plex, db

    def test_incomplete_load_does_not_advance_the_freshness_stamp(self, tmp_path):
        plex, db = self._load(tmp_path, fail_4k=True)

        assert plex.last_load_complete is False
        assert plex.last_load_incomplete_types == ["Movies"]
        assert plex._last_full_load_time == 0, (
            "A load that could not read every library must not claim freshness"
        )
        db.close()

    def test_complete_load_advances_the_freshness_stamp(self, tmp_path):
        # POSITIVE CONTROL. A fix that simply deleted the stamp would pass the
        # test above and break the 5-minute reload suppression outright.
        before = time.time()
        plex, db = self._load(tmp_path, fail_4k=False)

        assert plex.last_load_complete is True
        assert plex.last_load_incomplete_types == []
        assert plex._last_full_load_time >= before
        db.close()

    def test_incomplete_load_does_not_erase_an_earlier_complete_stamp(self, tmp_path):
        # DISAGREEING CASE: resetting the stamp to 0 also passes
        # test_incomplete_load_does_not_advance_the_freshness_stamp. It must be
        # left ALONE, not cleared — the earlier complete load really did happen,
        # and zeroing it throws away the only record of when.
        db = _seed_movies(tmp_path, [ALPHA, ZULU])
        good_4k = _movie_section(_mock_movie("Zulu Nine", 2, res="4k", size_gb=50.0))
        sections = {
            "Movies 1080p": _movie_section(_mock_movie("Alpha One", 1)),
            "Movies 4K": good_4k,
        }
        pm = _plex_manager(sections)
        plex = PlexService(config=_config(), db=db, plex_manager=pm)
        plex.load_libraries()
        complete_stamp = plex._last_full_load_time
        assert complete_stamp > 0

        sections["Movies 4K"] = None  # now it breaks
        plex.load_libraries()

        assert plex._last_full_load_time == complete_stamp
        assert plex.last_load_complete is False
        db.close()

    # ── final consumer for the stamp: the scanner's reload suppression ──

    def _drive_scan_decision(self, plex, db, cfg):
        """Run the REAL reload decision in ScannerService._run_scan_async.

        Returns True if the scanner performed another Plex load, False if it
        suppressed one as "fresh". _build_sources is stubbed to [] so the
        coroutine returns immediately after that decision — nothing is crawled,
        but the decision itself is the production code path, not a restatement
        of it.
        """
        svc = _scanner(plex, db, cfg)
        svc._build_sources = lambda *a, **k: []
        calls = []
        real_load = plex.load_libraries

        def _spy(*args, **kwargs):
            calls.append(kwargs)
            return real_load(*args, **kwargs)

        plex.load_libraries = _spy
        try:
            asyncio.run(svc._run_scan_async(
                scan_type="Deep Scan", source_type="All", pages=1,
                flags={}, search_query="", plex_refresh_mode="auto"))
        finally:
            plex.load_libraries = real_load
        return bool(calls)

    def test_scanner_reloads_plex_after_an_incomplete_load(self, tmp_path):
        # The reported symptom, at the consumer: the partial load was stamped
        # fresh, so the very next Deep Scan skipped the reload that would have
        # recovered the failed library — and matched against the partial index.
        plex, db = self._load(tmp_path, fail_4k=True)
        assert plex.plex_movies, (
            "precondition: the suppression check only runs when movies are "
            "already in memory, which the cache restore guarantees"
        )

        assert self._drive_scan_decision(plex, db, _config()) is True, (
            "A Deep Scan after an incomplete load must retry the Plex read"
        )
        db.close()

    def test_scanner_suppresses_reload_after_a_complete_load(self, tmp_path):
        # POSITIVE CONTROL for the same consumer. The 5-minute suppression is a
        # real optimisation for back-to-back scans; a fix that never stamped the
        # timestamp would reload on every scan and pass the test above.
        plex, db = self._load(tmp_path, fail_4k=False)

        assert self._drive_scan_decision(plex, db, _config()) is False, (
            "A Deep Scan seconds after a COMPLETE load must not reload Plex"
        )
        db.close()


# ======================================================================
# Merge mechanics — scoping, dedupe, and identity across the two stores
# ======================================================================

class TestRestoreScoping:

    def _titles(self, plex):
        return sorted(m.get("clean_title") for m in plex.plex_movies)

    def test_only_the_unreadable_librarys_rows_are_restored(self, tmp_path):
        db = _seed_movies(tmp_path, [ALPHA, BRAVO, ZULU])
        pm = _plex_manager({
            "Movies 1080p": _movie_section(_mock_movie("Alpha One", 1),
                                           _mock_movie("Bravo Two", 3)),
            "Movies 4K": None,
        })
        plex = PlexService(config=_config(), db=db, plex_manager=pm)
        plex.load_libraries()

        assert self._titles(plex) == ["alpha one", "bravo two", "zulu nine"]
        assert plex.last_load_restored_rows == 1
        assert plex.last_load_restored_libraries == ["Movies 4K"]
        db.close()

    def test_healthy_load_restores_nothing_and_indexes_only_live_rows(self, tmp_path):
        # POSITIVE CONTROL. No failures → no cache read merged in, and the
        # stale cache row for a deleted title must NOT reappear in the index.
        db = _seed_movies(tmp_path, [ALPHA, BRAVO, ZULU])
        pm = _plex_manager({
            "Movies 1080p": _movie_section(_mock_movie("Alpha One", 1)),
            "Movies 4K": _movie_section(
                _mock_movie("Zulu Nine", 2, res="4k", size_gb=50.0)),
        })
        plex = PlexService(config=_config(), db=db, plex_manager=pm)
        plex.load_libraries()

        assert self._titles(plex) == ["alpha one", "zulu nine"]
        assert plex.last_load_restored_rows == 0
        assert plex.last_load_restored_libraries == []
        assert plex.last_load_complete is True
        db.close()

    def test_degraded_library_restores_only_the_items_that_failed(self, tmp_path):
        # A per-item extraction failure (SH-H07) degrades a library that
        # otherwise read fine. The item that DID load keeps its FRESH row — it
        # must not be duplicated by its own cached copy, which is what a merge
        # using the wrong identity key would do.
        db = _seed_movies(tmp_path, [ALPHA, BRAVO], name="degraded.db")
        pm = _plex_manager({
            "Movies 1080p": _movie_section(
                _mock_movie("Alpha One", 1),
                _mock_movie("Bravo Two", 3, broken=True),  # extraction fails
            ),
            "Movies 4K": _movie_section(),
        })
        cfg = _config()
        plex = PlexService(config=cfg, db=db, plex_manager=pm)
        plex.load_libraries()

        assert self._titles(plex) == ["alpha one", "bravo two"]
        assert plex.last_load_restored_rows == 1
        assert [m["key"] for m in plex.plex_movies].count("1_10_0") == 1, (
            "the live row and its cached copy are one identity, not two"
        )
        # The index is what the matcher sees; a duplicate there would double
        # every version list in the UI.
        assert len(plex.plex_index["all_items"]) == 2
        db.close()

    def test_tv_restore_dedupes_despite_the_two_key_forms(self, tmp_path):
        # Live TV season dicts have NO 'key' field — their plex_cache key is the
        # bare rating_key, while movies use "{rating_key}_{media_id}_{part}".
        # An identity function that only understood the movie form (or that
        # compared int 200 to the stored str "200") would restore a duplicate of
        # the show that loaded perfectly well.
        db = DatabaseManager(db_path=str(tmp_path / "tv.db"))
        db.save_plex_cache([
            {"clean_title": "show good", "original_title": "Show Good", "year": 2020,
             "res": "1080p", "size": 2.0, "imdb_id": "tt0000100", "rating_key": 200,
             "season": 1, "episode_count": 1, "library_name": "TV"},
            {"clean_title": "show bad", "original_title": "Show Bad", "year": 2020,
             "res": "1080p", "size": 2.0, "imdb_id": "tt0000300", "rating_key": 400,
             "season": 1, "episode_count": 1, "library_name": "TV"},
        ], "TV Shows", full_replace=False)
        assert sorted(r["key"] for r in db.load_plex_cache("TV Shows")) == ["200", "400"]

        pm = _plex_manager({"TV": _tv_section(
            _mock_show("Show Good", 100, 200),
            _mock_show("Show Bad", 300, 400, broken=True),
        )})
        cfg = _config(movie_libs=[], tv_libs=["TV"])
        plex = PlexService(config=cfg, db=db, plex_manager=pm)
        plex.load_libraries()

        keys = [PlexService._cache_identity(r, True) for r in plex.plex_tv]
        assert sorted(keys) == ["200", "400"], f"expected one row each, got {keys}"
        assert plex.last_load_restored_rows == 1
        assert plex.last_load_incomplete_types == ["TV Shows"]
        db.close()

    def test_legacy_cache_without_library_names_still_restores(self, tmp_path):
        # plex_cache rows written before library_name existed cannot be
        # attributed, so the per-library filter cannot run. Restoring every
        # not-live row is the conservative fallback — reporting a title the user
        # owns as Missing feeds auto-grab, which is the costlier error.
        db = DatabaseManager(db_path=str(tmp_path / "legacy.db"))
        db.save_plex_cache(
            [{k: v for k, v in row.items() if k != "library_name"}
             for row in (ALPHA, ZULU)],
            "Movies", full_replace=False)
        assert all(not r["library_name"] for r in db.load_plex_cache("Movies"))

        pm = _plex_manager({
            "Movies 1080p": _movie_section(_mock_movie("Alpha One", 1)),
            "Movies 4K": None,
        })
        cfg = _config()
        plex = PlexService(config=cfg, db=db, plex_manager=pm)
        plex.load_libraries()

        assert sorted(m.get("clean_title") for m in plex.plex_movies) == [
            "alpha one", "zulu nine"]
        # Still deduped against the live pass, untagged or not.
        assert plex.last_load_restored_rows == 1

        items, _ = _match(plex, db, cfg, [_item("Zulu Nine", "tt0000002")])
        assert items[0].status == ScanStatus.IN_LIBRARY
        db.close()

    def test_restored_movie_row_is_not_eligible_as_a_tv_season_match(self, tmp_path):
        # DISAGREEING CASE for the row SHAPE, not the row set. save_plex_cache
        # stores `season` as 0 for movies, while a live movie dict has no
        # `season` key at all — and MatchingEngine decides "is this a TV season?"
        # with `p.get('season') == web_season`. So a cached movie row is a valid
        # candidate for a season-0 (specials) release where its live twin is not.
        # Measured against the real engine before normalising:
        #     LIVE-shaped movie row  as TV S0 candidate: []
        #     CACHE-shaped movie row as TV S0 candidate: [{... 'season': 0 ...}]
        # A merge that appended cached rows verbatim passes every other test in
        # this file and still smuggles that difference into the index.
        db = _seed_movies(tmp_path, [ALPHA, ZULU], name="shape.db")
        pm = _plex_manager({
            "Movies 1080p": _movie_section(_mock_movie("Alpha One", 1)),
            "Movies 4K": None,
        })
        cfg = _config()
        plex = PlexService(config=cfg, db=db, plex_manager=pm)
        plex.load_libraries()

        restored = [m for m in plex.plex_movies if m.get("imdb_id") == "tt0000002"]
        assert len(restored) == 1
        assert restored[0].get("season") is None, (
            "a restored MOVIE row must carry the live shape, or the matcher will "
            "offer it as a season-0 TV candidate"
        )

        # Drive the real engine to prove it, rather than trusting the field.
        engine = MatchingEngine(MockApp(cfg))
        specials = {"display_title": "Zulu Nine", "search_key": "zulu nine",
                    "year": 2020, "res": "4K", "size": "50 GB",
                    "is_tv": True, "season": 0, "imdb_id": "tt0000002"}
        matches, _ = engine.find_tv_season_matches(specials, plex.plex_index)
        assert matches == [], (
            f"a restored movie was matched as a TV season: {matches}")

        # POSITIVE CONTROL in the same breath: it is still found as a MOVIE.
        movie_web = {"display_title": "Zulu Nine", "search_key": "zulu nine",
                     "year": 2020, "res": "4K", "size": "50 GB",
                     "is_tv": False, "imdb_id": "tt0000002"}
        assert engine.find_movie_matches(movie_web, plex.plex_index)[0], (
            "normalising the shape must not hide the row from movie matching"
        )
        db.close()

    def test_restored_tv_row_keeps_its_season_number(self, tmp_path):
        # The mirror of the case above: TV rows must NOT be normalised — their
        # season number is the whole basis of season matching. A blanket
        # "season = None" would make every restored season unmatchable.
        db = DatabaseManager(db_path=str(tmp_path / "tvshape.db"))
        db.save_plex_cache([
            {"clean_title": "show bad", "original_title": "Show Bad", "year": 2020,
             "res": "1080p", "size": 2.0, "imdb_id": "tt0000300", "rating_key": 400,
             "season": 1, "episode_count": 1, "library_name": "TV"},
        ], "TV Shows", full_replace=False)
        pm = _plex_manager({"TV": _tv_section(
            _mock_show("Show Bad", 300, 400, broken=True))})
        cfg = _config(movie_libs=[], tv_libs=["TV"])
        plex = PlexService(config=cfg, db=db, plex_manager=pm)
        plex.load_libraries()

        assert plex.last_load_restored_rows == 1
        assert [r.get("season") for r in plex.plex_tv] == [1]

        engine = MatchingEngine(MockApp(cfg))
        web = {"display_title": "Show Bad", "search_key": "show bad", "year": 2020,
               "res": "1080p", "size": "2 GB", "is_tv": True, "season": 1,
               "imdb_id": "tt0000300"}
        assert engine.find_tv_season_matches(web, plex.plex_index)[0], (
            "a restored TV season must still match its own season number"
        )
        db.close()

    def test_a_failing_cache_read_degrades_instead_of_raising(self):
        # The restore reads plex_cache, which can itself fail (locked DB, the
        # SQLite corruption paths elsewhere in this codebase). It must fall back
        # to the pre-fix behaviour rather than take the whole load down with it —
        # and it must NOT claim freshness on the way out.
        db = MagicMock()
        db.load_plex_cache.side_effect = RuntimeError("database is locked")
        pm = _plex_manager({"Movies 1080p": None, "Movies 4K": None})
        plex = PlexService(config=_config(), db=db, plex_manager=pm)

        plex.load_libraries()  # must not raise

        assert plex.plex_movies == []
        assert plex.last_load_restored_rows == 0
        assert plex.last_load_complete is False
        assert plex._last_full_load_time == 0

    def test_a_db_stand_in_without_a_usable_cache_read_degrades(self):
        # A bare MagicMock db returns a MagicMock from load_plex_cache, not a
        # list. Every mock-based test in test_plex_partial_load_authority.py is
        # built that way, so iterating the result blindly would break them all
        # with a TypeError swallowed by load_libraries' outer except.
        db = MagicMock()
        pm = _plex_manager({"Movies 1080p": None, "Movies 4K": None})
        plex = PlexService(config=_config(), db=db, plex_manager=pm)

        plex.load_libraries()

        assert plex.plex_movies == []
        assert plex.last_load_restored_rows == 0
        # Proof the load ran to the end rather than dying in the merge: the
        # completeness verdict is only assigned after the cache-save block.
        assert plex.last_load_incomplete_types == ["Movies"]

    def test_restore_does_not_write_anything_back_to_the_cache(self, tmp_path):
        # The restored rows CAME from plex_cache; re-saving them would refresh
        # their last_updated and make a stale cache look freshly written to
        # check_cache_status. The incomplete content type must still skip its
        # save entirely.
        db = _seed_movies(tmp_path, [ALPHA, ZULU])
        pm = _plex_manager({
            "Movies 1080p": _movie_section(_mock_movie("Alpha One", 1)),
            "Movies 4K": None,
        })
        before = db.get_plex_cache_max_timestamp()["Movies"]
        spy = MagicMock(side_effect=db.save_plex_cache)
        db.save_plex_cache = spy

        plex = PlexService(config=_config(), db=db, plex_manager=pm)
        plex.load_libraries()

        assert [c.args[1] for c in spy.call_args_list] == [], (
            "an incomplete Movies load must not save at all, restored or not"
        )
        assert db.get_plex_cache_max_timestamp()["Movies"] == before
        # And the rows are all still there — the disk half of the guarantee.
        assert sorted(r["key"] for r in db.load_plex_cache("Movies")) == [
            "1_10_0", "2_20_0"]
        db.close()
