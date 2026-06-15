"""filename_utils — Pure filename parsing and sanitization utilities.

Pure filename parsing utilities for standalone use and easier testing.
These functions have no external dependencies beyond the standard library
and the module-level regex constants defined here.

Functions
---------
parse_filename(filename)
    Parse a torrent-style filename into structured metadata dict.

sanitize_filename(name)
    Strip filesystem-unsafe characters from a string.
"""

import os
import re
from typing import Optional

from backend.models import FilenameResult

# Common torrent/release tags to strip from parsed titles
TAGS_RE = re.compile(
    r'\b('
    r'REMUX|BluRay|Blu[\.\-]?Ray|BDRemux|BDRip|BRRip|'
    r'WEB[\.\-]?DL|WEB[\.\-]?Rip|WEBRip|WEBDL|AMZN|DSNP|HMAX|NF|ATVP|'
    r'HDTV|DVDRip|HDRip|'
    r'HEVC|x265|x264|AVC|H[\.\s]?264|H[\.\s]?265|'
    r'AAC|DTS[\.\-]?HD|DTS[\.\-]?X|DTS|Atmos|TrueHD|FLAC|AC3|EAC3|DD5[\.\s]?1|'
    r'DV|DoVi|Dolby[\.\s]?Vision|HDR10Plus|HDR10|HDR|SDR|'
    r'IMAX|REPACK|PROPER|EXTENDED|UNRATED|DIRECTORS[\.\s]?CUT|'
    r'MULTI|DL|DUAL|GERMAN|FRENCH|ITALIAN|SPANISH|'
    r'10bit|UHD'
    r')\b',
    re.IGNORECASE
)

# Characters not allowed in filesystem names
UNSAFE_CHARS_RE = re.compile(r'[<>"/\\|?*]')

# Quality/release markers that end an episode title in TV filenames
EPISODE_BOUNDARY_RE = re.compile(
    r'[.\s](?:2160p|1080p|720p|480p|4[Kk]|UHD|'
    r'BluRay|Blu[\.\-]Ray|BDRip|BDRemux|REMUX|'
    r'WEB[\.\-]?DL|WEBRip|WEBDL|HDTV|DVDRip|HDRip|'
    r'HEVC|[Xx]265|[Xx]264|H[\.\s]?264|H[\.\s]?265|'
    r'AMZN|DSNP|HMAX|NF|ATVP|IMAX)',
    re.IGNORECASE
)


def parse_filename(filename) -> FilenameResult:
    """Parse a torrent-style filename into structured metadata.

    Handles:
        - Standard movie filenames: ``Movie.Title.2024.1080p.BluRay.x265.mkv``
        - TV episodes:              ``Show.Name.S01E03.1080p.WEB-DL.mkv``
        - Alternate TV notation:    ``Show.Name.1x03.720p.mkv``

    Extraction order:
        1. Season/episode codes (SxxEyy or NxMM) — sets is_tv.
        2. Resolution (2160p/1080p/720p/4K/UHD) — truncates title at match.
        3. Year (4-digit 19xx/20xx surrounded by delimiters).
        4. Title — everything before the above tokens, with dots/underscores
           replaced by spaces and known release tags stripped.

    Args:
        filename: Raw filename string (with or without extension).

    Returns:
        dict with keys:
            title      (str)           — cleaned title string
            year       (int or None)   — release year
            season     (int or None)   — season number (TV only)
            episode    (int or None)   — episode number (TV only)
            resolution (str or None)   — e.g. "1080p", "2160p"
            is_tv      (bool)          — True when season/episode found
    """
    name = os.path.splitext(filename)[0]
    result = {
        "title": "", "year": None, "season": None,
        "episode": None, "resolution": None, "is_tv": False,
    }

    # 1. Season / episode
    se_match = re.search(r'[.\s\-_]S(\d{1,2})E(\d{1,3})', name, re.IGNORECASE)
    if se_match:
        result["season"] = int(se_match.group(1))
        result["episode"] = int(se_match.group(2))
        result["is_tv"] = True
        title_part = name[:se_match.start()]
        # Extract candidate episode title: text between SxxExx and quality marker
        after_ep = name[se_match.end():]
        ep_boundary = EPISODE_BOUNDARY_RE.search(after_ep)
        ep_raw = after_ep[:ep_boundary.start()] if ep_boundary else after_ep
        ep_clean = re.sub(r'[\.\-_]', ' ', ep_raw).strip()
        ep_clean = re.sub(r'\s+', ' ', ep_clean).strip()
        if len(ep_clean) >= 3:
            result["filename_episode_title"] = ep_clean
    else:
        alt = re.search(r'[.\s\-_](\d{1,2})x(\d{2,3})', name)
        if alt:
            result["season"] = int(alt.group(1))
            result["episode"] = int(alt.group(2))
            result["is_tv"] = True
            title_part = name[:alt.start()]
        else:
            title_part = name

    # 2. Resolution (from full name, not just title part)
    res_match = re.search(r'(2160p|1080p|720p|480p|4K|UHD)', name, re.IGNORECASE)
    if res_match:
        r = res_match.group(1).lower()
        result["resolution"] = "2160p" if r in ("4k", "uhd") else r
        # Truncate title at resolution if it appears in title_part
        rpos = title_part.lower().find(res_match.group(1).lower())
        if rpos > 0:
            title_part = title_part[:rpos]

    # 3. Year
    year_match = re.search(r'[.\s\(\-]((?:19|20)\d{2})[.\s\)\-]', title_part)
    if not year_match:
        year_match = re.search(r'[.\s\(\-]((?:19|20)\d{2})$', title_part)
    if year_match:
        result["year"] = int(year_match.group(1))
        ypos = title_part.find(year_match.group(0))
        if ypos > 0:
            title_part = title_part[:ypos]

    # 4. Clean title
    title = title_part
    title = re.sub(r'[\.\-_]', ' ', title)
    title = re.sub(r'\[.*?\]', '', title)
    title = re.sub(r'\(.*?\)', '', title)
    title = TAGS_RE.sub('', title)
    title = re.sub(r'-\w+$', '', title)  # trailing -GROUP tag
    title = re.sub(r'\s+', ' ', title).strip()
    result["title"] = title

    return result


def sanitize_filename(name):
    """Make a string safe for use as a filesystem filename.

    Replaces colons with " -" (common Plex convention) and removes all
    other characters that are illegal on Windows/macOS/Linux filesystems.

    Args:
        name: Raw string to sanitize.

    Returns:
        str: Filesystem-safe string with trailing dots and spaces removed.
    """
    name = name.replace(":", " -")
    name = UNSAFE_CHARS_RE.sub("", name)
    name = name.rstrip(". ")
    return name
