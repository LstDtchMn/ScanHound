"""Release-title grammar — ONE definition, used by every discovery path.

Sibling of :mod:`backend.release_policy`, and it exists for the same reason.
That module was written after two independently-authored URL canonicalisers
disagreed about a trailing slash and made a healthy pipeline report itself as
"0 of 100 acquired". This module is written after the same shape recurred one
layer down.

On 2026-08-01 a parity harness compared the two release-title readers ScanHound
had grown — ``parse_release_title`` on the RSS path and ``SourceBase.extract_*``
on the listing path — and found **five divergences on twelve titles**. Neither
reader was uniformly right:

===  ===================================================================
(a)  RSS emitted ``2160p`` where the listing path emitted ``4K``. Same
     meaning, different token, and nothing folded them before comparison.
(b)  RSS read ``1920x1080`` as **year 1920**, because its guard was
     ``(?!\\d)`` and ``x`` is not a digit. This reached a decision: a wrong
     ``title_year`` sets ``year_conflict``, which resolves the candidate to
     ``ambiguous`` and blocks it in the gate.
(c)  The listing path read ``DTS5.1`` as **season 5**, turning a movie into
     TV, because ``SEASON_PATTERN`` had no preceding-character guard.
(d)  ``S104`` was season 104 to RSS (``\\d{1,3}``) and season 10 to the
     listing path (``\\d{1,2}``).
(e)  RSS could not parse ``1.2 TB`` at all — its unit set stopped at ``GB``
     — so a terabyte release was judged with **no size**, while size feeds
     the upgrade comparison.
===  ===================================================================

Patching two regexes until they happened to agree would have left two
implementations free to drift again on the next edit. So the grammar lives here
once. If a rule changes, it changes for both paths in the same edit, by
construction.

**The callers keep their own output shapes.** ``extract_size`` still returns
``(display, bytes)`` and RSS still stores ``2160p``; only the *parsing* is
shared. Changing what is already persisted is a data-migration question, not a
parser question, so resolution comparisons go through
:func:`canonical_resolution` instead.
"""
from __future__ import annotations

import re
from typing import NamedTuple, Optional

# ─────────────────────────────── year ───────────────────────────────────────

#: A year must be delimited by a NON-WORD character (or a string edge) on both
#: sides. Defect (b) came from guarding only against adjacent *digits*: in
#: ``1920x1080`` the ``x`` is not a digit, so ``1920`` passed and became the
#: release year. Requiring a non-word neighbour rejects it, because ``x`` is a
#: word character — which is exactly what the listing path's ``\b...\b`` was
#: already doing correctly.
_YEAR_RE = re.compile(r"(?<!\w)((?:19|20)\d{2})(?!\w)")


def parse_year(text: str) -> Optional[int]:
    """First plausible release year in ``text``, or None.

    Returns None rather than 0 for "absent". The listing path's historical
    sentinel was ``0``, which is indistinguishable from a parsed value in
    arithmetic and sorts before every real year; callers that need the old
    sentinel convert at their own boundary.
    """
    match = _YEAR_RE.search(text or "")
    return int(match.group(1)) if match else None


# ────────────────────────── season / episode ────────────────────────────────

#: Both patterns carry the preceding-character guard. Without it (defect (c))
#: any alphanumeric run ending in S+digits supplies a season: ``DTS5.1`` gives
#: season 5, and a movie silently becomes TV.
_EPISODE_RE = re.compile(
    r"(?<![A-Z0-9])S(?P<season>\d{1,4})E(?P<episode>\d{1,4})"
    r"(?P<extra>(?:E\d{1,4})*)(?!\d)",
    re.I,
)
_SEASON_RE = re.compile(r"(?<![A-Z0-9])S(?P<season>\d{1,4})(?!E\d)", re.I)

#: Seasons wider than this are treated as AMBIGUOUS rather than guessed at.
#: ``S104`` is defect (d): it could be season 104, ``S1E04`` or ``S10E4``, and
#: the two readers picked differently (104 vs a silently truncated 10). Silent
#: truncation is the worse failure — it yields a confident wrong answer — so
#: neither behaviour is kept. Three-digit seasons do not occur in this source's
#: naming; a token that wide is far more likely malformed than genuine.
_MAX_SEASON_DIGITS = 2


class SeasonEpisode(NamedTuple):
    season: Optional[int]
    episode: Optional[int]
    episode_end: Optional[int]
    #: True when a season-like token was found but is too wide to interpret.
    #: Distinct from ``season is None``, which means none was found at all —
    #: callers must not treat "cannot tell" as "definitely a movie".
    ambiguous: bool


