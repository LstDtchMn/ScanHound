from backend.rename.path_translation import (
    find_unmapped_plex_path_prefixes,
    translate_plex_path,
)


def test_exact_match_translates():
    mappings = "F:\\Downloads => /library/movies"
    assert translate_plex_path("F:\\Downloads\\Movie.mkv", mappings) == "/library/movies/Movie.mkv"


def test_no_match_returns_unchanged():
    mappings = "F:\\Downloads => /library/movies"
    assert translate_plex_path("Z:\\Somewhere\\X.mkv", mappings) == "Z:\\Somewhere\\X.mkv"


def test_empty_mappings_returns_unchanged():
    assert translate_plex_path("F:\\Downloads\\X.mkv", "") == "F:\\Downloads\\X.mkv"
    assert translate_plex_path("F:\\Downloads\\X.mkv", None) == "F:\\Downloads\\X.mkv"


def test_empty_path_returns_unchanged():
    assert translate_plex_path("", "F:\\Downloads => /library/movies") == ""


def test_longest_prefix_wins():
    mappings = (
        "F:\\Downloads => /library/movies\n"
        "F:\\Downloads\\Sub => /library/movies-sub"
    )
    result = translate_plex_path("F:\\Downloads\\Sub\\Movie.mkv", mappings)
    assert result == "/library/movies-sub/Movie.mkv"


def test_path_boundary_safe_no_false_prefix_match():
    # A mapping for 'F:/Downloads' must not also match 'F:/Downloads2/...'
    mappings = "F:\\Downloads => /library/movies"
    assert translate_plex_path("F:\\Downloads2\\Other.mkv", mappings) == "F:\\Downloads2\\Other.mkv"


def test_exact_path_with_no_remainder():
    mappings = "F:\\Downloads => /library/movies"
    assert translate_plex_path("F:\\Downloads", mappings) == "/library/movies"


def test_malformed_line_no_arrow_is_skipped():
    mappings = "F:\\Downloads /library/movies\nG:\\Downloads => /library/movies-4k"
    assert translate_plex_path("G:\\Downloads\\X.mkv", mappings) == "/library/movies-4k/X.mkv"


def test_malformed_line_empty_side_is_skipped():
    mappings = " => /library/movies\nG:\\Downloads => /library/movies-4k"
    assert translate_plex_path("G:\\Downloads\\X.mkv", mappings) == "/library/movies-4k/X.mkv"


def test_junction_alias_mapping_translates():
    mappings = "C:\\1080p Drives\\1080p Bismark => /library/plex-source/l-1080p-bismark"
    raw = "C:\\1080p Drives\\1080p Bismark\\Movie.mkv"
    assert translate_plex_path(raw, mappings) == "/library/plex-source/l-1080p-bismark/Movie.mkv"


def test_unc_share_mapping_translates():
    mappings = "\\\\TURTLELANDSRV2\\1080p Lincoln => /library/plex-source/nas-1080p-lincoln"
    raw = "\\\\TURTLELANDSRV2\\1080p Lincoln\\Movie.mkv"
    assert translate_plex_path(raw, mappings) == "/library/plex-source/nas-1080p-lincoln/Movie.mkv"


def test_finds_local_prefix_with_no_mapping():
    rows = [{"file_path": "Z:\\Something\\Movie.mkv"}]
    result = find_unmapped_plex_path_prefixes(rows, "")
    assert result == ["Z:\\"]


def test_finds_junction_alias_prefix_with_no_mapping():
    rows = [{"file_path": "C:\\1080p Drives\\1080p New Drive\\Movie.mkv"}]
    result = find_unmapped_plex_path_prefixes(rows, "")
    assert result == ["C:\\1080p Drives\\1080p New Drive"]


def test_finds_unc_share_prefix_with_no_mapping():
    rows = [{"file_path": "\\\\TURTLELANDSRV2\\New Share\\Movie.mkv"}]
    result = find_unmapped_plex_path_prefixes(rows, "")
    assert result == ["\\\\TURTLELANDSRV2\\New Share"]


def test_mapped_prefix_is_not_flagged():
    rows = [{"file_path": "G:\\Movies 1\\Movie.mkv"}]
    mappings = "G:\\Movies 1 => /library/plex-source/g-movies-1"
    assert find_unmapped_plex_path_prefixes(rows, mappings) == []


def test_returns_distinct_prefixes_only():
    rows = [
        {"file_path": "Z:\\Something\\A.mkv"},
        {"file_path": "Z:\\Something\\B.mkv"},
    ]
    assert find_unmapped_plex_path_prefixes(rows, "") == ["Z:\\"]


def test_rows_with_no_file_path_are_skipped():
    rows = [{"file_path": None}, {"file_path": ""}, {}]
    assert find_unmapped_plex_path_prefixes(rows, "") == []
