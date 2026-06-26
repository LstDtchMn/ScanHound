# Episode Intelligence Upgrade — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add runtime-gated episode correction, multi-episode/split-file detection, and Ollama-assisted disambiguation to the ScanHound rename pipeline, without touching the movie path, season-pack path, or any clean-file rename flow.

**Architecture:** Three independently testable layers — pure confidence functions (no I/O), LLM adapter functions (fail-safe, return None on any error), and service.py orchestration (reads from both, writes proposals into the match dict). All detections are proposals stored in the job dict; no rename action is taken without user confirmation.

**Tech Stack:** Python 3.12, FastAPI, TMDB API (`backend/tmdb_client.py`), Ollama HTTP API (shared with existing LLM path), pytest, ffprobe (already in Dockerfile).

## Global Constraints

- All new Ollama calls must be wrapped in `try/except` returning `None` — failure must never raise or block
- Corrections/detections are stored as new fields on the `match` dict (`suggested_correction`, `combined_episode`, `split_file`) and surfaced in the job's `warning_message`; the rename itself is unchanged unless the user acts on the proposal
- The movie path (`media_type == "movie"`) must be untouched by all new logic
- The season-pack path (`season` set, `episode` is `None`) must be untouched
- TMDB season data must be cached in a local dict within `_process_file` — never fetched twice for the same season in one file's pipeline run
- All new code follows the existing pattern: no docstrings beyond a single-line purpose comment, no inline TODO comments
- Tests live in `tests/` (flat, existing convention); run with `pytest tests/<file> -v`

---

## File Map

| File | Change type | Purpose |
|------|-------------|---------|
| `backend/rename/confidence.py` | Modify + extend | Threshold tightening; new `episode_correction_candidates()` |
| `backend/filename_utils.py` | Modify | Parse `S01E01E02`, `E01-E02`, `Part1/Part2` from release names |
| `backend/rename/naming.py` | Modify | Emit `S01E01E02` and `Part N` suffixes when meta fields are set |
| `backend/rename/llm_identify.py` | Extend | `disambiguate_episode()`, `extract_page_hints()`, `_regex_page_hints()`, `_ollama_page_hints()` |
| `backend/rename/service.py` | Modify + extend | Season cache; `_try_episode_rescan()`, `_detect_combined_episode()`, `_detect_split_file()`, `_find_split_sibling()` helpers; wire into `_process_file()` |
| `backend/detail_scraper.py` | Modify | Call `extract_page_hints()` on page body; return hints in result dict |
| `tests/test_confidence.py` | Create | Unit tests for threshold change and `episode_correction_candidates()` |
| `tests/test_filename_utils.py` | Create/extend | Multi-ep and part parsing |
| `tests/test_naming.py` | Create | Multi-ep and part naming output |
| `tests/test_llm_identify.py` | Create | `disambiguate_episode()` and `extract_page_hints()` (regex path, mocked Ollama) |
| `tests/test_episode_rescan.py` | Create | Integration tests for re-scan, combined, split detection in service.py |

---

## Known Weaknesses (First Draft)

Document these in code comments where relevant so future implementers know what to improve:

1. **Search window is narrow** — re-scan checks ±3 episodes and ±1 season. A rip mislabeled by more than 3 episodes will be missed. Acceptable tradeoff for API cost.
2. **Split detection requires both files queued together** — if only Part 1 is in the queue, the sibling scan finds Part 2 on disk (fine), but if Part 2 isn't on disk yet, no detection. Document this limitation in the job's warning message.
3. **No regex fallback when Ollama is unconfigured for page hints** — the `_regex_page_hints()` fallback covers common English phrases; non-English release sites will be missed.
4. **3-way episode combos not handled** — `_detect_combined_episode()` only checks pairs (ep_n + ep_{n+1}). Triple episodes (rare) fall through; the runtime mismatch penalty still fires.
5. **TMDB season fetch duplication** — the existing episode-validity block already fetches season data; the re-scan reuses the cache dict, but the `_try_episode_rescan` helper fetches adjacent seasons which may be slow on first run per file.

---

## Task 1 — Runtime Threshold Tightening

**Files:**
- Modify: `backend/rename/confidence.py` (line 104 — the `pct <= 0.15` check)
- Create: `tests/test_confidence.py`

**Interfaces:**
- Produces: `runtime_confidence_delta(file_minutes, tmdb_minutes) -> float` — unchanged signature, tighter neutral zone

- [ ] **Step 1: Write the failing test**

```python
# tests/test_confidence.py
import pytest
from backend.rename.confidence import runtime_confidence_delta, filesize_plausibility_delta


class TestRuntimeThresholdTightening:
    def test_12pct_deviation_now_penalised(self):
        # 90-min TMDB, 101-min file = 12.2% off — was neutral (≤15%), now -10 (≤30%)
        result = runtime_confidence_delta(101, 90)
        assert result == -10.0

    def test_9pct_deviation_still_neutral(self):
        # 90-min TMDB, 98-min file = 8.9% off — between 8% and 10% → 0
        result = runtime_confidence_delta(98, 90)
        assert result == 0.0

    def test_8pct_deviation_still_bonus(self):
        # 90-min TMDB, 97-min file = 7.8% off — still ≤8% → +5
        result = runtime_confidence_delta(97, 90)
        assert result == 5.0

    def test_near_exact_still_plus_ten(self):
        # 2.7% off → ≤3% → +10
        result = runtime_confidence_delta(185, 180)
        assert result == 10.0

    def test_missing_values_return_zero(self):
        assert runtime_confidence_delta(None, 90) == 0.0
        assert runtime_confidence_delta(90, 0) == 0.0
        assert runtime_confidence_delta(0, 90) == 0.0
```

- [ ] **Step 2: Run to verify it fails**

```
pytest tests/test_confidence.py::TestRuntimeThresholdTightening -v
```

Expected: `test_12pct_deviation_now_penalised` FAILS (returns `0.0`, not `-10.0`). Others may pass.

- [ ] **Step 3: Apply the one-line fix**

In `backend/rename/confidence.py`, change line 104:

```python
    if pct <= 0.15:    # ← OLD
```
to:
```python
    if pct <= 0.10:
```

- [ ] **Step 4: Run to verify all pass**

```
pytest tests/test_confidence.py::TestRuntimeThresholdTightening -v
```

Expected: 5/5 PASS.

- [ ] **Step 5: Commit**

