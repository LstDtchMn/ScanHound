from backend.rename.path_translation import translate_plex_path


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
