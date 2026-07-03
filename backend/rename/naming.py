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
# Implicit optional segment: a *literal* "( ... )" wrapper in the template
# text (not the explicit "[...]" section syntax) whose entire contents are
# token placeholders (plus whitespace). Common author idiom, e.g.
# "{{title}} ({{year}})". Only these template-authored parens are eligible
# for empty-collapse — parens that are part of a token's *substituted value*
# (e.g. a title literally containing "()") never match this, because by the
# time this runs the "{{...}}" placeholders are still intact.
_PAREN_SECTION_RE = re.compile(r"\(([^()]*)\)")
# A one-shot sentinel that stands in for "a token substitution that resolved
# to empty". It's inserted instead of "" so that empty-collapse cleanup can
# target exactly the separators/whitespace that were written by the template
# author around that placeholder, without ever touching literal characters
# that came from a non-empty substituted value (e.g. a title ending in "-").
# Chosen to be something that can never appear in real input: it's stripped
# from any incoming token value up front.
_EMPTY_MARK = "\0"


def render_template(template: str, tokens: dict[str, Any]) -> str:
    """Render a ``{{token}}`` / ``[conditional section]`` template.

    Token values are stripped of path separators to prevent directory
    traversal via a malicious/badly-parsed title. (Ported from Nomen.)

    Optional segments can be marked either with explicit ``[...]`` bracket
    syntax, or with a bare ``(...)`` wrapper around token placeholders (e.g.
    ``{{title}} ({{year}})``); both collapse to nothing when every token
    inside resolves empty. Bare (non-bracket, non-paren) empty-token
    placeholders also clean up their own adjacent template-authored
    separator (e.g. ``{{title}} - {{episode_title}}`` with an empty
    ``episode_title``). This collapse only ever targets whitespace/separator
    characters that sit in the *template source* next to a placeholder that
    resolved empty — literal parentheses, brackets, or trailing hyphens that
    are part of a token's substituted value (e.g. a title like
    ``"Rush () 2013"`` or ``"Under-"``) are never touched.
    """
    str_tokens = {
        k: (str(v).replace("/", "-").replace("\\", "-").replace(_EMPTY_MARK, "")
            if v is not None else "")
        for k, v in tokens.items()
    }

    def _substitute(text: str, *, mark_empty: bool) -> str:
        def _replace(m: "re.Match") -> str:
            val = str_tokens.get(m.group(1), "")
            if val:
                return val
            default = m.group(2) or ""
            if default:
                return default
            return _EMPTY_MARK if mark_empty else ""
        return _TOKEN_RE.sub(_replace, text)

    def _all_refs_empty(inner: str) -> Optional[bool]:
        refs = _TOKEN_RE.findall(inner)
        if not refs:
            return None  # no tokens in here — not an optional segment
        return all(not (str_tokens.get(name, "") or default) for name, default in refs)

    def _process_bracket_section(m: "re.Match") -> str:
        inner = m.group(1)
        empty = _all_refs_empty(inner)
        if empty is None:  # literal brackets with no tokens — keep verbatim
            return inner
        return "" if empty else _substitute(inner, mark_empty=False)

    def _process_paren_section(m: "re.Match") -> str:
        inner = m.group(1)
        empty = _all_refs_empty(inner)
        if empty is None:  # literal parens with no tokens — keep verbatim
            return m.group(0)
        return "" if empty else f"({_substitute(inner, mark_empty=False)})"

    # Explicit "[...]" sections first (may themselves contain "(...)" text).
    result = _SECTION_RE.sub(_process_bracket_section, template)
    # Then bare "(...)" wrappers that are purely token placeholders — this
    # only inspects text that still has un-substituted "{{token}}" markup,
    # so it can never match parens introduced by a substituted value.
    result = _PAREN_SECTION_RE.sub(_process_paren_section, result)
    # Remaining bare placeholders: substitute, but mark empty resolutions
    # with a sentinel so the following cleanup can safely trim only the
    # template-authored separators around them.
    result = _substitute(result, mark_empty=True)
    # Strip a separator ("-", "_", or extra whitespace) that sits directly
    # against an empty-marker on either side — this is exactly the leftover
    # connector a template author wrote around a placeholder that turned out
    # empty (e.g. " - {{episode_title}}" -> " - \0" -> "").
    result = re.sub(r"[ \t]*[-_][ \t]*" + _EMPTY_MARK, "", result)
    result = re.sub(_EMPTY_MARK + r"[ \t]*[-_][ \t]*", "", result)
    # Any leftover markers (no adjacent separator, e.g. two placeholders
    # butted together) simply vanish.
    result = result.replace(_EMPTY_MARK, "")
    result = re.sub(r"\s{2,}", " ", result)
    return result.strip()


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
        "part": str(meta.get("part") or ""),
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
        # Disambiguate split parts even under a custom template: append the part
        # suffix unless the template already references {part}, so two parts
        # never render to the same colliding filename.
        part = meta.get("part")
        if part and "part" not in template.lower():
            base = f"{base} - Part {part}"
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