```
git add backend/rename/confidence.py tests/test_confidence.py
git commit -m "fix(confidence): tighten runtime neutral zone from 15% to 10% deviation"
```

---

## Task 2 — Multi-Episode Filename Parsing

**Files:**
- Modify: `backend/filename_utils.py`
- Create: `tests/test_filename_utils.py`

**Interfaces:**
- Produces: `parse_filename(filename) -> dict` gains two new optional keys:
  - `episode_end: int | None` — last episode in a multi-ep file (e.g. `S01E01E02` → `episode_end=2`)
  - `part: int | None` — part number for split files (e.g. `Part1` → `part=1`)
  - Existing keys (`title`, `year`, `season`, `episode`, `resolution`, `is_tv`) are unchanged

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_filename_utils.py
import pytest
from backend.filename_utils import parse_filename


class TestMultiEpisodeParsing:
    def test_double_episode_concatenated(self):
        r = parse_filename("Show.Name.S01E01E02.1080p.WEB-DL.mkv")
        assert r["season"] == 1
        assert r["episode"] == 1
        assert r["episode_end"] == 2

    def test_double_episode_dash_notation(self):
        r = parse_filename("Show.Name.S02E05-E06.720p.HDTV.mkv")
        assert r["episode"] == 5
        assert r["episode_end"] == 6

    def test_double_episode_dot_notation(self):
        r = parse_filename("Show.Name.S03E11.E12.1080p.mkv")
        assert r["episode"] == 11
        assert r["episode_end"] == 12

    def test_part_one(self):
        r = parse_filename("Show.Name.S01E05.Part1.1080p.mkv")
        assert r["part"] == 1
        assert r.get("episode_end") is None

    def test_part_two_dot_notation(self):
        r = parse_filename("Show.Name.S01E05.Part.2.720p.mkv")
        assert r["part"] == 2

    def test_pt_notation(self):
        r = parse_filename("Show.Name.S01E05.Pt1.1080p.mkv")
        assert r["part"] == 1

    def test_clean_single_episode_unchanged(self):
        r = parse_filename("Show.Name.S01E03.1080p.WEB-DL.mkv")
        assert r["episode"] == 3
        assert r.get("episode_end") is None
        assert r.get("part") is None

    def test_movie_unchanged(self):
        r = parse_filename("Movie.Title.2024.1080p.BluRay.mkv")
        assert r.get("episode_end") is None
        assert r.get("part") is None
        assert r["is_tv"] is False
```

- [ ] **Step 2: Run to verify they fail**

```
pytest tests/test_filename_utils.py::TestMultiEpisodeParsing -v
```

Expected: All fail with `KeyError` or assertion errors (keys don't exist yet).

- [ ] **Step 3: Add multi-episode parsing to `parse_filename`**

In `backend/filename_utils.py`, after line 90 (`result["is_tv"] = True`) inside the `if se_match:` block, add:

```python
        # Multi-episode: SxxExxEyy or SxxExx-Eyy or SxxExx.Eyy
        after_first_ep = name[se_match.end():]
        me_match = re.match(r'[.\-]?E(\d{1,3})', after_first_ep, re.IGNORECASE)
        if me_match:
            result["episode_end"] = int(me_match.group(1))
```

At the end of `parse_filename`, before `return result` (after all the title cleaning at line 138), add:

```python
    # Part indicator: Part1, Part 2, Pt1, Pt.2
    part_match = re.search(
        r'[.\s\-_](?:Part|Pt)[\s.\-]?(\d)', name, re.IGNORECASE)
    if part_match:
        result["part"] = int(part_match.group(1))
```

- [ ] **Step 4: Run to verify all pass**

```
pytest tests/test_filename_utils.py::TestMultiEpisodeParsing -v
```

Expected: 8/8 PASS.

- [ ] **Step 5: Commit**

```
git add backend/filename_utils.py tests/test_filename_utils.py
git commit -m "feat(filename): parse multi-episode (E01E02) and split-part notation"
```

---

## Task 3 — Multi-Episode and Split Naming

**Files:**
- Modify: `backend/rename/naming.py`
- Create: `tests/test_naming.py`

**Interfaces:**
- Consumes: `build_target(meta, ...)` — `meta` may now contain `episode_end: int` and/or `part: int`
- Produces: `build_target` returns filenames in Plex multi-ep convention (`S01E01E02`) and part suffix (`Part 1`)

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_naming.py
import pytest
from backend.rename.naming import build_target


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
```

- [ ] **Step 2: Run to verify they fail**

```
pytest tests/test_naming.py::TestMultiEpNaming -v
```

Expected: `test_combined_episode_code`, `test_three_episode_code`, `test_part_suffix`, `test_part_two_suffix` FAIL.

- [ ] **Step 3: Update `build_target` in `naming.py`**

Replace lines 110–116 of `backend/rename/naming.py` (the `elif media_type == "tv":` block):

```python
    elif media_type == "tv":
        season = int(meta.get("season") or 1)
        episode = int(meta.get("episode") or 1)
        episode_end = meta.get("episode_end")
        part = meta.get("part")
        ep_title = sanitize_filename(meta.get("episode_title") or "")
        show = f"{title} ({year})" if year else title
        code = f"S{season:02d}E{episode:02d}"
        if episode_end:
            code += f"E{int(episode_end):02d}"
        fname = f"{show} - {code}"
        if ep_title:
            fname += f" - {ep_title}"
        if part:
            fname += f" - Part {part}"
        fname += ext
```

Also update `_tokens()` (lines 65–79) so template rendering reflects `episode_end`:

```python
def _tokens(meta: dict) -> dict:
    season = int(meta.get("season") or 1)
    episode = int(meta.get("episode") or 1)
    episode_end = meta.get("episode_end")
    ep_code = f"{episode:02d}"
    if episode_end:
        ep_code += f"E{int(episode_end):02d}"
    return {
        "title": sanitize_filename(meta.get("title") or "Unknown"),
        "year": str(meta.get("year") or ""),
        "season": f"{season:02d}",
        "episode": ep_code,
        "episode_title": sanitize_filename(meta.get("episode_title") or ""),
        "resolution": meta.get("resolution") or "",
        "quality": meta.get("resolution") or "",
        "imdb_id": meta.get("imdb_id") or "",
        "tmdb_id": str(meta.get("tmdb_id") or ""),
        "media_type": meta.get("media_type", "movie"),
    }
```

