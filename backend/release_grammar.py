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

import enum
import re
from typing import Iterable, NamedTuple, Optional

# ─────────────────────────────── year ───────────────────────────────────────

#: A year must be delimited by a NON-WORD character (or a string edge) on both
#: sides. Defect (b) came from guarding only against adjacent *digits*: in
#: ``1920x1080`` the ``x`` is not a digit, so ``1920`` passed and became the
#: release year. Requiring a non-word neighbour rejects it, because ``x`` is a
#: word character — which is exactly what the listing path's ``\b...\b`` was
#: already doing correctly.
_YEAR_RE = re.compile(r"(?<!\w)((?:19|20)\d{2})(?!\w)")


class YearMatch(NamedTuple):
    year: int
    start: int


def find_year(text: str) -> Optional[YearMatch]:
    """Year with its position, for callers that split a title at its metadata."""
    match = _YEAR_RE.search(text or "")
    return YearMatch(int(match.group(1)), match.start()) if match else None


def parse_year(text: str) -> Optional[int]:
    """First plausible release year in ``text``, or None.

    Returns None rather than 0 for "absent". The listing path's historical
    sentinel was ``0``, which is indistinguishable from a parsed value in
    arithmetic and sorts before every real year; callers that need the old
    sentinel convert at their own boundary.
    """
    found = find_year(text)
    return found.year if found else None


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
    #: Position of the season/episode token, or None when nothing matched.
    #: Present even when the season is ambiguous: the token is still where the
    #: title's metadata begins, so a clean title can be cut there regardless of
    #: whether the season itself could be interpreted.
    start: Optional[int] = None


def parse_season_episode(text: str) -> SeasonEpisode:
    """Season/episode for ``text``, with over-wide seasons reported ambiguous."""
    text = text or ""

    episode_match = _EPISODE_RE.search(text)
    season_match = None if episode_match else _SEASON_RE.search(text)
    match = episode_match or season_match
    if match is None:
        return SeasonEpisode(None, None, None, False, None)

    season_digits = match.group("season")
    if len(season_digits.lstrip("0") or "0") > _MAX_SEASON_DIGITS:
        return SeasonEpisode(None, None, None, True, match.start())

    episode = end = None
    if episode_match:
        episode = int(episode_match.group("episode"))
        extra = episode_match.group("extra")
        if extra:
            trailing = re.findall(r"E(\d{1,4})", extra, re.I)
            if trailing:
                end = int(trailing[-1])
    return SeasonEpisode(int(season_digits), episode, end, False, match.start())


#: Title forms that mean "this is TV" without carrying an SxxExx token.
#: Divergence (f), measured 2026-08-02: the listing path recognised all four,
#: the RSS path recognised none of them. On the RSS side such a release is
#: classified as a MOVIE, which selects the wrong Plex library in
#: get_hdencode_candidate_context() and so reaches a different actionable
#: decision — precisely the A4 failure, on the path being promoted. Feeds are
#: per-category so a TV release usually arrives with a "tv" category to fall
#: back on, but "usually" is not "always", and the fallback does no work at all
#: for a TV-shaped release appearing in a movies feed.
_TV_FORM_RES = (
    re.compile(r"(?<!\w)Season\s*\d+(?!\w)", re.I),
    re.compile(r"(?<!\w)Complete\s*Series(?!\w)", re.I),
    re.compile(r"(?<!\w)Mini[\s.-]*Series(?!\w)", re.I),
    re.compile(r"(?<!\w)TV\s*Series(?!\w)", re.I),
)


class MediaType(str, enum.Enum):
    """Three outcomes, because two cannot express disagreement.

    ``AMBIGUOUS`` is the point of this enum. A boolean forces every conflict to
    resolve silently to one side, and the whole class of defects this module
    exists to fix is exactly that: a confident wrong answer produced where the
    honest answer was "the evidence disagrees".
    """

    TV = "tv"
    MOVIE = "movie"
    AMBIGUOUS = "ambiguous"


class Authority(enum.IntEnum):
    """How much a signal is entitled to decide. Higher wins outright.

    The key judgement is that ROUTE sits at the bottom: which feed a release
    arrived on, or which category page it was crawled from, is **routing
    metadata, not identity**. Divergence (f) proved it — a TV-shaped release
    genuinely appears in a movies feed — and the converse happens too, so a
    route may resolve a silent title but must never contradict a spoken one.
    """

    ROUTE = 1      # feed category, listing crawl mode
    TITLE = 2      # release-title grammar
    DETAIL = 3     # hydrated detail filename
    IDENTITY = 4   # confirmed external id, or a unique library match


class TypeEvidence(NamedTuple):
    media_type: MediaType   # TV or MOVIE; AMBIGUOUS is an output, not an input
    authority: Authority
    source: str = "unknown"  # provenance label, so a verdict can be explained


