"""Safe, replayable parsing of qualified HDEncode RSS evidence."""
from dataclasses import asdict, dataclass
from datetime import timezone
import email.utils
import hashlib
import html
import re
from typing import Optional
from urllib.parse import urlsplit, urlunsplit
import xml.etree.ElementTree as ET

from backend import release_grammar as grammar
from backend.candidate_evidence import EvidenceState


MAX_FEED_BYTES = 2 * 1024 * 1024
MAX_ENTRIES = 100
_ALLOWED_HOSTS = {"hdencode.org", "www.hdencode.org"}
_DANGEROUS_XML = re.compile(br"<!\s*(?:DOCTYPE|ENTITY)\b", re.I)
# Season, episode, year, resolution and size patterns USED to live here, in
# parallel with near-identical ones on the listing path. They were measured
# disagreeing on five of them (2026-08-01) and now live in
# backend.release_grammar. They are not kept here as "reference copies":
# a second definition that nothing calls is exactly how the two paths drifted
# apart in the first place.
_DV_RE = re.compile(r"(?<![A-Z0-9])(?:DV|DoVi)(?![A-Z0-9])|Dolby[ ._-]?Vision", re.I)
_HDR10P_RE = re.compile(r"(?<![A-Z0-9])(?:HDR10\+|HDR10P)(?![A-Z0-9])", re.I)
_HDR_RE = re.compile(r"(?<![A-Z0-9])(?:HDR10\+?|HDR10P|HDR|HLG)(?![A-Z0-9])", re.I)
_HEVC_RE = re.compile(r"(?<![A-Z0-9])(?:HEVC|H\.?265|X265)(?![A-Z0-9])", re.I)
_TAG_RE = re.compile(r"<[^>]+>")
_DESC_YEAR_RE = re.compile(
    r"(?:\bYear\b|\bRelease\s+year\b)\s*[:.-]\s*((?:19|20)\d{2})",
    re.I,
)


@dataclass(frozen=True)
class ParsedFeedEntry:
    guid: str
    canonical_url: str
    title: str
    pub_date: str
    categories: tuple[str, ...]
    raw_description: str
    raw_hash: str
    media_type: str
    clean_title: str
    title_year: Optional[int]
    description_year: Optional[int]
    season: Optional[int]
    episode: Optional[int]
    episode_end: Optional[int]
    resolution: Optional[str]
    size_text: Optional[str]
    size_gb: Optional[float]
    dv: str
    hdr: str
    hevc: str
    hdr_formats: tuple[str, ...]
    description_complete: bool

    def as_database_row(self):
        row = asdict(self)
        row["categories"] = list(self.categories)
        row["hdr_formats"] = list(self.hdr_formats)
        return row


@dataclass(frozen=True)
class ParsedFeed:
    feed_key: str
    channel_last_build_date: Optional[str]
    entries: tuple[ParsedFeedEntry, ...]
    body_sha256: str


def canonicalize_post_url(url):
    parsed = urlsplit((url or "").strip())
    if parsed.scheme.lower() != "https":
        raise ValueError("RSS entry URL must be HTTPS")
    host = (parsed.hostname or "").lower().rstrip(".")
    if host not in _ALLOWED_HOSTS:
        raise ValueError(f"RSS entry host is not approved: {host or '<missing>'}")
    path = re.sub(r"/+", "/", parsed.path or "/")
    if path != "/":
        path = path.rstrip("/") + "/"
    return urlunsplit(("https", "hdencode.org", path, "", ""))


def parse_feed(xml_bytes, feed_key):
    if len(xml_bytes) > MAX_FEED_BYTES:
        raise ValueError("RSS response exceeds the 2 MiB limit")
    if _DANGEROUS_XML.search(xml_bytes):
        raise ValueError("DTD/entity declarations are not allowed")
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as exc:
        raise ValueError("Malformed RSS XML") from exc
    if _local_name(root.tag) != "rss":
        raise ValueError("Expected RSS 2.0 root")
    channel = next(
        (child for child in root if _local_name(child.tag) == "channel"),
        None,
    )
    if channel is None:
        raise ValueError("RSS channel is missing")
    entries = []
    for item in (child for child in channel if _local_name(child.tag) == "item"):
        if len(entries) >= MAX_ENTRIES:
            raise ValueError("RSS entry limit exceeded")
        entries.append(_parse_item(item))
    return ParsedFeed(
        feed_key=feed_key,
        channel_last_build_date=_child_text(channel, "lastBuildDate") or None,
        entries=tuple(entries),
        body_sha256=hashlib.sha256(xml_bytes).hexdigest(),
    )


