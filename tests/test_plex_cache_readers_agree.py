"""The soft and strict plex_cache readers must return the SAME columns.

They deliberately differ in error contract — one returns [] on failure, the
other raises — and deliberately agree on projection. The strict one's docstring
claims "the same rows as list_plex_cache_movies", and `media_id` became
load-bearing for the version badges, so a column added to one SELECT and not the
other is exactly the drift that produced H1 (peer review round 3).
"""
import pytest

from backend.database import DatabaseManager


@pytest.fixture
def db(tmp_path):
    return DatabaseManager(str(tmp_path / "cache.db"))


def _seed(db):
    db._mutate(
        "INSERT INTO plex_cache (key, title, rating_key, media_id, content_type, "
        "res, size, year, is_tv, dovi, hdr, library_name, file_path, last_updated) "
        "VALUES ('k1', 'A Film', '10', '99', 'Movies', '1080p', '4.2', 2019, 0, 0, "
        "'', 'Movies', '/m/a.mkv', 1787157555.0)")


def test_both_readers_return_identical_columns(db):
    _seed(db)
    soft = db.list_plex_cache_movies()
    strict = db.list_plex_cache_movies_strict()
    assert soft and strict, "fixture produced no rows"
    assert set(soft[0]) == set(strict[0]), (
        f"projection drift: soft-only={set(soft[0]) - set(strict[0])} "
        f"strict-only={set(strict[0]) - set(soft[0])}")
    assert soft == strict


def test_both_carry_media_id_which_the_badges_depend_on(db):
    _seed(db)
    for rows in (db.list_plex_cache_movies(), db.list_plex_cache_movies_strict()):
        assert "media_id" in rows[0]
        assert rows[0]["media_id"] == "99"


def test_an_empty_table_is_an_empty_LIST_from_both_not_an_error(db):
    assert db.list_plex_cache_movies() == []
    assert db.list_plex_cache_movies_strict() == []
