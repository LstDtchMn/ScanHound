"""One hostname registry and one source classifier, shared by every consumer.

WHY THIS MODULE EXISTS. Peer review round 5 found two classifiers deciding "what
source is this?" independently, and they had already drifted apart:

* `download_queue._source()` -- decided the durable queue row's `source`, which
  drives active uniqueness, batch source, sibling pause scoping, resume ownership
  and the HDEncode retry-budget refund.
* `download_service._source_page_kind()` -- decided coordinator use, off-switch
  routing, dispatch and scrape-outcome ownership.

Both originally defaulted EVERYTHING that was not DDLBase or Adit-HD to
``"hdencode"``, so Rapidgator, 1fichier, Nitroflare, ddownload and any future host
were treated as HDEncode. Round 4 fixed that on the queue side only, which left the
two disagreeing -- and their host lists disagreed too: the queue knew about Katfile,
Turbobit, Hitfile, Fikper and Mega while the service's supported-host tuple listed
only four.

Two registries answering the same question is how that drift happened, so there is
now one.

WHAT IS DELIBERATELY *NOT* HERE. A per-host identity (`filehost:rapidgator.net`
rather than a single `filehost`) would scope source-wide pause more precisely, and
the reviewer suggested it. It is not implemented because nothing consumes it yet and
it would change the values stored in `download_queue_items.source` -- which the
active unique index `(source, canonical_url, service_type)` is built on. Adding an
unconsumed identity scheme would repeat the "signal nothing reads" mistake that has
appeared in three separate review rounds.
"""
from __future__ import annotations

from typing import Iterable, Sequence
from urllib.parse import urlparse

#: The operator-configurable HDEncode host, used when no config value is supplied.
DEFAULT_HDENCODE_HOST = "hdencode.org"

DDLBASE_HOST = "ddlbase.com"
ADITHD_HOST = "adit-hd.com"

#: Hosts that appear as DIRECT download links rather than source pages. A batch may
#: legitimately contain one of these instead of a release page, and it is not
#: HDEncode. This is the authoritative identity list; see
#: `download_service._SUPPORTED_DOWNLOAD_HOSTS` for the narrower question of which
#: of them the downloader can actually hand off, and the test asserting that the
#: support list stays a subset of this one so they cannot drift again.
DIRECT_FILE_HOSTS: Sequence[str] = (
    "rapidgator.net",
    "1fichier.com",
    "nitroflare.com",
    "ddownload.com",
    "katfile.com",
    "turbobit.net",
    "hitfile.net",
    "fikper.com",
    "frdl.io",
    "uploady.io",
    "filestore.to",
    "clicknupload.to",
    "mega.nz",
)

#: Every value `source_kind()` can return. Consumers that branch on identity should
#: be checked against this set when a new kind is added.
SOURCE_KINDS = ("hdencode", "ddlbase", "adithd", "direct_file", "other")


def host_of(url: str) -> str:
    """A URL's registrable hostname, lowercased, without port or credentials.

    Path and query text must never influence source routing -- a URL like
    ``https://evil.example/?next=https://ddlbase.com`` is not DDLBase.
    """
    try:
        raw = (url or "").strip()
        if not raw:
            return ""
        parsed = urlparse(raw if "://" in raw else "https://" + raw)
        return (parsed.hostname or "").lower().rstrip(".")
    except Exception:
        return ""


def host_matches(host: str, domain: str) -> bool:
    """Whether ``host`` is ``domain`` or a subdomain of it."""
    domain = (domain or "").lower().rstrip(".")
    if not domain or not host:
        return False
    return host == domain or host.endswith("." + domain)


def url_matches_domain(url: str, domains: Iterable[str]) -> bool:
    """Whether a URL's hostname matches any of ``domains``."""
    host = host_of(url)
    return any(host_matches(host, d) for d in domains)


def source_kind(url: str, hdencode_host: str = DEFAULT_HDENCODE_HOST) -> str:
    """Classify a URL's source AFFIRMATIVELY. Never defaults to HDEncode.

    ``hdencode_host`` accepts a bare domain or a full base URL, so callers can pass
    the operator's configured ``base_url`` directly and a mirror or changed domain
    classifies correctly instead of falling through.

    Returns one of :data:`SOURCE_KINDS`.
    """
    host = host_of(url)
    if not host:
        return "other"
    if host_matches(host, DDLBASE_HOST):
        return "ddlbase"
    if host_matches(host, ADITHD_HOST):
        return "adithd"
    configured = host_of(hdencode_host) or (hdencode_host or "").strip().lower()
    if host_matches(host, configured):
        return "hdencode"
    if any(host_matches(host, d) for d in DIRECT_FILE_HOSTS):
        return "direct_file"
    return "other"