#: Feed categories recognised as a TV route, normalised and matched WHOLE.
#: The previous rule was `"tv" in category.lower()`, which also fires on
#: "TVrip", "HDTV", "HDTV-Rip" and anything else that merely contains those two
#: letters — a guess presented as a signal. An unknown category is now retained
#: as provenance and contributes NOTHING, rather than being guessed at.
_TV_CATEGORIES = frozenset({
    "tv", "tv shows", "tv-shows", "tvshows", "television",
    "tv packs", "tv-packs", "tv series", "tv-series",
})
_MOVIE_CATEGORIES = frozenset({
    "movies", "movie", "films", "film",
})


def _normalise_category(value):
    return re.sub(r"\s+", " ", (value or "").strip().lower())


def _category_type_evidence(categories):
    """ROUTE-level evidence from feed categories, or None when they say nothing.

    Returns None on an unknown or empty category set — that is the fail-open
    direction for *evidence* and the fail-closed direction for *decisions*: it
    lets the title decide instead of asserting a type nobody established. A
    category set naming both kinds is also None, since the feed is then telling
    us nothing useful about this entry.
    """
    normalised = {_normalise_category(c) for c in (categories or ())}
    normalised.discard("")
    is_tv = bool(normalised & _TV_CATEGORIES)
    is_movie = bool(normalised & _MOVIE_CATEGORIES)
    if is_tv == is_movie:          # neither, or contradictory
        return None
    return grammar.TypeEvidence(
        grammar.MediaType.TV if is_tv else grammar.MediaType.MOVIE,
        grammar.Authority.ROUTE,
        "feed-category",
    )


def _parse_item(item):
    title = _required_text(item, "title")
    link = canonicalize_post_url(_required_text(item, "link"))
    guid = _required_text(item, "guid")
    pub_date = _parse_pub_date(_required_text(item, "pubDate"))
    categories = tuple(
        text
        for text in (
            (child.text or "").strip()
            for child in item
            if _local_name(child.tag) == "category"
        )
        if text
    )
    raw_description = _child_text(item, "description")
    plain_description = _description_text(raw_description)
    signals = parse_release_title(title)
    year_match = _DESC_YEAR_RE.search(plain_description)
    description_year = int(year_match.group(1)) if year_match else None
    # Resolved by AUTHORITY, through the same resolver the listing path uses,
    # so neither can reach a different verdict from the same evidence.
    #
    # The feed category is ROUTE-level: weakest, and unable to overrule a title.
    # It is also matched against a normalised allowlist rather than by
    # substring — `"tv" in category` also fires on "TVrip", "HDTV" and any
    # future category that merely contains those two letters, which is a guess
    # dressed as a signal.
    verdict = grammar.resolve_media_type([
        _category_type_evidence(categories),
        grammar.title_type_evidence(title, source="feed-title"),
    ])
    media_type = (
        "tv" if verdict.media_type is grammar.MediaType.TV else "movie"
    )
    raw_hash = hashlib.sha256(
        (title + "\0" + link + "\0" + raw_description).encode("utf-8")
    ).hexdigest()
    return ParsedFeedEntry(
        guid=guid,
        canonical_url=link,
        title=title,
        pub_date=pub_date,
        categories=categories,
        raw_description=raw_description,
        raw_hash=raw_hash,
        media_type=media_type,
        clean_title=signals["clean_title"],
        title_year=signals["year"],
        description_year=description_year,
        season=signals["season"],
        episode=signals["episode"],
        episode_end=signals["episode_end"],
        resolution=signals["resolution"],
        size_text=signals["size_text"],
        size_gb=signals["size_gb"],
        dv=signals["dv"],
        hdr=signals["hdr"],
        hevc=signals["hevc"],
        hdr_formats=signals["hdr_formats"],
        description_complete=_description_complete(raw_description),
    )