class MediaTypeVerdict(NamedTuple):
    media_type: MediaType
    #: True when nothing above ROUTE spoke. The verdict is usable for routing
    #: and display, but must not by itself authorise an autonomous action.
    provisional: bool
    #: Provenance of the deciding evidence, and of anything it overruled. Kept
    #: because a bare "ambiguous" is unactionable.
    because: tuple


def resolve_media_type(evidence: Iterable[TypeEvidence]) -> MediaTypeVerdict:
    """Merge typed evidence into one verdict.

    Rules, in order:

    * no evidence at all -> AMBIGUOUS, provisional;
    * only the highest authority level present decides — lower levels never
      override it, and never contribute to a conflict;
    * unanimity at that level -> that type;
    * disagreement at that level -> AMBIGUOUS (a strong conflict is a real
      finding, not something to average away);
    * a verdict decided solely by ROUTE is marked provisional.

    Deliberately NOT a boolean OR of "anything says TV". That rule cannot
    represent a contradiction, so it resolves every one of them to TV — which
    is how a film sitting on a TV page becomes a series.
    """
    items = [e for e in evidence if e is not None]
    if not items:
        return MediaTypeVerdict(MediaType.AMBIGUOUS, True, ("no evidence",))

    top = max(e.authority for e in items)
    deciding = [e for e in items if e.authority == top]
    types = {e.media_type for e in deciding}
    why = tuple(sorted(f"{e.source}={e.media_type.value}" for e in deciding))

    if len(types) > 1:
        return MediaTypeVerdict(MediaType.AMBIGUOUS, False, why)

    decided = types.pop()
    overruled = tuple(sorted(
        f"{e.source}={e.media_type.value} (overruled)"
        for e in items if e.authority < top and e.media_type is not decided))
    return MediaTypeVerdict(decided, top is Authority.ROUTE, why + overruled)


def title_type_evidence(text: str, *, source: str = "title") -> Optional[TypeEvidence]:
    """TV evidence from a title, or None when the title says nothing.

    Returns None rather than MOVIE for a silent title. ``title_indicates_tv``
    being False means "no TV signal here", never "this is a film" — asserting
    the latter would let a neutral title outrank a trustworthy feed category,
    which is the opposite of the intended precedence.
    """
    return (TypeEvidence(MediaType.TV, Authority.TITLE, source)
            if title_indicates_tv(text) else None)


def title_indicates_tv(text: str) -> bool:
    """Whether the TITLE ALONE is evidence of TV content.

    Deliberately title-only. Each path adds its own out-of-band signal on top —
    the listing path knows which category URL it crawled, the RSS path has feed
    categories — and those may only ever ADD TV-ness, never remove it. Keeping
    the title rule here is what stops the two from disagreeing about the same
    string.

    An AMBIGUOUS season counts. A token too wide to interpret is still a season
    token, and reading "cannot tell" as "not TV" would file a series as a film.
    """
    text = text or ""
    found = parse_season_episode(text)
    if found.season is not None or found.ambiguous:
        return True
    return any(pattern.search(text) for pattern in _TV_FORM_RES)


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


class SizeMatch(NamedTuple):
    gigabytes: float
    #: The size exactly as written, e.g. "82.4 GB" — for display and for the
    #: RSS reader's ``size_text`` field, which is shown to users verbatim.
    text: str
    start: int


def find_size(text: str, *, anchored: bool = False) -> Optional[SizeMatch]:
    """Size with its written form and position."""
    pattern = _SIZE_ANCHORED_RE if anchored else _SIZE_ANYWHERE_RE
    match = pattern.search(text or "")
    if not match:
        return None
    amount, unit = match.group("size"), match.group("unit")
    return SizeMatch(
        float(amount) * _SIZE_UNITS_GB[unit.upper()],
        f"{amount} {unit}",
        match.start(),
    )


def parse_size_gb(text: str, *, anchored: bool = False) -> Optional[float]:
    """Release size in gigabytes, or None.

    ``anchored=True`` requires the size to terminate the string, which is how
    HDEncode formats it in a feed title. The listing path searches article HTML
    where the size appears mid-document, so it passes ``anchored=False``. The
    *grammar* is identical either way; only the position requirement differs.
    """
    found = find_size(text, anchored=anchored)
    return found.gigabytes if found else None


def strip_trailing_size(text: str) -> str:
    """``text`` without a trailing size, for grammars that parse what remains.

    The RSS reader removes the size before looking for a year, so that a size
    like ``2019 GB`` cannot be mistaken for a release year.
    """
    match = _SIZE_ANCHORED_RE.search(text or "")
    return (text or "")[:match.start()].strip() if match else (text or "").strip()


# ───────────────────────────── resolution ───────────────────────────────────