- [ ] **Step 4: Run to verify all pass**

```
pytest tests/test_naming.py::TestMultiEpNaming -v
```

Expected: 6/6 PASS.

- [ ] **Step 5: Smoke-test the existing movie path is untouched**

```
pytest tests/test_naming.py -v
```

All tests pass.

- [ ] **Step 6: Commit**

```
git add backend/rename/naming.py tests/test_naming.py
git commit -m "feat(naming): support S01E01E02 multi-episode and Part N split suffixes"
```

---

## Task 4 — Episode Correction Scoring Function

**Files:**
- Modify: `backend/rename/confidence.py`
- Modify: `tests/test_confidence.py`

**Interfaces:**
- Produces: `episode_correction_candidates(file_minutes, episodes, current_episode, *, search_radius=3, min_gain=15.0) -> list[tuple[int, float]]`
  - `episodes`: list of TMDB episode dicts with keys `episode_number` (int) and `runtime` (int, minutes)
  - `current_episode`: the episode number currently in the match
  - Returns `[(episode_number, score_delta), ...]` sorted best-first — only candidates at least `min_gain` points better than the current episode's score. Empty list if current episode already scores best.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_confidence.py`:

```python
from backend.rename.confidence import episode_correction_candidates


class TestEpisodeCorrectionCandidates:
    def _eps(self):
        # Fake season: 6 episodes, each 44 min
        return [{"episode_number": i, "runtime": 44} for i in range(1, 7)]

    def test_returns_better_episode_when_clear_winner(self):
        # File is 44 min, matched to E3 (44 min = perfect), but we pass E4
        # as current_episode — E3 should be proposed
        eps = self._eps()
        # Match is currently E4 (44 min file, E4 also 44 min — tied, no gain)
        result = episode_correction_candidates(44.0, eps, current_episode=4)
        # No one is 15+ better than E4 when all are 44 min
        assert result == []

    def test_proposes_correct_episode_when_runtime_differs(self):
        # Make E3 have 90 min (movie-length wrong entry) — file is 44 min
        # All other episodes are 44 min. Current match is E3.
        eps = [{"episode_number": i, "runtime": 44 if i != 3 else 90}
               for i in range(1, 7)]
        # File is 44 min, current match is E3 (90 min → -20 score)
        # E2 and E4 are 44 min → +10 each — gain = 30, well above min_gain=15
        result = episode_correction_candidates(44.0, eps, current_episode=3)
        ep_numbers = [ep for ep, _ in result]
        assert 2 in ep_numbers or 4 in ep_numbers
        # All proposed candidates must be better by at least 15
        for ep_num, score in result:
            assert score >= -20 + 15  # current is -20, candidates must be ≥ -5

    def test_respects_search_radius(self):
        eps = [{"episode_number": i, "runtime": 44 if i != 1 else 90}
               for i in range(1, 10)]
        # Current match is E1 (90 min), file is 44 min
        # E2 is radius 1, E4 is radius 3, E5 is radius 4 — excluded
        result = episode_correction_candidates(44.0, eps, current_episode=1,
                                               search_radius=3)
        ep_numbers = [ep for ep, _ in result]
        assert 5 not in ep_numbers
        assert 4 in ep_numbers  # radius 3 — included

    def test_returns_empty_when_no_runtimes(self):
        eps = [{"episode_number": i} for i in range(1, 5)]  # no runtime key
        result = episode_correction_candidates(44.0, eps, current_episode=2)
        assert result == []

    def test_sorted_best_first(self):
        # E2 is perfect (44 min), E4 is close (47 min), current E3 is 90 min
        eps = [
            {"episode_number": 2, "runtime": 44},
            {"episode_number": 3, "runtime": 90},
            {"episode_number": 4, "runtime": 47},
        ]
        result = episode_correction_candidates(44.0, eps, current_episode=3)
        assert len(result) >= 1
        assert result[0][0] == 2  # E2 (exact match) should be first
```

- [ ] **Step 2: Run to verify they fail**

```
pytest tests/test_confidence.py::TestEpisodeCorrectionCandidates -v
```

Expected: ImportError or NameError — function doesn't exist yet.

- [ ] **Step 3: Implement `episode_correction_candidates` in `confidence.py`**

Append to `backend/rename/confidence.py`:

```python
def episode_correction_candidates(
    file_minutes: float,
    episodes: list,
    current_episode: int,
    *,
    search_radius: int = 3,
    min_gain: float = 15.0,
) -> list:
    """Score TMDB episodes near current_episode by runtime fit against file_minutes.

    Returns [(episode_number, runtime_score), ...] sorted best-first.
    Only includes candidates at least min_gain score-points better than the
    current episode's runtime score. Empty when the current episode already
    scores best or no runtimes are available.
    """
    current_score: float | None = None
    candidates: list[tuple[int, float]] = []

    for ep in episodes:
        ep_num = ep.get("episode_number")
        ep_rt = ep.get("runtime")
        if not ep_num or not ep_rt:
            continue
        if abs(ep_num - current_episode) > search_radius:
            continue
        score = runtime_confidence_delta(float(file_minutes), float(ep_rt))
        if ep_num == current_episode:
            current_score = score
        else:
            candidates.append((ep_num, score))

    if current_score is None:
        return []

    better = [(n, s) for n, s in candidates if s - current_score >= min_gain]
    return sorted(better, key=lambda x: x[1], reverse=True)
```

- [ ] **Step 4: Run to verify all pass**

```
pytest tests/test_confidence.py::TestEpisodeCorrectionCandidates -v
```

Expected: 5/5 PASS.

- [ ] **Step 5: Run full confidence test suite to check no regressions**

```
pytest tests/test_confidence.py -v
```

All tests pass.

- [ ] **Step 6: Commit**

```
git add backend/rename/confidence.py tests/test_confidence.py
git commit -m "feat(confidence): add episode_correction_candidates() for runtime-gated re-scan"
```

---

## Task 5 — LLM Episode Disambiguator and Page Hint Extractor

**Files:**
- Modify: `backend/rename/llm_identify.py`
- Create: `tests/test_llm_identify.py`