def parse_release_title(title):
    """Parse a feed title. Field extraction lives in :mod:`backend.release_grammar`.

    This reader and the listing reader used to carry independent copies of these
    patterns, and on 2026-08-01 they were measured disagreeing on five of them.
    Two of the defects were on this side: a year guard that read ``1920x1080``
    as year 1920, and a unit set that could not parse ``TB`` at all. Both
    reached the decision engine. The grammar is shared now so the next edit
    cannot reopen the gap.

    The RETURN SHAPE is unchanged, including ``resolution`` still being stored
    as ``2160p`` rather than the canonical comparison token — persisted values
    are a migration question, not a parser one.
    """
    raw = html.unescape(str(title or "")).strip()
    size = grammar.find_size(raw, anchored=True)
    size_text = size.text if size else None
    size_gb = size.gigabytes if size else None
    title_without_size = raw[:size.start].strip() if size else raw

    season_episode = grammar.parse_season_episode(title_without_size)
    season = season_episode.season
    episode = season_episode.episode
    episode_end = season_episode.episode_end

    year = grammar.parse_year(title_without_size)

    # Stored spelling, not the comparison token: every existing row in
    # hdencode_candidates holds '2160p', so emitting the canonical 'UHD' here
    # would split the column into two vocabularies. Comparisons go through
    # grammar.canonical_resolution() instead.
    found_resolution = grammar.find_resolution(title_without_size)
    resolution = None
    if found_resolution:
        resolution = (
            "2160p" if found_resolution.canonical == "UHD"
            else found_resolution.raw.lower()
        )

    dv = (
        EvidenceState.ASSERTED.value
        if _DV_RE.search(title_without_size)
        else EvidenceState.UNKNOWN.value
    )
    hdr_formats = []
    if _HDR10P_RE.search(title_without_size):
        hdr_formats.append("HDR10+")
    if re.search(r"(?<![A-Z0-9])HLG(?![A-Z0-9])", title_without_size, re.I):
        hdr_formats.append("HLG")
    if (
        re.search(r"(?<![A-Z0-9])HDR10(?![A-Z0-9+P])", title_without_size, re.I)
        and "HDR10+" not in hdr_formats
    ):
        hdr_formats.append("HDR10")
    if (
        re.search(r"(?<![A-Z0-9])HDR(?![A-Z0-9])", title_without_size, re.I)
        and not hdr_formats
    ):
        hdr_formats.append("HDR")

    hdr = (
        EvidenceState.ASSERTED.value
        if _HDR_RE.search(title_without_size)
        else EvidenceState.UNKNOWN.value
    )
    hevc = (
        EvidenceState.ASSERTED.value
        if _HEVC_RE.search(title_without_size)
        else EvidenceState.UNKNOWN.value
    )

    marker = grammar.metadata_start(title_without_size)
    clean_title = re.sub(r"[._]+", " ", title_without_size[:marker])
    clean_title = re.sub(r"\s+", " ", clean_title).strip(" -.")
    return {
        "clean_title": clean_title,
        "year": year,
        "season": season,
        "season_ambiguous": season_episode.ambiguous,
        "episode": episode,
        "episode_end": episode_end,
        "resolution": resolution,
        "size_text": size_text,
        "size_gb": size_gb,
        "dv": dv,
        "hdr": hdr,
        "hevc": hevc,
        "hdr_formats": tuple(hdr_formats),
    }


def _description_text(raw):
    return re.sub(r"\s+", " ", html.unescape(_TAG_RE.sub(" ", raw or ""))).strip()


def _description_complete(raw):
    text = _description_text(raw)
    if not text:
        return False
    return not (
        text.endswith("…")
        or text.endswith("...")
        or "&hellip;" in (raw or "")
        or "class=\"more-link\"" in (raw or "")
    )


def _parse_pub_date(value):
    parsed = email.utils.parsedate_to_datetime(value)
    if parsed is None:
        raise ValueError("RSS pubDate is not parseable")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat()


def _required_text(parent, name):
    value = _child_text(parent, name)
    if not value:
        raise ValueError(f"RSS item is missing {name}")
    return value


def _child_text(parent, name):
    for child in parent:
        if _local_name(child.tag) == name:
            return (child.text or "").strip()
    return ""


def _local_name(tag):
    return tag.rsplit("}", 1)[-1]
