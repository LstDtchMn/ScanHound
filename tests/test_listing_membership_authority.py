"""A failed detail scrape must not look like proof of acquisition.

THE WRONG ANSWER THIS FIXES, found by peer review in round 5. It is the only finding
across five rounds that produces an incorrect RESULT rather than a policy or hygiene
problem.

`_process_posts()` returns None when `scrape_details()` yields nothing or throws
(`scanner_service.py:1041-1049`), so that URL is dropped from the item list. But
`compare_shadow` was fed that detail-processed list. So:

    release U is on the raw listing page
    U is also in the RSS feed
    every listing page fetched cleanly  -> listing authority granted
    U's detail scrape fails             -> U absent from listing_items
    compare_shadow sees U as feed_only
    the miss resolver reads feed_only as AFFIRMATIVE ACQUISITION

A real miss resolves as acquired. The certifying signal described page traversal while
the thing it certified was produced later and could be partial.

Membership now comes from the raw listing set; the detail rows remain the authority for
status and media attribution, which is what CREATING a miss needs. One signal does not
certify both.

Every test here supplies a raw set that DISAGREES with the detail set, because that
disagreement is the entire bug. A fixture where the two agree cannot fail.
"""
from backend.hdencode_shadow import canonical_url, compare_shadow


def C(url):
    """The canonical form the comparison actually works in.

    My first version of this file compared raw URLs against the
    comparison's output and failed everywhere: canonical_url strips the
    trailing slash. Asserting against the un-normalised spelling tests my
    assumption about the API rather than its behaviour.
    """
    return canonical_url(url)

FEED = "https://hdencode.org/in-the-feed-2160p/"
DROPPED = "https://hdencode.org/detail-scrape-failed-2160p/"
LISTED = "https://hdencode.org/listing-only-2160p/"

BOTH = {"movies_all": "changed", "tv_all": "changed"}


def _item(url, *, status="missing", title="T"):
    return {"url": url, "title": title, "status": status, "category": "4k",
            "is_tv": False}


def _compare(*, rss, details, raw=None):
    return compare_shadow(
        rss_urls=rss, listing_items=details, rss_requests=2, listing_requests=1,
        normal_feeds_complete=True, normal_feed_outcomes=BOTH,
        listing_complete=True,
        raw_listing_urls=raw,
    )


class TestADetailFailureIsNotAcquisition:

    def test_a_dropped_detail_row_does_NOT_become_feed_only(self):
        """THE BUG, directly. DROPPED is on the raw listing and in the feed, but its
        detail scrape failed. It must not read as feed-only."""
        result = _compare(
            rss=[FEED, DROPPED],
            details=[_item(FEED)],            # DROPPED absent: detail scrape failed
            raw=[FEED, DROPPED],              # but the crawl DID see it
        )
        assert C(DROPPED) not in result.feed_only, (
            "a release the crawl saw on the listing must never appear as feed_only "
            "merely because its detail scrape failed -- the resolver reads feed_only "
            f"as acquisition. feed_only={result.feed_only}")
        assert result.duplicate_count == 2, (
            "both FEED and DROPPED are in the feed AND on the listing, so both are "
            f"duplicates; got duplicate_count={result.duplicate_count}")

    def test_it_is_reported_as_a_detail_drop_rather_than_lost(self):
        """Silently discarding it would be the other failure mode: no false
        acquisition, but no record of a real observability gap either."""
        result = _compare(
            rss=[FEED],
            details=[_item(FEED)],
            raw=[FEED, DROPPED],
        )
        assert C(DROPPED) in result.detail_dropped, result.detail_dropped

    def test_a_dropped_row_is_not_booked_as_a_miss(self):
        """It has no status and no media type, so it cannot be attributed to a feed.
        Inventing a miss from it would be as wrong as inventing an acquisition."""
        result = _compare(
            rss=[FEED],
            details=[_item(FEED)],
            raw=[FEED, DROPPED],
        )
        urls = {m["canonical_url"] for m in result.relevant_misses}
        assert C(DROPPED) not in urls, urls

    def test_a_genuine_feed_only_release_still_reads_as_feed_only(self):
        """POSITIVE CONTROL. If nothing is ever feed_only the fix is useless: real
        acquisitions must still be recognised, which is what resolves misses."""
        result = _compare(
            rss=[FEED],
            details=[],
            raw=[LISTED],                     # the crawl saw LISTED, not FEED
        )
        assert C(FEED) in result.feed_only, (
            "a release in the feed that the crawl genuinely did NOT see on the "
            "listing is a real feed_only sighting and must stay one")

    def test_a_listing_only_release_with_details_is_still_a_miss(self):
        """POSITIVE CONTROL for the other direction: attribution still works."""
        result = _compare(
            rss=[FEED],
            details=[_item(LISTED)],
            raw=[FEED, LISTED],
        )
        urls = {m["canonical_url"] for m in result.relevant_misses}
        assert C(LISTED) in urls, (result.relevant_misses, result.listing_only)


class TestTheRawSetIsAuthoritativeButNotDestructive:

    def test_a_detail_row_the_raw_set_missed_is_still_counted(self):
        """The raw set is a superset in principle, but a crawl that recorded an item
        without adding it to the seen-set must not lose it. Membership is the UNION,
        so neither source can silently delete evidence the other has."""
        result = _compare(
            rss=[],
            details=[_item(LISTED)],
            raw=[],                            # raw set inexplicably empty
        )
        assert C(LISTED) in result.listing_only, (
            "a detail row must still count as listing membership even if the raw "
            "set omitted it -- otherwise a crawler bug erases real observations")

    def test_omitting_the_raw_set_preserves_the_old_behaviour(self):
        """Callers and tests written before this argument must be unaffected."""
        with_raw = _compare(rss=[FEED], details=[_item(LISTED)],
                            raw=[FEED, LISTED])
        without = _compare(rss=[FEED], details=[_item(LISTED)], raw=None)
        assert C(FEED) in without.feed_only, (
            "without a raw set the detail-derived behaviour stands, so FEED -- "
            "absent from the detail rows -- is feed_only as it always was")
        assert C(FEED) not in with_raw.feed_only, (
            "and with a raw set that contains it, it is not")

    def test_urls_are_canonicalised_on_both_sides(self):
        """A trailing-slash mismatch between the raw set and the detail rows would
        reintroduce the bug through the back door -- this project has already been
        bitten once by exactly that."""
        result = _compare(
            rss=["https://hdencode.org/x-2160p"],
            details=[_item("https://hdencode.org/x-2160p/")],
            raw=["https://hdencode.org/x-2160p/"],
        )
        assert not result.feed_only, (
            f"the same release in three spellings must match; feed_only="
            f"{result.feed_only}")
        assert result.duplicate_count == 1