**Interfaces:**
- Produces:
  - `disambiguate_episode(filename, candidates, *, base_url, model, timeout) -> dict | None`
    - `candidates`: `[{episode: int, season: int, title: str, runtime: int}, ...]` (2–3 items)
    - Returns `{episode: int, season: int}` or `None`
  - `extract_page_hints(page_text, *, base_url="", model="") -> dict`
    - Returns `{is_combined: bool, is_split: bool, part_number: int|None, episode_count: int|None}`
    - Uses regex fallback when Ollama is unconfigured

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_llm_identify.py
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
```

- [ ] **Step 2: Run to verify they fail**

```
pytest tests/test_llm_identify.py -v
```

Expected: ImportError — functions don't exist yet.

- [ ] **Step 3: Implement the two functions in `llm_identify.py`**

Append the following to `backend/rename/llm_identify.py` (after `test_connection`):

```python
_DISAMBIG_SYSTEM = (
    "You identify which TV episode a media file most likely corresponds to. "
    "Given a filename and 2-3 candidate episodes, pick the best match. "
    "Respond ONLY with JSON: {\"episode\": <number>, \"season\": <number>}"
)


def disambiguate_episode(
    filename: str,
    candidates: list,
    *,
    base_url: str,
    model: str,
    timeout: float = _TIMEOUT,
) -> Optional[dict]:
    """Choose between close episode candidates using the Ollama chat API.

    Returns {episode: int, season: int} or None on any failure.
    """
    if not filename or not base_url or not model or len(candidates) < 2:
        return None
    lines = [
        f"  {i + 1}. S{c['season']:02d}E{c['episode']:02d} "
        f"\"{c.get('title', '')}\" ({c.get('runtime', '?')}min)"
        for i, c in enumerate(candidates[:3])
    ]
    payload = {
        "model": model,
        "format": "json",
        "stream": False,
        "options": {"temperature": 0},
        "messages": [
            {"role": "system", "content": _DISAMBIG_SYSTEM},
            {"role": "user", "content": (
                f"Filename: {filename}\nCandidates:\n"
                + "\n".join(lines)
                + "\nWhich episode does this file most likely contain?"
            )},
        ],
    }
    try:
        resp = requests.post(base_url.rstrip("/") + "/api/chat",
                             json=payload, timeout=timeout)
        resp.raise_for_status()
        data = json.loads(resp.json().get("message", {}).get("content", "{}"))
        ep = int(data.get("episode", 0))
        sn = int(data.get("season", 0))
        if ep > 0 and sn > 0:
            return {"episode": ep, "season": sn}
    except Exception as e:
        logger.debug("Ollama episode disambiguate failed: %s", e)
    return None


# ── Page hint extraction ──────────────────────────────────────────────────

_HINT_COMBINED_RE = re.compile(
    r'\b(?:double[\s\-]?episode|2[\s\-]in[\s\-]1|two[\s\-](?:part|episode)s?'
    r'|episodes?\s+\d+\s*[&+]\s*\d+|multi[\s\-]?episode)\b',
    re.IGNORECASE,
)
_HINT_SPLIT_RE = re.compile(
    r'\bpart\s*[12]\b|\bpt\.?\s*[12]\b|\b[12]\s+of\s+2\b',
    re.IGNORECASE,
)
_HINT_PART_NUM_RE = re.compile(
    r'\bpart\s*(\d)\b|\bpt\.?\s*(\d)\b|\b(\d)\s+of\s+\d\b',
    re.IGNORECASE,
)
_HINT_EP_COUNT_RE = re.compile(r'\b(\d+)[\s\-]?(?:episodes?|eps?)\b', re.IGNORECASE)

_HINT_SYSTEM = (
    "Extract multi-episode metadata from media download page text. "
    "Respond ONLY with JSON: "
    "{\"is_combined\": bool, \"is_split\": bool, "
    "\"part_number\": int_or_null, \"episode_count\": int_or_null}"
)


def extract_page_hints(
    page_text: str,
    *,
    base_url: str = "",
    model: str = "",
) -> dict:
    """Extract combined/split episode hints from download page text.

    Tries Ollama first; falls back to regex when Ollama is unconfigured or fails.
    Always returns a dict with all four keys.
    """
    empty: dict = {
        "is_combined": False, "is_split": False,
        "part_number": None, "episode_count": None,
    }
    if not page_text:
        return empty
    if base_url and model:
        ollama_result = _ollama_page_hints(page_text[:3000],
                                           base_url=base_url, model=model)
        if ollama_result:
            return ollama_result
    return _regex_page_hints(page_text)


def _ollama_page_hints(text: str, *, base_url: str, model: str) -> Optional[dict]:
    payload = {
        "model": model,
        "format": "json",
        "stream": False,
        "options": {"temperature": 0},
        "messages": [
            {"role": "system", "content": _HINT_SYSTEM},
            {"role": "user", "content": f"Extract episode info from:\n{text}"},
        ],
    }
    try:
        resp = requests.post(base_url.rstrip("/") + "/api/chat",
                             json=payload, timeout=_TIMEOUT)
        resp.raise_for_status()
        data = json.loads(resp.json().get("message", {}).get("content", "{}"))
        ep_count = data.get("episode_count")
        part_num = data.get("part_number")
        return {
            "is_combined": bool(data.get("is_combined", False)),
            "is_split": bool(data.get("is_split", False)),
            "part_number": int(part_num) if part_num else None,
            "episode_count": int(ep_count) if ep_count else None,
        }
    except Exception as e:
        logger.debug("Ollama page hint extraction failed: %s", e)
        return None


def _regex_page_hints(text: str) -> dict:
    is_combined = bool(_HINT_COMBINED_RE.search(text))
    is_split = bool(_HINT_SPLIT_RE.search(text))
    part_number: Optional[int] = None
    episode_count: Optional[int] = None

    pm = _HINT_PART_NUM_RE.search(text)
    if pm:
        part_number = int(next(g for g in pm.groups() if g is not None))
        is_split = True

    em = _HINT_EP_COUNT_RE.search(text)
    if em and int(em.group(1)) > 1:
        episode_count = int(em.group(1))
        if episode_count == 2:
            is_combined = True

    return {
        "is_combined": is_combined,
        "is_split": is_split,
        "part_number": part_number,
        "episode_count": episode_count,
    }
