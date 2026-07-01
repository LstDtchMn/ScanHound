from unittest.mock import MagicMock
from backend.plex_manager import PlexManager
from backend.plex_service import PlexService


def _pm_with_server():
    pm = PlexManager.__new__(PlexManager)
    pm._server = MagicMock()
    return pm


def _make_service(config=None, pm=None):
    return PlexService(config=config or {}, db=MagicMock(),
                       plex_manager=pm or MagicMock())


def _part(file, size=1_000_000_000):
    p = MagicMock()
    p.size = size
    p.file = file
    p.videoStreams.return_value = []
    return p


def _media(parts, res="4k"):
    m = MagicMock()
    m.videoResolution = res
    m.id = 42
    m.parts = parts
    return m


def _movie(media_list, title="M", year=2024, rk=7):
    mv = MagicMock()
    mv.title = title
    mv.year = year
    mv.ratingKey = rk
    mv.originalLanguage = "en"
    g = MagicMock(); g.id = "imdb://tt1"
    mv.guids = [g]
    mv.media = media_list
    return mv


def test_add_and_remove_label_fetch_and_call():
    pm = _pm_with_server()
    item = MagicMock()
    pm._server.fetchItem.return_value = item

    pm.add_label("123", "DV FEL")
    pm._server.fetchItem.assert_called_with(123)   # str -> int
    item.addLabel.assert_called_once_with("DV FEL")

    pm.remove_label("123", "DV MEL")
    item.removeLabel.assert_called_once_with("DV MEL")


def test_extract_captures_file_for_all_parts():
    svc = _make_service()
    movie = _movie([
        _media([_part("Y:/A/edition1.mkv"), _part("Y:/A/edition2.mkv")]),
        _media([_part("Z:/B/optimized.mp4")], res="1080"),
    ])
    rows = svc._extract_movie_data(movie)
    files = [r["file"] for r in rows]
    assert files == ["Y:/A/edition1.mkv", "Y:/A/edition2.mkv", "Z:/B/optimized.mp4"]


def test_extract_guards_empty_parts_and_none_file():
    svc = _make_service()
    empty_media = _media([])          # no parts
    none_part = _part(None)           # part with no file
    movie = _movie([empty_media, _media([none_part])])
    rows = svc._extract_movie_data(movie)
    # empty-parts media yields no row; None file is preserved (not a crash)
    assert [r["file"] for r in rows] == [None]