def parse_season_episode(text: str) -> SeasonEpisode:
    """Season/episode for ``text``, with over-wide seasons reported ambiguous."""
    text = text or ""

    episode_match = _EPISODE_RE.search(text)
    season_match = None if episode_match else _SEASON_RE.search(text)
    match = episode_match or season_match
    if match is None:
        return SeasonEpisode(None, None, None, False)

    season_digits = match.group("season")
    if len(season_digits.lstrip("0") or "0") > _MAX_SEASON_DIGITS:
        return SeasonEpisode(None, None, None, True)

    episode = end = None
    if episode_match:
        episode = int(episode_match.group("episode"))
        extra = episode_match.group("extra")
        if extra:
            trailing = re.findall(r"E(\d{1,4})", extra, re.I)
            if trailing:
                end = int(trailing[-1])
    return SeasonEpisode(int(season_digits), episode, end, False)


# ──────────────────────────────── size ──────────────────────────────────────

#: Defect (e): the RSS unit set stopped at GB, so ``1.2 TB`` parsed to nothing.
#: TB and TiB are included here. Binary and decimal spellings are treated as
#: equivalent at this scale, matching the behaviour both readers already had —
#: the ~7% difference between GiB and GB does not change any upgrade decision,
#: and pretending to a precision the source titles do not carry would be worse.
_SIZE_UNITS_GB = {
    "MB": 1 / 1024, "MIB": 1 / 1024,
    "GB": 1.0, "GIB": 1.0,
    "TB": 1024.0, "TIB": 1024.0,
}
_SIZE_BODY = r"(?P<size>\d+(?:\.\d+)?)\s*(?P<unit>TiB|TB|GiB|GB|MiB|MB)"
_SIZE_ANCHORED_RE = re.compile(
    r"(?:\s+[–-]\s+|\s+)" + _SIZE_BODY + r"\s*$", re.I)
_SIZE_ANYWHERE_RE = re.compile(_SIZE_BODY, re.I)


def parse_size_gb(text: str, *, anchored: bool = False) -> Optional[float]:
    """Release size in gigabytes, or None.

    ``anchored=True`` requires the size to terminate the string, which is how
    HDEncode formats it in a feed title. The listing path searches article HTML
    where the size appears mid-document, so it passes ``anchored=False``. The
    *grammar* is identical either way; only the position requirement differs.
    """
    pattern = _SIZE_ANCHORED_RE if anchored else _SIZE_ANYWHERE_RE
    match = pattern.search(text or "")
    if not match:
        return None
    return float(match.group("size")) * _SIZE_UNITS_GB[match.group("unit").upper()]


def strip_trailing_size(text: str) -> str:
    """``text`` without a trailing size, for grammars that parse what remains.

    The RSS reader removes the size before looking for a year, so that a size
    like ``2019 GB`` cannot be mistaken for a release year.
    """
    match = _SIZE_ANCHORED_RE.search(text or "")
    return (text or "")[:match.start()].strip() if match else (text or "").strip()


# ───────────────────────────── resolution ───────────────────────────────────

_RESOLUTION_RE = re.compile(r"(?<!\w)(2160p|1080p|1080i|720p|480p|4K|UHD)(?!\w)", re.I)

#: Defect (a). UHD is spelled at least three ways across this codebase, and the
#: readers, the database and the frontend chip do not agree on which. This is
#: the single token every COMPARISON must be made in. It is deliberately not
#: the token anything is STORED as: rewriting persisted values is a migration,
#: and the identical bug has already been fixed once at the frontend filter
#: boundary (2026-07-30) by folding at comparison time rather than at write time.
_UHD_SPELLINGS = {"4K", "2160P", "UHD"}


def canonical_resolution(value: Optional[str]) -> Optional[str]:
    """Fold a resolution to the one token comparisons are made in.

    An unrecognised spelling passes through upper-cased rather than becoming
    None: mapping the unknown to None would recreate the original defect in a
    new form — items that quietly cannot be matched by any filter at all.
    """
    if value is None:
        return None
    folded = str(value).strip().upper()
    if not folded:
        return None
    if folded in _UHD_SPELLINGS:
        return "UHD"
    return "1080P" if folded == "1080I" else folded


def parse_resolution(text: str) -> Optional[str]:
    """Canonical resolution for ``text``, or None. See :func:`canonical_resolution`."""
    match = _RESOLUTION_RE.search(text or "")
    return canonical_resolution(match.group(1)) if match else None
