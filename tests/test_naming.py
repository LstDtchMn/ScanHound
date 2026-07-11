import pytest
from backend.rename.naming import build_target, render_template


class TestMultiEpNaming:
    def _base(self, **kwargs):
        return {
            "media_type": "tv",
            "title": "The Show",
            "year": 2024,
            "season": 1,
            "episode": 1,
            "resolution": "1080p",
            "original_filename": "show.s01e01.mkv",
            **kwargs,
        }

    def test_single_episode_unchanged(self):
        fname, _ = build_target(self._base(), tv_root="/tv")
        assert "S01E01" in fname
        assert "E02" not in fname

    def test_combined_episode_code(self):
        fname, _ = build_target(self._base(episode_end=2), tv_root="/tv")
        assert "S01E01E02" in fname

    def test_three_episode_code(self):
        fname, _ = build_target(self._base(episode=3, episode_end=5), tv_root="/tv")
        assert "S01E03E05" in fname

    def test_part_suffix(self):
        fname, _ = build_target(self._base(part=1), tv_root="/tv")
        assert "Part 1" in fname

    def test_part_two_suffix(self):
        fname, _ = build_target(self._base(part=2), tv_root="/tv")
        assert "Part 2" in fname

    def test_movie_naming_unchanged(self):
        meta = {
            "media_type": "movie",
            "title": "Great Film",
            "year": 2024,
            "resolution": "1080p",
            "original_filename": "great.film.mkv",
        }
        fname, _ = build_target(meta, movie_root="/movies")
        assert "Part" not in fname
        assert "E0" not in fname


# ── Minor review fix: custom template keeps the Part suffix ──────────

from backend.rename.naming import build_target as _bt


def test_template_appends_part_when_not_referenced():
    f1, _ = _bt({"media_type": "tv", "title": "Show", "year": 2024, "season": 1,
                 "episode": 1, "part": 1, "original_filename": "x.mkv"},
                tv_root="/tv", template="{{title}} - S{{season}}E{{episode}}")
    f2, _ = _bt({"media_type": "tv", "title": "Show", "year": 2024, "season": 1,
                 "episode": 1, "part": 2, "original_filename": "x.mkv"},
                tv_root="/tv", template="{{title}} - S{{season}}E{{episode}}")
    assert "Part 1" in f1 and "Part 2" in f2 and f1 != f2  # no collision


def test_template_part_token_not_double_appended():
    f, _ = _bt({"media_type": "tv", "title": "Show", "year": 2024, "season": 1,
                "episode": 1, "part": 2, "original_filename": "x.mkv"},
               tv_root="/tv", template="{{title}} Part {{part}}")
    assert " - Part 2" not in f and "Part 2" in f


# ── C3: render_template must not eat legitimate title characters ─────

