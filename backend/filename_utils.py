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

# Common torrent/release tags to strip from parsed titles. Case-insensitive.
# Internal separators use [\s.\-] so a tag matches whether the filename used a
# dot, a hyphen, or (after delimiter normalization) a space — e.g. "WEB-DL",
# "WEB.DL", "WEB DL" all match. Ambiguous short tokens that are also common
# title words (MA, DC, SE, MAX, CAM, TS, RED, REAL, COMPLETE, LIMITED) are
# deliberately omitted so we don't clip real titles like "Mad Max" or "Cam".
TAGS_RE = re.compile(
    r'\b('
    # Source / rip
    r'REMUX|BluRay|Blu[\s.\-]?Ray|BDRemux|BDRip|BRRip|BD(?:25|50|66|100)|'
    r'WEB[\s.\-]?DL|WEB[\s.\-]?Rip|WEBRip|WEBDL|HDTV|PDTV|DVDRip|DVDSCR|DVD[59]?|'
    r'HDRip|UHDRip|HDTC|HDCAM|SATRip|WORKPRINT|SCREENER|'
    # Platforms / services
    r'AMZN|DSNP|HMAX|NF|ATVP|HULU|PCOK|PMTP|STAN|MUBI|CRAV|ROKU|TUBI|FREEVEE|STARZ|GPLAY|'
    # Video codec / bit depth
    r'HEVC|x265|x264|AVC|H[\s.]?264|H[\s.]?265|XviD|DivX|AV1|VP9|MPEG[\s.\-]?2|VC[\s.\-]?1|'
    r'8bit|10bit|12bit|'
    # Audio (incl. glued channel suffixes like DDP5.1)
    r'AAC|DTS[\s.\-]?HD(?:[\s.\-]?MA)?|DTS[\s.\-]?X|DTS|Atmos|TrueHD|FLAC|AC3|EAC3|'
    r'DDP?[\s.]?\d?(?:[\s.]?\d)?|Opus|LPCM|PCM|'
    # HDR / color
    r'DV|DoVi|Dolby[\s.\-]?Vision|HDR10\+?|HDR10Plus|HDR|SDR|HLG|WCG|'
    # Editions
    r'IMAX|Hybrid|REMASTERED|RESTORED|EXTENDED|UNRATED|UNCUT|REDUX|THEATRICAL|'
    r'DIRECTORS?[\s.\-]?CUT|FINAL[\s.\-]?CUT|OPEN[\s.\-]?MATTE|CRITERION|ANNIVERSARY|'
    r'REPACK|PROPER|RERIP|READNFO|'
    # Language
    r'MULTI|DL|DUAL|GERMAN|FRENCH|ITALIAN|SPANISH|'
    r'UHD'
    r')\b',
    re.IGNORECASE
)

# Characters not allowed in filesystem names
UNSAFE_CHARS_RE = re.compile(r'[<>"/\\|?*]')

# ASCII control characters (0x00-0x1F) — invalid/dangerous in filenames on
# Windows and generally unwanted anywhere.
CONTROL_CHARS_RE = re.compile(r'[\x00-\x1f]')

# Windows reserved device basenames (case-insensitive, stem-only — i.e. the
# name before any extension). Matching the *whole* stem avoids false
# positives on titles that merely start with or contain a reserved token
# (e.g. "Conan", "Con Air", "COM1the Movie").
RESERVED_NAME_RE = re.compile(
    r'^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])$', re.IGNORECASE)

# Quality/release markers that end an episode title in TV filenames
EPISODE_BOUNDARY_RE = re.compile(
    r'[.\s](?:Part[.\s\-]?\d|Pt[.\s\-]?\d|'
    r'2160p|1080p|720p|480p|4[Kk]|UHD|'
    r'BluRay|Blu[\.\-]Ray|BDRip|BDRemux|REMUX|'
    r'WEB[\.\-]?DL|WEBRip|WEBDL|HDTV|DVDRip|HDRip|'
    r'HEVC|[Xx]265|[Xx]264|H[\.\s]?264|H[\.\s]?265|'
    r'AMZN|DSNP|HMAX|NF|ATVP|IMAX)',
    re.IGNORECASE
)


