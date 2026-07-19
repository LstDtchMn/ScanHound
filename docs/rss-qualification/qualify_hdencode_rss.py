#!/usr/bin/env python3
"""
Read-only HDEncode RSS qualification probe for ScanHound.

Purpose:
  * Verify the live HTTP behavior and raw RSS metadata of HDEncode's official
    feeds before ScanHound's RSS-first discovery schema is finalized.
  * Produce JSON and Markdown reports without changing ScanHound configuration,
    database contents, or production behavior.

Safety:
  * Fixed HTTPS feed allowlist only.
  * Validates every redirect and resolved IP.
  * Rejects private, loopback, link-local, multicast, reserved, or unspecified IPs.
  * Requests feeds serially with a configurable delay.
  * Uses a 2 MiB body cap and rejects DTD/entity declarations.
  * Does not retrieve article pages or download links.
"""

from __future__ import annotations

import argparse
import collections
import datetime as dt
import email.utils
import gzip
import hashlib
import ipaddress
import json
import re
import socket
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable

ALLOWED_HOSTS = {
    "hdencode.org",
    "www.hdencode.org",
    "hdencode.com",
    "www.hdencode.com",
    "hdencode.ro",
    "www.hdencode.ro",
}

FEEDS: tuple[tuple[str, str], ...] = (
    ("home", "https://hdencode.org/feed/"),
    ("movies_all", "https://hdencode.org/tag/movies/feed/"),
    ("tv_all", "https://hdencode.org/tag/tv-shows/feed/"),
    ("movies_2160p", "https://hdencode.org/quality/2160p/feed/?tag=movies"),
    ("movies_1080p", "https://hdencode.org/quality/1080p/feed/?tag=movies"),
    ("movies_720p", "https://hdencode.org/quality/720p/feed/?tag=movies"),
    ("movies_remux", "https://hdencode.org/quality/remux/feed/?tag=movies"),
    ("movies_bluray_disc", "https://hdencode.org/quality/full-blu-ray-disc/feed/?tag=movies"),
    ("tv_2160p", "https://hdencode.org/quality/2160p/feed/?tag=tv-shows"),
    ("tv_1080p", "https://hdencode.org/quality/1080p/feed/?tag=tv-shows"),
    ("tv_720p", "https://hdencode.org/quality/720p/feed/?tag=tv-shows"),
    ("tv_webdl", "https://hdencode.org/quality/web-dl/feed/?tag=tv-shows"),
    ("tv_webrip", "https://hdencode.org/quality/webrip/feed/?tag=tv-shows"),
)

NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "content": "http://purl.org/rss/1.0/modules/content/",
    "dc": "http://purl.org/dc/elements/1.1/",
}

SIGNALS: dict[str, re.Pattern[str]] = {
    "title_year": re.compile(r"\b(?:19|20)\d{2}\b", re.I),
    "title_season": re.compile(r"\bS\d{1,3}\b", re.I),
    "title_episode": re.compile(r"\bE\d{1,4}(?:E\d{1,4})?\b", re.I),
    "title_resolution": re.compile(r"\b(?:2160p|1080p|720p|4K)\b", re.I),
    "title_size": re.compile(r"\b\d+(?:\.\d+)?\s*(?:GB|GiB|MB|MiB)\b", re.I),
    "title_dv": re.compile(r"\b(?:DV|DoVi|Dolby[ ._-]?Vision)\b", re.I),
    "title_hdr": re.compile(r"\b(?:HDR|HDR10\+?|HDR10P|HLG)\b", re.I),
    "title_hevc": re.compile(r"\b(?:HEVC|H\.?265|x265)\b", re.I),
    "body_filename": re.compile(r"\bFilename\b", re.I),
    "body_filesize": re.compile(r"\bFileSize\b", re.I),
    "body_duration": re.compile(r"\bDuration\b", re.I),
    "body_video_codec": re.compile(r"\bVideo\s+Codec\b", re.I),
    "body_resolution": re.compile(r"\bResolution\b", re.I),
    "body_frame_rate": re.compile(r"\bFrame\s+rate\b", re.I),
    "body_color_primaries": re.compile(r"\bColor\s+primaries\b", re.I),
    "body_audio": re.compile(r"\b(?:Audio|Channels|Format)\b", re.I),
    "body_subtitle": re.compile(r"\bSubtitle\b", re.I),
    "body_dv": re.compile(r"\b(?:Dolby\s+Vision|DoVi)\b", re.I),
    "body_hdr": re.compile(r"\b(?:HDR10\+?|HDR10P|HLG)\b", re.I),
}

