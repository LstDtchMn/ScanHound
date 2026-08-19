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

    def test_a_detail_row_the_raw_set_MISSED_is_a_contradiction(self):
        """REVERSED after round 6, and this one is worth reading.

        My first version asserted that a detail row absent from the raw set should
        still count as membership -- reasoning that a union "cannot lose an
        observation". The reviewer showed that is fail-open reconciliation of
        contradictory evidence, and that production establishes the opposite
        invariant: `_crawl_pages` adds a URL to `seen_post_urls` BEFORE appending it
        to `all_posts`, `_process_posts` only handles `all_posts`, and `run_scan`
        clears `self.items` each run. So `detail_urls` MUST be a subset of `raw`.

        A non-empty `detail_urls - raw` is therefore impossible in a healthy run.
        The union silently repaired it and could suppress a legitimate `feed_only`
        whenever raw membership had been truncated -- and my test ASSERTED that
        repair, so the test was protecting the defect.
        """
        result = _compare(
            rss=[],
            details=[_item(LISTED)],
            raw=[],                            # impossible in healthy production
        )
        assert C(LISTED) in result.membership_contradiction, (
            "a detail row with no raw sighting must be recorded as a contradiction, "
            f"not absorbed: {result.membership_contradiction}")
        assert result.listing_complete is False, (
            "and the contradiction must WITHHOLD listing authority -- recording it "
            "while still certifying the cycle would be a signal nothing consumes")
        assert C(LISTED) not in result.listing_only, (
            "raw is authoritative; the detail set is derived from it and cannot "
            "add membership of its own")

    def test_a_healthy_subset_relationship_keeps_authority(self):
        """POSITIVE CONTROL. detail_urls being a strict subset of raw is the NORMAL
        case -- cached skips and policy exclusions guarantee it -- so it must not
        trip the contradiction check."""
        result = _compare(
            rss=[],
            details=[_item(LISTED)],
            raw=[LISTED, DROPPED],             # raw is a superset: healthy
        )
        assert result.membership_contradiction == ()
        assert result.listing_complete is True

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


class TestCancellationCannotCertifyAPartialCrawl:
    """The counterexample round 6 supplied, asserted directly.

    I claimed an early stop always denies listing authority, via a four-hop chain:
    early stop -> early_stopped -> status "early_stopped" -> listing_complete False
    -> cycle_is_valid_evidence_for False. I flagged it as the kind of composition
    claim I keep getting wrong and asked for it to be checked. It was wrong.

    `_crawl_pages` has TWO bare `if self.stop_scan_flag: break` guards -- one in the
    source loop, one in the page loop -- and neither set `early_stopped`. Both
    `/scan/stop` and `BackgroundScanner.stop()` can set that flag externally. So:

        page 1 succeeds and yields posts
        something sets stop_scan_flag
        page 2 is never visited        -> raw seen-set is PARTIAL
        all_posts is non-empty
        early_stopped is still False
        status -> "complete"           -> listing authority granted

    A miss sitting on the unvisited page is absent from the partial raw set, so it
    emits as feed_only, and the resolver reads that as acquisition. The same wrong
    answer by a different route.

    These tests assert the STATE MACHINE rather than mocking a crawl, because a
    mocked crawl would prove only that my mock behaves as I expect.
    """

    def test_every_cancellation_guard_records_a_reason(self):
        """Structural, and deliberately so: the defect was a guard that recorded
        nothing. If a third cancellation point is added without a reason, this
        fails."""
        import inspect
        import re
        from backend.scanner_service import ScannerService
        src = inspect.getsource(ScannerService._crawl_pages)
        # Examine the whole guard BLOCK up to its break, not just the first line.
        # My first version asserted against line one and failed on my own comment --
        # a test that was checking its author's formatting, not the behaviour.
        blocks = []
        for match in re.finditer(r"if self\.stop_scan_flag:\n", src):
            tail = src[match.end():]
            blocks.append(tail[:tail.index("break") + 5])
        assert blocks, "the cancellation guards vanished; this test is stale"
        for block in blocks:
            assert "_last_crawl_termination" in block, (
                "a `if self.stop_scan_flag:` guard breaks without recording a "
                f"termination reason:\n{block}\nThat is the round-6 counterexample: "
                "a cancelled crawl fell through to 'complete' and certified a "
                "partial listing set as trustworthy evidence.")

    def test_a_recorded_cancellation_wins_over_the_boolean_chain(self):
        """The precedence matters. `early_stopped` is False on this path, so if the
        chain still started there, a cancelled crawl would reach 'complete'."""
        import inspect
        from backend.scanner_service import ScannerService
        src = inspect.getsource(ScannerService._crawl_pages)
        tail = src[src.index('self._last_crawl_status = "cancelled"') - 400:]
        assert 'if self._last_crawl_termination == "cancelled"' in tail, (
            "the status chain must test the recorded cancellation FIRST")

    def test_authority_is_keyed_on_the_termination_reason(self):
        """And the consumer must read that field, not the older status string."""
        import inspect
        from backend.background_scanner import BackgroundScanner
        src = inspect.getsource(BackgroundScanner)
        assert '_last_crawl_termination' in src, (
            "background_scanner must key listing authority on the explicit "
            "termination reason")
        assert 'listing_complete=(' in src

    def test_detail_failed_excludes_intentional_skips(self):
        """Round 6's other subtlety: the crawler adds a URL to its seen set BEFORE
        deciding to skip it as cached or exclude it by policy. So
        `listing_only - detail_urls` mixes intentional states with real failures and
        cannot be blocked on. `detail_failed` is scheduled-minus-completed, where
        those intentional states are absent by construction."""
        import inspect
        from backend.scanner_service import ScannerService
        src = inspect.getsource(ScannerService.last_crawl_detail_failed)
        assert "_last_crawl_detail_scheduled" in src
        assert "_last_crawl_detail_completed" in src
        # And the scheduled set is built from all_posts, which cached skips and
        # policy exclusions never enter.
        crawl = inspect.getsource(ScannerService._crawl_pages)
        assert "_last_crawl_detail_scheduled = {" in crawl
        assert "for post in all_posts" in crawl

    def test_detail_failed_reaches_the_comparison(self):
        """It must be plumbed, not merely computed -- the recurring failure."""
        result = _compare(rss=[], details=[_item(LISTED)], raw=[LISTED])
        assert result.detail_failed == ()
        with_failure = compare_shadow(
            rss_urls=[], listing_items=[_item(LISTED)], rss_requests=2,
            listing_requests=1, normal_feeds_complete=True,
            normal_feed_outcomes=BOTH, listing_complete=True,
            raw_listing_urls=[LISTED, DROPPED],
            detail_failed_urls=[DROPPED],
        )
        assert C(DROPPED) in with_failure.detail_failed, (
            "a genuine attribution failure must survive into the comparison, or "
            "readiness can never block on it")
