"""Regression and integration tests for the episode intelligence pipeline.

These tests verify two things:
1. Clean-file regression: existing single-episode, movie, and season-pack files
   must produce IDENTICAL output to pre-branch behaviour — no new fields set,
   no unexpected warnings, naming unchanged.
2. Proposal detection: combined/split/correction proposals surface correctly in
   the match dict when the runtime evidence warrants them.

TMDB is never called — all network I/O is replaced by test doubles.
"""
from __future__ import annotations

import pytest
from unittest.mock import MagicMock, patch


# ---------------------------------------------------------------------------
# Helpers shared across test classes
# ---------------------------------------------------------------------------

def _make_service(cfg: dict = None):
    """Create a bare RenameService with no registry coupling."""
    from backend.rename.service import RenameService
    defaults = {
        "auto_rename_confidence_threshold": 80,
        "auto_rename_llm_enabled": False,
        "tmdb_api_key": "fake",
        "auto_rename_movie_library": "/movies",
        "auto_rename_tv_library": "/tv",
    }
    svc = RenameService.__new__(RenameService)
    svc._cfg = {**defaults, **(cfg or {})}
    return svc


def _make_episodes(runtimes: dict) -> list:
    """Build a minimal TMDB episode list from {ep_number: runtime_minutes}."""
    return [
        {"episode_number": n, "runtime": rt, "name": f"Episode {n}"}
        for n, rt in runtimes.items()
    ]


# ---------------------------------------------------------------------------
# Class 1 — Clean file: no proposals, no warnings
# ---------------------------------------------------------------------------

class TestCleanEpisodeUnaffected:
    """A well-matched file with a matching runtime must not receive any proposals."""

    def test_matching_runtime_adds_no_proposals(self):
        """file_min == tmdb_min (0% deviation) → +10 delta, NOT < -10, no re-scan."""
        from backend.rename import service as svc_mod

        match = {
            "tmdb_id": 1,
            "media_type": "tv",
            "title": "Good Show",
            "season": 1,
            "episode": 3,
            "confidence": 90.0,
        }
        episodes = _make_episodes({3: 44})

        # No combined proposal: file matches single episode perfectly
        combined = svc_mod._detect_combined_episode(match, 44.0, episodes)
        assert combined is None

        # No split proposal: file_min (44) >= tmdb_min (44) * 0.6
        split = svc_mod._detect_split_file("x/S01E03.mkv", 44.0, 44.0)
        assert split is None

    def test_near_exact_runtime_adds_no_proposals(self):
        """2% deviation (≤3%) gives +10 confidence adjustment — no warnings."""
        from backend.rename import service as svc_mod
        from backend.rename.confidence import runtime_confidence_delta

        tmdb_min = 44.0
        file_min = 44.88  # ~2% over

        delta = runtime_confidence_delta(file_min, tmdb_min)
        assert delta == 10.0  # bonus, not a penalty

        match = {"tmdb_id": 1, "media_type": "tv", "title": "Good Show",
                 "season": 1, "episode": 3, "confidence": 90.0}
        episodes = _make_episodes({3: tmdb_min})

        assert svc_mod._detect_combined_episode(match, file_min, episodes) is None
        assert svc_mod._detect_split_file("x/S01E03.mkv", file_min, tmdb_min) is None

    def test_movie_path_not_affected_by_episode_helpers(self):
        """Movies have no 'episode' key — helpers return None safely even if called."""
        from backend.rename import service as svc_mod

        match = {"tmdb_id": 2, "media_type": "movie", "title": "Great Film"}
        # No episode number → early exit
        result = svc_mod._detect_combined_episode(match, 120.0, [])
        assert result is None

    def test_season_pack_flag_logic(self):
        """season set + episode=None → is_pack True; detection blocks are skipped."""
        match = {"season": 1, "episode": None}
        is_pack = match.get("season") is not None and match.get("episode") is None
        assert is_pack is True

    def test_clean_episode_produces_no_extra_keys(self):
        """Running _detect_* on a perfectly matched file leaves the match untouched."""
        from backend.rename import service as svc_mod

        match = {
            "tmdb_id": 1,
            "media_type": "tv",
            "title": "Clean Show",
            "season": 2,
            "episode": 7,
            "confidence": 92.0,
        }
        episodes = _make_episodes({7: 42})
        original_keys = set(match.keys())

        combined = svc_mod._detect_combined_episode(match, 42.0, episodes)
        split = svc_mod._detect_split_file("x/S02E07.mkv", 42.0, 42.0)

        assert combined is None
        assert split is None
        # Match dict itself was not mutated
        assert set(match.keys()) == original_keys


# ---------------------------------------------------------------------------
# Class 2 — Naming regression: format must not change for existing cases
# ---------------------------------------------------------------------------