MAX_BODY_BYTES_DEFAULT = 2 * 1024 * 1024
USER_AGENT = "ScanHound-RSS-Qualification/1.0 (+read-only metadata probe)"


@dataclass
class RedirectRecord:
    status: int
    source_url: str
    target_url: str


@dataclass
class EntrySummary:
    title: str | None
    link: str | None
    canonical_link: str | None
    guid: str | None
    guid_is_permalink: str | None
    pub_date: str | None
    parsed_pub_date: str | None
    author: str | None
    categories: list[str]
    description_length: int
    content_encoded_length: int
    body_source: str
    body_length: int
    signals: dict[str, bool]


@dataclass
class FeedReport:
    key: str
    requested_url: str
    status: int | None = None
    final_url: str | None = None
    redirect_chain: list[RedirectRecord] = field(default_factory=list)
    headers: dict[str, str | None] = field(default_factory=dict)
    body_bytes: int = 0
    body_sha256: str | None = None
    xml_root: str | None = None
    rss_version: str | None = None
    feed_type: str | None = None
    channel: dict[str, Any] = field(default_factory=dict)
    entry_count: int = 0
    coverage: dict[str, Any] = field(default_factory=dict)
    conditional_request: dict[str, Any] = field(default_factory=dict)
    samples: list[EntrySummary] = field(default_factory=list)
    all_entries: list[EntrySummary] = field(default_factory=list, repr=False)
    error: str | None = None


def validate_host_and_ip(url: str) -> None:
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme.lower() != "https":
        raise ValueError(f"Non-HTTPS URL rejected: {url}")
    host = (parsed.hostname or "").lower()
    if host not in ALLOWED_HOSTS:
        raise ValueError(f"Unapproved host rejected: {host or '<missing>'}")

    infos = socket.getaddrinfo(host, parsed.port or 443, type=socket.SOCK_STREAM)
    if not infos:
        raise ValueError(f"Host did not resolve: {host}")
    for info in infos:
        ip_text = info[4][0]
        ip = ipaddress.ip_address(ip_text)
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_multicast
            or ip.is_reserved
            or ip.is_unspecified
        ):
            raise ValueError(f"Unsafe resolved address rejected for {host}: {ip}")


class SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    def __init__(self, records: list[RedirectRecord], max_redirects: int = 3):
        super().__init__()
        self.records = records
        self.max_redirects = max_redirects

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if len(self.records) >= self.max_redirects:
            raise urllib.error.HTTPError(
                req.full_url, code, "Too many redirects", headers, fp
            )
        absolute = urllib.parse.urljoin(req.full_url, newurl)
        validate_host_and_ip(absolute)
        self.records.append(
            RedirectRecord(status=code, source_url=req.full_url, target_url=absolute)
        )
        return super().redirect_request(req, fp, code, msg, headers, absolute)


def build_opener(records: list[RedirectRecord]) -> urllib.request.OpenerDirector:
    context = ssl.create_default_context()
    return urllib.request.build_opener(
        urllib.request.HTTPSHandler(context=context),
        SafeRedirectHandler(records),
    )


def read_capped(response, max_bytes: int) -> bytes:
    encoding = (response.headers.get("Content-Encoding") or "").lower().strip()
    raw = response.read(max_bytes + 1)
    if len(raw) > max_bytes:
        raise ValueError(f"Response exceeds {max_bytes} byte limit")
    if encoding == "gzip":
        data = gzip.decompress(raw)
        if len(data) > max_bytes:
            raise ValueError(f"Decompressed response exceeds {max_bytes} byte limit")
        return data
    if encoding not in {"", "identity"}:
        raise ValueError(f"Unsupported Content-Encoding: {encoding}")
    return raw