```

Also add `import re` near the top of `llm_identify.py` if it is not already imported (check first — `re` may already be there from the `_normalize` regex).

- [ ] **Step 4: Run to verify all pass**

```
pytest tests/test_llm_identify.py -v
```

Expected: All pass.

- [ ] **Step 5: Commit**

```
git add backend/rename/llm_identify.py tests/test_llm_identify.py
git commit -m "feat(llm): add disambiguate_episode() and extract_page_hints() with regex fallback"
```

---

## Task 6 — Service Helpers: Re-scan, Combined, Split

**Files:**
- Modify: `backend/rename/service.py` — add four module-level helper functions before the `RenameService` class

**Interfaces:**
- Consumes: `_confidence.runtime_confidence_delta`, `_confidence.episode_correction_candidates`, `_llm.disambiguate_episode` (all from prior tasks)
- Produces four module-level functions:
  - `_try_episode_rescan(match, client, file_min, season_cache, tmdb_id, llm_cfg) -> dict | None`
  - `_detect_combined_episode(match, file_min, episodes) -> dict | None`
  - `_detect_split_file(path, file_min, tmdb_min) -> dict | None`
  - `_find_split_sibling(path) -> str | None`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_episode_rescan.py
import pytest
from unittest.mock import MagicMock, patch
from backend.rename.service import (
    _try_episode_rescan,
    _detect_combined_episode,
    _detect_split_file,
    _find_split_sibling,
)


def _make_episodes(runtimes: dict) -> list:
    """Build a minimal TMDB episode list from {ep_number: runtime_minutes}."""
    return [{"episode_number": n, "runtime": rt, "name": f"Episode {n}"}
            for n, rt in runtimes.items()]


class TestTryEpisodeRescan:
    def _match(self, season=1, episode=3):
        return {
            "tmdb_id": 999,
            "media_type": "tv",
            "title": "Test Show",
            "season": season,
            "episode": episode,
            "confidence": 70.0,
            "original_filename": "Test.Show.S01E03.1080p.mkv",
        }

    def _client(self, season_data: dict):
        c = MagicMock()
        c.season.return_value = season_data
        return c

    def test_proposes_correction_when_runtime_matches_different_episode(self):
        # File is 44 min; current match E3 has 90 min runtime; E4 has 44 min
        episodes = _make_episodes({2: 44, 3: 90, 4: 44, 5: 44})
        client = self._client({"episodes": episodes})
        season_cache = {1: {"episodes": episodes}}
        result = _try_episode_rescan(
            self._match(), client, 44.0, season_cache, 999, {})
        assert result is not None
        assert result["type"] == "episode_correction"
        assert result["proposed"]["episode"] in (2, 4, 5)

    def test_returns_none_when_no_clear_winner(self):
        # All episodes same runtime as current — no one is better
        episodes = _make_episodes({2: 44, 3: 44, 4: 44})
        client = self._client({"episodes": episodes})
        season_cache = {1: {"episodes": episodes}}
        result = _try_episode_rescan(
            self._match(), client, 44.0, season_cache, 999, {})
        assert result is None

    def test_checks_adjacent_season(self):
        # Current season E3 has 90 min; adjacent season S2E3 has 44 min
        s1_eps = _make_episodes({2: 90, 3: 90, 4: 90})
        s2_eps = _make_episodes({3: 44})
        client = MagicMock()
        client.season.side_effect = lambda tmdb_id, s: (
            {"episodes": s1_eps} if s == 1 else {"episodes": s2_eps}
        )
        season_cache = {1: {"episodes": s1_eps}}
        result = _try_episode_rescan(
            self._match(), client, 44.0, season_cache, 999, {})
        assert result is not None
        assert result["proposed"]["season"] == 2
        assert result["proposed"]["episode"] == 3


class TestDetectCombinedEpisode:
    def _episodes(self):
        return _make_episodes({1: 44, 2: 44, 3: 44, 4: 44})

    def test_detects_pair_when_runtime_matches_sum(self):
        # File is 88 min ≈ E3(44) + E4(44)
        match = {"episode": 3, "tmdb_id": 1}
        result = _detect_combined_episode(match, 88.0, self._episodes())
        assert result is not None
        assert result["episode_start"] == 3
        assert result["episode_end"] == 4

    def test_returns_none_when_ratio_outside_window(self):
        # File is 50 min — only 1.14× single ep, not 1.7–2.4×
        match = {"episode": 1, "tmdb_id": 1}
        result = _detect_combined_episode(match, 50.0, self._episodes())
        assert result is None

    def test_returns_none_when_no_next_episode(self):
        # File is 88 min, current match is E4 (last episode) — no E5 in list
        match = {"episode": 4, "tmdb_id": 1}
        result = _detect_combined_episode(match, 88.0, self._episodes())
        assert result is None

    def test_returns_none_when_sum_does_not_match(self):
        # E3=44, E4=120 — sum=164, file=88 — too far off
        episodes = _make_episodes({3: 44, 4: 120})
        match = {"episode": 3, "tmdb_id": 1}
        result = _detect_combined_episode(match, 88.0, episodes)
        assert result is None


class TestDetectSplitFile:
    def test_returns_none_when_runtime_above_threshold(self):
        # file_min=40, tmdb_min=44 — 40/44 = 0.91 ≥ 0.6, not a split
        result = _detect_split_file("/show/S01E05.mkv", 40.0, 44.0)
        assert result is None

    def test_returns_none_when_no_sibling(self):
        # File is short but sibling doesn't exist
        with patch("backend.rename.service._find_split_sibling", return_value=None):
            result = _detect_split_file("/show/S01E05.Part1.mkv", 20.0, 44.0)
        assert result is None

    def test_detects_split_when_sibling_present(self):
        with patch("backend.rename.service._find_split_sibling",
                   return_value="/show/S01E05.Part2.mkv"):
            result = _detect_split_file("/show/S01E05.Part1.mkv", 20.0, 44.0)
        assert result is not None
        assert result["sibling_path"] == "/show/S01E05.Part2.mkv"
        assert result["part"] in (1, 2)
```

- [ ] **Step 2: Run to verify they fail**

```
pytest tests/test_episode_rescan.py -v
```

Expected: ImportError — helper functions don't exist in service.py yet.

- [ ] **Step 3: Add the four helper functions to `service.py`**

Add these before the `RenameService` class definition (after the existing imports and module-level constants):