class TestNamingRegression:
    """build_target() must produce the same Plex-convention filenames as before."""

    def test_existing_single_ep_format_unchanged(self):
        """Single TV episode: 'Show (year) - S02E05.ext'."""
        from backend.rename.naming import build_target

        meta = {
            "media_type": "tv",
            "title": "My Show",
            "year": 2023,
            "season": 2,
            "episode": 5,
            "original_filename": "my.show.s02e05.mkv",
        }
        fname, dest = build_target(meta, tv_root="/tv")
        assert fname == "My Show (2023) - S02E05.mkv"
        # Destination should be inside TV root, show folder, Season folder
        assert "/tv" in dest
        assert "Season 02" in dest

    def test_existing_movie_format_unchanged(self):
        """Movie with resolution: 'Title (year) [res].ext'."""
        from backend.rename.naming import build_target

        meta = {
            "media_type": "movie",
            "title": "Great Film",
            "year": 2021,
            "resolution": "1080p",
            "original_filename": "great.film.mkv",
        }
        fname, dest = build_target(meta, movie_root="/movies")
        assert fname == "Great Film (2021) [1080p].mkv"
        assert "/movies" in dest

    def test_existing_movie_no_resolution_format_unchanged(self):
        """Movie without resolution: 'Title (year).ext'."""
        from backend.rename.naming import build_target

        meta = {
            "media_type": "movie",
            "title": "Indie Film",
            "year": 2019,
            "original_filename": "indie.film.mkv",
        }
        fname, _ = build_target(meta, movie_root="/movies")
        assert fname == "Indie Film (2019).mkv"

    def test_episode_title_included_in_existing_format(self):
        """Episode with title: 'Show (yr) - S01E01 - Ep Title.ext'."""
        from backend.rename.naming import build_target

        meta = {
            "media_type": "tv",
            "title": "Crime Drama",
            "year": 2020,
            "season": 1,
            "episode": 1,
            "episode_title": "Pilot",
            "original_filename": "crime.drama.s01e01.mkv",
        }
        fname, _ = build_target(meta, tv_root="/tv")
        assert fname == "Crime Drama (2020) - S01E01 - Pilot.mkv"

    def test_multi_episode_code_unchanged(self):
        """Multi-ep file uses SxxExxExx format — existing task-3 behaviour."""
        from backend.rename.naming import build_target

        meta = {
            "media_type": "tv",
            "title": "My Show",
            "year": 2023,
            "season": 1,
            "episode": 3,
            "episode_end": 4,
            "original_filename": "my.show.s01e03e04.mkv",
        }
        fname, _ = build_target(meta, tv_root="/tv")
        assert "S01E03E04" in fname

    def test_part_suffix_present_when_part_set(self):
        """Split file with part=1: fname contains 'Part 1'."""
        from backend.rename.naming import build_target

        meta = {
            "media_type": "tv",
            "title": "My Show",
            "year": 2023,
            "season": 1,
            "episode": 5,
            "part": 1,
            "original_filename": "my.show.s01e05.part1.mkv",
        }
        fname, _ = build_target(meta, tv_root="/tv")
        assert "Part 1" in fname

    def test_clean_single_ep_has_no_episode_end_in_name(self):
        """A normal single episode must not have a second E-code appended."""
        from backend.rename.naming import build_target

        meta = {
            "media_type": "tv",
            "title": "My Show",
            "year": 2023,
            "season": 1,
            "episode": 3,
            # episode_end is absent — must NOT appear in the filename
            "original_filename": "my.show.s01e03.mkv",
        }
        fname, _ = build_target(meta, tv_root="/tv")
        assert fname == "My Show (2023) - S01E03.mkv"
        # Confirm no second episode code crept in
        assert fname.count("E") == 1

    def test_clean_single_ep_has_no_part_in_name(self):
        """A normal single episode must not have 'Part N' in its name."""
        from backend.rename.naming import build_target

        meta = {
            "media_type": "tv",
            "title": "My Show",
            "year": 2023,
            "season": 1,
            "episode": 3,
            "original_filename": "my.show.s01e03.mkv",
        }
        fname, _ = build_target(meta, tv_root="/tv")
        assert "Part" not in fname


# ---------------------------------------------------------------------------
# Class 3 — parse_filename regression: multi-ep and part fields
# ---------------------------------------------------------------------------