def request_feed(
    url: str,
    *,
    timeout: float,
    max_bytes: int,
    validators: dict[str, str] | None = None,
) -> tuple[int, str, list[RedirectRecord], dict[str, str], bytes]:
    validate_host_and_ip(url)
    records: list[RedirectRecord] = []
    opener = build_opener(records)
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml;q=0.9",
        "Accept-Encoding": "identity",
        "Connection": "close",
    }
    if validators:
        headers.update(validators)
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with opener.open(req, timeout=timeout) as response:
            data = read_capped(response, max_bytes)
            return (
                int(response.status),
                response.geturl(),
                records,
                {k.lower(): v for k, v in response.headers.items()},
                data,
            )
    except urllib.error.HTTPError as exc:
        if exc.code == 304:
            return (
                304,
                exc.geturl(),
                records,
                {k.lower(): v for k, v in exc.headers.items()},
                b"",
            )
        body = exc.read(min(max_bytes, 8192))
        raise RuntimeError(
            f"HTTP {exc.code} for {url}; response prefix={body[:200]!r}"
        ) from exc


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def text_of(parent: ET.Element | None, path: str) -> str | None:
    if parent is None:
        return None
    node = parent.find(path, NS)
    if node is None or node.text is None:
        return None
    value = node.text.strip()
    return value or None


def parse_date(value: str | None) -> str | None:
    if not value:
        return None
    try:
        parsed = email.utils.parsedate_to_datetime(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.timezone.utc)
        return parsed.astimezone(dt.timezone.utc).isoformat()
    except Exception:
        try:
            parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=dt.timezone.utc)
            return parsed.astimezone(dt.timezone.utc).isoformat()
        except Exception:
            return None


def canonicalize_entry_url(url: str | None) -> str | None:
    if not url:
        return None
    try:
        parsed = urllib.parse.urlsplit(url.strip())
    except Exception:
        return None
    host = (parsed.hostname or "").lower()
    if host not in ALLOWED_HOSTS:
        return None
    path = re.sub(r"/+", "/", parsed.path or "/")
    if path != "/":
        path = path.rstrip("/") + "/"
    # Feed post links should not need query state for identity. Keep only a
    # conservative allowlist if HDEncode ever introduces a meaningful identifier.
    query_pairs = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    query = urllib.parse.urlencode(
        [(k, v) for k, v in query_pairs if k.lower() in {"p", "post"}]
    )
    return urllib.parse.urlunsplit(("https", "hdencode.org", path, query, ""))


def safe_xml_parse(data: bytes) -> ET.Element:
    prefix = data[:4096].upper()
    if b"<!DOCTYPE" in prefix or b"<!ENTITY" in prefix:
        raise ValueError("DTD/entity declaration rejected")
    parser = ET.XMLParser()
    return ET.fromstring(data, parser=parser)


def entry_body(description: str | None, content_encoded: str | None) -> tuple[str, str]:
    if content_encoded and len(content_encoded) >= len(description or ""):
        return content_encoded, "content:encoded"
    return description or "", "description" if description else "none"


def summarize_entry(node: ET.Element, feed_type: str) -> EntrySummary:
    if feed_type == "atom":
        title = text_of(node, "atom:title") or text_of(node, "title")
        link = None
        for link_node in node.findall("atom:link", NS) + node.findall("link"):
            rel = (link_node.attrib.get("rel") or "alternate").lower()
            href = link_node.attrib.get("href")
            if href and rel in {"alternate", ""}:
                link = href.strip()
                break
        guid = text_of(node, "atom:id") or text_of(node, "id")
        guid_is_permalink = None
        pub_date = (
            text_of(node, "atom:published")
            or text_of(node, "atom:updated")
            or text_of(node, "published")
            or text_of(node, "updated")
        )
        author = text_of(node, "atom:author/atom:name") or text_of(node, "author/name")
        categories = [
            (item.attrib.get("term") or (item.text or "")).strip()
            for item in node.findall("atom:category", NS) + node.findall("category")
        ]
        description = text_of(node, "atom:summary") or text_of(node, "summary")
        content_encoded = text_of(node, "atom:content") or text_of(node, "content")
    else:
        title = text_of(node, "title")
        link = text_of(node, "link")
        guid_node = node.find("guid")
        guid = guid_node.text.strip() if guid_node is not None and guid_node.text else None
        guid_is_permalink = (
            guid_node.attrib.get("isPermaLink") if guid_node is not None else None
        )
        pub_date = text_of(node, "pubDate")
        author = text_of(node, "dc:creator") or text_of(node, "author")
        categories = [
            (item.text or "").strip()
            for item in node.findall("category")
            if (item.text or "").strip()
        ]
        description = text_of(node, "description")
        content_encoded = text_of(node, "content:encoded")

    body, body_source = entry_body(description, content_encoded)
    combined_title = title or ""
    signals = {
        name: bool(pattern.search(combined_title if name.startswith("title_") else body))
        for name, pattern in SIGNALS.items()
    }
    return EntrySummary(
        title=title,
        link=link,
        canonical_link=canonicalize_entry_url(link),
        guid=guid,
        guid_is_permalink=guid_is_permalink,
        pub_date=pub_date,
        parsed_pub_date=parse_date(pub_date),
        author=author,
        categories=categories,
        description_length=len(description or ""),
        content_encoded_length=len(content_encoded or ""),
        body_source=body_source,
        body_length=len(body),
        signals=signals,
    )


