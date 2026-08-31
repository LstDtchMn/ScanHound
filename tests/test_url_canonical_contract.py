"""Contract tests for URL identity (canonical-URL inventory §5.1, round 9).

These pin the TWO identity forms, the named A→B bridge, and the delegation of
every legacy canonicaliser to the shared module — so a drift in any copy is a
test failure, not a silent re-run of the 0-of-100 incident.
"""
import pytest

from backend.url_canonical import (
    LISTING_IDENTITY_VERSION,
    POST_IDENTITY_VERSION,
    canonicalize_hdencode_post_url,
    canonicalize_listing_url,
    post_to_listing_identity,
    same_post,
)


class TestTheTwoForms:
    def test_form_a_appends_slash_and_forces_bare_host(self):
        assert canonicalize_hdencode_post_url(
            "HTTPS://WWW.HDEncode.org/Foo/?utm=1#x") == "https://hdencode.org/Foo/"

    def test_form_b_strips_slash_and_keeps_host(self):
        assert canonicalize_listing_url(
            "HTTPS://WWW.HDEncode.org/Foo/?utm=1#x") == "https://www.hdencode.org/Foo"

    def test_form_a_collapses_duplicate_slashes_form_b_does_not(self):
        assert canonicalize_hdencode_post_url(
            "https://hdencode.org//a//b/") == "https://hdencode.org/a/b/"
        assert canonicalize_listing_url(
            "https://hdencode.org//a//b/") == "https://hdencode.org//a//b"

    def test_form_a_fails_closed_on_http_and_foreign_hosts(self):
        with pytest.raises(ValueError):
            canonicalize_hdencode_post_url("http://hdencode.org/x/")
        with pytest.raises(ValueError):
            canonicalize_hdencode_post_url("https://evil.example/x/")

    def test_version_constants_exist_and_are_distinct(self):
        assert POST_IDENTITY_VERSION == "hdencode-post-v1"
        assert LISTING_IDENTITY_VERSION == "listing-v1"
        assert POST_IDENTITY_VERSION != LISTING_IDENTITY_VERSION


class TestTheNamedBridge:
    def test_bridge_joins_candidates_to_shadow_ledger_form(self):
        """The measured schism: candidates key WITH slash, shadow misses
        WITHOUT. The bridge must map A→B exactly, or every cross-store join
        stays at the measured zero."""
        raw = "https://hdencode.org/some-release-2160p/"
        form_a = canonicalize_hdencode_post_url(raw)
        form_b = canonicalize_listing_url(raw)
        assert form_a == "https://hdencode.org/some-release-2160p/"
        assert form_b == "https://hdencode.org/some-release-2160p"
        assert post_to_listing_identity(form_a) == form_b

    def test_same_post_across_all_three_spellings(self):
        a = "https://hdencode.org/movie-2026-2160p/"
        assert same_post(a, "https://hdencode.org/movie-2026-2160p")   # B form
        assert same_post(a, "HTTPS://WWW.HDEncode.org/movie-2026-2160p/")  # raw variant
        assert not same_post(a, "https://hdencode.org/other-movie-2026/")  # negative control
        assert not same_post("", "")  # empty is never an identity


class TestDelegationNotCopies:
    """The two Form-B duplicates and the parser must be BACKED by the shared
    module — equal output on the documented divergence inputs, so a drift in
    any copy breaks here first."""

    CASES = [
        "https://hdencode.org/some-release-2160p/",
        "HTTPS://WWW.HDEncode.org/Foo/?utm=1#x",
        "https://hdencode.org//a//b/",
        "https://hdencode.org/x",
        "",
    ]

    def test_url_identity_delegates(self):
        from backend.url_identity import canonicalize_listing_url as legacy
        for case in self.CASES:
            assert legacy(case) == canonicalize_listing_url(case), case

    def test_shadow_delegates_for_absolute_urls(self):
        from backend.hdencode_shadow import canonical_url as shadow
        for case in [c for c in self.CASES if c.lower().startswith("http")]:
            assert shadow(case) == canonicalize_listing_url(case), case

    def test_feed_parser_delegates(self):
        from backend.sources.hdencode_feed_parser import canonicalize_post_url
        assert canonicalize_post_url("https://hdencode.org/z/") == \
            canonicalize_hdencode_post_url("https://hdencode.org/z/")
        assert canonicalize_post_url.__doc__ and "url_canonical" in canonicalize_post_url.__doc__


class TestFeedIdentityStaysSeparate:
    def test_applying_either_form_to_feed_urls_would_merge_distinct_feeds(self):
        """WHY feed identity is not in the shared module: these are two
        DIFFERENT feeds, and both forms erase the difference. This test is
        the standing reason nothing may 'helpfully' canonicalise a feed URL."""
        movies = "https://hdencode.org/quality/2160p/feed/?tag=movies"
        tv = "https://hdencode.org/quality/2160p/feed/?tag=tv-shows"
        assert movies != tv
        assert canonicalize_listing_url(movies) == canonicalize_listing_url(tv)
        assert canonicalize_hdencode_post_url(movies) == canonicalize_hdencode_post_url(tv)