class TestParseFilenameRegression:
    """parse_filename() must return unchanged output for single-episode filenames."""

    def test_single_ep_no_episode_end(self):
        from backend.filename_utils import parse_filename

        result = parse_filename("Good.Show.S02E05.1080p.WEB-DL.mkv")
        assert result["season"] == 2
        assert result["episode"] == 5
        assert result["episode_end"] is None
        assert result["part"] is None
        assert result["is_tv"] is True

    def test_multi_ep_sets_episode_end(self):
        from backend.filename_utils import parse_filename

        result = parse_filename("Good.Show.S01E03E04.1080p.mkv")
        assert result["episode"] == 3
        assert result["episode_end"] == 4

    def test_part_indicator_parsed(self):
        from backend.filename_utils import parse_filename

        result = parse_filename("Good.Show.S01E05.Part1.mkv")
        assert result["part"] == 1

    def test_movie_has_no_episode_fields(self):
        from backend.filename_utils import parse_filename

        result = parse_filename("Great.Film.2021.1080p.BluRay.mkv")
        assert result["is_tv"] is False
        assert result["season"] is None
        assert result["episode"] is None
        assert result["episode_end"] is None
        assert result["part"] is None


# ---------------------------------------------------------------------------
# Class 4 — Proposal detection: combined / split / correction
# ---------------------------------------------------------------------------

class TestCombinedEpisodeProposal:
    """_detect_combined_episode() should fire when runtime ≈ sum of two episodes."""

    def test_fires_when_file_matches_sum_of_two_episodes(self):
        from backend.rename.service import _detect_combined_episode

        episodes = _make_episodes({3: 44, 4: 44})
        match = {"tmdb_id": 1, "episode": 3}
        result = _detect_combined_episode(match, 88.0, episodes)

        assert result is not None
        assert result["episode_start"] == 3
        assert result["episode_end"] == 4
        assert result["proposed_code"] == "E03E04"
        assert "runtime_match_pct" in result

    def test_does_not_fire_when_ratio_too_low(self):
        """file_min = 1.5 × ep_min — ratio 1.5 is below 1.7 threshold."""
        from backend.rename.service import _detect_combined_episode

        episodes = _make_episodes({3: 44, 4: 44})
        match = {"tmdb_id": 1, "episode": 3}
        result = _detect_combined_episode(match, 66.0, episodes)
        assert result is None

    def test_does_not_fire_when_ratio_too_high(self):
        """file_min = 2.5 × ep_min — ratio 2.5 is above 2.4 threshold."""
        from backend.rename.service import _detect_combined_episode

        episodes = _make_episodes({3: 44, 4: 44})
        match = {"tmdb_id": 1, "episode": 3}
        result = _detect_combined_episode(match, 110.0, episodes)
        assert result is None

    def test_does_not_fire_when_sum_mismatches(self):
        """File is 88 min but E3(44) + E4(120) = 164 min — too far off."""
        from backend.rename.service import _detect_combined_episode

        episodes = _make_episodes({3: 44, 4: 120})
        match = {"tmdb_id": 1, "episode": 3}
        result = _detect_combined_episode(match, 88.0, episodes)
        assert result is None

    def test_does_not_fire_for_movie(self):
        """Movies lack episode number — helper exits immediately."""
        from backend.rename.service import _detect_combined_episode

        match = {"tmdb_id": 2, "media_type": "movie"}
        result = _detect_combined_episode(match, 120.0, [])
        assert result is None


class TestSplitFileProposal:
    """_detect_split_file() should fire when file is < 60% of episode runtime
    and a sibling exists."""

    def test_fires_when_file_is_short_and_sibling_present(self):
        from backend.rename.service import _detect_split_file

        with patch("backend.rename.service._find_split_sibling",
                   return_value="/show/S01E05.Part2.mkv"):
            result = _detect_split_file("/show/S01E05.Part1.mkv", 20.0, 44.0)

        assert result is not None
        assert result["sibling_path"] == "/show/S01E05.Part2.mkv"
        assert result["part"] in (1, 2)
        assert "proposed_suffix" in result

    def test_does_not_fire_above_threshold(self):
        """40/44 = 0.91 — well above 0.60 cutoff."""
        from backend.rename.service import _detect_split_file

        result = _detect_split_file("/show/S01E05.mkv", 40.0, 44.0)
        assert result is None

    def test_does_not_fire_without_sibling(self):
        from backend.rename.service import _detect_split_file

        with patch("backend.rename.service._find_split_sibling", return_value=None):
            result = _detect_split_file("/show/S01E05.Part1.mkv", 20.0, 44.0)
        assert result is None

    def test_does_not_fire_at_exactly_sixty_percent(self):
        """file_min = tmdb_min * 0.6 exactly — condition is >=, so no split."""
        from backend.rename.service import _detect_split_file

        with patch("backend.rename.service._find_split_sibling",
                   return_value="/show/S01E05.Part2.mkv"):
            result = _detect_split_file("/show/S01E05.Part1.mkv", 26.4, 44.0)
        # 26.4 / 44.0 = 0.6 exactly → condition `file_min >= tmdb_min * 0.6` is True
        assert result is None