def count_coverage(entries: list[EntrySummary]) -> dict[str, Any]:
    total = len(entries)
    def count(predicate) -> int:
        return sum(1 for entry in entries if predicate(entry))

    coverage: dict[str, Any] = {
        "total": total,
        "title": count(lambda e: bool(e.title)),
        "link": count(lambda e: bool(e.link)),
        "approved_canonical_link": count(lambda e: bool(e.canonical_link)),
        "guid": count(lambda e: bool(e.guid)),
        "pub_date": count(lambda e: bool(e.pub_date)),
        "parseable_pub_date": count(lambda e: bool(e.parsed_pub_date)),
        "author": count(lambda e: bool(e.author)),
        "categories": count(lambda e: bool(e.categories)),
        "description": count(lambda e: e.description_length > 0),
        "content_encoded": count(lambda e: e.content_encoded_length > 0),
        "nonempty_body": count(lambda e: e.body_length > 0),
        "body_source": dict(collections.Counter(e.body_source for e in entries)),
        "duplicate_guid_count": 0,
        "duplicate_canonical_link_count": 0,
        "signal_counts": {
            name: sum(1 for e in entries if e.signals.get(name))
            for name in SIGNALS
        },
    }
    guid_counts = collections.Counter(e.guid for e in entries if e.guid)
    link_counts = collections.Counter(e.canonical_link for e in entries if e.canonical_link)
    coverage["duplicate_guid_count"] = sum(v - 1 for v in guid_counts.values() if v > 1)
    coverage["duplicate_canonical_link_count"] = sum(
        v - 1 for v in link_counts.values() if v > 1
    )
    parsed_dates = sorted(e.parsed_pub_date for e in entries if e.parsed_pub_date)
    coverage["oldest_entry_at"] = parsed_dates[0] if parsed_dates else None
    coverage["newest_entry_at"] = parsed_dates[-1] if parsed_dates else None
    if total:
        coverage["percentages"] = {
            key: round(value * 100.0 / total, 1)
            for key, value in coverage.items()
            if isinstance(value, int) and key not in {
                "total", "duplicate_guid_count", "duplicate_canonical_link_count"
            }
        }
        coverage["signal_percentages"] = {
            key: round(value * 100.0 / total, 1)
            for key, value in coverage["signal_counts"].items()
        }
    return coverage


def parse_feed(report: FeedReport, data: bytes) -> None:
    root = safe_xml_parse(data)
    report.xml_root = root.tag
    name = local_name(root.tag).lower()
    if name == "rss":
        report.feed_type = "rss"
        report.rss_version = root.attrib.get("version")
        channel = root.find("channel")
        report.channel = {
            "title": text_of(channel, "title"),
            "link": text_of(channel, "link"),
            "description": text_of(channel, "description"),
            "language": text_of(channel, "language"),
            "last_build_date": text_of(channel, "lastBuildDate"),
            "parsed_last_build_date": parse_date(text_of(channel, "lastBuildDate")),
            "generator": text_of(channel, "generator"),
            "ttl": text_of(channel, "ttl"),
            "atom_self_links": [
                node.attrib.get("href")
                for node in (channel.findall("atom:link", NS) if channel is not None else [])
                if (node.attrib.get("rel") or "").lower() == "self"
            ],
        }
        item_nodes = channel.findall("item") if channel is not None else []
    elif name == "feed":
        report.feed_type = "atom"
        report.channel = {
            "title": text_of(root, "atom:title") or text_of(root, "title"),
            "link": next(
                (
                    node.attrib.get("href")
                    for node in root.findall("atom:link", NS) + root.findall("link")
                    if node.attrib.get("href")
                    and (node.attrib.get("rel") or "alternate").lower() == "alternate"
                ),
                None,
            ),
            "description": text_of(root, "atom:subtitle") or text_of(root, "subtitle"),
            "language": root.attrib.get("{http://www.w3.org/XML/1998/namespace}lang"),
            "last_build_date": text_of(root, "atom:updated") or text_of(root, "updated"),
            "parsed_last_build_date": parse_date(
                text_of(root, "atom:updated") or text_of(root, "updated")
            ),
            "generator": text_of(root, "atom:generator") or text_of(root, "generator"),
            "ttl": None,
            "atom_self_links": [
                node.attrib.get("href")
                for node in root.findall("atom:link", NS) + root.findall("link")
                if (node.attrib.get("rel") or "").lower() == "self"
            ],
        }
        item_nodes = root.findall("atom:entry", NS) + root.findall("entry")
    else:
        raise ValueError(f"Unsupported XML root: {root.tag}")

    entries = [summarize_entry(node, report.feed_type) for node in item_nodes]
    report.entry_count = len(entries)
    report.coverage = count_coverage(entries)
    report.samples = entries[:5]
    report.all_entries = entries


