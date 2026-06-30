"""Tests for disambiguate_episode() and extract_page_hints() in llm_identify."""
import pytest
from unittest.mock import patch, MagicMock
from backend.rename.llm_identify import disambiguate_episode, extract_page_hints


class TestDisambiguateEpisode:
    _CANDIDATES = [
        {"episode": 3, "season": 1, "title": "The Right One", "runtime": 44},
        {"episode": 5, "season": 1, "title": "Another Episode", "runtime": 44},
    ]

    def test_returns_none_when_no_base_url(self):
        result = disambiguate_episode(
            "Show.S01E03.mkv", self._CANDIDATES,
            base_url="", model="minicpm-v")
        assert result is None

    def test_returns_none_when_fewer_than_two_candidates(self):
        result = disambiguate_episode(
            "Show.S01E03.mkv", [self._CANDIDATES[0]],
            base_url="http://ollama:11434", model="minicpm-v")
        assert result is None

    def test_returns_none_on_ollama_error(self):
        with patch("requests.post", side_effect=Exception("connection refused")):
            result = disambiguate_episode(
                "Show.S01E03.mkv", self._CANDIDATES,
                base_url="http://ollama:11434", model="minicpm-v")
        assert result is None

    def test_parses_valid_ollama_response(self):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "message": {"content": '{"episode": 3, "season": 1}'}
        }
        mock_resp.raise_for_status = MagicMock()
        with patch("requests.post", return_value=mock_resp):
            result = disambiguate_episode(
                "Show.S01E03.mkv", self._CANDIDATES,
                base_url="http://ollama:11434", model="minicpm-v")
        assert result == {"episode": 3, "season": 1}


class TestExtractPageHints:
    def test_detects_double_episode_phrase(self):
        result = extract_page_hints("This is a double episode special.")
        assert result["is_combined"] is True

    def test_detects_2_in_1(self):
        result = extract_page_hints("A 2-in-1 release combining episodes 4 and 5.")
        assert result["is_combined"] is True

    def test_detects_part_1(self):
        result = extract_page_hints("Season finale Part 1 of 2.")
        assert result["is_split"] is True
        assert result["part_number"] == 1

    def test_detects_part_2(self):
        result = extract_page_hints("Season finale Part 2.")
        assert result["is_split"] is True
        assert result["part_number"] == 2

    def test_no_hints_returns_false_flags(self):
        result = extract_page_hints("Great episode, action-packed.")
        assert result["is_combined"] is False
        assert result["is_split"] is False
        assert result["part_number"] is None
        assert result["episode_count"] is None

    def test_empty_text_returns_empty_hints(self):
        result = extract_page_hints("")
        assert result["is_combined"] is False

    def test_ollama_used_when_configured(self):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "message": {"content": '{"is_combined": true, "is_split": false, "part_number": null, "episode_count": 2}'}
        }
        mock_resp.raise_for_status = MagicMock()
        with patch("requests.post", return_value=mock_resp):
            result = extract_page_hints(
                "Two episodes combined.", base_url="http://ollama:11434", model="m")
        assert result["is_combined"] is True
        assert result["episode_count"] == 2

    def test_falls_back_to_regex_on_ollama_failure(self):
        with patch("requests.post", side_effect=Exception("timeout")):
            result = extract_page_hints(
                "double episode release",
                base_url="http://ollama:11434", model="m")
        assert result["is_combined"] is True  # regex caught it


# ── Subtitle-based identification ────────────────────────────────────

from backend.rename import llm_identify
from backend.rename.llm_identify import identify_from_subtitles, _strip_srt


class TestStripSrt:
    def test_strips_indices_timestamps_and_markup(self):
        srt = (
            "1\n00:00:01,000 --> 00:00:04,000\n<i>Hello there.</i>\n\n"
            "2\n00:00:05,000 --> 00:00:07,000\n{\an8}General Kenobi.\n"
        )
        assert _strip_srt(srt) == ["Hello there.", "General Kenobi."]


