"""Plex-convention naming + template rendering for renamed media.

Ported/adapted from Nomen's ``template_engine.py`` (the ``{{token}}`` /
``[section]`` renderer, including its path-separator sanitization) and
``file_manager_path.py`` (Plex/Jellyfin folder + filename conventions), made
standalone — no app/mixin coupling. Reuses ScanHound's ``sanitize_filename``.
"""
from __future__ import annotations

import os
import re
from typing import Any, Optional

from backend.filename_utils import sanitize_filename

VIDEO_EXTENSIONS = frozenset({
    ".mkv", ".mp4", ".avi", ".wmv", ".mov", ".m4v",
    ".ts", ".flv", ".webm", ".mpg", ".mpeg", ".m2ts",
})

_TOKEN_RE = re.compile(r"\{\{(\w+)(?:\|([^}]*))?\}\}")
_SECTION_RE = re.compile(r"\[([^\[\]]*)\]")


def render_template(template: str, tokens: dict[str, Any]) -> str:
    """Render a ``{{token}}`` / ``[conditional section]`` template.

    Token values are stripped of path separators to prevent directory
    traversal via a malicious/badly-parsed title. (Ported from Nomen.)
    """
    str_tokens = {
        k: (str(v).replace("/", "-").replace("\\", "-") if v is not None else "")
        for k, v in tokens.items()
    }

    def _substitute(text: str) -> str:
        def _replace(m: "re.Match") -> str:
            val = str_tokens.get(m.group(1), "")
            return val if val else (m.group(2) or "")
        return _TOKEN_RE.sub(_replace, text)

    def _process_section(m: "re.Match") -> str:
        inner = m.group(1)
        refs = _TOKEN_RE.findall(inner)
        if not refs:  # literal brackets with no tokens — keep verbatim
            return inner
        if all(not (str_tokens.get(name, "") or default) for name, default in refs):
            return ""
        return _substitute(inner)

    result = _SECTION_RE.sub(_process_section, template)
    result = _substitute(result)
    result = result.replace("()", "").replace("[]", "")
    result = re.sub(r"\s{2,}", " ", result)
    return result.strip(" -_")


def video_extension(filename: str) -> str:
    """Return a recognised video extension (with dot), defaulting to .mkv."""
    _, ext = os.path.splitext(filename or "")
    ext = ext.lower()
    return ext if ext in VIDEO_EXTENSIONS else ".mkv"


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


def _destination(meta, *, movie_root, tv_root, title, year) -> str:
    if meta.get("media_type") == "tv":
        show = f"{title} ({year})" if year else title
        season = int(meta.get("season") or 1)
        return os.path.join(tv_root, show, f"Season {season:02d}")
    folder = f"{title} ({year})" if year else title
    return os.path.join(movie_root, folder)


def build_target(meta: dict, *, movie_root: str = "", tv_root: str = "",
                 template: Optional[str] = None) -> tuple[str, str]:
    """Return ``(new_filename, destination_dir)`` for a media item, Plex-style.

    ``meta`` keys: media_type ('movie'|'tv'), title, year, season, episode,
    episode_title, resolution, original_filename. A non-empty ``template`` is
    rendered with the token dict; otherwise the Plex default convention applies:
      movie → ``Title (Year)/Title (Year) [res].ext``
      tv    → ``Title (Year)/Season NN/Title (Year) - SNNENN - Episode.ext``
    """
    media_type = meta.get("media_type", "movie")
    title = sanitize_filename(meta.get("title") or "Unknown")
    year = meta.get("year") or 0
    ext = video_extension(meta.get("original_filename", ""))
    resolution = meta.get("resolution") or ""

    if template:
        base = sanitize_filename(render_template(template, _tokens(meta)))
        fname = (base or title) + ext
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
    else:
        folder = f"{title} ({year})" if year else title
        fname = (f"{folder} [{resolution}]" if resolution else folder) + ext

    dest = _destination(meta, movie_root=movie_root, tv_root=tv_root, title=title, year=year)
    return fname, dest
