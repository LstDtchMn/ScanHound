"""TV episode-boundary detection helpers.

Pure(ish) logic extracted from ``rename/service.py`` (C5 decomposition):
runtime-based episode rescan/disambiguation and combined/split-file
detection. These take their collaborators (a TMDB ``client`` callable, an
LLM disambiguation config) as plain parameters rather than via ``self`` or a
live DB handle, so they're free of ``RenameService``/DB coupling and
unit-test cleanly with mocks — matching how they were already tested before
this move.
"""
from __future__ import annotations

import os
import re as _re
from typing import Optional

from backend.rename import confidence as _confidence
from backend.rename import llm_identify as _llm


def _try_episode_rescan(
    match: dict,
    client,
    file_min: float,
    season_cache: dict,
    tmdb_id: int,
    llm_cfg: dict,
) -> Optional[dict]:
    # Find a better episode assignment when runtime is suspicious.
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
    current_score = (
        _confidence.runtime_confidence_delta(file_min, current_rt)
        if current_rt else -30.0
    )

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
    _used_ollama = False
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
                _used_ollama = True

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
        "method": "ollama" if _used_ollama else "runtime",
    }


def _detect_combined_episode(match: dict, file_min: float, episodes: list) -> Optional[dict]:
    # Detect if a file contains two consecutive episodes.
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
    # Detect if a file is one part of a split episode.
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
    # Return a sibling video file in the same directory with the same SxxExx code.
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