#: Exactly the union both readers already accepted. Deliberately NOT widened:
#: adding 1080i or 480p here would make titles newly parseable that previously
#: yielded no resolution at all — a behaviour change, smuggled into a fix whose
#: whole purpose is to make the two paths agree without altering what either
#: one decides. :func:`canonical_resolution` still tolerates those spellings,
#: because values also arrive from the database rather than from a title.
_RESOLUTION_RE = re.compile(r"(?<!\w)(2160p|1080p|720p|4K|UHD)(?!\w)", re.I)

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


class ResolutionMatch(NamedTuple):
    #: The one token comparisons are made in.
    canonical: str
    #: The spelling as written in the title, for callers that persist it.
    raw: str
    start: int


def find_resolution(text: str) -> Optional[ResolutionMatch]:
    """Resolution with both spellings and its position."""
    match = _RESOLUTION_RE.search(text or "")
    if not match:
        return None
    raw = match.group(1)
    return ResolutionMatch(canonical_resolution(raw), raw, match.start())


def parse_resolution(text: str) -> Optional[str]:
    """Canonical resolution for ``text``, or None. See :func:`canonical_resolution`."""
    found = find_resolution(text)
    return found.canonical if found else None


_DIMENSION_VALUE_RE = re.compile(r"(?<!\w)(\d{3,4})\s*[xX]\s*(\d{3,4})(?!\w)")


def resolution_from_dimensions(text: str) -> Optional[str]:
    """Canonical resolution implied by an explicit WxH pixel dimension.

    A dimension is NOT a resolution token and never enters the resolution
    vocabulary (that inversion is how divergence (b)'s cousin lived in
    DetailScraper). Detail pages legitimately state "Resolution: 3840x2160",
    so THIS function is the one sanctioned conversion — explicit, named, and
    faithful to the scraper's historical mapping: exact standard values only,
    anything else stays None rather than being guessed into a class.
    """
    match = _DIMENSION_VALUE_RE.search(text or "")
    if not match:
        return None
    width, height = int(match.group(1)), int(match.group(2))
    if width in (3840, 2160) or height == 2160:
        return "UHD"
    if width == 1080 or height == 1080:
        return "1080P"
    if width == 720 or height == 720:
        return "720P"
    return None


def find_all_sizes(text: str) -> list:
    """Every size in ``text``, in document order.

    For callers that must choose among several — a listing detail page lists
    per-file sizes and the scraper keeps the largest. Selection stays with
    the caller; the *grammar* (units, spelling, TB included) lives here.
    """
    return [
        SizeMatch(
            float(m.group("size")) * _SIZE_UNITS_GB[m.group("unit").upper()],
            f"{m.group('size')} {m.group('unit')}",
            m.start(),
        )
        for m in _SIZE_ANYWHERE_RE.finditer(text or "")
    ]


# ─────────────────────────── title / metadata split ─────────────────────────

#: A pixel dimension such as ``1920x1080``. It is metadata, never part of a
#: name, and it must be recognised HERE even though no field is parsed from it.
#:
#: This exists because of a second-order effect of the year fix. While the year
#: guard was broken, ``1920x1080`` matched as year 1920, which put the metadata
#: boundary at the dimension and produced the right clean title for the wrong
#: reason. Correcting the year moved the boundary to the real year further
#: right, and the dimension started leaking into the title — turning
#: "Concert Film" into "Concert Film 1920x1080", which then fails identity
#: matching. Naming the token directly fixes both.
_DIMENSION_RE = re.compile(r"(?<!\w)\d{3,4}\s*[xX]\s*\d{3,4}(?!\w)")


def metadata_start(text: str) -> int:
    """Index where a release title stops being a name and starts being tags.

    Both readers cut the human-readable title at the first metadata token, and
    both previously computed that boundary from their own regex matches — so a
    divergence in *any* of the four patterns silently moved the title boundary
    too, not just the parsed field. Deriving it here keeps the cut in step with
    the grammar by construction.

    This is a PRIORITY CHAIN, not the earliest match: season/episode wins over
    year, which wins over resolution. That is the behaviour the RSS reader
    already had, and it is deliberately preserved — the two differ for a title
    like ``Show 2019 S01E02``, where the chain keeps the year in the name
    ("Show 2019") and an earliest-match rule would drop it ("Show"). Changing
    how titles are cut is a matching change, not a parity fix, so it is out of
    scope here.

    Returns ``len(text)`` when the title carries no metadata at all.
    """
    text = text or ""
    season = parse_season_episode(text)
    if season.start is not None:
        boundary = season.start
    else:
        year = find_year(text)
        if year is not None:
            boundary = year.start
        else:
            resolution = find_resolution(text)
            boundary = resolution.start if resolution else len(text)

    # A pixel dimension is metadata too, and it can sit to the LEFT of whatever
    # the chain picked. Take the earlier of the two so it never leaks into the
    # name; the chain still decides between season, year and resolution.
    dimension = _DIMENSION_RE.search(text)
    return min(boundary, dimension.start()) if dimension else boundary
