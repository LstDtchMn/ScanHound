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