class TestIdentifyFromSubtitles:
    def test_none_when_no_base_url(self):
        assert identify_from_subtitles("x.mkv", base_url="", model="m") is None

    def test_none_when_no_subtitles(self):
        with patch.object(llm_identify, "_extract_subtitle_text", return_value=None):
            assert identify_from_subtitles(
                "x.mkv", base_url="http://o:11434", model="m") is None

    def test_none_when_too_few_lines(self):
        with patch.object(llm_identify, "_extract_subtitle_text",
                          return_value="1\n00:00:01,000 --> 00:00:02,000\nHi\n"):
            assert identify_from_subtitles(
                "x.mkv", base_url="http://o:11434", model="m") is None

    def test_parses_model_response_with_candidates(self):
        srt = "\n".join(
            f"{i}\n00:00:{i:02d},000 --> 00:00:{i + 1:02d},000\nLine {i}."
            for i in range(1, 21))
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"message": {
            "content": '{"title": "The Matrix", "year": 1999, "type": "movie"}'}}
        mock_resp.raise_for_status = MagicMock()
        with patch.object(llm_identify, "_extract_subtitle_text", return_value=srt), \
             patch("requests.post", return_value=mock_resp):
            result = identify_from_subtitles(
                "x.mkv", base_url="http://o:11434", model="m",
                candidates=[{"title": "The Matrix", "year": 1999}])
        assert result and result["title"] == "The Matrix"
        assert result["media_type"] == "movie"


# ── OCR-the-credits identification ───────────────────────────────────

from backend.rename.llm_identify import identify_from_credits_ocr


class TestIdentifyFromCreditsOcr:
    def test_none_without_tesseract(self):
        with patch("shutil.which", return_value=None):
            assert identify_from_credits_ocr(
                "x.mkv", base_url="http://o:11434", model="m") is None

    def test_deterministic_title_hit(self):
        with patch("shutil.which", return_value="/usr/bin/tesseract"), \
             patch.object(llm_identify, "_video_duration_seconds", return_value=6000), \
             patch.object(llm_identify, "_ocr_frame",
                          return_value="Directed by someone  THE MATRIX  Warner Bros"):
            result = identify_from_credits_ocr(
                "x.mkv", base_url="http://o:11434", model="m",
                candidates=[{"title": "The Matrix", "year": 1999, "media_type": "movie"}])
        assert result and result["title"] == "The Matrix"
        assert result["media_type"] == "movie"

    def test_llm_fallback_when_no_verbatim_hit(self):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"message": {
            "content": '{"title": "Inception", "year": 2010, "type": "movie"}'}}
        mock_resp.raise_for_status = MagicMock()
        with patch("shutil.which", return_value="/usr/bin/tesseract"), \
             patch.object(llm_identify, "_video_duration_seconds", return_value=6000), \
             patch.object(llm_identify, "_ocr_frame",
                          return_value="unrelated credits text blah blah blah crew list"), \
             patch("requests.post", return_value=mock_resp):
            result = identify_from_credits_ocr(
                "x.mkv", base_url="http://o:11434", model="m",
                candidates=[{"title": "Inception", "year": 2010}])
        assert result and result["title"] == "Inception"


# ── Deterministic cast/director matching (OCR rung) ──────────────────

from backend.rename.llm_identify import _name_in_text, _match_people, _norm_ocr


class TestNameInText:
    def test_surname_match(self):
        assert _name_in_text("Adam Sandler", _norm_ocr("ASSISTANT TO MR. SANDLER"))

    def test_full_name_match(self):
        assert _name_in_text("Drew Barrymore", _norm_ocr("Starring DREW BARRYMORE"))

    def test_short_surname_rejected(self):
        # "lee" is < 4 chars and the full name isn't present → no match.
        assert not _name_in_text("Spike Lee", _norm_ocr("camera operator MR LEE"))

    def test_no_match(self):
        assert not _name_in_text("Tom Hanks", _norm_ocr("unrelated crew listing here"))


