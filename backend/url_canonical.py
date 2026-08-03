"""The ONE home for URL identity — every canonicaliser lives or delegates here.

Why this module exists (canonical-URL inventory, 2026-08-03): the codebase
grew three canonicaliser implementations producing two different identity
forms, and the corpus measurement showed the split is total — every RSS-side
store keys on Form A, both measurement ledgers key on Form B, and a raw
string join across that boundary matches ZERO rows. The 0-of-100 shadow
incident was this exact failure. From here on, a new identity function may
only be added to this module, with a version constant, and every cross-form
join must go through a NAMED bridge defined here.

THE TWO FORMS (deliberately both kept — round-9 decision):

* **Form A — HDEncode post identity** (`canonicalize_hdencode_post_url`):
  https-only, host allowlisted and forced to bare ``hdencode.org``,
  duplicate slashes collapsed, trailing slash APPENDED, query+fragment
  dropped. This is the acquisition-population key: ``hdencode_candidates``
  and its FK tables, and (once bound) the sweep listing-ledger frontier.
* **Form B — listing/measurement identity** (`canonicalize_listing_url`):
  scheme/host lowercased, trailing slash STRIPPED, query+fragment dropped,
  host otherwise untouched (www kept). This is the key of
  ``listing_policy_exclusions`` and ``hdencode_shadow_misses``.

FEED identity is NOT here on purpose: feed URLs carry identity-bearing query
strings (``?tag=movies`` vs ``?tag=tv-shows`` are different feeds), so
neither form may ever be applied to a feed URL. Feeds are identified by
their configured URL string, verbatim.
"""
from typing import Optional
import re
from urllib.parse import urlsplit, urlunsplit

#: Version of the Form-A post identity. Stored alongside every sweep-ledger
#: row; bump it whenever the transform changes, and write the migration
#: policy for already-persisted keys in the same commit.
POST_IDENTITY_VERSION = "hdencode-post-v1"

#: Version of the Form-B listing/measurement identity.
LISTING_IDENTITY_VERSION = "listing-v1"

_ALLOWED_POST_HOSTS = {"hdencode.org", "www.hdencode.org"}


def canonicalize_hdencode_post_url(url) -> str:
    """Form A — the HDEncode acquisition-population identity.

    Raises ``ValueError`` on non-https or a foreign host: a post identity is
    only defined for HDEncode posts, and failing closed beats silently
    minting an identity in the wrong namespace.
    """
    parsed = urlsplit((url or "").strip())
    if parsed.scheme.lower() != "https":
        raise ValueError("RSS entry URL must be HTTPS")
    host = (parsed.hostname or "").lower().rstrip(".")
    if host not in _ALLOWED_POST_HOSTS:
        raise ValueError(f"RSS entry host is not approved: {host or '<missing>'}")
    path = re.sub(r"/+", "/", parsed.path or "/")
    if path != "/":
        path = path.rstrip("/") + "/"
    return urlunsplit(("https", "hdencode.org", path, "", ""))


def canonicalize_listing_url(url: Optional[str]) -> str:
    """Form B — the listing/measurement identity.

    Collapses trailing-slash, query, fragment and scheme/host case variance.
    Applied at the storage boundary of the policy-exclusion store and inside
    the shadow comparison; NOT applied to ``skip_urls`` (raw-vs-raw by
    design — see url_identity.py's scope-limit note).
    """
    if not url:
        return ""
    try:
        parts = urlsplit(str(url).strip())
        path = parts.path.rstrip("/") or "/"
        return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, "", ""))
    except Exception:
        return str(url).strip()


def post_to_listing_identity(post_identity: str) -> str:
    """THE named A→B bridge. The only sanctioned way to join a Form-A key
    (candidates, frontier) against a Form-B store (shadow misses, policy
    exclusions). Defined as Form B applied to the Form-A string, which for a
    valid post identity reduces to stripping the trailing slash."""
    return canonicalize_listing_url(post_identity)


def same_post(url_a, url_b) -> bool:
    """True when two HDEncode URLs (any spelling: raw href, Form A, Form B)
    denote the same post. Compares in Form-B space after best-effort
    normalisation, so it tolerates the www/host divergence between forms."""
    def _key(u):
        try:
            return post_to_listing_identity(canonicalize_hdencode_post_url(u))
        except ValueError:
            return canonicalize_listing_url(u)
    return _key(url_a) == _key(url_b) and canonicalize_listing_url(url_a) != ""
