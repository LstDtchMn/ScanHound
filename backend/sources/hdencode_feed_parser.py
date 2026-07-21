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

from backend.candidate_evidence import EvidenceState


MAX_FEED_BYTES = 2 * 1024 * 1024
MAX_ENTRIES = 100
_ALLOWED_HOSTS = {"hdencode.org", "www.hdencode.org"}
_DANGEROUS_XML = re.compile(br"<!\s*(?:DOCTYPE|ENTITY)\b", re.I)
_EPISODE_RE = re.compile(
    r"(?<![A-Z0-9])S(?P<season>\d{1,3})E(?P<episode>\d{1,4})"
    r"(?P<extra>(?:E\d{1,4})*)(?!\d)",
    re.I,
)
_SEASON_RE = re.compile(r"(?<![A-Z0-9])S(?P<season>\d{1,3})(?!E\d)", re.I)
_YEAR_RE = re.compile(r"(?<!\d)((?:19|20)\d{2})(?!\d)")
_RESOLUTION_RE = re.compile(r"(?<!\w)(2160p|1080p|720p|4K|UHD)(?!\w)", re.I)
_SIZE_RE = re.compile(
    r"(?:\s+[–-]\s+|\s+)(?P<size>\d+(?:\.\d+)?)\s*"
    r"(?P<unit>GiB|GB|MiB|MB)\s*$",
    re.I,
)
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
    media_type = (
        "tv"
        if signals["season"] is not None
        or any("tv" in category.lower() for category in categories)
        else "movie"
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
    raw = html.unescape(str(title or "")).strip()
    size_match = _SIZE_RE.search(raw)
    size_text = None
    size_gb = None
    title_without_size = raw
    if size_match:
        amount = float(size_match.group("size"))
        unit = size_match.group("unit").upper()
        size_text = f"{size_match.group('size')} {size_match.group('unit')}"
        size_gb = amount / 1024.0 if unit in {"MB", "MIB"} else amount
        title_without_size = raw[:size_match.start()].strip()

    episode_match = _EPISODE_RE.search(title_without_size)
    season_match = None if episode_match else _SEASON_RE.search(title_without_size)
    season = (
        int(episode_match.group("season"))
        if episode_match
        else int(season_match.group("season"))
        if season_match
        else None
    )
    episode = int(episode_match.group("episode")) if episode_match else None
    episode_end = None
    if episode_match and episode_match.group("extra"):
        extras = re.findall(r"E(\d{1,4})", episode_match.group("extra"), re.I)
        if extras:
            episode_end = int(extras[-1])

    year_match = _YEAR_RE.search(title_without_size)
    year = int(year_match.group(1)) if year_match else None
    resolution_match = _RESOLUTION_RE.search(title_without_size)
    resolution = None
    if resolution_match:
        value = resolution_match.group(1).upper()
        resolution = "2160p" if value in {"4K", "UHD"} else value.lower()

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

    marker = (
        episode_match.start()
        if episode_match
        else season_match.start()
        if season_match
        else year_match.start()
        if year_match
        else resolution_match.start()
        if resolution_match
        else len(title_without_size)
    )
    clean_title = re.sub(r"[._]+", " ", title_without_size[:marker])
    clean_title = re.sub(r"\s+", " ", clean_title).strip(" -.")
    return {
        "clean_title": clean_title,
        "year": year,
        "season": season,
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