def _clean_title(raw: str) -> str:
    """Turn a raw title fragment into a clean search title: drop bracketed
    content and a trailing release-group tag, normalize ._- to spaces, strip
    known release tags, and collapse whitespace."""
    s = re.sub(r'\[.*?\]', '', raw or '')
    s = re.sub(r'\(.*?\)', '', s)
    s = re.sub(r'\{.*?\}', '', s)             # {imdb-tt…}, {edition-…} etc.
    s = re.sub(r'[-_][A-Za-z0-9]+$', '', s)   # trailing -GROUP / _GROUP
    s = re.sub(r'[\.\-_]', ' ', s)
    s = re.sub(r'\btt\d{7,8}\b', ' ', s, flags=re.IGNORECASE)  # bare imdb id
    s = re.sub(r'\s+', ' ', s).strip()
    # Strip release tags from everything AFTER the first word, never the first
    # word itself — a real title's leading word can legitimately equal a tag
    # (Stan, Opus, Hybrid, Dual, …). Stripping the whole string deletes such
    # titles outright; protecting the head keeps them while still removing
    # mid/trailing junk (MULTI, WEB-DL, DDP5.1, …).
    parts = s.split(' ', 1)
    if len(parts) == 2:
        s = (parts[0] + ' ' + TAGS_RE.sub(' ', parts[1])).strip()
    s = re.sub(r'\s+', ' ', s).strip()
    return s


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
            episode_end (int or None)  — last episode in multi-ep file
            resolution (str or None)   — e.g. "1080p", "2160p"
            part       (int or None)   — split file part number
            is_tv      (bool)          — True when season/episode found
    """
    name = os.path.splitext(filename)[0]
    result = {
        "title": "", "year": None, "season": None,
        "episode": None, "episode_end": None, "resolution": None, "is_tv": False,
        "part": None, "imdb_id": None,
    }

    # IMDB id embedded by some renamers/groups: {imdb-tt1234567}, [tt1234567],
    # or a bare tt-token. When present it's an exact, fuzzy-free resolve, so the
    # matcher tries it first. \b bounds prevent matching inside longer alnum runs.
    imdb_match = re.search(r'\b(tt\d{7,8})\b', name, re.IGNORECASE)
    if imdb_match:
        result["imdb_id"] = imdb_match.group(1).lower()

    # 1. Season / episode
    se_match = re.search(r'[.\s\-_]S(\d{1,2})E(\d{1,3})', name, re.IGNORECASE)
    if se_match:
        result["season"] = int(se_match.group(1))
        result["episode"] = int(se_match.group(2))
        result["is_tv"] = True
        title_part = name[:se_match.start()]
        # Extract candidate episode title: text between SxxExx and quality marker
        after_ep = name[se_match.end():]
        # Multi-episode: SxxExxEyy or SxxExx-Eyy or SxxExx.Eyy
        me_match = re.match(r'[.\-]?E(\d{1,3})', after_ep, re.IGNORECASE)
        if me_match:
            result["episode_end"] = int(me_match.group(1))
            # Skip the second episode code so it isn't mistaken for a title.
            after_ep = after_ep[me_match.end():]
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

    # 3. Year — delimiter classes include '_' so underscore-delimited scene
    #    releases (e.g. Smallfoot_2018_...) extract the year (and the truncation
    #    below then strips it and any trailing junk out of the title).
    year_match = re.search(r'[._\s\(\-]((?:19|20)\d{2})[._\s\)\-]', title_part)
    if not year_match:
        year_match = re.search(r'[._\s\(\-]((?:19|20)\d{2})$', title_part)
    if year_match:
        result["year"] = int(year_match.group(1))
        ypos = title_part.find(year_match.group(0))
        if ypos > 0:
            title_part = title_part[:ypos]

    # 3b. a.k.a. / alternate title — split so each side can be searched on its
    #     own (e.g. "Ohikkoshi a.k.a. Moving" → title "Ohikkoshi", aka "Moving").
    result["aka"] = None
    aka_split = re.split(r'[._\s\-]+a\.?k\.?a\.?[._\s\-]+', title_part, maxsplit=1,
                         flags=re.IGNORECASE)
    if len(aka_split) == 2:
        title_part = aka_split[0]
        aka_clean = _clean_title(aka_split[1])
        if aka_clean:
            result["aka"] = aka_clean

    # 4. Clean title
    result["title"] = _clean_title(title_part)

    # Part indicator: Part1, Part 2, Pt1, Pt.2, Part 10, … (1-2 digits so a
    # double-digit part isn't truncated to its leading digit and collided).
    part_match = re.search(
        r'[.\s\-_](?:Part|Pt)[\s.\-]?(\d{1,2})', name, re.IGNORECASE)
    if part_match:
        result["part"] = int(part_match.group(1))

    return result


def sanitize_filename(name):
    """Make a string safe for use as a filesystem filename.

    Replaces colons with " -" (common Plex convention), removes all other
    characters that are illegal on Windows/macOS/Linux filesystems, strips
    ASCII control characters, and guards against Windows reserved device
    basenames (CON, PRN, AUX, NUL, COM1-9, LPT1-9) by appending a trailing
    underscore so a pathological title becomes a clean job instead of a
    hard filesystem failure.

    Args:
        name: Raw string to sanitize.

    Returns:
        str: Filesystem-safe string with trailing dots and spaces removed.
    """
    name = name.replace(":", " -")
    name = UNSAFE_CHARS_RE.sub("", name)
    name = CONTROL_CHARS_RE.sub("", name)
    name = name.rstrip(". ")
    if RESERVED_NAME_RE.match(name):
        name = name + "_"
    return name