def selected_headers(headers: dict[str, str]) -> dict[str, str | None]:
    wanted = (
        "content-type",
        "content-length",
        "content-encoding",
        "etag",
        "last-modified",
        "cache-control",
        "expires",
        "age",
        "date",
        "server",
        "vary",
    )
    return {name: headers.get(name) for name in wanted}


def qualify_feed(
    key: str,
    url: str,
    *,
    timeout: float,
    max_bytes: int,
) -> FeedReport:
    report = FeedReport(key=key, requested_url=url)
    try:
        status, final_url, redirects, headers, data = request_feed(
            url, timeout=timeout, max_bytes=max_bytes
        )
        report.status = status
        report.final_url = final_url
        report.redirect_chain = redirects
        report.headers = selected_headers(headers)
        report.body_bytes = len(data)
        report.body_sha256 = hashlib.sha256(data).hexdigest()
        if status != 200:
            raise ValueError(f"Unexpected initial status: {status}")
        parse_feed(report, data)

        validators: dict[str, str] = {}
        if headers.get("etag"):
            validators["If-None-Match"] = headers["etag"]
        if headers.get("last-modified"):
            validators["If-Modified-Since"] = headers["last-modified"]

        if validators:
            second_status, second_final, second_redirects, second_headers, second_data = (
                request_feed(
                    url,
                    timeout=timeout,
                    max_bytes=max_bytes,
                    validators=validators,
                )
            )
            report.conditional_request = {
                "sent": validators,
                "status": second_status,
                "final_url": second_final,
                "redirect_chain": [asdict(item) for item in second_redirects],
                "headers": selected_headers(second_headers),
                "body_bytes": len(second_data),
                "not_modified": second_status == 304,
            }
        else:
            report.conditional_request = {
                "sent": {},
                "status": None,
                "not_modified": None,
                "reason": "No ETag or Last-Modified header supplied",
            }
    except Exception as exc:
        report.error = f"{type(exc).__name__}: {exc}"
    return report


def aggregate(reports: list[FeedReport]) -> dict[str, Any]:
    link_memberships: dict[str, list[str]] = collections.defaultdict(list)
    guid_memberships: dict[str, list[str]] = collections.defaultdict(list)
    for report in reports:
        for entry in report.all_entries:
            if entry.canonical_link:
                link_memberships[entry.canonical_link].append(report.key)
            if entry.guid:
                guid_memberships[entry.guid].append(report.key)

    cross_feed_links = {
        link: sorted(set(keys))
        for link, keys in link_memberships.items()
        if len(set(keys)) > 1
    }
    cross_feed_guids = {
        guid: sorted(set(keys))
        for guid, keys in guid_memberships.items()
        if len(set(keys)) > 1
    }
    successful = [r for r in reports if not r.error]
    return {
        "feeds_requested": len(reports),
        "feeds_successful": len(successful),
        "feeds_failed": len(reports) - len(successful),
        "entries_seen": sum(r.entry_count for r in successful),
        "feeds_with_etag": sum(1 for r in successful if r.headers.get("etag")),
        "feeds_with_last_modified": sum(
            1 for r in successful if r.headers.get("last-modified")
        ),
        "feeds_returning_304": sum(
            1 for r in successful
            if r.conditional_request.get("status") == 304
        ),
        "cross_feed_duplicate_link_count": len(cross_feed_links),
        "cross_feed_duplicate_guid_count": len(cross_feed_guids),
        "cross_feed_duplicate_link_samples": dict(
            list(sorted(cross_feed_links.items()))[:20]
        ),
        "cross_feed_duplicate_guid_samples": dict(
            list(sorted(cross_feed_guids.items()))[:20]
        ),
    }


