"""Secure conditional RSS client for the qualified HDEncode feeds."""
from __future__ import annotations

from dataclasses import dataclass
import gzip
import ipaddress
import socket
import urllib.error
import urllib.parse
import urllib.request

from backend.hdencode_coordinator import require_transport_authorization
from backend.sources.hdencode_feed_parser import MAX_FEED_BYTES


_ALLOWED_HOSTS = {"hdencode.org", "www.hdencode.org"}
_USER_AGENT = "ScanHound-RSS/1.0"
_MAX_REDIRECTS = 3
_READ_CHUNK = 64 * 1024


@dataclass(frozen=True)
class FeedResponse:
    status: int
    final_url: str
    last_modified: str | None
    body: bytes
    redirects: tuple[str, ...] = ()


def validate_feed_url(url: str) -> None:
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme.lower() != "https":
        raise ValueError("HDEncode RSS requires HTTPS")
    host = (parsed.hostname or "").lower().rstrip(".")
    if host not in _ALLOWED_HOSTS:
        raise ValueError(f"Unapproved HDEncode RSS host: {host or '<missing>'}")
    infos = socket.getaddrinfo(
        host,
        parsed.port or 443,
        type=socket.SOCK_STREAM,
    )
    if not infos:
        raise ValueError(f"HDEncode RSS host did not resolve: {host}")
    for info in infos:
        address = ipaddress.ip_address(info[4][0])
        if (
            address.is_private
            or address.is_loopback
            or address.is_link_local
            or address.is_multicast
            or address.is_reserved
            or address.is_unspecified
        ):
            raise ValueError(f"Unsafe HDEncode RSS address rejected: {address}")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _read_limited(stream, *, gzip_encoded: bool) -> bytes:
    source = gzip.GzipFile(fileobj=stream) if gzip_encoded else stream
    data = bytearray()
    while True:
        remaining = MAX_FEED_BYTES + 1 - len(data)
        if remaining <= 0:
            raise ValueError("RSS response exceeds the 2 MiB limit")
        chunk = source.read(min(_READ_CHUNK, remaining))
        if not chunk:
            break
        data.extend(chunk)
        if len(data) > MAX_FEED_BYTES:
            raise ValueError("RSS response exceeds the 2 MiB limit")
    return bytes(data)


class HDEncodeFeedClient:
    def __init__(self, *, timeout: float = 20.0):
        self.timeout = float(timeout)
        self._opener = urllib.request.build_opener(_NoRedirect)

    def fetch(self, url: str, *, last_modified: str | None = None) -> FeedResponse:
        require_transport_authorization("rss")
        current = url
        redirects = []

        for _ in range(_MAX_REDIRECTS + 1):
            validate_feed_url(current)
            headers = {
                "User-Agent": _USER_AGENT,
                "Accept": "application/rss+xml, application/xml;q=0.9",
                "Accept-Encoding": "gzip",
            }
            if last_modified:
                headers["If-Modified-Since"] = last_modified
            request = urllib.request.Request(current, headers=headers)

            try:
                response = self._opener.open(request, timeout=self.timeout)
            except urllib.error.HTTPError as exc:
                if exc.code == 304:
                    return FeedResponse(
                        status=304,
                        final_url=current,
                        last_modified=(
                            exc.headers.get("Last-Modified") or last_modified
                        ),
                        body=b"",
                        redirects=tuple(redirects),
                    )
                if exc.code in {301, 302, 303, 307, 308}:
                    location = exc.headers.get("Location")
                    if not location:
                        raise ValueError("RSS redirect omitted Location") from exc
                    next_url = urllib.parse.urljoin(current, location)
                    validate_feed_url(next_url)
                    redirects.append(next_url)
                    current = next_url
                    continue
                return FeedResponse(
                    status=int(exc.code),
                    final_url=current,
                    last_modified=exc.headers.get("Last-Modified"),
                    body=b"",
                    redirects=tuple(redirects),
                )

            with response:
                status = int(response.getcode())
                final_url = response.geturl()
                validate_feed_url(final_url)
                content_encoding = (
                    response.headers.get("Content-Encoding") or ""
                ).lower()
                encodings = {
                    part.strip()
                    for part in content_encoding.split(",")
                    if part.strip()
                }
                body = _read_limited(
                    response,
                    gzip_encoded="gzip" in encodings,
                )
                return FeedResponse(
                    status=status,
                    final_url=final_url,
                    last_modified=response.headers.get("Last-Modified"),
                    body=body,
                    redirects=tuple(redirects),
                )

        raise ValueError("RSS redirect limit exceeded")
