"""Optional Ollama-assisted media identification.

A thin, fail-safe adapter: given a messy release filename, ask a local Ollama
model to extract structured fields. Used ONLY as a fallback for low-confidence
deterministic matches — the result is always re-validated against TMDB and the
confidence gate, so the model never supplies IDs or bypasses review. Any error,
timeout, or non-JSON response yields ``None`` and the caller falls back cleanly.

Uses ``requests`` (already a ScanHound dependency); no extra packages.
"""
from __future__ import annotations

import json
import logging
from typing import Any, List, Optional

import requests

logger = logging.getLogger(__name__)

_TIMEOUT = 20.0
_SYSTEM = (
    "You extract structured metadata from a single media release filename. "
    "Respond ONLY with a JSON object with keys: "
    "title (string), year (integer or null), type ('movie' or 'tv'), "
    "season (integer or null), episode (integer or null). "
    "Do not invent IDs. Use null when a field is unknown. "
    "When TMDB candidates are provided, pick the one that best matches the "
    "filename and return its exact title and year."
)


def identify(filename: str, *, base_url: str, model: str,
             timeout: float = _TIMEOUT,
             parsed_year: Optional[int] = None,
             candidates: Optional[List[dict]] = None) -> Optional[dict[str, Any]]:
    """Ask Ollama to parse a release filename into structured fields.

    ``parsed_year`` anchors disambiguation when the filename year is known.
    ``candidates`` is a list of ``{title, year, confidence}`` dicts from a
    prior TMDB search — giving the model concrete options cuts hallucination
    on ambiguous titles (remakes, foreign films, generic names).

    Returns ``{title, year, media_type, season, episode}`` or ``None`` on any
    failure (the caller then keeps its deterministic result).
    """
    if not filename or not base_url or not model:
        return None

    user_content = filename
    if parsed_year:
        user_content += f"\nParsed year: {parsed_year}"
    if candidates:
        lines = [
            f"  {i + 1}. \"{c['title']}\" ({c.get('year') or '?'}) "
            f"— confidence {c.get('confidence', 0):.0f}"
            for i, c in enumerate(candidates[:5])
        ]
        user_content += "\nTMDB candidates:\n" + "\n".join(lines)

    payload = {
        "model": model,
        "format": "json",
        "stream": False,
        "options": {"temperature": 0},
        "messages": [
            {"role": "system", "content": _SYSTEM},
            {"role": "user", "content": user_content},
        ],
    }
    try:
        resp = requests.post(base_url.rstrip("/") + "/api/chat",
                             json=payload, timeout=timeout)
        resp.raise_for_status()
        content = resp.json().get("message", {}).get("content", "")
        return _normalize(json.loads(content))
    except Exception as e:
        logger.debug("Ollama identify failed: %s", e)
        return None


def _normalize(data: Any) -> Optional[dict[str, Any]]:
    if not isinstance(data, dict):
        return None
    title = data.get("title")
    title = title.strip() if isinstance(title, str) else ""
    if not title:
        return None

    def _int(v):
        try:
            return int(v) if v is not None else None
        except (TypeError, ValueError):
            return None

    mtype = data.get("type")
    return {
        "title": title,
        "year": _int(data.get("year")),
        "media_type": mtype if mtype in ("movie", "tv") else None,
        "season": _int(data.get("season")),
        "episode": _int(data.get("episode")),
    }


def video_duration_minutes(video_path: str, timeout: int = 30) -> float | None:
    """Return the duration of a video file in minutes via ffprobe.

    Returns a rounded integer number of minutes, or ``None`` if ffprobe is
    unavailable or the file cannot be read.  Entirely fail-safe.
    """
    import shutil
    import subprocess

    if not video_path:
        return None
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return None
    try:
        r = subprocess.run(
            [ffprobe, "-v", "quiet", "-print_format", "json",
             "-show_format", video_path],
            capture_output=True, text=True, timeout=timeout,
        )
        if r.returncode != 0:
            return None
        duration = float(json.loads(r.stdout)["format"]["duration"])
        return round(duration / 60) if duration > 0 else None
    except Exception:
        return None


# Keep a seconds-based alias used internally by identify_from_frames.
def _video_duration_seconds(video_path: str) -> float | None:
    mins = video_duration_minutes(video_path)
    return mins * 60 if mins is not None else None


_VISION_TIMEOUT = 45.0
_VISION_SYSTEM = (
    "You identify movies and TV shows from video frames. "
    "Look for title cards, opening or closing credits, watermarks, and any "
    "visible on-screen text. Respond ONLY with a JSON object with keys: "
    "title (string or null), year (integer or null), type ('movie' or 'tv'). "
    "If you cannot identify the title from this frame, set title to null."
)
# Phase 1: high-value timestamps likely to contain title cards / opening credits.
# End-credits offset (~95% of duration) is appended dynamically when duration is known.
_FRAME_PRIORITY = [30, 60, 120]
# Phase 2: if priority frames all miss, sample this many evenly-spaced intervals
# across the video (or fall back to fixed times if duration is unknown).
_FRAME_GRID_COUNT = 8
_FRAME_GRID_FIXED = [180, 300, 600, 900, 1200, 1500, 1800, 2400]
_FRAME_MAX = 12  # hard cap across both phases