def markdown_report(payload: dict[str, Any]) -> str:
    aggregate_data = payload["aggregate"]
    lines = [
        "# HDEncode RSS qualification report",
        "",
        f"Generated: `{payload['generated_at']}`",
        "",
        "## Summary",
        "",
        "| Metric | Value |",
        "|---|---:|",
    ]
    for key, value in aggregate_data.items():
        if isinstance(value, (dict, list)):
            continue
        lines.append(f"| {key.replace('_', ' ')} | {value} |")

    lines += [
        "",
        "## Feed results",
        "",
        "| Feed | Status | Entries | ETag | Last-Modified | Conditional | Error |",
        "|---|---:|---:|:---:|:---:|:---:|---|",
    ]
    for report in payload["feeds"]:
        conditional = report["conditional_request"].get("status")
        lines.append(
            "| {key} | {status} | {entry_count} | {etag} | {modified} | "
            "{conditional} | {error} |".format(
                key=report["key"],
                status=report["status"] if report["status"] is not None else "—",
                entry_count=report["entry_count"],
                etag="yes" if report["headers"].get("etag") else "no",
                modified="yes" if report["headers"].get("last-modified") else "no",
                conditional=conditional if conditional is not None else "—",
                error=(report["error"] or "").replace("|", "\\|"),
            )
        )

    for report in payload["feeds"]:
        lines += [
            "",
            f"## {report['key']}",
            "",
            f"- Requested: `{report['requested_url']}`",
            f"- Final: `{report['final_url']}`",
            f"- Status: `{report['status']}`",
            f"- Type: `{report['feed_type']}` / RSS version `{report['rss_version']}`",
            f"- Entries: `{report['entry_count']}`",
            f"- Error: `{report['error']}`",
            "",
            "### Channel",
            "",
            "```json",
            json.dumps(report["channel"], indent=2, ensure_ascii=False),
            "```",
            "",
            "### Headers",
            "",
            "```json",
            json.dumps(report["headers"], indent=2, ensure_ascii=False),
            "```",
            "",
            "### Coverage",
            "",
            "```json",
            json.dumps(report["coverage"], indent=2, ensure_ascii=False),
            "```",
            "",
            "### Sample entries",
            "",
            "```json",
            json.dumps(report["samples"], indent=2, ensure_ascii=False),
            "```",
        ]
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output-dir",
        default=".",
        help="Directory for JSON and Markdown reports",
    )
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument("--delay", type=float, default=2.0)
    parser.add_argument("--max-bytes", type=int, default=MAX_BODY_BYTES_DEFAULT)
    parser.add_argument(
        "--only",
        action="append",
        default=[],
        help="Run only the named feed key; repeatable",
    )
    args = parser.parse_args()

    selected = FEEDS
    if args.only:
        wanted = set(args.only)
        unknown = wanted - {key for key, _ in FEEDS}
        if unknown:
            parser.error(f"Unknown feed key(s): {', '.join(sorted(unknown))}")
        selected = tuple(item for item in FEEDS if item[0] in wanted)

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    reports: list[FeedReport] = []
    for index, (key, url) in enumerate(selected):
        print(f"[{index + 1}/{len(selected)}] qualifying {key}: {url}", flush=True)
        report = qualify_feed(
            key,
            url,
            timeout=args.timeout,
            max_bytes=args.max_bytes,
        )
        reports.append(report)
        print(
            f"  status={report.status} entries={report.entry_count} "
            f"error={report.error or 'none'}",
            flush=True,
        )
        if index + 1 < len(selected):
            time.sleep(max(0.0, args.delay))

    payload = {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "probe_version": 1,
        "feeds": [
            {
                **{
                    key: value
                    for key, value in asdict(report).items()
                    if key != "all_entries"
                }
            }
            for report in reports
        ],
        "aggregate": aggregate(reports),
    }

    json_path = output_dir / "hdencode_rss_qualification.json"
    md_path = output_dir / "hdencode_rss_qualification.md"
    json_path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    md_path.write_text(markdown_report(payload), encoding="utf-8")

    print(f"Wrote {json_path}", flush=True)
    print(f"Wrote {md_path}", flush=True)
    return 0 if all(report.error is None for report in reports) else 2


if __name__ == "__main__":
    raise SystemExit(main())
