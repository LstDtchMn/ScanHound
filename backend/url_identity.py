"""Shared canonical identity for source URLs.

Lives in its own module so the crawler, the storage layer and (later) the RSS
side all resolve identity the same way. Canonicalising in only one caller made
the exclusion store canonical *by convention* rather than by construction, which
would break the moment a second writer appeared.
"""
from typing import Optional
from urllib.parse import urlsplit, urlunsplit


def canonicalize_listing_url(url: Optional[str]) -> str:
    """Stable identity for a listing post.

    Collapses trailing-slash, query, fragment and scheme/host case variance so
    the same release cannot occupy two rows or read as new again under a
    variant.

    SCOPE LIMIT, deliberate: this is the identity for the policy-exclusion store
    only. It is NOT applied to the ordinary ``skip_urls`` comparison, because
    ``background_scan_cache`` and ``scanned_urls`` hold raw hrefs and
    canonicalising one side of that comparison would silently miss every
    existing cache entry and re-scrape the whole catalogue. Canonicalising
    everywhere is a larger change that must migrate those stores in the same
    commit.
    """
    if not url:
        return ""
    try:
        parts = urlsplit(str(url).strip())
        path = parts.path.rstrip("/") or "/"
        return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, "", ""))
    except Exception:
        return str(url).strip()
