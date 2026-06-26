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