def identify_from_frames(
    video_path: str, *, base_url: str, model: str,
    timeout: float = _VISION_TIMEOUT,
) -> Optional[dict[str, Any]]:
    """Extract frames from a video file and ask a vision-capable Ollama model
    to identify the title.

    Two-phase strategy:
      Phase 1 — priority timestamps (30s, 60s, 120s + end-credits at ~95%)
                 that are most likely to show a title card or opening credits.
      Phase 2 — if no hit, expand to an evenly-spaced grid across the full
                 video (or fixed fallback times when duration is unknown).

    Returns on the first positive identification, or ``None`` after exhausting
    up to ``_FRAME_MAX`` frames.  Entirely fail-safe — any exception at any
    step returns ``None``.
    """
    import base64
    import os as _os
    import subprocess
    import tempfile

    if not video_path or not base_url or not model:
        return None

    def _run(args: list[str], tout: int = 15) -> Optional[bytes]:
        try:
            r = subprocess.run(args, capture_output=True, timeout=tout)
            return r.stdout if r.returncode == 0 else None
        except Exception:
            return None

    def _duration() -> Optional[float]:
        return _video_duration_seconds(video_path)

    def _extract_frame(offset_sec: int) -> Optional[str]:
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
            tmp_path = tmp.name
        _run([
            "ffmpeg", "-y", "-ss", str(offset_sec), "-i", video_path,
            "-vframes", "1", "-q:v", "3", "-f", "image2", tmp_path,
        ])
        try:
            with open(tmp_path, "rb") as f:
                raw = f.read()
            _os.unlink(tmp_path)
            return base64.b64encode(raw).decode() if raw else None
        except Exception:
            return None

    def _ask_vision(b64_image: str) -> Optional[dict[str, Any]]:
        payload = {
            "model": model,
            "stream": False,
            "options": {"temperature": 0},
            "messages": [{
                "role": "user",
                "content": (
                    _VISION_SYSTEM
                    + "\n\nIdentify the movie or TV show in this frame."
                ),
                "images": [b64_image],
            }],
        }
        try:
            resp = requests.post(base_url.rstrip("/") + "/api/chat",
                                 json=payload, timeout=timeout)
            resp.raise_for_status()
            content = resp.json().get("message", {}).get("content", "")
            content = content.strip().lstrip("```json").lstrip("```").rstrip("```").strip()
            result = _normalize(json.loads(content))
            return result if result and result.get("title") else None
        except Exception as e:
            logger.debug("Ollama vision frame failed: %s", e)
            return None

    def _try(offset: int, tried: set) -> Optional[dict[str, Any]]:
        if offset in tried:
            return None
        tried.add(offset)
        b64 = _extract_frame(offset)
        if not b64:
            return None
        result = _ask_vision(b64)
        if result and result.get("title"):
            logger.debug("Vision identified at %ds: %s", offset, result.get("title"))
            return result
        return None

    dur = _duration()
    tried: set[int] = set()

    # ── Phase 1: priority timestamps ──────────────────────────────────────
    phase1 = list(_FRAME_PRIORITY)
    if dur and dur > 300:
        phase1.append(int(dur * 0.95))  # end credits

    for offset in phase1:
        if len(tried) >= _FRAME_MAX:
            break
        if dur and offset >= dur:
            continue
        result = _try(offset, tried)
        if result:
            return result

    # ── Phase 2: grid scan across the video ──────────────────────────────
    if dur:
        # Evenly-spaced samples at 10%–90% of duration, avoiding already tried.
        grid = [int(dur * p / (_FRAME_GRID_COUNT + 1))
                for p in range(1, _FRAME_GRID_COUNT + 1)]
    else:
        grid = list(_FRAME_GRID_FIXED)

    for offset in grid:
        if len(tried) >= _FRAME_MAX:
            break
        if dur and offset >= dur:
            continue
        result = _try(offset, tried)
        if result:
            return result

    logger.debug("Vision: no identification after %d frames for %s",
                 len(tried), video_path)
    return None


def test_connection(base_url: str, timeout: float = 5.0) -> dict:
    """Probe Ollama's ``/api/tags``. Returns ``{ok, models?, error?}``."""
    if not base_url:
        return {"ok": False, "error": "No base URL configured"}
    try:
        resp = requests.get(base_url.rstrip("/") + "/api/tags", timeout=timeout)
        resp.raise_for_status()
        return {"ok": True, "models": [m.get("name") for m in resp.json().get("models", [])]}
    except Exception as e:
        return {"ok": False, "error": str(e)}