class TestMatchPeople:
    _cands = [
        {"title": "50 First Dates", "year": 2004, "media_type": "movie",
         "cast": ["Adam Sandler", "Drew Barrymore", "Rob Schneider"],
         "director": "Peter Segal"},
        {"title": "The Big Lebowski", "year": 1998, "media_type": "movie",
         "cast": ["Jeff Bridges", "John Goodman"], "director": "Joel Coen"},
    ]

    def test_two_people_clear_winner(self):
        text = _norm_ocr("DIRECTED BY PETER SEGAL  MR SANDLER  MS BARRYMORE")
        r = _match_people(text, self._cands)
        assert r and r["title"] == "50 First Dates" and r["media_type"] == "movie"

    def test_single_person_below_guard(self):
        assert _match_people(_norm_ocr("only SANDLER appears"), self._cands) is None

    def test_tie_is_rejected(self):
        cands = [{"title": "A", "cast": ["Common Smith", "Other Jones"]},
                 {"title": "B", "cast": ["Common Smith", "Other Jones"]}]
        assert _match_people(_norm_ocr("smith and jones in the credits"), cands) is None


class TestOcrCastMatch:
    def test_cast_match_without_llm(self):
        with patch("shutil.which", return_value="/usr/bin/tesseract"), \
             patch.object(llm_identify, "_video_duration_seconds", return_value=6000), \
             patch.object(llm_identify, "_ocr_frame",
                          return_value="DIRECTED BY PETER SEGAL  MR SANDLER  MS BARRYMORE"):
            result = identify_from_credits_ocr(
                "x.mkv", base_url="", model="",  # no LLM → deterministic only
                candidates=[
                    {"title": "50 First Dates", "year": 2004, "media_type": "movie",
                     "cast": ["Adam Sandler", "Drew Barrymore"], "director": "Peter Segal"},
                    {"title": "Click", "year": 2006, "media_type": "movie",
                     "cast": ["Kate Beckinsale"], "director": "Frank Coraci"}])
        assert result and result["title"] == "50 First Dates"


class TestMatchPeopleGuard:
    """The >=2-distinct-people + clear-winner guard."""

    def test_single_shared_surname_rejected(self):
        # One surname alone is below the >=2-distinct bar → no pick.
        cands = [{"title": "Smallfoot", "cast": ["Channing Tatum", "James Corden"]},
                 {"title": "Sing", "cast": ["Tori Kelly", "Reese Witherspoon"]}]
        text = _norm_ocr("song performed by KELLY  no cast cards here")
        assert _match_people(text, cands) is None

    def test_two_full_names_win_without_director(self):
        cands = [{"title": "X", "cast": ["Adam Sandler", "Drew Barrymore"]},
                 {"title": "Y", "cast": ["Bill Murray"]}]
        text = _norm_ocr("starring ADAM SANDLER and DREW BARRYMORE tonight")
        r = _match_people(text, cands)
        assert r and r["title"] == "X"

    def test_director_plus_one_surname_wins(self):
        cands = [{"title": "X", "cast": ["Adam Sandler"], "director": "Peter Segal"},
                 {"title": "Y", "cast": ["Bill Murray"], "director": "Harold Ramis"}]
        text = _norm_ocr("DIRECTED BY PETER SEGAL  starring MR SANDLER")
        r = _match_people(text, cands)
        assert r and r["title"] == "X"


# ── Feature: dependency_status (rename health) ───────────────────────

class TestDependencyStatus:
    def test_reports_binary_keys(self):
        from backend.rename.llm_identify import dependency_status
        s = dependency_status()
        assert set(s) == {"ffmpeg", "ffprobe", "tesseract"}
        assert all(isinstance(v, bool) for v in s.values())
