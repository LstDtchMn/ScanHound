"""Unit tests for the ported rename core: confidence, naming, fileops."""
import os
import pytest

from backend.rename import confidence, naming, fileops


class TestConfidence:
    def test_identical_titles_high(self):
        assert confidence.match_confidence("The Matrix", "The Matrix") >= 95

    def test_year_match_boosts_score(self):
        assert confidence.match_confidence("Dune", "Dune", 2021, 2021) >= \
            confidence.match_confidence("Dune", "Dune")

    def test_conflicting_year_penalized(self):
        same = confidence.match_confidence("Dune", "Dune", 2021, 2021)
        diff = confidence.match_confidence("Dune", "Dune", 2021, 1984)
        assert diff < same

    def test_unrelated_titles_low(self):
        assert confidence.match_confidence("The Matrix", "Frozen") < 50

    def test_similarity_bounds(self):
        assert confidence.dice_similarity("", "x") == 0.0
        assert 0.0 <= confidence.title_similarity("the office us", "The Office") <= 1.0


class TestNaming:
    def test_movie_plex_name(self):
        fname, dest = naming.build_target(
            {"media_type": "movie", "title": "Dune", "year": 2021,
             "resolution": "2160p", "original_filename": "dune.2021.2160p.mkv"},
            movie_root="/movies")
        assert fname == "Dune (2021) [2160p].mkv"
        assert dest == os.path.join("/movies", "Dune (2021)")

    def test_tv_plex_name(self):
        fname, dest = naming.build_target(
            {"media_type": "tv", "title": "The Office", "year": 2005,
             "season": 2, "episode": 5, "episode_title": "Halloween",
             "original_filename": "the.office.s02e05.mkv"},
            tv_root="/tv")
        assert fname == "The Office (2005) - S02E05 - Halloween.mkv"
        assert dest == os.path.join("/tv", "The Office (2005)", "Season 02")

    def test_template_substitution_with_default(self):
        out = naming.render_template("{{title}} ({{year}})", {"title": "Heat", "year": "1995"})
        assert out == "Heat (1995)"

    def test_template_drops_empty_conditional_section(self):
        out = naming.render_template("{{title}}[ - {{episode_title}}]",
                                     {"title": "Movie", "episode_title": ""})
        assert out == "Movie"

    def test_template_keeps_section_when_present(self):
        out = naming.render_template("{{title}}[ - {{episode_title}}]",
                                     {"title": "Show", "episode_title": "Pilot"})
        assert out == "Show - Pilot"

    def test_template_strips_path_separators(self):
        out = naming.render_template("{{title}}", {"title": "a/b\\c"})
        assert "/" not in out and "\\" not in out

    def test_unknown_extension_defaults_to_mkv(self):
        fname, _ = naming.build_target(
            {"media_type": "movie", "title": "X", "year": 2020,
             "original_filename": "x.iso"}, movie_root="/m")
        assert fname.endswith(".mkv")


class TestFileOps:
    def test_hardlink_and_undo(self, tmp_path):
        src = tmp_path / "src.mkv"; src.write_text("data")
        dst = tmp_path / "lib" / "out.mkv"
        assert fileops.place_file(str(src), str(dst), "hardlink") == "hardlink"
        assert dst.exists() and src.exists()
        fileops.undo_place(str(src), str(dst), "hardlink")
        assert not dst.exists() and src.exists()

    def test_move_and_undo(self, tmp_path):
        src = tmp_path / "src.mkv"; src.write_text("data")
        dst = tmp_path / "lib" / "out.mkv"
        assert fileops.place_file(str(src), str(dst), "move") == "move"
        assert dst.exists() and not src.exists()
        fileops.undo_place(str(src), str(dst), "move")
        assert src.exists() and not dst.exists()

    def test_copy_verifies_and_keeps_source(self, tmp_path):
        src = tmp_path / "src.mkv"; src.write_bytes(b"hello world")
        dst = tmp_path / "out.mkv"
        assert fileops.place_file(str(src), str(dst), "copy") == "copy"
        assert dst.read_bytes() == b"hello world" and src.exists()

    def test_refuses_to_overwrite(self, tmp_path):
        src = tmp_path / "src.mkv"; src.write_text("a")
        dst = tmp_path / "out.mkv"; dst.write_text("existing")
        with pytest.raises(FileExistsError):
            fileops.place_file(str(src), str(dst), "move")

    def test_missing_source_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            fileops.place_file(str(tmp_path / "nope.mkv"), str(tmp_path / "o.mkv"), "move")