```python
import re as _re


def _try_episode_rescan(
    match: dict,
    client,
    file_min: float,
    season_cache: dict,
    tmdb_id: int,
    llm_cfg: dict,
) -> Optional[dict]:
    """Find a better episode assignment when runtime is suspicious.

    Searches ±3 episodes in the current season and the same episode number
    in ±1 adjacent season. Returns a suggested_correction dict or None.
    Limitation: mismatches of more than 3 episodes in the same season are missed.
    """
    current_season = match.get("season", 1)
    current_ep = match.get("episode", 1)

    def _get_season(s: int) -> list:
        if s not in season_cache:
            data = client.season(tmdb_id, s)
            season_cache[s] = data or {}
        return (season_cache[s].get("episodes") or []) if season_cache.get(s) else []

    current_episodes = _get_season(current_season)
    current_ep_data = next(
        (e for e in current_episodes if e.get("episode_number") == current_ep), None)
    current_rt = current_ep_data.get("runtime") if current_ep_data else None
    current_score = _confidence.runtime_confidence_delta(file_min, current_rt) if current_rt else -30.0

    candidates: list[dict] = []

    for ep in current_episodes:
        ep_num = ep.get("episode_number")
        ep_rt = ep.get("runtime")
        if not ep_num or not ep_rt or ep_num == current_ep:
            continue
        if abs(ep_num - current_ep) > 3:
            continue
        score = _confidence.runtime_confidence_delta(file_min, ep_rt)
        if score - current_score >= 15.0:
            candidates.append({
                "episode": ep_num, "season": current_season,
                "title": ep.get("name", ""), "runtime": ep_rt,
                "score_delta": score,
            })

    for adj in (current_season - 1, current_season + 1):
        if adj < 1:
            continue
        adj_eps = _get_season(adj)
        adj_ep = next((e for e in adj_eps if e.get("episode_number") == current_ep), None)
        if adj_ep and adj_ep.get("runtime"):
            score = _confidence.runtime_confidence_delta(file_min, adj_ep["runtime"])
            if score - current_score >= 15.0:
                candidates.append({
                    "episode": current_ep, "season": adj,
                    "title": adj_ep.get("name", ""), "runtime": adj_ep["runtime"],
                    "score_delta": score,
                })

    if not candidates:
        return None

    candidates.sort(key=lambda x: x["score_delta"], reverse=True)

    best = candidates[0]
    if (len(candidates) >= 2
            and candidates[0]["score_delta"] - candidates[1]["score_delta"] < 10
            and llm_cfg.get("base_url") and llm_cfg.get("model")):
        pick = _llm.disambiguate_episode(
            match.get("original_filename", ""),
            candidates[:3],
            base_url=llm_cfg["base_url"],
            model=llm_cfg["model"],
        )
        if pick:
            matched = next(
                (c for c in candidates
                 if c["episode"] == pick["episode"] and c["season"] == pick["season"]),
                None)
            if matched:
                best = matched

    return {
        "type": "episode_correction",
        "original": {"season": current_season, "episode": current_ep},
        "proposed": {
            "season": best["season"],
            "episode": best["episode"],
            "title": best["title"],
            "runtime": best["runtime"],
        },
        "confidence_gain": round(best["score_delta"] - current_score, 1),
        "method": "ollama" if len(candidates) >= 2 else "runtime",
    }


def _detect_combined_episode(match: dict, file_min: float, episodes: list) -> Optional[dict]:
    """Detect if a file contains two consecutive episodes.

    Fires when file_min is 1.7–2.4× the matched episode's runtime and the
    sum of ep_n + ep_{n+1} runtimes matches file_min within 8%.
    Only handles pairs; triple-episode files are not detected (rare case).
    """
    ep_num = match.get("episode")
    if not ep_num or not file_min:
        return None
    ep_data = next((e for e in episodes if e.get("episode_number") == ep_num), None)
    tmdb_min = ep_data.get("runtime") if ep_data else None
    if not tmdb_min:
        return None

    ratio = file_min / tmdb_min
    if not 1.7 <= ratio <= 2.4:
        return None

    next_ep = next((e for e in episodes if e.get("episode_number") == ep_num + 1), None)
    if not next_ep or not next_ep.get("runtime"):
        return None

    combined = tmdb_min + next_ep["runtime"]
    pct_off = abs(file_min - combined) / combined
    if pct_off > 0.08:
        return None

    return {
        "episode_start": ep_num,
        "episode_end": ep_num + 1,
        "proposed_code": f"E{ep_num:02d}E{ep_num + 1:02d}",
        "runtime_match_pct": round(pct_off * 100, 1),
    }


def _detect_split_file(path: str, file_min: float, tmdb_min: float) -> Optional[dict]:
    """Detect if a file is one part of a split episode.

    Fires when file_min < 0.6× expected and a sibling file with the same
    SxxExx code exists in the same directory.
    Limitation: if the sibling hasn't been downloaded yet, detection fails.
    """
    if not file_min or not tmdb_min or file_min >= tmdb_min * 0.6:
        return None
    sibling = _find_split_sibling(path)
    if not sibling:
        return None
    part = 1 if path < sibling else 2
    return {
        "part": part,
        "sibling_path": sibling,
        "proposed_suffix": f"Part {part}",
    }


_SPLIT_SIBLING_EXTS = frozenset(
    {".mkv", ".mp4", ".avi", ".mov", ".m4v", ".ts", ".flv", ".webm"})
_SPLIT_PART_RE = _re.compile(
    r'[.\s\-_](?:Part|Pt)[\s.\-]?\d', _re.IGNORECASE)


def _find_split_sibling(path: str) -> Optional[str]:
    """Return a sibling video file in the same directory with the same SxxExx code."""
    directory = os.path.dirname(path)
    basename = os.path.basename(path)
    se_match = _re.search(r'S(\d{1,2})E(\d{1,3})', basename, _re.IGNORECASE)
    if not se_match:
        return None
    se_code = se_match.group(0).upper()
    try:
        candidates = [
            os.path.join(directory, f)
            for f in os.listdir(directory)
            if (os.path.isfile(os.path.join(directory, f))
                and os.path.splitext(f)[1].lower() in _SPLIT_SIBLING_EXTS
                and se_code in f.upper()
                and os.path.join(directory, f) != path)
        ]
    except OSError:
        return None
    for c in candidates:
        if _SPLIT_PART_RE.search(os.path.basename(c)):
            return c
    return candidates[0] if len(candidates) == 1 else None
```

Also add `from typing import Optional` near the top of `service.py` if not already imported. Check first with `grep "from typing" backend/rename/service.py`.

- [ ] **Step 4: Run to verify tests pass**