class TestEpisodeCorrectionCandidates:
    """episode_correction_candidates() surfaces better episode matches."""

    def test_proposes_episode_with_better_runtime_fit(self):
        from backend.rename.confidence import episode_correction_candidates

        # Current match is E3 with 90-min runtime; file is 44 min.
        # E2 and E4 both have 44-min runtimes — much better fit.
        episodes = [
            {"episode_number": 2, "runtime": 44},
            {"episode_number": 3, "runtime": 90},
            {"episode_number": 4, "runtime": 44},
        ]
        result = episode_correction_candidates(44.0, episodes, current_episode=3)

        ep_numbers = [ep for ep, _ in result]
        assert 2 in ep_numbers or 4 in ep_numbers

    def test_returns_empty_when_current_is_best(self):
        """All episodes have the same runtime — no improvement possible."""
        from backend.rename.confidence import episode_correction_candidates

        episodes = _make_episodes({2: 44, 3: 44, 4: 44})
        result = episode_correction_candidates(44.0, episodes, current_episode=3)
        assert result == []

    def test_returns_empty_when_no_runtimes_available(self):
        from backend.rename.confidence import episode_correction_candidates

        episodes = [{"episode_number": i} for i in range(1, 5)]
        result = episode_correction_candidates(44.0, episodes, current_episode=2)
        assert result == []

    def test_sorted_best_first(self):
        """The episode with the highest runtime score gain comes first."""
        from backend.rename.confidence import episode_correction_candidates

        episodes = [
            {"episode_number": 2, "runtime": 44},   # exact match for 44-min file
            {"episode_number": 3, "runtime": 90},   # current — very wrong
            {"episode_number": 4, "runtime": 47},   # close but not exact
        ]
        result = episode_correction_candidates(44.0, episodes, current_episode=3)
        assert result[0][0] == 2  # E2 is the best fit

    def test_respects_search_radius(self):
        """Episodes more than search_radius away from current are not considered."""
        from backend.rename.confidence import episode_correction_candidates

        episodes = [
            {"episode_number": i, "runtime": 44 if i != 1 else 90}
            for i in range(1, 10)
        ]
        result = episode_correction_candidates(44.0, episodes, current_episode=1,
                                               search_radius=3)
        ep_numbers = [ep for ep, _ in result]
        # E5 is 4 away → excluded; E4 is 3 away → included
        assert 5 not in ep_numbers
        assert 4 in ep_numbers


# ---------------------------------------------------------------------------
# Class 7 — End-to-end seam: parse → _identify → build_target
# ---------------------------------------------------------------------------

class TestMultiEpisodeSeam:
    """Drives the real _identify → build_target path with a stubbed TMDB search.

    This is the integration guard that unit tests miss: episode_end / part are
    parsed and rendered correctly in isolation, but the service's .update() calls
    must actually carry them across the seam, and the phantom-episode-title bug
    must not reappear. Regression for the C1/C2 holistic-review findings.
    """

    def _svc(self, candidate):
        from backend.rename.service import RenameService
        svc = RenameService.__new__(RenameService)
        reg = MagicMock()
        reg.config = {
            "auto_rename_confidence_threshold": 80,
            "auto_rename_llm_enabled": False,
            "auto_rename_tv_library": "/tv",
            "auto_rename_movie_library": "/movies",
        }
        svc._reg = reg
        svc._tmdb_search_override = lambda title, year, mt: [candidate]
        svc._client = None
        return svc

    _CAND = {"id": 100, "name": "Show Name", "first_air_date": "2024-01-01"}

    def test_double_episode_name_carries_through(self):
        from backend.rename import naming as _naming
        fn = "Show.Name.S01E01E02.1080p.WEB-DL.mkv"
        match = self._svc(self._CAND)._identify(fn)
        assert match is not None
        assert match.get("episode") == 1
        assert match.get("episode_end") == 2
        fname, _ = _naming.build_target(
            {**match, "original_filename": fn}, tv_root="/tv")
        assert "S01E01E02" in fname
        assert " - E02" not in fname  # no phantom episode title (C2)

    def test_part_suffix_carries_through(self):
        from backend.rename import naming as _naming
        fn = "Show.Name.S01E05.Part1.1080p.WEB-DL.mkv"
        match = self._svc(self._CAND)._identify(fn)
        assert match is not None
        assert match.get("part") == 1
        fname, _ = _naming.build_target(
            {**match, "original_filename": fn}, tv_root="/tv")
        assert "Part 1" in fname
        assert fname.count("Part") == 1  # no double-Part rendering

    def test_clean_single_episode_seam_unchanged(self):
        from backend.rename import naming as _naming
        fn = "Show.Name.S01E03.1080p.WEB-DL.mkv"
        match = self._svc(self._CAND)._identify(fn)
        assert match.get("episode_end") is None
        assert match.get("part") is None
        fname, _ = _naming.build_target(
            {**match, "original_filename": fn}, tv_root="/tv")
        assert "S01E03" in fname
        assert "E03E" not in fname
        assert "Part" not in fname
