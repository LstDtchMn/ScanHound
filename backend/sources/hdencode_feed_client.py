"""Pinned-IP HTTPS client for qualified HDEncode RSS feeds."""
from __future__ import annotations

from dataclasses import dataclass
import gzip
import http.client
import ipaddress
import socket
import ssl
import urllib.parse

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


def _public_addresses(host: str, port: int) -> tuple[str, ...]:
    infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    addresses = []
    for info in infos:
        value = info[4][0]
        address = ipaddress.ip_address(value)
        if (
            address.is_private
            or address.is_loopback
            or address.is_link_local
            or address.is_multicast
            or address.is_reserved
            or address.is_unspecified
        ):
            raise ValueError(f"Unsafe HDEncode RSS address rejected: {address}")
        if value not in addresses:
            addresses.append(value)
    if not addresses:
        raise ValueError(f"HDEncode RSS host did not resolve: {host}")
    return tuple(addresses)


def _validated_target(url: str):
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme.lower() != "https":
        raise ValueError("HDEncode RSS requires HTTPS")
    host = (parsed.hostname or "").lower().rstrip(".")
    if host not in _ALLOWED_HOSTS:
        raise ValueError(f"Unapproved HDEncode RSS host: {host or '<missing>'}")
    port = parsed.port or 443
    if port != 443:
        raise ValueError("HDEncode RSS requires the standard HTTPS port")
    return parsed, host, port, _public_addresses(host, port)


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host, port, connect_address, *, timeout):
        super().__init__(
            host,
            port=port,
            timeout=timeout,
            context=ssl.create_default_context(),
        )
        self._connect_address = connect_address

    def connect(self):
        raw = socket.create_connection(
            (self._connect_address, self.port),
            self.timeout,
            self.source_address,
        )
        self.sock = self._context.wrap_socket(raw, server_hostname=self.host)


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

    def fetch(self, url: str, *, last_modified: str | None = None) -> FeedResponse:
        require_transport_authorization("rss")
        current = url
        redirects = []
        for _ in range(_MAX_REDIRECTS + 1):
            parsed, host, port, addresses = _validated_target(current)
            path = urllib.parse.urlunsplit(("", "", parsed.path or "/", parsed.query, ""))
            headers = {
                "User-Agent": _USER_AGENT,
                "Accept": "application/rss+xml, application/xml;q=0.9",
                "Accept-Encoding": "gzip",
                "Host": host,
            }
            if last_modified:
                headers["If-Modified-Since"] = last_modified

            last_error = None
            response = None
            connection = None
            for address in addresses:
                connection = _PinnedHTTPSConnection(
                    host, port, address, timeout=self.timeout
                )
                try:
                    connection.request("GET", path, headers=headers)
                    response = connection.getresponse()
                    break
                except OSError as exc:
                    last_error = exc
                    connection.close()
                    response = None
            if response is None:
                raise last_error or OSError("No approved RSS address was reachable")

            try:
                status = int(response.status)
                modified = response.getheader("Last-Modified") or last_modified
                if status == 304:
                    return FeedResponse(status, current, modified, b"", tuple(redirects))
                if status in {301, 302, 303, 307, 308}:
                    location = response.getheader("Location")
                    if not location:
                        raise ValueError("RSS redirect omitted Location")
                    next_url = urllib.parse.urljoin(current, location)
                    _validated_target(next_url)
                    redirects.append(next_url)
                    current = next_url
                    continue
                encoding = {
                    part.strip().lower()
                    for part in (response.getheader("Content-Encoding") or "").split(",")
                    if part.strip()
                }
                body = _read_limited(response, gzip_encoded="gzip" in encoding)
                return FeedResponse(status, current, modified, body, tuple(redirects))
            finally:
                connection.close()
        raise ValueError("RSS redirect limit exceeded")