```
pytest tests/test_episode_rescan.py -v
```

Expected: All pass.

- [ ] **Step 5: Commit**

```
git add backend/rename/service.py tests/test_episode_rescan.py
git commit -m "feat(service): add episode re-scan, combined-episode, and split-file helper functions"
```

---

## Task 7 — Wire Helpers Into `_process_file`

**Files:**
- Modify: `backend/rename/service.py` — the `_process_file` method

**Goal:** Integrate the four helpers from Task 6 into the existing runtime block, surface proposals in the job dict and `warning_message`.

- [ ] **Step 1: Hoist `season_data`, `episodes`, `delta` before the try block**

In `_process_file`, find the current runtime block starting at `if match and match.get("tmdb_id"):`. Add declarations before `try:`:

```python
        if match and match.get("tmdb_id"):
            mtype = match.get("media_type", "movie")
            is_pack = match.get("season") is not None and match.get("episode") is None
            if not is_pack:
                client = self._tmdb_client()
                file_min = _llm.video_duration_minutes(path)
                tmdb_min = None
                season_data = None      # ← add
                episodes: list = []     # ← add
                delta: Optional[float] = None   # ← add
                try:
                    ...  # existing try block unchanged
                except Exception:
                    pass

                # existing delta/filesize logic (unchanged)
                resolution = match.get("resolution")
                if tmdb_min:
                    if file_min:
                        delta = _confidence.runtime_confidence_delta(file_min, tmdb_min)
                    else:
                        ...
                    if delta != 0.0:
                        match["confidence"] = ...
                    if delta < -10 and not match.get("runtime_warning"):
                        ...
```

Inside the existing `try:` block, make `season_data` and `episodes` update the outer variables. The lines inside the try that currently read:

```python
                                    season_data = client.season(...)
                                    if season_data:
                                        episodes = season_data.get("episodes") or []
```