class TestRenderTemplateOverCollapse:
    def test_trailing_hyphen_in_title_survives(self):
        # A title that legitimately ends in a hyphen (e.g. "Under-") must not
        # be stripped by the generic trailing " -_" cleanup.
        out = render_template("{{title}}", {"title": "Under-"})
        assert out == "Under-"

    def test_literal_empty_parens_in_title_survive(self):
        # Literal "()" that is part of the title text (not a leftover empty
        # optional section) must survive the empty-section cleanup.
        out = render_template("{{title}}", {"title": "Rush () 2013"})
        assert out == "Rush () 2013"

    def test_literal_empty_brackets_in_title_survive(self):
        out = render_template("{{title}}", {"title": "Artist []"})
        assert out == "Artist []"

    def test_real_title_with_year_parens_survives_through_custom_template(self):
        # Realistic case cited in the review: a title itself carrying parens,
        # rendered through a custom template that also has its own optional
        # year segment.
        out = render_template("{{title}} ({{year}})",
                               {"title": "Rush (2013)", "year": "2013"})
        assert out == "Rush (2013) (2013)"

    def test_genuinely_empty_optional_section_still_cleaned(self):
        # This is the behavior C3 must preserve: a template-authored
        # optional section that resolves empty (e.g. missing year) still
        # collapses cleanly, with no dangling separator artifacts.
        out = render_template("{{title}} ({{year}})", {"title": "Show", "year": ""})
        assert out == "Show"

    def test_default_naming_path_unaffected(self):
        # The non-template (default Plex convention) path never goes through
        # render_template at all, so it must be completely unaffected.
        fname, _ = build_target(
            {"media_type": "movie", "title": "Dune", "year": 2021,
             "resolution": "2160p", "original_filename": "dune.2021.2160p.mkv"},
            movie_root="/movies")
        assert fname == "Dune (2021) [2160p].mkv"

    def test_bare_hyphen_separator_idiom_still_collapses(self):
        # Regression guard: a template author's bare (non-bracket) separator
        # idiom around a token that resolves empty must still collapse
        # cleanly, same as before this fix.
        out = render_template("{{title}} - {{episode_title}}",
                               {"title": "Show", "episode_title": ""})
        assert out == "Show"

    def test_bare_hyphen_separator_idiom_keeps_value_when_present(self):
        out = render_template("{{title}} - {{episode_title}}",
                               {"title": "Show", "episode_title": "Pilot"})
        assert out == "Show - Pilot"


# ── Flat movie folders (opt-in): single-file movies land in library root ──

def test_flat_single_movie_goes_to_library_root():
    meta = {"media_type": "movie", "title": "Sinners", "year": 2025,
            "resolution": "1080p", "original_filename": "Sinners.2025.1080p.mkv"}
    fname, dest = build_target(meta, movie_root="/lib/movies", tv_root="/lib/tv", flat=True)
    assert dest == "/lib/movies"
    assert fname == "Sinners (2025) [1080p].mkv"


def test_flat_split_movie_keeps_subfolder():
    # A split movie keeps its own subfolder even with flat=True, AND (since the
    # part-suffix fix) the default movie filename branch appends "- Part N" so
    # the two parts don't collide inside that subfolder.
    meta = {"media_type": "movie", "title": "Sinners", "year": 2025,
            "resolution": "1080p", "part": 2, "original_filename": "Sinners.2025.CD2.mkv"}
    fname, dest = build_target(meta, movie_root="/lib/movies", tv_root="/lib/tv", flat=True)
    import os
    assert dest == os.path.join("/lib/movies", "Sinners (2025)")
    assert "Part 2" in fname


def test_flat_off_movie_keeps_subfolder():
    meta = {"media_type": "movie", "title": "Sinners", "year": 2025,
            "resolution": "1080p", "original_filename": "Sinners.2025.1080p.mkv"}
    import os
    fname, dest = build_target(meta, movie_root="/lib/movies", tv_root="/lib/tv")  # flat defaults False
    assert dest == os.path.join("/lib/movies", "Sinners (2025)")


def test_movie_split_parts_get_distinct_filenames():
    """A split movie with NO custom template must still get a per-part suffix so
    the two parts don't render to the identical colliding filename (previously
    both CD1 and CD2 became 'Sinners (2025) [1080p].mkv')."""
    base = {"media_type": "movie", "title": "Sinners", "year": 2025,
            "resolution": "1080p", "original_filename": "Sinners.2025.mkv"}
    f1, _ = build_target({**base, "part": 1}, movie_root="/movies")
    f2, _ = build_target({**base, "part": 2}, movie_root="/movies")
    assert "Part 1" in f1
    assert "Part 2" in f2
    assert f1 != f2


def test_flat_does_not_affect_tv():
    meta = {"media_type": "tv", "title": "Severance", "year": 2022, "season": 2,
            "episode": 1, "original_filename": "Severance.S02E01.mkv"}
    import os
    fname, dest = build_target(meta, movie_root="/lib/movies", tv_root="/lib/tv", flat=True)
    assert dest == os.path.join("/lib/tv", "Severance (2022)", "Season 02")
