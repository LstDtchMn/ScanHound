from backend.rename import fileops


def test_dedupe_dest_free_path_unchanged(tmp_path):
    p = tmp_path / "Movie (2024) [2160p].mkv"
    assert fileops.dedupe_dest(str(p)) == str(p)


def test_dedupe_dest_suffixes_and_preserves_ext(tmp_path):
    p = tmp_path / "Movie (2024).mkv"; p.write_bytes(b"x")
    out = fileops.dedupe_dest(str(p))
    assert out.endswith("Movie (2024) (1).mkv")


def test_dedupe_dest_case_insensitive(tmp_path):
    (tmp_path / "movie.mkv").write_bytes(b"x")
    out = fileops.dedupe_dest(str(tmp_path / "MOVIE.mkv"))
    assert out.endswith("MOVIE (1).mkv")


def test_dedupe_dest_case_insensitive_suffix_increment(tmp_path):
    (tmp_path / "movie.mkv").write_bytes(b"x")
    (tmp_path / "Movie (1).MKV").write_bytes(b"x")

    out = fileops.dedupe_dest(str(tmp_path / "MOVIE.mkv"))

    assert out.endswith("MOVIE (2).mkv")


def test_dedupe_dest_missing_parent_preserves_requested_path(tmp_path):
    requested = tmp_path / "not-created" / "Movie.mkv"
    assert fileops.dedupe_dest(str(requested)) == str(requested)