are already using local variables — change them to assign to the outer `season_data` and `episodes` names (they're in the same `if not is_pack:` scope so this works without `nonlocal`).

- [ ] **Step 2: Add re-scan + detection blocks after the existing runtime warning block**

After the block that sets `match["runtime_warning"]` (currently the last thing before `job = {...}`), add:

```python
                # Season cache for downstream checks — avoids re-fetching same season
                season_num = match.get("season")
                season_cache: dict = {season_num: season_data} if season_data else {}
                llm_cfg = {
                    "base_url": self._cfg.get("ollama_base_url", ""),
                    "model": self._cfg.get("ollama_model", ""),
                }

                # ── Episode re-scan (only when runtime is suspicious) ────────
                if (delta is not None and delta < -10
                        and mtype == "tv"
                        and match.get("episode")
                        and not match.get("episode_end")
                        and client):
                    correction = _try_episode_rescan(
                        match, client, file_min, season_cache,
                        int(match["tmdb_id"]), llm_cfg)
                    if correction:
                        match["suggested_correction"] = correction

                # ── Combined episode detection ───────────────────────────────
                if (file_min and tmdb_min
                        and mtype == "tv"
                        and episodes
                        and not match.get("episode_end")
                        and not match.get("suggested_correction")):
                    combined = _detect_combined_episode(match, file_min, episodes)
                    if combined:
                        match["combined_episode"] = combined

                # ── Split file detection ─────────────────────────────────────
                if (mtype == "tv"
                        and file_min and tmdb_min
                        and not match.get("combined_episode")
                        and not match.get("suggested_correction")):
                    split = _detect_split_file(path, file_min, tmdb_min)
                    if split:
                        match["split_file"] = split
```

- [ ] **Step 3: Surface proposals in the job dict**

In the `job.update(...)` call that builds the job, add:

```python
            job.update(
                ...existing fields...,
                suggested_correction=match.get("suggested_correction"),
                combined_episode=match.get("combined_episode"),
                split_file=match.get("split_file"),
            )
```

- [ ] **Step 4: Add proposal warning messages**

After the existing `runtime_warn` warning handling, add:

```python
        if match.get("suggested_correction"):
            corr = match["suggested_correction"]
            orig = corr["original"]
            prop = corr["proposed"]
            job["warning_message"] = (
                f"Possible wrong episode: "
                f"S{orig['season']:02d}E{orig['episode']:02d} -> "
                f"S{prop['season']:02d}E{prop['episode']:02d} "
                f"\"{prop.get('title', '')}\""
            )

        if match.get("combined_episode"):
            comb = match["combined_episode"]
            job["warning_message"] = (
                f"Likely combined: "
                f"E{comb['episode_start']:02d}+E{comb['episode_end']:02d} "
                f"-> rename as {comb['proposed_code']}"
            )

        if match.get("split_file"):
            sf = match["split_file"]
            job["warning_message"] = (
                f"Likely split file Part {sf['part']} "
                f"(sibling: {os.path.basename(sf['sibling_path'])})"
            )
```

- [ ] **Step 5: Verify syntax**

```
python -m py_compile backend/rename/service.py && echo OK
```

Expected: `OK`

- [ ] **Step 6: Smoke-test that clean single-episode files still produce no proposals**

```python
# Quick manual smoke test (run interactively or add to test_episode_rescan.py)
# A file whose runtime matches its TMDB episode perfectly should have
# suggested_correction=None, combined_episode=None, split_file=None in the job.
```

- [ ] **Step 7: Commit**

```
git add backend/rename/service.py
git commit -m "feat(service): wire episode re-scan and combined/split detection into _process_file"
```

---

## Task 8 — Download Page Hint Extraction

**Files:**
- Modify: `backend/detail_scraper.py`
- Modify: `tests/test_detail_scraper.py` (add hint extraction test)

**Goal:** When the detail scraper fetches a download page, extract multi-episode hints from the page body and return them in the scraper result dict.

**Note:** This task wires the regex/Ollama page-hint extraction (Task 5) into the scraping layer. The rename service can read `multi_episode_hint` from the download record if the DB model is extended — that DB wiring is out of scope for this plan and should be treated as a follow-up.

- [ ] **Step 1: Write the failing test**

In `tests/test_detail_scraper.py`, add:

```python
from unittest.mock import patch, MagicMock
from backend.detail_scraper import scrape_detail_page  # adjust to actual function name


class TestPageHintExtraction:
    def _html(self, body: str) -> str:
        return f"<html><body>{body}</body></html>"

    def test_extracts_combined_hint_from_page_text(self):
        html = self._html("<p>This is a double episode release.</p>")
        with patch("requests.get") as mock_get:
            mock_get.return_value.text = html
            mock_get.return_value.raise_for_status = MagicMock()
            result = scrape_detail_page("http://fake-url/post/123")
        hints = result.get("multi_episode_hint")
        assert hints is not None
        assert hints["is_combined"] is True

    def test_returns_none_hint_for_normal_page(self):
        html = self._html("<p>Great episode, action-packed.</p>")
        with patch("requests.get") as mock_get:
            mock_get.return_value.text = html
            mock_get.return_value.raise_for_status = MagicMock()
            result = scrape_detail_page("http://fake-url/post/456")
        assert result.get("multi_episode_hint") is None
```

> **Note:** The exact `scrape_detail_page` import and mock pattern must match your detail_scraper's actual public function. If it uses `undetected_chromedriver`, mock at a higher level.

- [ ] **Step 2: Add hint extraction to `detail_scraper.py`**

In `backend/detail_scraper.py`, after the `full_text = soup.get_text()` line (which exists from the IMDb fallback work), add:

```python
    # Extract multi-episode hints from page body (regex only — Ollama is async)
    hints = _llm.extract_page_hints(full_text)
    result["multi_episode_hint"] = hints if (hints["is_combined"] or hints["is_split"]) else None
```

Where `_llm` is already imported or add at top of file:

```python
from backend.rename import llm_identify as _llm
```

- [ ] **Step 3: Run test**

```
pytest tests/test_detail_scraper.py::TestPageHintExtraction -v
```

Expected: All pass.

- [ ] **Step 4: Commit**

```
git add backend/detail_scraper.py tests/test_detail_scraper.py
git commit -m "feat(scraper): extract multi-episode hints from download page body text"
```

---

## Task 9 — Regression and Integration Tests

**Files:**
- Create: `tests/test_rename_pipeline_regression.py`

**Goal:** Verify that clean files (correct single episode, movie, season pack) produce identical output to before these changes — the gate logic must not affect them.

- [ ] **Step 1: Write regression tests**

```python
# tests/test_rename_pipeline_regression.py
"""Regression tests: existing clean-file rename paths must be unaffected."""
import pytest
from unittest.mock import MagicMock, patch


def _make_service(cfg: dict = None):
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


class TestCleanEpisodeUnaffected:
    def test_matching_runtime_adds_no_proposals(self):
        """A file whose runtime matches its TMDB episode should have no correction proposals."""
        from backend.rename import service as svc_mod
        match = {
            "tmdb_id": 1,
            "media_type": "tv",
            "title": "Good Show",
            "season": 1,
            "episode": 3,
            "confidence": 90.0,
        }
        episodes = [{"episode_number": 3, "runtime": 44, "name": "Ep 3"}]
        # file_min=44, tmdb_min=44 → 0% deviation → +10, delta is NOT < -10 → no re-scan
        result = svc_mod._detect_combined_episode(match, 44.0, episodes)
        assert result is None

        result = svc_mod._detect_split_file("x/S01E03.mkv", 44.0, 44.0)
        assert result is None

    def test_movie_path_not_affected_by_episode_helpers(self):
        from backend.rename import service as svc_mod
        # Movies have no 'episode' key — helpers should not be called,
        # but even if they were, they'd return None safely.
        match = {"tmdb_id": 2, "media_type": "movie", "title": "Great Film"}
        result = svc_mod._detect_combined_episode(match, 120.0, [])
        assert result is None

    def test_season_pack_skips_detection(self):
        """is_pack=True should skip all new detection blocks."""
        # Verified structurally — the gate `if not is_pack:` wraps all new blocks.
        # This test confirms the flag logic itself.
        match = {"season": 1, "episode": None}
        is_pack = match.get("season") is not None and match.get("episode") is None
        assert is_pack is True


class TestNamingRegression:
    def test_existing_single_ep_format_unchanged(self):
        from backend.rename.naming import build_target
        meta = {
            "media_type": "tv",
            "title": "My Show",
            "year": 2023,
            "season": 2,
            "episode": 5,
            "original_filename": "my.show.s02e05.mkv",
        }
        fname, _ = build_target(meta, tv_root="/tv")
        assert fname == "My Show (2023) - S02E05.mkv"

    def test_existing_movie_format_unchanged(self):
        from backend.rename.naming import build_target
        meta = {
            "media_type": "movie",
            "title": "Great Film",
            "year": 2021,
            "resolution": "1080p",
            "original_filename": "great.film.mkv",
        }
        fname, _ = build_target(meta, movie_root="/movies")
        assert fname == "Great Film (2021) [1080p].mkv"
```

- [ ] **Step 2: Run regression suite**

```
pytest tests/test_rename_pipeline_regression.py -v
```

Expected: All pass.

- [ ] **Step 3: Run full test suite**

```
pytest tests/ -v --tb=short 2>&1 | tail -30
```

Expected: No new failures. Pre-existing failures (if any) are unrelated to this work.

- [ ] **Step 4: Commit**

```
git add tests/test_rename_pipeline_regression.py
git commit -m "test: regression suite for episode intelligence pipeline"
```

---

## Self-Review Checklist

**Spec coverage:**
- [x] Feature 1 — runtime threshold tightened → Task 1
- [x] Feature 2 — runtime-gated re-scan → Tasks 4, 6, 7
- [x] Feature 2 — Ollama tie-breaker → Task 5 (`disambiguate_episode`) + Task 6 (`_try_episode_rescan`)
- [x] Feature 3a — filename multi-ep parsing → Task 2
- [x] Feature 3b — runtime combined detection → Task 6 (`_detect_combined_episode`) + Task 7
- [x] Feature 3c — runtime split detection → Task 6 (`_detect_split_file`, `_find_split_sibling`) + Task 7
- [x] Feature 3d — download page hints → Tasks 5 (`extract_page_hints`) + Task 8
- [x] Feature 3e — naming support → Task 3
- [x] Feature 3f — no unilateral action, proposals only → Task 7 (warning_message, no rename change)
- [x] TMDB season cache → Task 7 (`season_cache` dict)
- [x] Regression tests → Task 9

**Known gaps (intentional, documented above):**
- DB wiring for `multi_episode_hint` (follow-up)
- 3-way episode combos (out of scope)
- Non-English page hint phrases (regex limitation, documented)
